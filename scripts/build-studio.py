#!/usr/bin/env python3
"""CUVIA Build Studio (MVP) — 무의존(파이썬 표준 라이브러리만) 빌드 콘솔.

원천데이터(SHP/CSV)를 기반으로 지오코드 인덱스·타일을 빌드하고, 종류별 진행률을 SSE로
실시간 표시하며, 체크박스로 빌드 대상을 정제하고, 완료물을 폐쇄망 번들로 패키징한다.
이미 검증된 CLI 파이프라인(09/10/11/12/13/package)을 그대로 빌드 엔진으로 구동한다.

기동:  python3 scripts/build-studio.py            # http://localhost:8090
       BUILD_HOME=~/geocode-build PORT=8090 python3 scripts/build-studio.py
"""
import json, os, pathlib, queue, subprocess, threading, time, re, ssl, sqlite3, shutil, zipfile, urllib.request, urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = pathlib.Path(__file__).resolve().parents[1]
BUILD_HOME = pathlib.Path(os.environ.get("BUILD_HOME", os.path.expanduser("~/geocode-build")))
PORT = int(os.environ.get("PORT", "8090"))
TILE_PORT = int(os.environ.get("TILE_PORT", "8080"))   # 미리보기가 스타일을 읽어올 tileserver 포트
STYLE_STUDIO_PORT = os.environ.get("STYLE_STUDIO_PORT", "8091")   # 스타일 편집기(style-studio) — /style 은 여기로 일원화(리다이렉트)
COMPOSE_FILE = os.environ.get("COMPOSE_FILE", str(BUILD_HOME / "deploy/docker-compose.yml"))
# 기본은 로컬 전용(127.0.0.1). 외부 노출이 필요할 때만 HOST=0.0.0.0 으로 명시. 인증이 없으므로
# 0.0.0.0 바인딩 시 같은 LAN의 누구나 빌드 실행/업로드가 가능함을 운영자가 인지해야 함.
HOST = os.environ.get("HOST", "127.0.0.1")
MAX_CTRL = 256 * 1024                                           # 제어 API(JSON) 본문 상한
# 업로드는 raw 바디 스트리밍(청크→디스크)이라 용량 상한 없음 — 대용량 내비DB/건물DB 웹 업로드 지원.
DIST = BUILD_HOME / "dist"                       # package.sh 산출물(번들·images·builds.json)
DATA_VERSIONS = BUILD_HOME / "data-versions.json"  # (구) 출처 버전 JSON — 최초 1회 DB로 마이그레이션 후 .bak 보존
DB_PATH = BUILD_HOME / "build-studio.db"         # 업로드 이력·버전·검증 상태 통합 sqlite
SOURCES_FILE = ROOT / "scripts" / "data-sources.json"  # 데이터 출처 레지스트리
SOURCES_DIR = BUILD_HOME / "sources"             # 출처별 업로드 파일 저장(sources/<key>/)


def _period_from_name(name):
    """파일명에서 기준일 추출 — 20260618 / 202605 우선. 예: 202605_내비게이션용DB_전체분.7z → 202605"""
    m = re.search(r"(?<!\d)(\d{8}|\d{6})(?!\d)", name or "")
    return m.group(1) if m else ""


def _load_json(p, default):
    try:
        return json.loads(pathlib.Path(p).read_text(encoding="utf-8"))
    except Exception:
        return default

def load_sources():
    return _load_json(SOURCES_FILE, {}).get("sources", [])

# ── 업로드 이력·버전·검증 상태(통합 sqlite) ───────────────────────────
SCHEMA_DDL = """
CREATE TABLE IF NOT EXISTS sources_state(
  key TEXT PRIMARY KEY, current TEXT, latest TEXT, checked_at TEXT,
  file TEXT, staged_sig TEXT,
  validation_status TEXT, validation_msg TEXT, validated_at TEXT);
CREATE TABLE IF NOT EXISTS upload_history(
  id INTEGER PRIMARY KEY AUTOINCREMENT, key TEXT, file TEXT, period TEXT,
  size INTEGER, uploaded_at TEXT);
CREATE INDEX IF NOT EXISTS ix_hist_key ON upload_history(key, id DESC);
"""
_STATE_COLS = ("current", "latest", "checked_at", "file", "staged_sig",
               "validation_status", "validation_msg", "validated_at")
_DB_READY = False
_DB_LOCK = threading.Lock()


def _migrate_versions_once(conn):
    """(구) data-versions.json → DB 최초 1회 이전. json 은 .bak 로 보존."""
    if conn.execute("SELECT 1 FROM sources_state LIMIT 1").fetchone():
        return
    if not DATA_VERSIONS.is_file():
        return
    for key, rec in (_load_json(DATA_VERSIONS, {}) or {}).items():
        conn.execute("INSERT OR REPLACE INTO sources_state(key,current,latest,checked_at,file,staged_sig) "
                     "VALUES(?,?,?,?,?,?)",
                     (key, rec.get("current"), rec.get("latest"), rec.get("checked_at"),
                      rec.get("file"), rec.get("staged_sig")))
        for h in (rec.get("history") or []):
            conn.execute("INSERT INTO upload_history(key,file,period,size,uploaded_at) VALUES(?,?,?,?,?)",
                         (key, h.get("file"), h.get("period"), h.get("size"), h.get("uploaded_at")))
    conn.commit()
    try:
        DATA_VERSIONS.rename(str(DATA_VERSIONS) + ".bak")
    except Exception:
        pass


def _db():
    global _DB_READY
    BUILD_HOME.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    if not _DB_READY:
        with _DB_LOCK:
            if not _DB_READY:
                conn.executescript(SCHEMA_DDL)
                _migrate_versions_once(conn)
                _DB_READY = True
    return conn


def load_versions():
    """전 출처 상태(현재/최신/검증/staged_sig)+이력(최근10)을 nested dict 로 — 기존 호출부 호환."""
    c = _db(); out = {}
    try:
        for row in c.execute(f"SELECT key,{','.join(_STATE_COLS)} FROM sources_state"):
            out[row[0]] = {col: val for col, val in zip(_STATE_COLS, row[1:]) if val is not None}
        for key, file, period, size, uat in c.execute(
                "SELECT key,file,period,size,uploaded_at FROM upload_history ORDER BY id DESC LIMIT 400"):
            h = out.setdefault(key, {}).setdefault("history", [])
            if len(h) < 10:
                h.append({"file": file, "period": period, "size": size, "uploaded_at": uat})
    finally:
        c.close()
    return out


def save_versions(v):
    """상태 컬럼만 upsert(이력은 _record_upload 가 관리). rec 는 load_versions 산출이라 기존값 보존."""
    c = _db()
    try:
        sets = ",".join(f"{col}=excluded.{col}" for col in _STATE_COLS)
        for key, rec in (v or {}).items():
            c.execute(f"INSERT INTO sources_state(key,{','.join(_STATE_COLS)}) "
                      f"VALUES(?,{','.join(['?'] * len(_STATE_COLS))}) "
                      f"ON CONFLICT(key) DO UPDATE SET {sets}",
                      [key] + [rec.get(col) for col in _STATE_COLS])
        c.commit()
    finally:
        c.close()


def _record_upload(key, name, size):
    """업로드 1파일 → 이력 append + 상태(file/current) 갱신 + 검증상태 pending 리셋. period 반환."""
    period = _period_from_name(name)
    c = _db()
    try:
        c.execute("INSERT INTO upload_history(key,file,period,size,uploaded_at) VALUES(?,?,?,?,?)",
                  (key, name, period, size, time.strftime("%Y-%m-%d %H:%M")))
        c.execute("INSERT INTO sources_state(key,file,current,validation_status) VALUES(?,?,?, 'pending') "
                  "ON CONFLICT(key) DO UPDATE SET file=excluded.file, "
                  "current=COALESCE(excluded.current, sources_state.current), "
                  "validation_status='pending', validation_msg=NULL, validated_at=NULL",
                  (key, name, period or None))
        c.commit()
    finally:
        c.close()
    return period


def _set_validation(key, status, msg):
    c = _db()
    try:
        c.execute("INSERT INTO sources_state(key,validation_status,validation_msg,validated_at) "
                  "VALUES(?,?,?,?) ON CONFLICT(key) DO UPDATE SET "
                  "validation_status=excluded.validation_status, validation_msg=excluded.validation_msg, "
                  "validated_at=excluded.validated_at",
                  (key, status, msg, time.strftime("%Y-%m-%d %H:%M")))
        c.commit()
    finally:
        c.close()


# ── 업로드 데이터 경량 검증(구조·핵심 컬럼만 — 압축 central directory + 헤더 1줄만 읽어 GB급도 수초) ──
def _split_hdr(line):
    return [c.strip().strip('"').strip() for c in (line or "").rstrip("\r\n").split(",")]


def _zip_csv_members(path):
    with zipfile.ZipFile(path) as z:
        return [n for n in z.namelist() if n.lower().endswith(".csv")]


def _zip_header(path, member, encoding):
    with zipfile.ZipFile(path) as z, z.open(member) as fp:
        return fp.readline().decode(encoding, "replace")


def _v_sangga(files):
    need = {"상호명", "경도", "위도"}
    zips = [f for f in files if f.suffix.lower() == ".zip"]
    csvs = [f for f in files if f.suffix.lower() == ".csv"]
    if zips:
        mem = _zip_csv_members(zips[0])
        if not mem:
            return ("fail", "zip 안에 CSV가 없습니다")
        miss = need - set(_split_hdr(_zip_header(zips[0], mem[0], "utf-8-sig")))
        return ("ok" if not miss else "warn", f"zip · CSV {len(mem)}개" + ("" if not miss else f" · 컬럼 누락 {sorted(miss)}"))
    if csvs:
        with open(csvs[0], encoding="utf-8-sig", errors="replace") as f:
            miss = need - set(_split_hdr(f.readline()))
        return ("ok" if not miss else "warn", f"CSV {len(csvs)}개" + ("" if not miss else f" · 컬럼 누락 {sorted(miss)}"))
    return ("fail", "zip 또는 CSV가 필요합니다")


def _v_localdata(files):
    need = {"사업장명"}; coord = {"좌표정보(X)", "좌표정보(Y)"}
    zips = [f for f in files if f.suffix.lower() == ".zip"]
    csvs = [f for f in files if f.suffix.lower() == ".csv"]

    def judge(cols, ncsv, tag):
        ok = (need <= cols) and bool(coord & cols)
        return ("ok" if ok else "warn", f"{tag} · CSV {ncsv}개" + ("" if ok else " · 컬럼 확인필요(사업장명/좌표정보)"))
    if zips:
        mem = _zip_csv_members(zips[0])
        if not mem:
            return ("fail", "zip 안에 CSV가 없습니다")
        return judge(set(_split_hdr(_zip_header(zips[0], mem[0], "cp949"))), len(mem), "zip")
    if csvs:
        with open(csvs[0], encoding="cp949", errors="replace") as f:
            return judge(set(_split_hdr(f.readline())), len(csvs), "폴더")
    return ("fail", "zip 또는 CSV가 필요합니다")


def _v_navi(files):
    z7 = [f for f in files if f.suffix.lower() == ".7z"]
    mb = [f for f in files if f.name.lower().startswith("match_build")]
    if z7:
        tool = shutil.which("7z") or shutil.which("7za") or shutil.which("7zr")
        if not tool:
            return ("warn", ".7z 업로드됨 — 7z 미설치로 내용검증 생략(빌드 시 추출)")
        try:
            r = subprocess.run([tool, "l", str(z7[0])], capture_output=True, text=True, timeout=180)
            cnt = len(set(re.findall(r"match_build_\w+\.txt", r.stdout)))
        except Exception as e:
            return ("warn", f".7z (목록 확인 실패: {str(e)[:60]})")
        return ("ok" if cnt else "warn", f".7z · match_build {cnt}개 시도" if cnt else ".7z지만 match_build_*.txt 미발견")
    if mb:
        return ("ok", f"match_build_*.txt {len(mb)}개")
    return ("fail", ".7z 또는 match_build_*.txt 가 필요합니다")


def _v_building(files):
    zips = [f for f in files if f.suffix.lower() == ".zip"]
    shps = [f for f in files if f.suffix.lower() == ".shp"]
    if zips:
        tot = 0
        for z in zips:
            try:
                with zipfile.ZipFile(z) as zf:
                    tot += len([n for n in zf.namelist() if n.lower().endswith(".shp")])
            except Exception:
                return ("warn", f"zip {len(zips)}개 (일부 열기 실패)")
        return ("ok" if tot else "warn", f"zip {len(zips)}개 · shp {tot}개" if tot else f"zip {len(zips)}개지만 .shp 없음")
    if shps:
        return ("ok", f"shp {len(shps)}개")
    return ("fail", "zip(.shp 포함) 또는 .shp 가 필요합니다")


_VALIDATORS = {"juso_navi": _v_navi, "sangga": _v_sangga, "localdata": _v_localdata, "building_db": _v_building}


def _validate_source(key):
    sdir = SOURCES_DIR / key
    files = sorted((f for f in sdir.rglob("*") if f.is_file()), key=lambda p: str(p)) if sdir.is_dir() else []
    if not files:
        return ("fail", "업로드된 파일이 없습니다")
    fn = _VALIDATORS.get(key)
    if not fn:
        return ("warn", f"{len(files)}개 파일(검증기 미정의)")
    try:
        return fn(files)
    except Exception as e:
        return ("fail", f"검증 오류: {str(e)[:120]}")

def load_builds():
    return _load_json(DIST / "builds.json", [])

def _norm(x):
    return re.sub(r"\D", "", str(x or ""))   # 기준일 비교용 — 숫자만(202605 vs 2026-06-18 호환)

def fetch_latest(source):
    """레지스트리의 latest_check(url/regex/pick)로 외부 페이지에서 최신 기준일 추출. 실패 시 None.
    URL/정규식은 신뢰된 data-sources.json 출처라 사용자 입력이 아님(SSRF 무관)."""
    lc = source.get("latest_check")
    if not lc:
        return None
    url = lc.get("url") or source.get("url")
    if not url or (lc.get("type") != "json" and not lc.get("regex")):
        return None
    headers = {"User-Agent": "Mozilla/5.0", "Accept": "application/json,*/*"}
    data = None; method = lc.get("method", "GET").upper()
    if lc.get("body") is not None:   # POST JSON 바디 — {year}/{month}는 현재 연·월로 치환(juso 등)
        t = time.localtime()
        sub = lambda v: t.tm_year if v == "{year}" else (t.tm_mon if v == "{month}" else v)
        data = json.dumps({k: sub(v) for k, v in lc["body"].items()}).encode()
        headers["Content-Type"] = "application/json"; method = "POST"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    # 공공 사이트(VWorld 등) 인증서 체인 이슈 회피 — 공개 메타데이터 조회 전용
    resp = urllib.request.urlopen(req, timeout=20, context=ssl._create_unverified_context()).read().decode("utf-8", "replace")
    if lc.get("type") == "json":   # JSON API — json_path(점표기) 리스트에서 filter 후 field 추출
        node = json.loads(resp)
        for seg in (lc.get("json_path") or "").split("."):
            node = node.get(seg, {}) if isinstance(node, dict) else {}
        rows = node if isinstance(node, list) else []
        flt = lc.get("filter") or {}
        vals = [str(r.get(lc["field"])) for r in rows
                if isinstance(r, dict) and r.get(lc["field"]) and all(r.get(k) == v for k, v in flt.items())]
    else:
        vals = re.findall(lc.get("regex", ""), resp)
    if not vals:
        return None
    return max(vals) if lc.get("pick", "max") == "max" else vals[0]


def _nonempty_dir(p):
    return p.is_dir() and any(p.iterdir())


def _safe_relpath(name):
    """업로드 상대경로 정규화 — 폴더 구조(서브디렉토리) 보존 + 경로조작(.. /절대경로) 차단."""
    parts = []
    for seg in (name or "").replace("\\", "/").split("/"):
        seg = seg.strip()
        if seg in ("", ".", ".."):
            continue
        parts.append(seg)
    return "/".join(parts)


def prepare_sources(keys=None):
    """sources/<key>/ 의 '모든' 업로드 파일을 build_input.dest 로 적재(누적).
    확장자별: .zip=stdlib 추출 / .7z=CLI(있으면) / 그외(.csv·.txt·.shp 등)=복사.
    업로드가 없으면 기존 staged 재사용(직전 빌드분 또는 BUILD_HOME 직접 배치 데이터)."""
    import shutil, zipfile
    ver = load_versions(); report = []
    for s in load_sources():
        bi = s.get("build_input")
        if not bi or (keys is not None and s["key"] not in keys):
            continue
        key = s["key"]; dest = BUILD_HOME / bi["dest"]
        srcdir = SOURCES_DIR / key
        files = sorted((f for f in srcdir.rglob("*") if f.is_file()), key=lambda p: str(p)) if srcdir.is_dir() else []
        if not files:   # 업로드 없음 → 기존 staged 있으면 재사용(직접 배치 포함)
            report.append({"key": key, "action": "reused" if _nonempty_dir(dest) else "missing", "dest": str(dest)})
            continue
        sig = ";".join(f"{f.relative_to(srcdir)}:{f.stat().st_size}" for f in files)   # 업로드 집합 변경 시에만 재적재
        rec = ver.get(key, {})
        if rec.get("staged_sig") == sig and _nonempty_dir(dest):
            report.append({"key": key, "action": "ok", "n": len(files), "dest": str(dest)})
            continue
        try:
            shutil.rmtree(dest, ignore_errors=True); dest.mkdir(parents=True, exist_ok=True)
            tool = shutil.which("7z") or shutil.which("7za") or shutil.which("7zr")
            n = 0; errs = []
            for f in files:
                ext = f.suffix.lower()
                if ext == ".zip":
                    try:
                        with zipfile.ZipFile(f) as z: z.extractall(dest); n += 1
                    except Exception as e:
                        errs.append(f"{f.name}: zip {str(e)[:50]}")
                elif ext == ".7z":
                    if not tool:
                        errs.append(f"{f.name}: 7z 없음 — scripts/setup-build-host.sh 실행(p7zip 설치) 후 재빌드, 또는 .txt 직접배치"); continue
                    r = subprocess.run([tool, "x", "-y", f"-o{dest}", str(f)], capture_output=True, text=True, timeout=3600)
                    if r.returncode != 0: errs.append(f"{f.name}: {(r.stderr or '')[-80:]}")
                    else: n += 1
                else:   # 폴더 업로드(서브디렉토리 포함) 구조 보존 복사 — localdata 대분류 폴더 등
                    rel = f.relative_to(srcdir); (dest / rel).parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(f, dest / rel); n += 1
            ver.setdefault(key, {})["staged_sig"] = sig; save_versions(ver)
            action = "staged" if (n and not errs) else ("partial" if n else "error")
            report.append({"key": key, "action": action, "n": n, "dest": str(dest),
                           "msg": "; ".join(errs)[:200] if errs else None})
        except Exception as e:
            report.append({"key": key, "action": "error", "msg": str(e)[:200]})
    return report

# 원천데이터 기본 경로(업로드/환경변수로 덮어쓸 수 있음)
# 빌드 입력 — 기본값은 Build Studio 업로드 적재 경로(prepare_sources). 외부 경로는 환경변수로 덮어쓰기.
SRC_JUSO = os.environ.get("SRC_JUSO", str(BUILD_HOME / "staged/navi"))
SRC_LOCALDATA = os.environ.get("SRC_LOCALDATA", str(BUILD_HOME / "staged/localdata"))
SRC_GIS = os.environ.get("SRC_GIS", str(BUILD_HOME / "staged/gis"))

# ── 빌드 대상 정의 ─────────────────────────────────────────────────
# kind: 카드 id / label / cmd(argv) / dep(선행 대상) / weight(진행률 가중 단계 키워드)
def TARGETS():
    py = "python3"
    return {
        "localdata": dict(label="인허가 정제 (LOCALDATA→상가포맷)", dep=None,
            cmd=[py, str(ROOT/"scripts/11-build-localdata.py"), SRC_LOCALDATA,
                 str(BUILD_HOME/"poi-all/localdata_clean.csv")]),   # 09가 읽는 poi-all 에 직접 출력(geocoder 반영)
        "geocode": dict(label="통합 지오코딩 인덱스", dep="localdata",
            cmd=[py, str(ROOT/"scripts/09-gen-geocode.py"), "--src", SRC_JUSO,
                 "--osm", str(BUILD_HOME/"osm.sqlite"), "--poi-csv-dir", str(BUILD_HOME/"poi-all"),
                 "--out", str(BUILD_HOME/"geocode.sqlite")]),
        "buildings": dict(label="3D 건물 타일", dep=None,
            cmd=["bash", str(ROOT/"scripts/10-gen-buildings.sh"), SRC_GIS]),
        "poi": dict(label="시설 라벨 타일 (poi.mbtiles)", dep="geocode",
            cmd=["bash", str(BUILD_HOME/"build-poi.sh")]),
        "qc": dict(label="QC 검증", dep=None,
            cmd=[py, str(ROOT/"scripts/13-qc-check.py"), "--db", str(BUILD_HOME/"geocode.sqlite"),
                 "--tiles", str(BUILD_HOME/"tiles"), "--style", str(ROOT/"style/style.json"),
                 "--config", str(ROOT/"server/tileserver-config.json"), "--api", "http://localhost:8082"]),
        "package": dict(label="폐쇄망 번들 패키징", dep=None,
            cmd=["bash", str(ROOT/"scripts/package.sh")]),
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
        ordered = [k for k in CANON if k in plan]
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
        if self.path == "/style":   # 스타일 편집기는 style-studio 로 일원화 → 리다이렉트
            host = (self.headers.get("Host") or "localhost").split(":")[0]
            self.send_response(302)
            self.send_header("Location", f"http://{host}:{STYLE_STUDIO_PORT}/")
            self.send_header("Content-Length", "0"); self.end_headers()
            return
        if self.path.startswith("/vendor/"):
            return self._static(self.path.lstrip("/").split("?")[0])
        if self.path == "/api/targets":
            return self._json({"targets": [{"kind": k, "label": v["label"], "dep": v["dep"]}
                                            for k, v in TARGETS().items()]})
        if self.path == "/api/builds":
            p = DIST / "cuvia-map-bundle.tgz"
            cur = {"exists": p.is_file(), "size": (p.stat().st_size if p.is_file() else 0)}
            return self._json({"builds": load_builds(), "current_bundle": cur, "dist": str(DIST)})
        if self.path == "/api/sources":
            ver = load_versions()
            out = []
            for s in load_sources():
                v = ver.get(s["key"], {}); cur = v.get("current"); lat = v.get("latest")
                status = "unknown"
                if cur and lat:   # 공통 정밀도로 비교(YYYYMM vs YYYYMMDD 동월 오판 방지)
                    a, b = _norm(cur), _norm(lat); k = min(len(a), len(b))
                    status = ("update" if b[:k] > a[:k] else "current") if k else "unknown"
                out.append({**s, "current": cur, "latest": lat, "auto": bool(s.get("latest_check")),
                            "uploadable": bool(s.get("build_input")),
                            "checked_at": v.get("checked_at"), "status": status,
                            "file": v.get("file"), "history": v.get("history", []),
                            "validation": v.get("validation_status"), "validation_msg": v.get("validation_msg"),
                            "validated_at": v.get("validated_at")})
            return self._json({"sources": out, "build_home": str(BUILD_HOME)})
        if self.path == "/api/download/bundle":
            p = DIST / "cuvia-map-bundle.tgz"
            if not p.is_file():
                return self.send_error(404)
            self.send_response(200)
            self.send_header("Content-Type", "application/gzip")
            self.send_header("Content-Disposition", 'attachment; filename="cuvia-map-bundle.tgz"')
            self.send_header("Content-Length", str(p.stat().st_size)); self.end_headers()
            with open(p, "rb") as f:                       # 청크 스트리밍(3GB도 메모리 적재 X)
                while True:
                    chunk = f.read(1 << 20)
                    if not chunk: break
                    try: self.wfile.write(chunk)
                    except (BrokenPipeError, ConnectionResetError): break
            return
        if self.path == "/api/events":
            return self._sse()
        self.send_error(404)

    def do_POST(self):
        if self.path == "/api/build":
            n = int(self.headers.get("Content-Length", "0"))
            if n > MAX_CTRL: return self._json({"error": "본문 과대"}, 413)
            body = json.loads(self.rfile.read(n) or "{}")
            targets = body.get("targets", [])
            prep = prepare_sources()   # 업로드 데이터 적재(미업로드는 직전 재사용)
            ordered = MGR.enqueue(targets)
            return self._json({"queued": ordered, "prepared": prep})
        if self.path == "/api/sources/version":   # 출처 기준일 수동 등록/갱신(현재/최신)
            n = int(self.headers.get("Content-Length", "0"))
            if n > MAX_CTRL: return self._json({"error": "본문 과대"}, 413)
            b = json.loads(self.rfile.read(n) or "{}")
            key = b.get("key"); field = b.get("field"); val = (b.get("value") or "").strip()
            valid = {s["key"] for s in load_sources()}
            if key not in valid or field not in ("current", "latest"):
                return self._json({"error": "잘못된 key/field"}, 400)
            ver = load_versions(); rec = ver.setdefault(key, {})
            rec[field] = val
            if field == "latest":
                rec["checked_at"] = time.strftime("%Y-%m-%d %H:%M")
            save_versions(ver)
            return self._json({"ok": True, key: rec})
        if self.path == "/api/sources/check":   # 외부 출처에서 최신 기준일 자동조회(latest_check 설정 시)
            n = int(self.headers.get("Content-Length", "0"))
            if n > MAX_CTRL: return self._json({"error": "본문 과대"}, 413)
            key = (json.loads(self.rfile.read(n) or "{}")).get("key")
            src = next((s for s in load_sources() if s["key"] == key), None)
            if not src: return self._json({"error": "알 수 없는 key"}, 400)
            if not src.get("latest_check"): return self._json({"error": "자동조회 미지원 출처(수동 입력)"}, 400)
            try:
                latest = fetch_latest(src)
            except Exception as e:
                return self._json({"error": f"조회 실패: {e}"}, 502)
            if not latest:
                return self._json({"error": "최신 기준일을 찾지 못함(패턴 불일치/사이트 변경)"}, 404)
            ver = load_versions(); rec = ver.setdefault(key, {})
            rec["latest"] = latest; rec["checked_at"] = time.strftime("%Y-%m-%d %H:%M")
            save_versions(ver)
            return self._json({"ok": True, "key": key, "latest": latest})
        if self.path.startswith("/api/sources/upload"):
            return self._upload_stream()
        if self.path == "/api/sources/validate":   # 업로드 데이터 경량 검증(구조·핵심 컬럼)
            n = int(self.headers.get("Content-Length", "0"))
            if n > MAX_CTRL: return self._json({"error": "본문 과대"}, 413)
            key = (json.loads(self.rfile.read(n) or "{}")).get("key")
            if key not in {s["key"] for s in load_sources()}:
                return self._json({"error": "알 수 없는 key"}, 400)
            status, msg = _validate_source(key)
            _set_validation(key, status, msg)
            return self._json({"ok": True, "key": key, "status": status, "msg": msg})
        self.send_error(404)

    def _upload_stream(self):
        # 출처별 raw 바디 스트리밍 업로드 — ?key=<출처>&name=<상대경로>. 청크→디스크(용량 상한 없음, 폴더는 파일당 1요청).
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        key = (q.get("key") or [""])[0]
        rel = _safe_relpath((q.get("name") or [""])[0])
        srcmap = {s["key"]: s for s in load_sources()}
        if key not in srcmap:
            return self._json({"error": "알 수 없는 key"}, 400)
        if not srcmap[key].get("build_input"):
            return self._json({"error": "이 출처는 업로드 미지원"}, 400)
        if not rel:
            return self._json({"error": "파일명(name) 누락"}, 400)
        dest = SOURCES_DIR / key / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        n = int(self.headers.get("Content-Length", "0"))
        written = 0
        try:
            with open(dest, "wb") as o:
                remaining = n
                while remaining > 0:
                    chunk = self.rfile.read(min(1 << 20, remaining))
                    if not chunk:
                        break
                    o.write(chunk); remaining -= len(chunk); written += len(chunk)
        except Exception as e:
            return self._json({"error": f"저장 실패: {str(e)[:120]}"}, 500)
        period = _record_upload(key, rel, written)
        return self._json({"ok": True, "key": key, "file": rel, "size": written, "period": period})

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




PAGE = r"""<!doctype html><html lang=ko><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>CUVIA Build Studio</title>
<style>
 :root{--bg:#0f1420;--card:#161d2c;--bd:#26304a;--mut:#8d9bb5;--tx:#e8edf5;--ac:#5b9bd5}
 *{box-sizing:border-box} body{margin:0;background:var(--bg);color:var(--tx);
   font:14px/1.5 -apple-system,system-ui,'Apple SD Gothic Neo',sans-serif;word-break:keep-all}
 header{padding:16px 22px;border-bottom:1px solid var(--bd);display:flex;align-items:center;gap:10px}
 h1{font-size:17px;margin:0;font-weight:600} .sub{color:var(--mut);font-size:12px}
 .wrap{display:grid;grid-template-columns:320px 1fr;gap:16px;padding:18px;max-width:1200px;margin:0 auto}
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
 .ds{font-size:12px;color:var(--mut);overflow-wrap:anywhere} .ds b{color:var(--tx);font-weight:500} .ds a{white-space:nowrap}
 .chip{display:inline-block;white-space:nowrap;font-size:11px;color:#7fd1ff;background:#0c1018;border-radius:6px;padding:1px 7px;margin-left:6px}
 .src{background:#10192b;border:1px solid var(--bd);border-radius:9px;padding:10px 12px;margin-bottom:9px}
 .src a{color:var(--ac);cursor:pointer;white-space:nowrap}
 .src-h{font-size:13px;line-height:1.45}
 .src-act{margin-top:5px;font-size:12px}
 .src-ver{margin-top:7px;padding-top:7px;border-top:1px solid #1c2438;font-size:11px;line-height:1.7}
 .src-meta{margin-top:6px;font-size:11px}
 .mut{color:var(--mut)}
</style>
<header><div style="width:9px;height:9px;border-radius:99px;background:var(--ac)"></div>
 <div><h1>CUVIA Build Studio</h1><div class=sub id=bh>지도데이터 빌드 콘솔</div></div>
 <a href="/style" target="_blank" rel="noopener" style="margin-left:auto;color:var(--ac);text-decoration:none;border:1px solid var(--bd);border-radius:8px;padding:7px 13px;font-size:13px">🎨 스타일 디자인 →</a></header>
<div class=wrap>
 <div>
  <div class=panel><h2>데이터 출처 · 업로드</h2>
   <div class=sub style="font-size:12px;color:var(--mut);margin-bottom:10px;line-height:1.6">출처별로 <b>zip 또는 폴더</b>를 업로드하면 자동 검증 후 빌드합니다. 용량 제한 없음(대용량 스트리밍). 카드에 <b>드래그&드롭</b>도 가능.</div>
   <div id=sources class=ds></div></div>
  <div class=panel style="margin-top:14px"><h2>빌드 이력</h2>
   <div id=builds class=ds></div></div>
 </div>
 <div>
  <div class=panel><h2>빌드 대상 (정제 체크)</h2>
   <div id=checks></div>
   <div style="display:flex;gap:8px;margin-top:12px">
     <button id=run>빌드 시작</button></div></div>
  <div class=panel style="margin-top:14px"><h2>빌드 진행률</h2><div id=cards></div>
   <h2 style="margin-top:14px">실시간 로그</h2><pre id=log></pre></div>
 </div>
</div>
<script>
const $=s=>document.querySelector(s), cards={}, bars={}, sts={};
let TARGETS=[];
fetch('/api/targets').then(r=>r.json()).then(d=>{
  TARGETS=d.targets.filter(t=>t.kind[0]!=='_');
  $('#checks').innerHTML=TARGETS.map(t=>`<label class=row><input type=checkbox value="${t.kind}" checked> ${t.label}${t.dep?`<span class=chip>← ${t.dep}</span>`:''}</label>`).join('');
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
 setStatus(d.kind,d.status,d.progress); if(d.line)logln('['+d.kind+'] '+d.line);
 if(d.status==='done')loadBuilds();};
function fmt(d){const x=String(d||'').replace(/\D/g,'');return x.length===8?`${x.slice(0,4)}-${x.slice(4,6)}-${x.slice(6,8)}`:x.length===6?`${x.slice(0,4)}-${x.slice(4,6)}`:(d||'−');}
function srcStatus(s){return s.status==='update'?'🔴 업데이트 있음':(s.status==='current'?'🟢 최신':'—');}
function vbadge(s){if(!s.uploadable)return '';
  const m={ok:'🟢 검증 OK',warn:'🟡 검증 경고',fail:'🔴 검증 실패',pending:'⏳ 검증 대기'},v=s.validation;
  const lab=v?(m[v]||v):(s.file?'⏳ 검증 대기':'<span class=mut>미업로드</span>');
  const btn=s.file?` <a onclick="validateSource('${s.key}')">[${v&&v!=='pending'?'재검증':'검증'}]</a>`:'';
  return `<div class=src-meta>${lab}${s.validation_msg?` · <span class=mut>${s.validation_msg}</span>`:''}${btn}</div>`;}
let SRC=[];
function loadSources(){fetch('/api/sources').then(r=>r.json()).then(d=>{
  if(d.build_home)$('#bh').textContent='BUILD_HOME: '+d.build_home;
  SRC=d.sources||[];
  $('#sources').innerHTML=SRC.map(s=>`<div class=src ${s.uploadable?`data-key="${s.key}"`:''}>
   <div class=src-h><b>${s.name}</b> <span class=chip>${s.category}</span></div>
   <div class=src-act><a href="${s.url}" target=_blank rel=noopener>다운로드 ↗</a>${s.uploadable?` · <a onclick="pickFiles('${s.key}',false)">⬆ 파일/zip</a> · <a onclick="pickFiles('${s.key}',true)">📁 폴더</a> <span class=mut>· 드롭 가능</span>`:` · <span class=mut>업로드 미지원(오프라인 재빌드)</span>`}</div>
   ${s.uploadable?`<div class=bar id=ub_${s.key} style="display:none;margin-top:6px"><i id=ubi_${s.key}></i></div>`:''}
   <div class=src-ver>현재 <b>${fmt(s.current)}</b> <a onclick="setVer('${s.key}','current')">[수정]</a> · 최신 <b>${fmt(s.latest)}</b> ${s.auto?`<a onclick="checkLatest('${s.key}',this)">[최신 조회]</a>`:`<a onclick="setVer('${s.key}','latest')">[확인]</a>`} · ${srcStatus(s)}${s.checked_at?` <span class=mut>(${s.checked_at})</span>`:''}</div>
   ${s.file?`<div class=src-meta>📄 ${s.file}${(s.history&&s.history.length>1)?` · 이력 ${s.history.length}`:''}</div>`:''}
   ${vbadge(s)}</div>`).join('');
  bindDrops();});}
function setVer(key,field){const v=prompt((field==='current'?'현재(빌드에 쓴)':'최신')+' 기준일 입력 — 예: 202605 또는 2026-06-19');
  if(v==null)return; fetch('/api/sources/version',{method:'POST',body:JSON.stringify({key,field,value:v})})
   .then(r=>r.json()).then(d=>{if(d.error)alert(d.error);loadSources();});}
function checkLatest(key,a){if(a)a.textContent='조회중…';
  fetch('/api/sources/check',{method:'POST',body:JSON.stringify({key})}).then(r=>r.json()).then(d=>{
   if(d.error)alert('최신 조회 실패: '+d.error); loadSources();}).catch(e=>{alert('실패: '+e);loadSources();});}
function pickFiles(key,dir){const inp=document.createElement('input');inp.type='file';inp.multiple=true;
  if(dir)inp.webkitdirectory=true;
  inp.onchange=()=>{const fs=[...inp.files];if(fs.length)uploadAll(key,fs);};inp.click();}
function relName(f){const r=f._rel||f.webkitRelativePath||'';const p=r.split('/').filter(Boolean);
  return p.length>1?p.slice(1).join('/'):f.name;}   // 폴더 업로드 시 선택폴더명(첫 세그먼트) 제거 → 내부구조 보존
function setUbar(key,p){const b=$('#ub_'+key),i=$('#ubi_'+key);if(b){b.style.display='';i.style.width=Math.round(p*100)+'%';}}
function upOne(key,f){return new Promise((res,rej)=>{const x=new XMLHttpRequest();
  x.open('POST','/api/sources/upload?key='+encodeURIComponent(key)+'&name='+encodeURIComponent(relName(f)));
  x.upload.onprogress=e=>{if(e.lengthComputable)setUbar(key,e.loaded/e.total);};
  x.onload=()=>{try{const d=JSON.parse(x.responseText);d.error?rej(d.error):res(d);}catch(_){rej('응답오류 '+x.status);}};
  x.onerror=()=>rej('네트워크 오류');x.send(f);});}
async function uploadAll(key,files){if(!files.length)return;
  const total=files.reduce((a,f)=>a+(f.size||0),0)||1;
  logln('⇧ '+key+' 업로드 '+files.length+'개 ('+gb(total)+') …');
  let done=0,doneB=0;
  for(const f of files){try{await upOne(key,f);}catch(e){logln('  ✗ '+relName(f)+' — '+e);}
    done++;doneB+=(f.size||0);setUbar(key,doneB/total);}
  logln('  ✓ '+key+' 업로드 완료 ('+done+'/'+files.length+') — 검증 중…');
  await validateSource(key,true);setUbar(key,1);
  setTimeout(()=>{const b=$('#ub_'+key);if(b)b.style.display='none';},900);loadSources();}
function validateSource(key,quiet){return fetch('/api/sources/validate',{method:'POST',body:JSON.stringify({key})})
  .then(r=>r.json()).then(d=>{if(d.error){logln('✗ '+key+' 검증 실패: '+d.error);return;}
    const ic={ok:'🟢',warn:'🟡',fail:'🔴'}[d.status]||'•';
    logln(ic+' '+key+' 검증: '+d.status+(d.msg?' — '+d.msg:''));if(!quiet)loadSources();})
  .catch(e=>logln('✗ '+key+' 검증 오류: '+e));}
function bindDrops(){document.querySelectorAll('.src[data-key]').forEach(el=>{const key=el.getAttribute('data-key');
  ['dragenter','dragover'].forEach(ev=>el.addEventListener(ev,e=>{e.preventDefault();e.stopPropagation();el.style.borderColor='var(--ac)';}));
  ['dragleave','dragend','drop'].forEach(ev=>el.addEventListener(ev,e=>{e.preventDefault();e.stopPropagation();el.style.borderColor='';}));
  el.addEventListener('drop',e=>gatherFiles(e.dataTransfer).then(fs=>uploadAll(key,fs)));});}
function gb(n){return (n/1073741824).toFixed(2)+'GB';}
function loadBuilds(){fetch('/api/builds').then(r=>r.json()).then(d=>{
  const c=d.current_bundle||{};
  const dl=c.exists?`<a href="/api/download/bundle" style="color:var(--ac)">⬇ 현재 번들 다운로드 (${gb(c.size)})</a>`:'<i>번들 없음 — 빌드/패키징을 먼저</i>';
  const rows=(d.builds||[]).map(b=>`<div style="font-size:11px;padding:3px 0;border-bottom:1px solid var(--bd)">
   <b>${b.version}</b> · ${(b.built_at||'').replace('T',' ').slice(0,16)} · ${gb(b.bundle_bytes||0)}${b.git?' · '+b.git:''}${b.qc?' · QC '+b.qc:''}</div>`).join('')||'<i>이력 없음</i>';
  $('#builds').innerHTML=`<div style="margin-bottom:8px">${dl}</div>${rows}`;});}
loadSources(); loadBuilds();
const TSRC={localdata:['localdata'],geocode:['juso_navi','sangga','localdata'],buildings:['building_db']};
$('#run').onclick=()=>{const t=[...document.querySelectorAll('#checks input:checked')].map(x=>x.value);
 if(!t.length)return alert('대상을 선택하세요');
 const need=new Set();t.forEach(k=>(TSRC[k]||[]).forEach(s=>need.add(s)));
 const byKey=Object.fromEntries(SRC.map(s=>[s.key,s]));
 [...need].forEach(k=>{const s=byKey[k];if(s&&s.validation!=='ok')logln('⚠ '+(s.name||k)+' 검증 '+(s.validation||'안 됨')+' — 그대로 빌드(경고)');});
 const A={staged:'⬆ 새 데이터 적재',ok:'✓ 적재됨',reused:'↻ 직전 데이터 사용','missing':'⚠ 데이터 없음',partial:'⚠ 일부만 적재(오류)','no-tool':'⚠ 추출도구 없음',error:'✗ 오류'};
 fetch('/api/build',{method:'POST',body:JSON.stringify({targets:t})}).then(r=>r.json()).then(d=>{
   (d.prepared||[]).forEach(p=>logln('  📦 '+p.key+': '+(A[p.action]||p.action)+(p.n?' ('+p.n+'개)':'')+(p.msg?' — '+p.msg:'')));
   logln('▶ 큐: '+d.queued.join(', '));loadSources();});};
// 드롭된 폴더를 재귀적으로 펼쳐 파일 목록으로(webkitGetAsEntry). _rel(fullPath)로 폴더구조 보존.
function gatherFiles(dt){
 const items=dt&&dt.items;
 if(!items||!items.length||!items[0].webkitGetAsEntry) return Promise.resolve([...((dt&&dt.files)||[])]);
 const entries=[...items].map(it=>it.webkitGetAsEntry&&it.webkitGetAsEntry()).filter(Boolean), out=[];
 const walk=en=>new Promise(res=>{
   if(en.isFile) en.file(f=>{try{f._rel=en.fullPath;}catch(_){}out.push(f);res();},()=>res());
   else if(en.isDirectory){const rd=en.createReader(),acc=[];
     (function batch(){rd.readEntries(es=>{if(!es.length){Promise.all(acc.map(walk)).then(res);return;}acc.push(...es);batch();},()=>res());})();}
   else res();});
 return Promise.all(entries.map(walk)).then(()=>out);
}
</script></html>"""


if __name__ == "__main__":
    shown = "localhost" if HOST in ("127.0.0.1", "localhost") else HOST
    warn = "" if HOST in ("127.0.0.1", "localhost") else "  ⚠ 외부노출(무인증) — 신뢰망에서만"
    print(f"CUVIA Build Studio → http://{shown}:{PORT}  (bind {HOST}, BUILD_HOME={BUILD_HOME}){warn}", flush=True)
    ThreadingHTTPServer((HOST, PORT), H).serve_forever()
