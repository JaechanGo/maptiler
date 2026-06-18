#!/usr/bin/env python3
"""CUVIA Build Studio (MVP) — 무의존(파이썬 표준 라이브러리만) 빌드 콘솔.

원천데이터(SHP/CSV)를 기반으로 지오코드 인덱스·타일을 빌드하고, 종류별 진행률을 SSE로
실시간 표시하며, 체크박스로 빌드 대상을 정제하고, 완료물을 폐쇄망 번들로 패키징한다.
이미 검증된 CLI 파이프라인(09/10/11/12/13/package)을 그대로 빌드 엔진으로 구동한다.

기동:  python3 scripts/build-studio.py            # http://localhost:8090
       BUILD_HOME=~/geocode-build PORT=8090 python3 scripts/build-studio.py
"""
import json, os, pathlib, queue, subprocess, threading, time, re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import style_objects

ROOT = pathlib.Path(__file__).resolve().parents[1]
BUILD_HOME = pathlib.Path(os.environ.get("BUILD_HOME", os.path.expanduser("~/geocode-build")))
PORT = int(os.environ.get("PORT", "8090"))
TILE_PORT = int(os.environ.get("TILE_PORT", "8080"))   # 미리보기가 스타일을 읽어올 tileserver 포트
COMPOSE_FILE = os.environ.get("COMPOSE_FILE", str(BUILD_HOME / "deploy/docker-compose.yml"))
# 기본은 로컬 전용(127.0.0.1). 외부 노출이 필요할 때만 HOST=0.0.0.0 으로 명시. 인증이 없으므로
# 0.0.0.0 바인딩 시 같은 LAN의 누구나 빌드 실행/업로드가 가능함을 운영자가 인지해야 함.
HOST = os.environ.get("HOST", "127.0.0.1")
MAX_UPLOAD = int(os.environ.get("MAX_UPLOAD", str(1024**3)))   # 업로드 본문 상한(기본 1GB) — 초과 413
MAX_CTRL = 256 * 1024                                           # 제어 API(JSON) 본문 상한
UPLOADS = BUILD_HOME / "uploads"
UPLOADS.mkdir(parents=True, exist_ok=True)

# 원천데이터 기본 경로(업로드/환경변수로 덮어쓸 수 있음)
SRC_JUSO = os.environ.get("SRC_JUSO", "/Users/jaechango_cudo/Downloads/지도정보/202605_내비게이션용DB_전체분")
SRC_LOCALDATA = os.environ.get("SRC_LOCALDATA", "/Users/jaechango_cudo/Downloads/인허가정보")

# ── 빌드 대상 정의 ─────────────────────────────────────────────────
# kind: 카드 id / label / cmd(argv) / dep(선행 대상) / weight(진행률 가중 단계 키워드)
def TARGETS():
    py = "python3"
    return {
        "localdata": dict(label="인허가 정제 (LOCALDATA→상가포맷)", dep=None,
            cmd=[py, str(ROOT/"scripts/11-build-localdata.py"), SRC_LOCALDATA,
                 str(BUILD_HOME/"localdata/localdata_clean.csv")]),
        "geocode": dict(label="통합 지오코딩 인덱스", dep="localdata",
            cmd=[py, str(ROOT/"scripts/09-gen-geocode.py"), "--src", SRC_JUSO,
                 "--osm", str(BUILD_HOME/"osm.sqlite"), "--poi-csv-dir", str(BUILD_HOME/"poi-all"),
                 "--out", str(BUILD_HOME/"geocode.sqlite")]),
        "buildings": dict(label="3D 건물 타일", dep=None,
            cmd=["bash", str(ROOT/"scripts/10-gen-buildings.sh")]),
        "poi": dict(label="시설 라벨 타일 (poi.mbtiles)", dep="geocode",
            cmd=["bash", str(BUILD_HOME/"build-poi.sh")]),
        "qc": dict(label="QC 검증", dep=None,
            cmd=[py, str(ROOT/"scripts/13-qc-check.py"), "--db", str(BUILD_HOME/"geocode.sqlite"),
                 "--tiles", str(BUILD_HOME/"tiles"), "--style", str(ROOT/"style/style.json"),
                 "--config", str(ROOT/"server/tileserver-config.json"), "--api", "http://localhost:8082"]),
        "package": dict(label="폐쇄망 번들 패키징", dep=None,
            cmd=["bash", str(ROOT/"scripts/package.sh")]),
        "__selftest__": dict(label="셀프테스트(가짜 진행률)", dep=None, cmd=None),
    }

CANON = ["localdata", "geocode", "buildings", "poi", "qc", "package"]

def progress_of(kind, line, st):
    """스크립트 stderr 라인 → 진행률(0..1) 휴리스틱. st는 대상별 누적 상태(dict)."""
    l = line.strip()
    if kind == "geocode":
        m = re.search(r"addr:(\w+)", l)
        if m: st.setdefault("sido", set()).add(m.group(1)); return 0.05 + 0.55*len(st["sido"])/17
        if l.startswith("osm:"): return 0.65
        if l.startswith("biz:"): return 0.9
        if l.startswith("OK:"): return 1.0
    elif kind == "poi":
        if "features" in l: return 0.35
        if "tippecanoe" in l: return 0.55
        if l.startswith("OK:"): return 1.0
    elif kind == "buildings":
        # 10-gen-buildings.sh는 시도별 '  [i] LAYER → …' 단일 인덱스로 출력(N/M 아님).
        m = re.search(r"\[(\d+)\]", l)
        if m: return min(0.9, 0.1 + 0.8*int(m.group(1))/17)   # 시도 17개 기준 단조 증가
        if "병합" in l or "tile-join" in l: return 0.92
        if l.startswith("OK:"): return 1.0
    elif kind in ("localdata", "package"):
        if l.startswith("OK:") or "반입 대상" in l: return 1.0
    elif kind == "qc":
        if "[PASS]" in l or "[WARN]" in l or "[FAIL]" in l:
            st["n"] = st.get("n", 0) + 1; return min(0.95, st["n"]/16)
        if l.startswith("결과:"): return 1.0
    return None


# ── Job / JobManager (스레드 + subprocess + SSE pub/sub) ───────────
class Manager:
    def __init__(self):
        self.jobs = {}          # kind -> {status,progress,log[],st}
        self.subs = []          # SSE 구독자 Queue 목록
        self.lock = threading.Lock()
        self.work = queue.Queue()   # FIFO 워커 큐 (순차 실행)
        threading.Thread(target=self._worker, daemon=True).start()

    def publish(self, ev):
        with self.lock:
            for q in list(self.subs):
                try: q.put_nowait(ev)
                except queue.Full: pass

    def subscribe(self):
        q = queue.Queue(maxsize=1000)
        with self.lock:
            self.subs.append(q)
            snapshot = {k: {"status": j["status"], "progress": j["progress"]} for k, j in self.jobs.items()}
        return q, snapshot

    def unsubscribe(self, q):
        with self.lock:
            if q in self.subs: self.subs.remove(q)

    def enqueue(self, kinds):
        # 전이(transitive) 의존성까지 worklist로 해소 → dep의 dep도 모두 포함.
        T = TARGETS(); plan = set(); stack = list(kinds)
        while stack:
            k = stack.pop()
            t = T.get(k)
            if not t or k in plan: continue
            plan.add(k)
            if t["dep"] and t["dep"] in T: stack.append(t["dep"])
        ordered = [k for k in CANON if k in plan] + [k for k in plan if k == "__selftest__"]
        # jobs 변형은 subscribe()의 스냅샷과 같은 lock으로 보호(동시 SSE 연결 중 dict 크기변경 크래시 방지).
        # 이미 queued/running 인 kind는 재적재하지 않음(중복 동시 실행 방지). publish/work.put은 lock 밖에서.
        started = []
        with self.lock:
            for k in ordered:
                cur = self.jobs.get(k)
                if cur and cur["status"] in ("queued", "running"): continue
                self.jobs[k] = {"status": "queued", "progress": 0.0, "log": [], "st": {}}
                started.append(k)
        for k in started:
            self.publish({"kind": k, "status": "queued", "progress": 0.0})
            self.work.put(k)
        return started

    def _worker(self):
        while True:
            kind = self.work.get()
            self._run(kind)

    def _emit(self, kind, line):
        j = self.jobs[kind]; j["log"].append(line); j["log"][:] = j["log"][-400:]
        p = progress_of(kind, line, j["st"])
        if p is not None: j["progress"] = max(j["progress"], min(p, 1.0))
        self.publish({"kind": kind, "status": j["status"], "progress": j["progress"], "line": line})

    def _run(self, kind):
        j = self.jobs[kind]; j["status"] = "running"
        self.publish({"kind": kind, "status": "running", "progress": j["progress"]})
        try:
            if kind == "__selftest__":
                for i in range(1, 21):
                    time.sleep(0.25); j["progress"] = i/20
                    self._emit(kind, f"  셀프테스트 진행 {i*5}% …")
            else:
                cmd = TARGETS()[kind]["cmd"]
                self._emit(kind, "$ " + " ".join(cmd))
                # PYTHONUNBUFFERED=1 — 파이썬 자식이 파이프로 출력 시 블록버퍼링되어 진행률이
                # 종료 시점에 몰리는 것을 막아 라인 단위 라이브 스트리밍을 보장.
                env = {**os.environ, "PYTHONUNBUFFERED": "1"}
                p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                     text=True, bufsize=1, cwd=str(ROOT), env=env)
                for line in p.stdout:
                    self._emit(kind, line.rstrip("\n"))
                rc = p.wait()
                if rc != 0: raise RuntimeError(f"종료코드 {rc}")
            j["status"] = "done"; j["progress"] = 1.0
        except Exception as e:
            j["status"] = "error"; self._emit(kind, f"[오류] {e}")
        self.publish({"kind": kind, "status": j["status"], "progress": j["progress"]})

MGR = Manager()


# ── 스타일 색 테마 적용 ───────────────────────────────────────────
HEXRE = re.compile(r"^#[0-9a-fA-F]{6}$")
VALID_KEYS = {o["key"] for o in style_objects.OBJECTS}

def apply_style_theme(theme):
    # 유효 키 + #rrggbb 만 통과(주입 방지) → style/theme.json 저장 → build_style → tileserver 재시작
    clean = {k: v for k, v in (theme or {}).items() if k in VALID_KEYS and isinstance(v, str) and HEXRE.match(v)}
    (ROOT / "style").mkdir(exist_ok=True)
    (ROOT / "style" / "theme.json").write_text(json.dumps(clean, ensure_ascii=False, indent=2), encoding="utf-8")
    log = []
    try:
        r = subprocess.run(["python3", str(ROOT / "scripts/build_style.py")],
                           capture_output=True, text=True, cwd=str(ROOT), timeout=60)
        log.append(r.stdout.strip() or r.stderr.strip())
        if r.returncode != 0: return {"ok": False, "error": "build_style 실패", "log": log}
    except Exception as e:
        return {"ok": False, "error": f"build_style 오류: {e}", "log": log}
    # tileserver는 시작 시 스타일을 캐시하므로 서빙본 갱신엔 재시작이 필요하나, amd64 에뮬레이션이라
    # 부팅이 수십 초 걸린다 → 동기 대기하지 않고 detached로 띄우고 즉시 반환(라이브 미리보기는 이미 반영됨).
    reloaded = "스킵(compose 없음)"
    if os.path.exists(COMPOSE_FILE):
        try:
            subprocess.Popen(["docker", "compose", "-f", COMPOSE_FILE, "restart", "tileserver"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
            reloaded = "재시작 요청됨(수십초 소요)"
        except Exception as e:
            reloaded = f"오류: {e}"
    return {"ok": True, "applied": len(clean), "reloaded": reloaded, "log": log}


# ── HTTP 핸들러 ────────────────────────────────────────────────────
class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    def log_message(self, *a): pass

    def _json(self, obj, code=200):
        b = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code); self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)

    def _send(self, body, ctype, code=200):
        b = body if isinstance(body, bytes) else body.encode()
        self.send_response(code); self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)

    def _static(self, rel):
        # ROOT 하위 정적파일만(경로조작 차단). maplibre 등 vendor 자산 서빙.
        base = ROOT.resolve()
        p = (base / rel).resolve()
        if base not in p.parents or not p.is_file():   # 경로조작·접두사형제 디렉토리 우회 차단
            return self.send_error(404)
        ext = p.suffix.lower()
        ctype = {".js": "application/javascript", ".css": "text/css", ".png": "image/png",
                 ".json": "application/json", ".svg": "image/svg+xml"}.get(ext, "application/octet-stream")
        self._send(p.read_bytes(), ctype + ("; charset=utf-8" if ext in (".js", ".css", ".json", ".svg") else ""))

    def do_GET(self):
        if self.path == "/" or self.path.startswith("/?"):
            return self._send(PAGE, "text/html; charset=utf-8")
        if self.path == "/style":
            return self._send(STYLE_PAGE, "text/html; charset=utf-8")
        if self.path.startswith("/vendor/"):
            return self._static(self.path.lstrip("/").split("?")[0])
        if self.path == "/api/style/objects":
            try:
                style = json.loads((ROOT / "style/style.json").read_text(encoding="utf-8"))
                cur = style_objects.current_colors(style)
            except Exception:
                cur = {}
            objs = [{"key": o["key"], "label": o["label"], "targets": o["targets"],
                     "color": cur.get(o["key"]) or "#888888"} for o in style_objects.OBJECTS]
            return self._json({"objects": objs, "tile_port": TILE_PORT})
        if self.path == "/api/targets":
            return self._json({"targets": [{"kind": k, "label": v["label"], "dep": v["dep"]}
                                            for k, v in TARGETS().items()]})
        if self.path == "/api/datasets":
            ds = []
            for f in sorted(UPLOADS.glob("*")):
                ds.append({"name": f.name, "size": f.stat().st_size})
            return self._json({"datasets": ds, "build_home": str(BUILD_HOME)})
        if self.path == "/api/events":
            return self._sse()
        self.send_error(404)

    def do_POST(self):
        if self.path == "/api/build":
            n = int(self.headers.get("Content-Length", "0"))
            if n > MAX_CTRL: return self._json({"error": "본문 과대"}, 413)
            body = json.loads(self.rfile.read(n) or "{}")
            ordered = MGR.enqueue(body.get("targets", []))
            return self._json({"queued": ordered})
        if self.path == "/api/upload":
            return self._upload()
        if self.path == "/api/style":
            n = int(self.headers.get("Content-Length", "0"))
            if n > MAX_CTRL: return self._json({"error": "본문 과대"}, 413)
            body = json.loads(self.rfile.read(n) or "{}")
            return self._json(apply_style_theme(body.get("theme", {})))
        self.send_error(404)

    def _upload(self):
        # cgi 모듈이 3.13에서 제거되어 무의존 multipart/form-data 파서를 직접 구현.
        ctype = self.headers.get("Content-Type", "")
        m = re.search(r"boundary=([^;]+)", ctype)
        if "multipart/form-data" not in ctype or not m:
            return self._json({"error": "multipart 필요"}, 400)
        boundary = b"--" + m.group(1).strip().strip('"').encode()
        n = int(self.headers.get("Content-Length", "0"))
        if n > MAX_UPLOAD:   # 메모리 고갈 방지 — 대용량 원천(내비DB/SHP, 수 GB)은 BUILD_HOME 경로 지정 권장
            return self._json({"error": f"업로드 상한 {MAX_UPLOAD//1024//1024}MB 초과 — 대용량은 BUILD_HOME에 직접 배치"}, 413)
        body = self.rfile.read(n)
        saved = []                              # 한 요청에 여러 파일 파트(name="file")를 모두 저장
        for part in body.split(boundary):
            if b'name="file"' not in part: continue
            idx = part.find(b"\r\n\r\n")
            if idx < 0: continue
            head = part[:idx].decode("utf-8", "replace")
            data = part[idx+4:]
            if data.endswith(b"\r\n"): data = data[:-2]
            fm = re.search(r'filename="([^"]*)"', head)
            if not fm or not fm.group(1): continue
            name = os.path.basename(fm.group(1))   # 경로조작 차단
            if not name: continue
            dest = UPLOADS / name
            with open(dest, "wb") as o: o.write(data)
            saved.append({"saved": name, "size": dest.stat().st_size, "detected": detect(name)})
        if not saved: return self._json({"error": "파일 파트 없음"}, 400)
        return self._json({"files": saved})

    def _sse(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        # HTTP/1.1 + 본문길이 불명 스트림이므로 keep-alive 대신 connection close로 프레이밍을 명확히 함
        # (EventSource는 끊기면 retry로 자동 재연결). close_connection=True 로 핸들러 루프도 종료.
        self.send_header("Cache-Control", "no-cache"); self.send_header("Connection", "close")
        self.end_headers(); self.close_connection = True
        q, snap = MGR.subscribe()
        try:
            self.wfile.write(b"retry: 3000\n\n")
            self.wfile.write(("data: " + json.dumps({"snapshot": snap}, ensure_ascii=False) + "\n\n").encode())
            self.wfile.flush()
            while True:
                try: ev = q.get(timeout=15)
                except queue.Empty:
                    self.wfile.write(b": keepalive\n\n"); self.wfile.flush(); continue
                self.wfile.write(("data: " + json.dumps(ev, ensure_ascii=False) + "\n\n").encode())
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            MGR.unsubscribe(q)


def detect(name):
    n = name.lower()
    if n.endswith(".7z") or "내비" in name or "navi" in n: return "도로명주소 내비DB"
    if n.endswith(".zip") or n.endswith(".shp"): return "SHP (GIS건물/전자지도) — EPSG 확인필요"
    if "상가" in name: return "소상공인 상가정보 CSV"
    if "인허가" in name or "localdata" in n: return "LOCALDATA 인허가 CSV (EPSG:5174)"
    if n.endswith(".csv"): return "CSV"
    if n.endswith(".pbf") or n.endswith(".osm"): return "OSM"
    return "미상"


PAGE = r"""<!doctype html><html lang=ko><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>CUVIA Build Studio</title>
<style>
 :root{--bg:#0f1420;--card:#161d2c;--bd:#26304a;--mut:#8d9bb5;--tx:#e8edf5;--ac:#5b9bd5}
 *{box-sizing:border-box} body{margin:0;background:var(--bg);color:var(--tx);
   font:14px/1.5 -apple-system,system-ui,'Apple SD Gothic Neo',sans-serif}
 header{padding:16px 22px;border-bottom:1px solid var(--bd);display:flex;align-items:center;gap:10px}
 h1{font-size:17px;margin:0;font-weight:600} .sub{color:var(--mut);font-size:12px}
 .wrap{display:grid;grid-template-columns:300px 1fr;gap:16px;padding:18px;max-width:1100px}
 .panel{background:var(--card);border:1px solid var(--bd);border-radius:12px;padding:14px 16px}
 .panel h2{font-size:13px;margin:0 0 10px;font-weight:600;color:var(--mut);text-transform:uppercase;letter-spacing:.04em}
 label.row{display:flex;align-items:center;gap:8px;padding:5px 0;cursor:pointer}
 .bar{height:7px;background:#0c1018;border-radius:99px;overflow:hidden;margin-top:6px}
 .bar>i{display:block;height:100%;width:0;background:var(--ac);transition:width .3s}
 .tcard{padding:11px 13px;border:1px solid var(--bd);border-radius:9px;margin-bottom:9px}
 .tcard .h{display:flex;justify-content:space-between;align-items:center;font-size:13px}
 .st{font-size:11px;padding:2px 8px;border-radius:99px;background:#0c1018;color:var(--mut)}
 .st.running{color:#7fd1ff} .st.done{color:#7ee0a0} .st.error{color:#ff8585} .st.queued{color:#d9c07a}
 button{background:var(--ac);color:#06121f;border:0;border-radius:8px;padding:9px 16px;font-weight:600;cursor:pointer}
 button.ghost{background:transparent;color:var(--tx);border:1px solid var(--bd)}
 button:disabled{opacity:.5;cursor:default}
 pre{background:#0a0e16;border:1px solid var(--bd);border-radius:9px;padding:10px;height:230px;
   overflow:auto;font:11px/1.45 ui-monospace,Menlo,monospace;color:#bcd;white-space:pre-wrap;margin:10px 0 0}
 .up{border:1.5px dashed var(--bd);border-radius:9px;padding:18px 12px;text-align:center;color:var(--mut);font-size:12px;cursor:pointer;transition:.15s}
 .up:hover{border-color:var(--mut)}
 .up.drag{border-color:var(--ac);background:#0c1a2c;color:var(--tx)}
 .up a{color:var(--ac);text-decoration:underline;cursor:pointer}
 .ds{font-size:12px;color:var(--mut)} .ds b{color:var(--tx);font-weight:500}
 .chip{display:inline-block;font-size:11px;color:#7fd1ff;background:#0c1018;border-radius:6px;padding:1px 7px;margin-left:6px}
</style>
<header><div style="width:9px;height:9px;border-radius:99px;background:var(--ac)"></div>
 <div><h1>CUVIA Build Studio</h1><div class=sub id=bh>지도데이터 빌드 콘솔</div></div>
 <a href="/style" style="margin-left:auto;color:var(--ac);text-decoration:none;border:1px solid var(--bd);border-radius:8px;padding:7px 13px;font-size:13px">🎨 스타일 디자인 →</a></header>
<div class=wrap>
 <div>
  <div class=panel><h2>데이터 업로드</h2>
   <div class=up id=dz>
    <input type=file id=fin multiple style="display:none">
    <div style="font-size:13px"><b>드래그&드롭</b> 또는 <a id=browse>파일 선택</a></div>
    <div style="margin-top:5px">여러 개 동시 · 붙여넣기(⌘V) 가능 · SHP·CSV·PBF 자동감지</div></div>
   <div id=dslist class=ds style="margin-top:10px"></div></div>
  <div class=panel style="margin-top:14px"><h2>빌드 대상 (정제 체크)</h2>
   <div id=checks></div>
   <div style="display:flex;gap:8px;margin-top:12px">
     <button id=run>빌드 시작</button>
     <button class=ghost id=selftest>셀프테스트</button></div></div>
 </div>
 <div class=panel><h2>빌드 진행률</h2><div id=cards></div>
   <h2 style="margin-top:14px">실시간 로그</h2><pre id=log></pre></div>
</div>
<script>
const $=s=>document.querySelector(s), cards={}, bars={}, sts={};
let TARGETS=[];
fetch('/api/datasets').then(r=>r.json()).then(d=>{$('#bh').textContent='BUILD_HOME: '+d.build_home;render(d.datasets)});
function render(ds){$('#dslist').innerHTML=ds.length?ds.map(x=>`<div>· <b>${x.name}</b> (${(x.size/1048576).toFixed(1)}MB)</div>`).join(''):'<i>업로드된 파일 없음</i>'}
fetch('/api/targets').then(r=>r.json()).then(d=>{
  TARGETS=d.targets.filter(t=>t.kind[0]!=='_');
  $('#checks').innerHTML=TARGETS.map(t=>`<label class=row><input type=checkbox value="${t.kind}" ${['geocode','poi','qc'].includes(t.kind)?'checked':''}> ${t.label}${t.dep?`<span class=chip>← ${t.dep}</span>`:''}</label>`).join('');
});
function card(kind,label){if(cards[kind])return;const el=document.createElement('div');el.className='tcard';
 el.innerHTML=`<div class=h><span>${label||kind}</span><span class=st id=st_${kind}>대기</span></div><div class=bar><i id=bar_${kind}></i></div>`;
 $('#cards').appendChild(el);cards[kind]=el;bars[kind]=$('#bar_'+kind);sts[kind]=$('#st_'+kind);}
const LBL={};
function lbl(k){const t=TARGETS.find(x=>x.kind===k);return t?t.label:k}
function setStatus(k,s,p){card(k,lbl(k));if(p!=null)bars[k].style.width=Math.round(p*100)+'%';
 if(s){const m={queued:'대기',running:'진행중',done:'완료',error:'오류'};sts[k].textContent=m[s]||s;sts[k].className='st '+s;}}
function logln(t){const p=$('#log');p.textContent+=t+'\n';p.scrollTop=p.scrollHeight}
const es=new EventSource('/api/events');
es.onmessage=e=>{const d=JSON.parse(e.data);
 if(d.snapshot){for(const k in d.snapshot)setStatus(k,d.snapshot[k].status,d.snapshot[k].progress);return}
 setStatus(d.kind,d.status,d.progress); if(d.line)logln('['+d.kind+'] '+d.line);};
$('#run').onclick=()=>{const t=[...document.querySelectorAll('#checks input:checked')].map(x=>x.value);
 if(!t.length)return alert('대상을 선택하세요'); fetch('/api/build',{method:'POST',body:JSON.stringify({targets:t})}).then(r=>r.json()).then(d=>logln('▶ 큐: '+d.queued.join(', ')));};
$('#selftest').onclick=()=>fetch('/api/build',{method:'POST',body:JSON.stringify({targets:['__selftest__']})}).then(r=>r.json()).then(d=>logln('▶ '+d.queued.join(', ')));
function uploadFiles(files){
 const fs=[...(files||[])]; if(!fs.length)return;
 const fd=new FormData(); fs.forEach(f=>fd.append('file',f));
 logln('⇧ 업로드('+fs.length+'): '+fs.map(f=>f.name).join(', '));
 fetch('/api/upload',{method:'POST',body:fd}).then(r=>r.json()).then(d=>{
   if(d.error)return logln('✗ '+d.error);
   (d.files||[]).forEach(x=>logln('✓ 저장: '+x.saved+' · '+(x.size/1048576).toFixed(1)+'MB · '+x.detected));
   fetch('/api/datasets').then(r=>r.json()).then(x=>render(x.datasets));
 }).catch(e=>logln('✗ 업로드 실패: '+e));}
const dz=$('#dz');
$('#browse').onclick=()=>$('#fin').click();
dz.onclick=e=>{if(e.target.id!=='browse')$('#fin').click()};
$('#fin').onchange=e=>{uploadFiles(e.target.files);e.target.value='';};
['dragenter','dragover'].forEach(ev=>dz.addEventListener(ev,e=>{e.preventDefault();e.stopPropagation();dz.classList.add('drag')}));
['dragleave','dragend','drop'].forEach(ev=>dz.addEventListener(ev,e=>{e.preventDefault();e.stopPropagation();dz.classList.remove('drag')}));
dz.addEventListener('drop',e=>uploadFiles(e.dataTransfer&&e.dataTransfer.files));
document.addEventListener('paste',e=>{const f=e.clipboardData&&e.clipboardData.files;if(f&&f.length)uploadFiles(f);});
</script></html>"""


STYLE_PAGE = r"""<!doctype html><html lang=ko><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>스타일 디자인 — CUVIA</title>
<link rel=stylesheet href="/vendor/maplibre/maplibre-gl.css">
<style>
 body{margin:0;background:#0f1420;color:#e8edf5;font:14px/1.5 -apple-system,system-ui,'Apple SD Gothic Neo',sans-serif;height:100vh;overflow:hidden}
 .top{display:flex;align-items:center;gap:12px;padding:11px 18px;border-bottom:1px solid #26304a}
 .top a{color:#8d9bb5;text-decoration:none;font-size:13px} h1{font-size:15px;margin:0;font-weight:600}
 .main{display:flex;height:calc(100vh - 50px)}
 .side{width:310px;border-right:1px solid #26304a;overflow:auto;padding:12px 16px;flex:none}
 #map{flex:1;height:100%}
 .row{display:flex;align-items:center;gap:9px;padding:7px 0;border-bottom:1px solid #1a2233}
 .row label{flex:1;font-size:13px}
 input[type=color]{width:36px;height:28px;border:1px solid #26304a;border-radius:6px;background:none;padding:0;cursor:pointer}
 input[type=text]{width:82px;background:#0a0e16;border:1px solid #26304a;color:#e8edf5;border-radius:6px;padding:5px 7px;font:12px ui-monospace,Menlo,monospace}
 button{background:#5b9bd5;color:#06121f;border:0;border-radius:8px;padding:8px 15px;font-weight:600;cursor:pointer}
 button.g{background:transparent;color:#e8edf5;border:1px solid #26304a}
 .st{font-size:12px;color:#8d9bb5}
 .hint{font-size:11px;color:#5f6b80;margin-top:14px;line-height:1.6}
</style>
<div class=top>
 <a href="/">← 대시보드</a>
 <h1>스타일 디자인 · 객체별 색상</h1>
 <button class=g id=reset style="margin-left:auto">되돌리기</button>
 <button id=save>저장 & 적용</button>
 <span class=st id=status></span>
</div>
<div class=main>
 <div class=side><div id=rows></div>
   <p class=hint>색을 바꾸면 미리보기에 즉시 반영됩니다. 팔레트(색상칸 클릭) 또는 #색상값 직접 입력 모두 가능.
   <br>‘저장 & 적용’ → style.json에 기록 + 타일서버 재시작(영구 반영).</p>
 </div>
 <div id=map></div>
</div>
<script src="/vendor/maplibre/maplibre-gl.js"></script>
<script>
 const $=s=>document.querySelector(s); let OBJ=[], map=null, INIT={};
 fetch('/api/style/objects').then(r=>r.json()).then(d=>{
   OBJ=d.objects;
   $('#rows').innerHTML=OBJ.map(o=>`<div class=row><label>${o.label}</label>
     <input type=text id=h_${o.key} value="${o.color}" maxlength=7 spellcheck=false>
     <input type=color id=c_${o.key} value="${o.color}"></div>`).join('');
   OBJ.forEach(o=>{ INIT[o.key]=o.color;
     const c=$('#c_'+o.key), h=$('#h_'+o.key);
     c.oninput=()=>{h.value=c.value; apply(o,c.value)};
     h.oninput=()=>{if(/^#[0-9a-fA-F]{6}$/.test(h.value)){c.value=h.value; apply(o,h.value)}};
   });
   map=new maplibregl.Map({container:'map',
     style:`http://${location.hostname}:${d.tile_port}/styles/cuvia/style.json`,
     center:[126.9784,37.5666], zoom:14.5, pitch:55, bearing:-18, attributionControl:false});
   map.addControl(new maplibregl.NavigationControl());
 }).catch(e=>$('#status').textContent='객체 로드 실패: '+e);
 function apply(o,color){ if(!map||!map.isStyleLoaded())return;
   o.targets.forEach(t=>{ try{map.setPaintProperty(t[0],t[1],color)}catch(e){} }); }
 $('#reset').onclick=()=>OBJ.forEach(o=>{const v=INIT[o.key];$('#c_'+o.key).value=v;$('#h_'+o.key).value=v;apply(o,v)});
 $('#save').onclick=()=>{
   const theme={}; OBJ.forEach(o=>theme[o.key]=$('#c_'+o.key).value);
   $('#status').textContent='적용 중…';
   fetch('/api/style',{method:'POST',body:JSON.stringify({theme})}).then(r=>r.json()).then(d=>{
     $('#status').textContent=d.ok?`✓ 저장 ${d.applied}개 · 타일서버 ${d.reloaded}`:('✗ '+(d.error||'오류'));
   }).catch(e=>$('#status').textContent='✗ 실패: '+e);
 };
</script></html>"""


if __name__ == "__main__":
    shown = "localhost" if HOST in ("127.0.0.1", "localhost") else HOST
    warn = "" if HOST in ("127.0.0.1", "localhost") else "  ⚠ 외부노출(무인증) — 신뢰망에서만"
    print(f"CUVIA Build Studio → http://{shown}:{PORT}  (bind {HOST}, BUILD_HOME={BUILD_HOME}){warn}", flush=True)
    ThreadingHTTPServer((HOST, PORT), H).serve_forever()
