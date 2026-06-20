#!/usr/bin/env python3
"""CUVIA Build Studio (MVP) — 무의존(파이썬 표준 라이브러리만) 빌드 콘솔.

원천데이터(SHP/CSV)를 기반으로 지오코드 인덱스·타일을 빌드하고, 종류별 진행률을 SSE로
실시간 표시하며, 체크박스로 빌드 대상을 정제하고, 완료물을 폐쇄망 번들로 패키징한다.
이미 검증된 CLI 파이프라인(09/10/11/12/13/package)을 그대로 빌드 엔진으로 구동한다.

기동:  python3 scripts/build-studio.py            # http://localhost:8090
       BUILD_HOME=~/geocode-build PORT=8090 python3 scripts/build-studio.py
"""
import json, os, pathlib, queue, subprocess, threading, time, re, ssl, sqlite3, shutil, zipfile, hashlib, urllib.request, urllib.parse
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


def _v_boundary(files):
    # 행정구역 경계 shp(zip 내부) — admin 은 BND_ADM_DONG_PG.shp 직배치, legal 은 시도별 zip-of-zips.
    zips = [f for f in files if f.suffix.lower() == ".zip"]
    shps = [f for f in files if f.suffix.lower() == ".shp"]
    if zips:
        shp_n = nested = 0
        for z in zips:
            try:
                with zipfile.ZipFile(z) as zf:
                    names = zf.namelist()
                    shp_n += sum(1 for n in names if n.lower().endswith(".shp"))
                    nested += sum(1 for n in names if n.lower().endswith(".zip"))
            except Exception:
                return ("warn", f"zip {len(zips)}개 (일부 열기 실패)")
        if shp_n:   return ("ok", f"zip {len(zips)}개 · shp {shp_n}개")
        if nested:  return ("ok", f"zip {len(zips)}개 · 중첩 zip {nested}개(시도별 묶음 — 빌드 시 재귀추출)")
        return ("warn", f"zip {len(zips)}개지만 .shp/중첩 zip 없음")
    if shps:
        return ("ok", f"shp {len(shps)}개")
    return ("fail", "zip(.shp 포함) 또는 .shp 가 필요합니다")


_VALIDATORS = {"juso_navi": _v_navi, "sangga": _v_sangga, "localdata": _v_localdata, "building_db": _v_building,
               "boundary_legal": _v_boundary, "boundary_admin": _v_boundary}


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


# ── 건물DB(GIS건물통합정보) 시도 파싱 — F_FAC_BUILDING_<시도코드2자리>_<YYYYMM> ──────────
# VWorld dsId=18 은 시도 17개 단위 SHP. zip 내부 레이어명에서 시도코드·기준일 추출.
_BUILDING_SIDO = {
    "11": "서울특별시", "26": "부산광역시", "27": "대구광역시", "28": "인천광역시",
    "29": "광주광역시", "30": "대전광역시", "31": "울산광역시", "36": "세종특별자치시",
    "41": "경기도", "42": "강원특별자치도", "51": "강원특별자치도", "43": "충청북도",
    "44": "충청남도", "45": "전북특별자치도", "52": "전북특별자치도", "46": "전라남도",
    "47": "경상북도", "48": "경상남도", "50": "제주특별자치도",
}
# 파일명에 코드 없이 한글 시도명만 있는 경우 폴백(부분일치, 긴 별칭 우선)
_SIDO_ALIASES = [
    ("11", "서울"), ("26", "부산"), ("27", "대구"), ("28", "인천"), ("29", "광주"),
    ("30", "대전"), ("31", "울산"), ("36", "세종"), ("41", "경기"), ("51", "강원"),
    ("43", "충청북"), ("43", "충북"), ("44", "충청남"), ("44", "충남"),
    ("52", "전라북"), ("52", "전북"), ("46", "전라남"), ("46", "전남"),
    ("47", "경상북"), ("47", "경북"), ("48", "경상남"), ("48", "경남"), ("50", "제주"),
]
_BLD_PAT = re.compile(r"F_FAC_BUILDING_(\d{2})_(\d{6,8})", re.I)


def _building_meta(path):
    """업로드 건물 파일 → (시도코드|None, 기준일 digits|''). 우선 파일명, 다음 zip 내부 SHP명, 폴백 한글 시도명/날짜."""
    name = path.name
    m = _BLD_PAT.search(name)
    if m:
        return m.group(1), m.group(2)
    names = []
    if path.suffix.lower() == ".zip":
        try:
            if zipfile.is_zipfile(path):
                with zipfile.ZipFile(path) as zf:
                    names = zf.namelist()
        except Exception:
            names = []
        for n in names:               # 내부 SHP 레이어명에서 코드+기준일
            mm = _BLD_PAT.search(n)
            if mm:
                return mm.group(1), mm.group(2)
    blob = name + " " + " ".join(names)
    for code, alias in _SIDO_ALIASES:  # 한글 시도명 폴백
        if alias in blob:
            return code, _period_from_name(name) or _period_from_name(" ".join(names))
    return None, _period_from_name(name)


def scan_uploaded_files(key):
    """업로드 출처 디렉토리의 파일 단위 현황 — 파일명·기준일·크기·업로드시각·최신화상태(+건물DB는 시도 그룹핑).
    진실원천=디스크 실파일(rglob), 업로드시각은 upload_history 로 보강."""
    src = next((s for s in load_sources() if s["key"] == key), None)
    if not src:
        return None
    sdir = SOURCES_DIR / key
    rec = load_versions().get(key, {}); latest = rec.get("latest")
    hist = {}
    c = _db()
    try:
        for f, uat in c.execute("SELECT file,uploaded_at FROM upload_history WHERE key=? ORDER BY id DESC", (key,)):
            hist.setdefault(f, uat)   # 파일명별 최신 업로드시각
    finally:
        c.close()
    is_bld = (key == "building_db")
    groups = {}
    if sdir.is_dir():
        for f in sorted((p for p in sdir.rglob("*") if p.is_file()), key=lambda p: str(p)):
            rel = str(f.relative_to(sdir))
            code, asof = _building_meta(f) if is_bld else (None, _period_from_name(f.name))
            st = f.stat()
            row = {"file": rel, "asof": asof or None, "size": st.st_size,
                   "uploaded_at": hist.get(rel) or hist.get(f.name)
                                  or time.strftime("%Y-%m-%d %H:%M", time.localtime(st.st_mtime)),
                   "status": _cmp_period(asof, latest)}
            g = groups.setdefault(code or "", {"code": code, "files": []})
            g["files"].append(row)
    regions = []
    for gk in sorted(groups, key=lambda k: (k == "", k)):   # 미분류(빈키) 맨 뒤
        g = groups[gk]; asofs = [r["asof"] for r in g["files"] if r["asof"]]
        newest = max(asofs, key=_norm) if asofs else None
        regions.append({"code": g["code"],
                        "name": (_BUILDING_SIDO.get(g["code"]) if g["code"] else None),
                        "asof": newest, "status": _cmp_period(newest, latest),
                        "n": len(g["files"]), "files": g["files"]})
    return {"key": key, "latest": latest, "grouped": is_bld,
            "total": sum(r["n"] for r in regions), "regions": regions}


def load_builds():
    return _load_json(DIST / "builds.json", [])

def _norm(x):
    return re.sub(r"\D", "", str(x or ""))   # 기준일 비교용 — 숫자만(202605 vs 2026-06-18 호환)

def _cmp_period(cur, lat):
    """기준일 vs 최신 → 'current'|'update'|'unknown'. 공통 정밀도 비교(YYYYMM vs YYYYMMDD 동월 오판 방지)."""
    if not cur or not lat:
        return "unknown"
    a, b = _norm(cur), _norm(lat); k = min(len(a), len(b))
    if not k:
        return "unknown"
    return "update" if b[:k] > a[:k] else "current"

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


def _unzip_recursive(path, dest, depth=4):
    """zip 추출 + 내부 중첩 zip 재귀 추출(zip-of-zips: 법정동 LSMD 시도별 묶음 등). 내부 zip은 풀고 제거."""
    dest = pathlib.Path(dest)
    with zipfile.ZipFile(path) as z:
        z.extractall(dest)
    for _ in range(depth):
        inners = [p for p in dest.rglob("*.zip") if p.is_file()]
        if not inners:
            break
        for inner in inners:
            try:
                with zipfile.ZipFile(inner) as z:
                    z.extractall(inner.parent)
                inner.unlink()
            except Exception:
                pass


def prepare_sources(keys=None):
    """sources/<key>/ 의 '모든' 업로드 파일을 build_input.dest 로 적재(누적).
    확장자별: .zip=stdlib 추출 / .7z=CLI(있으면) / .tar·.tgz·.txz=stdlib 추출 / 그외(.csv·.txt·.shp 등)=복사.
    업로드가 없으면 기존 staged 재사용(직전 빌드분 또는 BUILD_HOME 직접 배치 데이터)."""
    import shutil, tarfile, zipfile
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
                        _unzip_recursive(f, dest); n += 1   # 중첩 zip(zip-of-zips: 법정동 시도별 묶음)도 재귀 추출
                    except Exception as e:
                        errs.append(f"{f.name}: zip {str(e)[:50]}")
                elif ext == ".7z":
                    if not tool:
                        errs.append(f"{f.name}: 7z 없음 — scripts/setup-build-host.sh 실행(p7zip 설치) 후 재빌드, 또는 .txt 직접배치"); continue
                    r = subprocess.run([tool, "x", "-y", f"-o{dest}", str(f)], capture_output=True, text=True, timeout=3600)
                    if r.returncode != 0: errs.append(f"{f.name}: {(r.stderr or '')[-80:]}")
                    else: n += 1
                elif ext == ".tar" or f.name.lower().endswith((".tar.gz", ".tgz", ".tar.bz2", ".tbz2", ".tar.xz", ".txz")):
                    try:
                        with tarfile.open(f) as t:
                            try: t.extractall(dest, filter="data")   # py3.12+ 경로조작 방지
                            except TypeError: t.extractall(dest)     # 구버전 폴백
                        n += 1
                    except Exception as e:
                        errs.append(f"{f.name}: tar {str(e)[:50]}")
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
        "facility": dict(label="생활편의시설 정제 (facility)", dep=None,
            cmd=[py, str(ROOT/"scripts/11b-build-facility.py"), str(BUILD_HOME/"staged/facility"),
                 str(BUILD_HOME/"poi-all/facility_clean.csv")]),   # kind=facility 로 09가 적재(biz 분리)
        "osm_vector": dict(label="OSM 벡터타일 (korea.mbtiles)", dep=None,
            cmd=["bash", str(ROOT/"scripts/02-gen-vector.sh")]),
        "osm_sqlite": dict(label="OSM 지오코더 소스 (osm.sqlite)", dep="osm_vector",
            cmd=[py, str(ROOT/"scripts/osm-from-mbtiles.py")]),
        "dong": dict(label="아파트 동 라벨 (dong.mbtiles)", dep=None,
            cmd=["bash", "-c", f'python3 "{ROOT/"scripts/04-gen-dong-labels.py"}" && python3 "{ROOT/"scripts/05-gen-dong-tiles.py"}"']),
        "geocode": dict(label="통합 지오코딩 인덱스", dep=["localdata", "facility"],
            cmd=[py, str(ROOT/"scripts/09-gen-geocode.py"), "--src", SRC_JUSO,
                 "--osm", str(BUILD_HOME/"osm.sqlite"), "--poi-csv-dir", str(BUILD_HOME/"poi-all"),
                 "--out", str(BUILD_HOME/"geocode.sqlite"), "--dedup", "er"]),   # ER 중복제거+건물키 backfill 적용(없으면 09 기본 legacy)
        "areas": dict(label="행정구역 경계 (법정동·행정동 → areas)", dep="geocode",
            cmd=["bash", "-c",
                 f'python3 "{ROOT/"scripts/06-gen-areas.py"}" --shp "{BUILD_HOME/"sources/boundary/legal"}" --srs EPSG:5186 --name-field EMD_NM --code-field EMD_CD --type legal-dong --db "{BUILD_HOME/"geocode.sqlite"}"'
                 f' && python3 "{ROOT/"scripts/06-gen-areas.py"}" --shp "{BUILD_HOME/"sources/boundary/admin/BND_ADM_DONG_PG.shp"}" --srs EPSG:5186 --name-field ADM_NM --code-field ADM_CD --type admin-dong --db "{BUILD_HOME/"geocode.sqlite"}"']),
        "buildings": dict(label="3D 건물 타일", dep=None,
            cmd=["bash", str(ROOT/"scripts/10-gen-buildings.sh"), SRC_GIS]),
        "poi": dict(label="시설 라벨 타일 (poi.mbtiles)", dep="geocode",
            cmd=["bash", str(ROOT/"scripts/12-build-poi.sh")]),   # repo 원본 직접 — 배포본(BUILD_HOME/build-poi.sh) 동기 불필요
        "qc": dict(label="QC 검증", dep=None,
            cmd=[py, str(ROOT/"scripts/13-qc-check.py"), "--db", str(BUILD_HOME/"geocode.sqlite"),
                 "--tiles", str(BUILD_HOME/"tiles"), "--style", str(ROOT/"style/style.json"),
                 "--config", str(ROOT/"server/tileserver-config.json"), "--api", "http://localhost:8082"]),
        "package": dict(label="폐쇄망 번들 패키징", dep=None,
            cmd=["bash", str(ROOT/"scripts/package.sh")]),
    }

CANON = ["osm_vector", "osm_sqlite", "dong", "localdata", "facility", "geocode", "areas", "buildings", "poi", "qc", "package"]


def _deps(t):
    """TARGETS 항목의 dep(None|str|list) → dep 키 리스트로 평탄화."""
    d = t.get("dep") if isinstance(t, dict) else None
    if not d: return []
    return [d] if isinstance(d, str) else list(d)


# ── 타겟 최신성(freshness) — 자동 재빌드 판정 ────────────────────────
# 각 타겟의 "최신"=(입력 소스 SHA + 빌드 스크립트 해시 + 상위 타겟 시그니처)가 직전 성공빌드와
# 동일 AND 산출물 파일 존재. 동일하면 자동 건너뜀('fresh'). 사용자가 명시 체크한 타겟은 항상 빌드.
#   src     : 직접 읽는 원천 소스키(sources_state.staged_sig 로 변경 추적; 자식 <key>:* 포함)
#   dep_art : 산출물을 입력으로 받는 상위 타겟(그 타겟 시그니처를 재귀 포함 → 연쇄 변경 전파)
#   scripts : 빌드 로직 스크립트(내용 해시 — 코드 수정 시 재빌드)
#   out     : 산출물 경로(전부 존재해야 '최신'); always=True 면 항상 빌드(qc·package)
TFRESH = {
    "osm_vector": {"src": ["osm"], "scripts": ["scripts/02-gen-vector.sh"],
                   "out": [ROOT / "tiles/korea.mbtiles"]},
    "osm_sqlite": {"dep_art": ["osm_vector"], "scripts": ["scripts/osm-from-mbtiles.py"],
                   "out": [BUILD_HOME / "osm.sqlite"]},
    "dong": {"src": ["osm"], "scripts": ["scripts/04-gen-dong-labels.py", "scripts/05-gen-dong-tiles.py"],
             "out": [ROOT / "tiles/dong.mbtiles"]},
    "localdata": {"src": ["localdata"], "scripts": ["scripts/11-build-localdata.py"],
                  "out": [BUILD_HOME / "poi-all/localdata_clean.csv"]},
    "facility": {"src": ["facility"], "scripts": ["scripts/11b-build-facility.py"],
                 "out": [BUILD_HOME / "poi-all/facility_clean.csv"]},
    "geocode": {"src": ["juso_navi", "sangga"], "dep_art": ["osm_sqlite", "localdata", "facility"],
                "scripts": ["scripts/09-gen-geocode.py", "scripts/dedup_er.py"],
                "out": [BUILD_HOME / "geocode.sqlite"]},
    "areas": {"src": ["boundary_legal", "boundary_admin"], "dep_art": ["geocode"],
              "scripts": ["scripts/06-gen-areas.py"], "out": [BUILD_HOME / "geocode.sqlite"]},
    "buildings": {"src": ["building_db"], "scripts": ["scripts/10-gen-buildings.sh"],
                  "out": [BUILD_HOME / "tiles/buildings.mbtiles"]},
    "poi": {"dep_art": ["geocode"], "scripts": ["scripts/12-build-poi.sh"],
            "out": [BUILD_HOME / "tiles/poi.mbtiles"]},
    "qc": {"always": True},
    "package": {"always": True},
}

BUILD_STATE = BUILD_HOME / "build_state.json"   # 타겟별 직전 성공빌드 시그니처


def load_build_state():
    try:
        return json.loads(BUILD_STATE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_build_state(st):
    BUILD_STATE.parent.mkdir(parents=True, exist_ok=True)
    tmp = BUILD_STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(st, ensure_ascii=False, indent=0), encoding="utf-8")
    tmp.replace(BUILD_STATE)


_SCRIPT_HASH_CACHE = {}   # rel → (mtime_ns, hash) — 동일 요청 내 반복 해시 방지


def _script_hash(rel):
    """빌드 스크립트 내용 해시(앞 16헥스). 없으면 'missing'(산출물검사로 커버)."""
    p = ROOT / rel
    try:
        mt = p.stat().st_mtime_ns
    except OSError:
        return "missing"
    c = _SCRIPT_HASH_CACHE.get(rel)
    if c and c[0] == mt:
        return c[1]
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 16), b""):
            h.update(b)
    v = h.hexdigest()[:16]
    _SCRIPT_HASH_CACHE[rel] = (mt, v)
    return v


def _target_sig(kind, ver=None, _seen=None):
    """타겟 최신 판정용 시그니처 — 입력 소스 SHA + 스크립트 해시 + 상위 타겟 시그니처(재귀)."""
    if ver is None:
        ver = load_versions()
    if _seen is None:
        _seen = set()
    if kind in _seen:
        return ""   # 순환 방지
    _seen = _seen | {kind}
    m = TFRESH.get(kind, {})
    parts = []
    for sk in m.get("src", []):   # 소스 + 자식(<sk>:*) staged_sig 정렬 결합
        subs = sorted(f"{kk}:{(v or {}).get('staged_sig', '')}"
                      for kk, v in ver.items() if kk == sk or kk.startswith(sk + ":"))
        parts.append(sk + "=" + "|".join(subs))
    for s in m.get("scripts", []):
        parts.append(s + "=" + _script_hash(s))
    for d in m.get("dep_art", []):
        parts.append("dep:" + d + "=" + _target_sig(d, ver, _seen))
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()[:24]


def target_freshness(kind, ver=None, state=None):
    """'fresh'(건너뜀가능) | 'stale'(입력변경) | 'missing'(산출물없음) | 'always'(항상빌드) | None."""
    m = TFRESH.get(kind)
    if not m:
        return None
    if m.get("always"):
        return "always"
    outs = m.get("out", [])
    if not outs or not all(pathlib.Path(o).exists() for o in outs):
        return "missing"
    if state is None:
        state = load_build_state()
    if ver is None:
        ver = load_versions()
    return "fresh" if (state.get(kind, {}) or {}).get("sig") == _target_sig(kind, ver) else "stale"


def all_freshness():
    ver = load_versions(); state = load_build_state()
    return {k: target_freshness(k, ver, state) for k in TARGETS()}


def record_build_sig(kind):
    """빌드 성공 후 시그니처 기록(always 타겟·미정의 타겟은 생략)."""
    m = TFRESH.get(kind)
    if not m or m.get("always"):
        return
    try:
        st = load_build_state()
        st[kind] = {"sig": _target_sig(kind), "built_at": time.strftime("%Y-%m-%d %H:%M")}
        save_build_state(st)
    except Exception:
        pass


def required_sources(kinds):
    """선택 타겟 + 전이 의존(빌드 dep & 산출물 dep)이 필요로 하는 소스키 집합."""
    T = TARGETS(); plan = set(); stack = list(kinds)
    while stack:
        k = stack.pop()
        if k in plan or k not in T:
            continue
        plan.add(k)
        for d in _deps(T[k]):
            stack.append(d)
        for d in TFRESH.get(k, {}).get("dep_art", []):
            stack.append(d)
    srcs = set()
    for k in plan:
        srcs.update(TFRESH.get(k, {}).get("src", []))
    return srcs


def source_presence(key, ver=None):
    """소스 데이터 존재/검증 상태 → (present:bool, validation:str|None). 자식 <key>:* 도 인정."""
    if ver is None:
        ver = load_versions()
    rec = ver.get(key, {}) or {}
    present = bool(rec.get("staged_sig") or rec.get("file") or rec.get("current"))
    if not present:
        present = any((v or {}).get("staged_sig") or (v or {}).get("current")
                      for k, v in ver.items() if k.startswith(key + ":"))
    return present, rec.get("validation_status")


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
        self.last_targets = []  # 직전 /api/build 정제 체크(매니페스트 기록용)
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
        # 명시 선택(kinds)은 항상 빌드. 의존성으로 끌려온 타겟은 '최신(fresh)'이면 자동 건너뜀
        # (산출물 그대로 재사용). 단일 워커 FIFO + CANON 정렬이라 상위는 하위보다 먼저 처리됨.
        T = TARGETS(); explicit = set(k for k in kinds if k in T)
        plan = set(); stack = list(explicit)
        while stack:
            k = stack.pop()
            t = T.get(k)
            if not t or k in plan: continue
            plan.add(k)
            for d in _deps(t):
                if d in T: stack.append(d)
        ordered = [k for k in CANON if k in plan]
        fresh_map = all_freshness()   # ver/state 1회 로드 — 의존성 자동 건너뜀 판정
        # jobs 변형은 subscribe()의 스냅샷과 같은 lock으로 보호(동시 SSE 연결 중 dict 크기변경 크래시 방지).
        # 이미 queued/running 인 kind는 재적재하지 않음(중복 동시 실행 방지). publish/work.put은 lock 밖에서.
        started = []; fresh = []
        with self.lock:
            for k in ordered:
                cur = self.jobs.get(k)
                if cur and cur["status"] in ("queued", "running"): continue
                if k not in explicit and fresh_map.get(k) == "fresh":   # 의존성+최신 → 건너뜀(재사용)
                    self.jobs[k] = {"status": "fresh", "progress": 1.0, "log": [], "st": {}}
                    fresh.append(k); continue
                self.jobs[k] = {"status": "queued", "progress": 0.0, "log": [], "st": {}}
                started.append(k)
        for k in fresh:
            self.publish({"kind": k, "status": "fresh", "progress": 1.0})
        for k in started:
            self.publish({"kind": k, "status": "queued", "progress": 0.0})
            self.work.put(k)
        return {"queued": started, "fresh": fresh}

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
        j = self.jobs[kind]
        # 상위(dep) 작업이 실패/스킵이면 하위는 실행하지 않고 skipped 처리.
        # 단일 워커 FIFO + CANON 정렬이라 상위는 이미 종료상태. 상위 산출물 부재로 인한
        # 혼란스러운 연쇄 에러(예: osm_vector 실패 → osm_sqlite 가 없는 korea.mbtiles 열다 죽음) 방지.
        bad = next((d for d in _deps(TARGETS().get(kind, {}))
                    if (self.jobs.get(d) or {}).get("status") in ("error", "skipped")), None)
        if bad:
            j["status"] = "skipped"
            self._emit(kind, f"[건너뜀] 상위 작업 '{bad}' 실패/스킵 — 실행하지 않음")
            self.publish({"kind": kind, "status": "skipped", "progress": j["progress"]})
            return
        j["status"] = "running"
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
            record_build_sig(kind)   # 성공 시그니처 기록 → 다음 빌드에서 변경없으면 자동 건너뜀
            if kind == "package":   # 빌드(패키징) 완료 → 매니페스트 자동저장(번들 포함)
                try: save_manifest(with_bundle=True)
                except Exception as e: self._emit(kind, f"[프로필 저장 실패] {e}")
        except Exception as e:
            j["status"] = "error"; self._emit(kind, f"[오류] {e}")
        self.publish({"kind": kind, "status": j["status"], "progress": j["progress"]})

MGR = Manager()


# ── 내용주소 저장소(store) + 자동수집(collector) ─────────────────────
# store/<sha[:2]>/<sha> 로 같은 내용 1벌 저장(중복 0, 영구 보관). 수집은 항목 1건씩 순차
# (Referer·백오프·HTML 차단감지) → SHA 비교(동일=staged 유지·재빌드 생략) → staged 추출/배치 → DB(현재일·sha=staged_sig).
STORE_DIR = BUILD_HOME / "store"
FACILITY_CATALOG = ROOT / "scripts" / "facility-catalog.json"
LOCALDATA_REGIONS = ROOT / "scripts" / "localdata-regions.json"
_COLLECT_LOCK = threading.Lock()
_DL_HEADERS = {"User-Agent": "Mozilla/5.0", "Referer": "https://www.localdata.go.kr/"}


def _sha256_file(path, buf=1 << 20):
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(buf), b""):
            h.update(chunk)
    return h.hexdigest()


def store_put(tmp_path):
    """임시파일을 SHA로 store에 1벌 저장(이미 있으면 tmp 삭제). (sha, reused) 반환."""
    sha = _sha256_file(tmp_path)
    dest = STORE_DIR / sha[:2] / sha
    if dest.exists():
        os.remove(tmp_path); return sha, True
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(tmp_path), str(dest))
    return sha, False


def store_path(sha):
    return STORE_DIR / sha[:2] / sha


def _http_download(url, dest, headers=None, retries=3, timeout=900):
    """파일 다운로드 — 302→/error·HTML 에러페이지 감지 + 백오프 재시도. 스트리밍 저장, size 반환."""
    hd = {**_DL_HEADERS, **(headers or {})}
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=hd)
            with urllib.request.urlopen(req, timeout=timeout, context=ssl._create_unverified_context()) as r:
                if "/error" in r.geturl():
                    raise RuntimeError(f"차단/에러 리다이렉트: {r.geturl()}")
                with open(dest, "wb") as o:
                    head = r.read(1 << 16)
                    low = head[:300].lstrip().lower()
                    if low.startswith(b"<!doctype html") or b"<html" in low:
                        raise RuntimeError("파일 아님(HTML 에러페이지) — 한국 IP/세션 확인")
                    o.write(head)
                    while True:
                        chunk = r.read(1 << 20)
                        if not chunk:
                            break
                        o.write(chunk)
            return os.path.getsize(dest)
        except Exception as e:
            last = e
            if attempt < retries - 1:
                time.sleep(3 * (attempt + 1))
    raise RuntimeError(f"다운로드 실패({retries}회): {str(last)[:140]}")


def _extract_into(src, dest_dir, orig_name=None):
    """수집/저장 파일을 staged 로 추출/복사 — 내용(매직바이트) 기반 판별(파일명 무관: store는 확장자 없는 <sha>).
    zip/tar/7z 추출, 그 외(csv 등) 복사. 확장자 없으면 .csv 보존(11/11b 가 *.csv glob)."""
    import tarfile
    dest_dir = pathlib.Path(dest_dir); dest_dir.mkdir(parents=True, exist_ok=True)
    src = pathlib.Path(src)
    if zipfile.is_zipfile(src):
        _unzip_recursive(src, dest_dir); return   # 중첩 zip 재귀
    if tarfile.is_tarfile(src):
        with tarfile.open(src) as t:
            try: t.extractall(dest_dir, filter="data")
            except TypeError: t.extractall(dest_dir)
        return
    with open(src, "rb") as fh: head = fh.read(6)
    if head == b"7z\xbc\xaf\x27\x1c":
        tool = shutil.which("7z") or shutil.which("7za") or shutil.which("7zr")
        if not tool: raise RuntimeError("7z 미설치 — scripts/setup-build-host.sh")
        subprocess.run([tool, "x", "-y", f"-o{dest_dir}", str(src)], check=True, capture_output=True, timeout=3600); return
    nm = orig_name or src.name
    if "." not in nm: nm += ".csv"
    shutil.copy2(src, dest_dir / nm)


def _collect_plan(item_key):
    """항목키 → (source, urls[], dest, mode). 항목키: '<srckey>' 또는 '<srckey>:<sub>'."""
    skey, _, sub = item_key.partition(":")
    sub = sub or None
    src = next((s for s in load_sources() if s["key"] == skey), None)
    if not src:
        raise RuntimeError(f"알 수 없는 출처: {skey}")
    col = src.get("collect") or {}; method = col.get("method")
    if method == "localdata_facility":
        cat = _load_json(FACILITY_CATALOG, {}); base = cat.get("base", "")
        it = next((x for x in cat.get("items", []) if x["key"] == sub), None)
        if not it:
            raise RuntimeError(f"facility 항목 없음: {sub}")
        return src, [base + p for p in it["paths"]], BUILD_HOME / "staged/facility" / sub, "extract"
    if method == "localdata_all":
        reg = _load_json(LOCALDATA_REGIONS, {}); base = reg.get("base", "")
        if sub:
            return src, [base + reg["region_path"].replace("{orgCode}", sub)], BUILD_HOME / "staged/localdata" / sub, "extract"
        return src, [base + reg["all_path"]], BUILD_HOME / "staged/localdata", "extract"
    if method == "geofabrik":
        return src, [col["url"]], ROOT / col.get("dest", "data/osm/south-korea.osm.pbf"), "file"  # 02-gen-vector.sh 가 ROOT/data/osm 읽음
    if method == "datago_filedown":   # data.go.kr 2단계: selectFileDataDownload.do(메타) → fileDownload.do(파일)
        meta_url = (col.get("detail_url", "https://www.data.go.kr/tcs/dss/selectFileDataDownload.do")
                    + f"?recommendDataYn=Y&publicDataPk={col.get('publicDataPk')}"
                    + f"&publicDataDetailPk={urllib.parse.quote(col.get('publicDataDetailPk', ''))}")
        req = urllib.request.Request(meta_url, headers={"User-Agent": "Mozilla/5.0", "Referer": src.get("url", "")})
        meta = json.loads(urllib.request.urlopen(req, timeout=30, context=ssl._create_unverified_context()).read().decode("utf-8", "replace"))
        if not (meta.get("status") and meta.get("atchFileId")):
            raise RuntimeError("상가 메타 조회 실패(atchFileId 없음) — data.go.kr 변경/차단 확인")
        dnm = (meta.get("dataSetFileDetailInfo") or {}).get("dataNm", "sangga")
        url = ("https://www.data.go.kr/cmm/cmm/fileDownload.do"
               + f"?atchFileId={meta['atchFileId']}&fileDetailSn={meta.get('fileDetailSn', '1')}&dataNm={urllib.parse.quote(dnm)}")
        return src, [url], BUILD_HOME / src.get("build_input", {}).get("dest", "poi-all/sangga"), "extract"
    raise RuntimeError(f"수집 미지원: {skey}")


def collect_catalog():
    """자동수집 목록(통합) — 소스 + facility 14종 + localdata 17시·도, DB 상태 병합. sha=staged_sig."""
    ver = load_versions(); out = []
    for s in load_sources():
        col = s.get("collect") or {}; key = s["key"]; v = ver.get(key, {}); m = col.get("method")
        node = {"key": key, "name": s["name"], "category": s["category"], "method": m,
                "collectable": bool(col), "host_capture": bool(col.get("host_capture")),
                "default_collect": bool(s.get("default_collect")), "uploadable": bool(s.get("build_input")),
                "auto": bool(s.get("latest_check")), "current": v.get("current"), "latest": v.get("latest"),
                "sha": v.get("staged_sig"), "checked_at": v.get("checked_at"), "children": []}
        if m == "localdata_facility":
            for it in _load_json(FACILITY_CATALOG, {}).get("items", []):
                cv = ver.get(f"{key}:{it['key']}", {})
                node["children"].append({"key": f"{key}:{it['key']}", "name": it["name"],
                    "default_collect": bool(it.get("default")), "role": it.get("role"),
                    "current": cv.get("current"), "latest": cv.get("latest"), "checked_at": cv.get("checked_at"), "sha": cv.get("staged_sig")})
        elif m == "localdata_all":
            for rg in _load_json(LOCALDATA_REGIONS, {}).get("regions", []):
                cv = ver.get(f"{key}:{rg['orgCode']}", {})
                node["children"].append({"key": f"{key}:{rg['orgCode']}", "name": rg["name"],
                    "default_collect": True, "current": cv.get("current"), "latest": cv.get("latest"), "checked_at": cv.get("checked_at"), "sha": cv.get("staged_sig")})
        out.append(node)
    return out


def _datago_search_filepk(keyword, want_prefix=None):
    """data.go.kr fileData 검색 → publicDataPk. want_prefix 제목 접두 일치 우선, 없으면 첫 '행정안전부' 데이터셋."""
    import unicodedata
    kw = urllib.parse.quote(keyword)
    url = f"https://www.data.go.kr/tcs/dss/selectDataSetList.do?dType=FILE&keyword={kw}&perPage=30&currentPage=1"
    html = urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"}),
                                  timeout=20, context=ssl._create_unverified_context()).read().decode("utf-8", "replace")
    html = unicodedata.normalize("NFC", html); first = None
    for pk, inner in re.findall(r'<a href="/data/(\d+)/fileData\.do"[^>]*>(.*?)</a>', html, re.S):
        t = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", inner)).strip()
        t = re.sub(r"^(CSV|XLS|XLSX|JSON|XML|HWP|PDF|ZIP|SHP)\s+", "", t)
        if want_prefix and t.startswith(want_prefix):
            return pk
        if first is None and t.startswith("행정안전부"):
            first = pk
    return first


def _datago_updt(pk):
    """publicDataPk → 현행화 날짜(yyyy-mm-dd). dataNm 의 _YYYYMMDD 우선, 없으면 updtDt."""
    if not pk:
        return None
    url = f"https://www.data.go.kr/tcs/dss/selectFileDataDownload.do?publicDataPk={pk}&fileDetailSn=1"
    info = json.loads(urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"}),
          timeout=20, context=ssl._create_unverified_context()).read().decode("utf-8", "replace")).get("dataSetFileDetailInfo", {})
    m = re.search(r"(\d{4})(\d{2})(\d{2})$", (info.get("dataNm") or "").replace("_", ""))
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    return (info.get("updtDt") or info.get("registDt") or "")[:10] or None


def _collect_latest(item_key):
    """항목별 최신일 — facility=data.go.kr 행정안전부_<시설명> 현행화일, localdata 지역=인허가 전국 대표(병원, ≈D-2)."""
    skey, _, sub = item_key.partition(":")
    if skey == "facility":
        it = next((x for x in _load_json(FACILITY_CATALOG, {}).get("items", []) if x["key"] == sub), None)
        return _datago_updt(_datago_search_filepk(f"행정안전부 {it['name']}")) if it else None
    if skey == "localdata":
        return _datago_updt(_datago_search_filepk("행정안전부_건강_병원", "행정안전부_건강_병원"))
    return None


def _item_dest(item_key):
    """항목키 → staged 경로(개별 다운/업로드용). 상위=build_input.dest, 하위=staged/<군>/<sub>."""
    skey, _, sub = item_key.partition(":")
    src = next((s for s in load_sources() if s["key"] == skey), None)
    if not src:
        return None
    if not sub:
        bi = src.get("build_input"); return (BUILD_HOME / bi["dest"]) if bi else None
    col = src.get("collect") or {}
    if col.get("method") == "localdata_facility":
        return BUILD_HOME / "staged/facility" / sub
    if col.get("method") == "localdata_all":
        return BUILD_HOME / "staged/localdata" / sub
    return None


def run_collect(selected):
    """선택 항목 1건씩 순차 수집(차단 회피 간격). SHA 동일이면 staged 유지·재빌드 생략. SSE=kind 'collect'."""
    with _COLLECT_LOCK:
        ver = load_versions()
        MGR.jobs["collect"] = {"status": "running", "progress": 0.0, "log": [], "st": {}}
        MGR.publish({"kind": "collect", "status": "running", "progress": 0.0})
        STORE_DIR.mkdir(parents=True, exist_ok=True)
        tmpdir = BUILD_HOME / "tmp"; tmpdir.mkdir(parents=True, exist_ok=True)
        total = len(selected); done = 0; changed = []
        for item_key in selected:
            try:
                src, urls, dest, mode = _collect_plan(item_key)
                MGR._emit("collect", f"⇩ {item_key} 다운로드 ({len(urls)}파일)…")
                shas = []
                if mode == "file":
                    destp = pathlib.Path(dest); destp.parent.mkdir(parents=True, exist_ok=True)
                    tmp = tmpdir / (item_key.replace(":", "_") + ".part")
                    sz = _http_download(urls[0], tmp)
                    sha, reused = store_put(tmp); shas.append(sha)
                    if not reused or not destp.exists():
                        shutil.copy2(store_path(sha), destp)
                    MGR._emit("collect", f"  {sz/1e6:.1f}MB sha={sha[:8]}{' (변경없음)' if reused else ' → 배치'}")
                else:
                    allreused = True
                    for i, u in enumerate(urls):
                        tmp = tmpdir / (item_key.replace(":", "_") + f"_{i}.part")
                        sz = _http_download(u, tmp)
                        sha, reused = store_put(tmp); shas.append(sha); allreused = allreused and reused
                        MGR._emit("collect", f"  파일{i+1}/{len(urls)} {sz/1e6:.1f}MB sha={sha[:8]}{' (변경없음)' if reused else ''}")
                    if (not allreused) or (not _nonempty_dir(pathlib.Path(dest))):
                        shutil.rmtree(dest, ignore_errors=True)
                        for sha in shas:
                            _extract_into(store_path(sha), dest)
                cur_sha = ",".join(shas); prev = ver.get(item_key, {}).get("staged_sig")
                rec = ver.setdefault(item_key, {})
                rec["staged_sig"] = cur_sha; rec["current"] = time.strftime("%Y-%m-%d")
                rec["checked_at"] = time.strftime("%Y-%m-%d %H:%M")
                save_versions({item_key: rec})
                changed.append(item_key) if cur_sha != prev else None
                MGR._emit("collect", f"  {'✓ 갱신' if cur_sha != prev else '= 변경 없음'}: {item_key}")
            except Exception as e:
                MGR._emit("collect", f"  ✗ {item_key}: {str(e)[:140]}")
            done += 1
            MGR.jobs["collect"]["progress"] = done / max(total, 1)
            MGR.publish({"kind": "collect", "status": "running", "progress": done / max(total, 1)})
            time.sleep(6)   # 차단 회피 간격
        if 'osm' in changed:   # OSM 변경 시 변환(planetiler→korea.mbtiles, osm.sqlite, 동) 자동 enqueue
            bt = (next((s for s in load_sources() if s['key'] == 'osm'), {}).get('collect') or {}).get('build_targets') or []
            if bt:
                MGR._emit("collect", f"  ▶ OSM 변환 빌드 enqueue: {', '.join(bt)}")
                MGR.enqueue(bt)
        MGR.jobs["collect"]["status"] = "done"
        MGR._emit("collect", f"OK: 수집 완료 — 변경 {len(changed)}/{total}")
        MGR.publish({"kind": "collect", "status": "done", "progress": 1.0})
        return changed


# ── 빌드 프로필(매니페스트) — 빌드에 쓴 파일집합(SHA) 스냅샷 저장·불러오기 ─────────
MANIFESTS_DIR = BUILD_HOME / "manifests"


def list_manifests():
    out = []
    if MANIFESTS_DIR.is_dir():
        for p in sorted(MANIFESTS_DIR.glob("*.json"), reverse=True):
            try:
                m = json.loads(p.read_text(encoding="utf-8"))
                bdir = MANIFESTS_DIR / m["id"]
                m["has_bundle"] = (bdir / "cuvia-map-bundle.tgz").is_file()
                out.append(m)
            except Exception:
                pass
    return out


def _working_snapshot():
    """현재 sources_state(staged_sig=sha, current, file)에서 수집/업로드된 항목 스냅샷."""
    snap = {}
    for key, rec in load_versions().items():
        if rec.get("staged_sig") or rec.get("current"):
            snap[key] = {"sha": rec.get("staged_sig"), "current": rec.get("current"), "file": rec.get("file")}
    return snap


def save_manifest(name=None, with_bundle=False):
    MANIFESTS_DIR.mkdir(parents=True, exist_ok=True)
    mid = time.strftime("%Y%m%d-%H%M%S"); base = mid; i = 1
    while (MANIFESTS_DIR / f"{mid}.json").exists():   # 같은 초 저장 충돌 방지
        mid = f"{base}-{i}"; i += 1
    man = {"id": mid, "name": name or f"빌드 {time.strftime('%Y-%m-%d %H:%M')}",
           "created": time.strftime("%Y-%m-%d %H:%M"), "sources": _working_snapshot(),
           "targets": list(getattr(MGR, "last_targets", []) or [])}
    try:
        man["git"] = (subprocess.run(["git", "-C", str(ROOT), "rev-parse", "--short", "HEAD"],
                      capture_output=True, text=True, timeout=10).stdout.strip() or None)
    except Exception:
        man["git"] = None
    bundle = DIST / "cuvia-map-bundle.tgz"; images = DIST / "images.tar"
    if with_bundle and bundle.is_file():   # 이름붙은 프로필마다 출력 번들 보관
        bdir = MANIFESTS_DIR / mid; bdir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(bundle, bdir / "cuvia-map-bundle.tgz")
        if images.is_file(): shutil.copy2(images, bdir / "images.tar")
        man["bundle_bytes"] = (bdir / "cuvia-map-bundle.tgz").stat().st_size
    (MANIFESTS_DIR / f"{mid}.json").write_text(json.dumps(man, ensure_ascii=False), encoding="utf-8")
    return man


def load_manifest(mid):
    """프로필 SHA들을 store 에서 staged 로 재구성 + DB 상태 복원 → 다음 빌드가 그 파일로."""
    p = MANIFESTS_DIR / f"{mid}.json"
    if not p.is_file():
        raise RuntimeError("프로필 없음")
    man = json.loads(p.read_text(encoding="utf-8")); ver = load_versions(); restored = 0
    for key, info in (man.get("sources") or {}).items():
        sha = info.get("sha"); dest = _item_dest(key)
        if sha and dest:
            if dest.suffix:   # 파일형(osm .pbf)
                dest.parent.mkdir(parents=True, exist_ok=True)
                sp = store_path(sha.split(",")[0])
                if sp.exists(): shutil.copy2(sp, dest)
            else:
                shutil.rmtree(dest, ignore_errors=True)
                for s in sha.split(","):
                    sp = store_path(s)
                    if sp.exists(): _extract_into(sp, dest)
            restored += 1
        rec = ver.setdefault(key, {}); rec["staged_sig"] = sha
        rec["current"] = info.get("current"); rec["file"] = info.get("file")
        save_versions({key: rec})
    return {"id": mid, "name": man.get("name"), "restored": restored}


def rename_manifest(mid, name):
    p = MANIFESTS_DIR / f"{mid}.json"
    if not p.is_file():
        raise RuntimeError("프로필 없음")
    man = json.loads(p.read_text(encoding="utf-8")); man["name"] = name
    p.write_text(json.dumps(man, ensure_ascii=False), encoding="utf-8"); return man


def _referenced_shas():
    """남은 매니페스트 + 현재 working set 이 참조하는 모든 SHA(콤마결합 분해)."""
    refs = set()
    for m in list_manifests():
        for info in (m.get("sources") or {}).values():
            refs.update(s for s in str(info.get("sha") or "").split(",") if s)
    for rec in load_versions().values():
        refs.update(s for s in str(rec.get("staged_sig") or "").split(",") if s)
    return refs


def gc_store():
    """어떤 매니페스트/working set 도 참조 않는 store SHA 파일 정리. (지운 개수, 회수 bytes)."""
    if not STORE_DIR.is_dir():
        return 0, 0
    refs = _referenced_shas(); n = 0; freed = 0
    for sub in STORE_DIR.iterdir():
        if not sub.is_dir():
            continue
        for f in sub.iterdir():
            if f.is_file() and f.name not in refs:
                try:
                    freed += f.stat().st_size; f.unlink(); n += 1
                except OSError:
                    pass
    return n, freed


def delete_manifest(mid):
    (MANIFESTS_DIR / f"{mid}.json").unlink(missing_ok=True)
    shutil.rmtree(MANIFESTS_DIR / mid, ignore_errors=True)
    return gc_store()   # 삭제 후 미참조 SHA 파일 정리


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
            fr = all_freshness()   # 타겟별 최신성(fresh/stale/missing/always) — 프론트 배지·자동 체크해제용
            return self._json({"targets": [{"kind": k, "label": v["label"], "dep": v["dep"], "fresh": fr.get(k)}
                                            for k, v in TARGETS().items()]})
        if self.path == "/api/builds":
            p = DIST / "cuvia-map-bundle.tgz"
            cur = {"exists": p.is_file(), "size": (p.stat().st_size if p.is_file() else 0)}
            return self._json({"builds": load_builds(), "current_bundle": cur, "dist": str(DIST)})
        if self.path == "/api/profiles":
            return self._json({"profiles": list_manifests()})
        if self.path.startswith("/api/profiles/bundle"):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            bp = MANIFESTS_DIR / (q.get("id") or [""])[0] / "cuvia-map-bundle.tgz"
            if not bp.is_file():
                return self.send_error(404)
            self.send_response(200); self.send_header("Content-Type", "application/gzip")
            self.send_header("Content-Disposition", f'attachment; filename="{(q.get("id") or ["p"])[0]}-bundle.tgz"')
            self.send_header("Content-Length", str(bp.stat().st_size)); self.end_headers()
            with open(bp, "rb") as f:
                while True:
                    ch = f.read(1 << 20)
                    if not ch: break
                    try: self.wfile.write(ch)
                    except (BrokenPipeError, ConnectionResetError): break
            return
        if self.path == "/api/sources":
            ver = load_versions()
            out = []
            for s in load_sources():
                v = ver.get(s["key"], {}); cur = v.get("current"); lat = v.get("latest")
                status = _cmp_period(cur, lat)
                out.append({**s, "current": cur, "latest": lat, "auto": bool(s.get("latest_check")),
                            "uploadable": bool(s.get("build_input")),
                            "checked_at": v.get("checked_at"), "status": status,
                            "file": v.get("file"), "history": v.get("history", []),
                            "validation": v.get("validation_status"), "validation_msg": v.get("validation_msg"),
                            "validated_at": v.get("validated_at")})
            return self._json({"sources": out, "build_home": str(BUILD_HOME)})
        if self.path.startswith("/api/sources/files"):   # 업로드 파일별 현황(건물DB=시도 그룹핑·최신화)
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            data = scan_uploaded_files((q.get("key") or [""])[0])
            if data is None:
                return self._json({"error": "알 수 없는 key"}, 400)
            return self._json(data)
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
        if self.path == "/api/collect/catalog":
            return self._json({"items": collect_catalog(), "build_home": str(BUILD_HOME)})
        if self.path.startswith("/api/collect/download"):   # 항목별 staged 를 zip 스트리밍(내 PC로)
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            key = (q.get("key") or [""])[0]; dest = _item_dest(key)
            if not dest or not dest.exists():
                return self.send_error(404)
            import zipfile as _zf
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Disposition", f'attachment; filename="{key.replace(":", "_") or "item"}.zip"')
            self.send_header("Connection", "close"); self.end_headers(); self.close_connection = True
            try:
                z = _zf.ZipFile(self.wfile, "w", _zf.ZIP_STORED, allowZip64=True)
                if dest.is_dir():
                    for f in sorted(dest.rglob("*")):
                        if f.is_file(): z.write(f, str(f.relative_to(dest)))
                else:
                    z.write(dest, dest.name)
                z.close()
            except (BrokenPipeError, ConnectionResetError):
                pass
            return
        self.send_error(404)

    def do_POST(self):
        if self.path == "/api/build":
            n = int(self.headers.get("Content-Length", "0"))
            if n > MAX_CTRL: return self._json({"error": "본문 과대"}, 413)
            body = json.loads(self.rfile.read(n) or "{}")
            targets = body.get("targets", [])
            MGR.last_targets = list(body.get("targets", []))   # 정제 체크 기록(매니페스트)
            prep = prepare_sources()   # 업로드 데이터 적재(미업로드는 직전 재사용)
            res = MGR.enqueue(targets)   # {queued, fresh}
            return self._json({"queued": res["queued"], "fresh": res["fresh"], "prepared": prep})
        if self.path == "/api/build/check":   # 빌드 전 사전점검 — 선택 타겟이 필요로 하는 소스 누락/검증실패 목록
            n = int(self.headers.get("Content-Length", "0"))
            if n > MAX_CTRL: return self._json({"error": "본문 과대"}, 413)
            body = json.loads(self.rfile.read(n) or "{}")
            targets = body.get("targets", [])
            ver = load_versions(); names = {s["key"]: s["name"] for s in load_sources()}
            miss = []; inval = []
            for k in sorted(required_sources(targets)):
                present, val = source_presence(k, ver)
                if not present:
                    miss.append({"key": k, "name": names.get(k, k)})
                elif val in ("warn", "fail"):
                    inval.append({"key": k, "name": names.get(k, k), "validation": val})
            return self._json({"missing": miss, "invalid": inval})
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
        if self.path == "/api/collect/start":
            n = int(self.headers.get("Content-Length", "0"))
            if n > MAX_CTRL: return self._json({"error": "본문 과대"}, 413)
            sel = (json.loads(self.rfile.read(n) or "{}")).get("selected", [])
            if not sel: return self._json({"error": "수집할 항목을 선택하세요"}, 400)
            if _COLLECT_LOCK.locked(): return self._json({"error": "이미 수집 진행 중"}, 409)
            threading.Thread(target=run_collect, args=(sel,), daemon=True).start()
            return self._json({"ok": True, "started": sel})
        if self.path.startswith("/api/collect/upload"):   # 항목별 사용자 정의 파일 업로드 → staged 직접 적재
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            key = (q.get("key") or [""])[0]; name = _safe_relpath((q.get("name") or [""])[0])
            dest = _item_dest(key)
            if not dest: return self._json({"error": "알 수 없는 항목"}, 400)
            if not name: return self._json({"error": "파일명 누락"}, 400)
            tmpdir = BUILD_HOME / "tmp"; tmpdir.mkdir(parents=True, exist_ok=True)
            tmp = tmpdir / ("up_" + key.replace(":", "_") + "_" + name.replace("/", "_"))
            nbytes = int(self.headers.get("Content-Length", "0")); written = 0
            try:
                with open(tmp, "wb") as o:
                    rem = nbytes
                    while rem > 0:
                        ch = self.rfile.read(min(1 << 20, rem))
                        if not ch: break
                        o.write(ch); rem -= len(ch); written += len(ch)
                STORE_DIR.mkdir(parents=True, exist_ok=True)
                sha, _ = store_put(tmp)
                shutil.rmtree(dest, ignore_errors=True); _extract_into(store_path(sha), dest)
                ver = load_versions(); rec = ver.setdefault(key, {})
                rec["staged_sig"] = sha; rec["current"] = time.strftime("%Y-%m-%d"); rec["file"] = name
                save_versions({key: rec})
            except Exception as e:
                return self._json({"error": f"적재 실패: {str(e)[:120]}"}, 500)
            return self._json({"ok": True, "key": key, "size": written, "sha": sha[:8]})
        if self.path == "/api/collect/check":   # 항목별(하위 포함) 최신일 조회
            n = int(self.headers.get("Content-Length", "0"))
            if n > MAX_CTRL: return self._json({"error": "본문 과대"}, 413)
            key = (json.loads(self.rfile.read(n) or "{}")).get("key")
            if not key: return self._json({"error": "key 필요"}, 400)
            try:
                latest = _collect_latest(key)
            except Exception as e:
                return self._json({"error": f"조회 실패: {str(e)[:100]}"}, 502)
            if not latest: return self._json({"error": "최신일 못 찾음(패턴/차단)"}, 404)
            ver = load_versions(); rec = ver.setdefault(key, {})
            rec["latest"] = latest; rec["checked_at"] = time.strftime("%Y-%m-%d %H:%M")
            save_versions({key: rec})
            return self._json({"ok": True, "key": key, "latest": latest})
        if self.path in ("/api/profiles/save", "/api/profiles/load", "/api/profiles/rename", "/api/profiles/delete"):
            n = int(self.headers.get("Content-Length", "0"))
            if n > MAX_CTRL: return self._json({"error": "본문 과대"}, 413)
            b = json.loads(self.rfile.read(n) or "{}")
            try:
                if self.path.endswith("/save"): return self._json({"ok": True, "manifest": save_manifest((b.get("name") or "").strip() or None)})
                if self.path.endswith("/load"): return self._json({"ok": True, **load_manifest(b.get("id"))})
                if self.path.endswith("/rename"): return self._json({"ok": True, "manifest": rename_manifest(b.get("id"), (b.get("name") or "").strip())})
                if self.path.endswith("/delete"): gc = delete_manifest(b.get("id")); return self._json({"ok": True, "gc_removed": gc[0], "gc_freed": gc[1]})
            except Exception as e:
                return self._json({"error": str(e)[:140]}, 400)
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
 .wrap{display:grid;grid-template-columns:480px 1fr;gap:16px;padding:18px;max-width:1560px;margin:0 auto}
 @media(max-width:1100px){.wrap{grid-template-columns:1fr}}
 .panel{background:var(--card);border:1px solid var(--bd);border-radius:12px;padding:14px 16px}
 .panel h2{font-size:13px;margin:0 0 10px;font-weight:600;color:var(--mut);text-transform:uppercase;letter-spacing:.04em}
 label.row{display:flex;align-items:center;gap:8px;padding:5px 0;cursor:pointer}
 .bar{height:7px;background:#0c1018;border-radius:99px;overflow:hidden;margin-top:6px}
 .bar>i{display:block;height:100%;width:0;background:var(--ac);transition:width .3s}
 .tcard{padding:11px 13px;border:1px solid var(--bd);border-radius:9px;margin-bottom:9px}
 .tcard .h{display:flex;justify-content:space-between;align-items:center;font-size:13px}
 .st{font-size:11px;padding:2px 8px;border-radius:99px;background:#0c1018;color:var(--mut)}
 .st.running{color:#7fd1ff} .st.done{color:#7ee0a0} .st.error{color:#ff8585} .st.queued{color:#d9c07a} .st.skipped{color:#9aa3ad} .st.fresh{color:#7ee0a0}
 .tb{display:inline-block;white-space:nowrap;font-size:10px;border-radius:6px;padding:1px 6px;margin-left:4px}
 .tb.fresh{color:#7ee0a0;background:#0e1a12} .tb.stale{color:#d9c07a;background:#1a160c} .tb.miss{color:#ff8585;background:#1a0e0e} .tb.always{color:#9aa3ad;background:#0c1018}
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
 .node{padding:9px 2px;border-bottom:1px solid var(--bd)}
 .nrow{display:flex;align-items:center;gap:7px}
 .nl{display:flex;align-items:center;gap:6px;flex:1;min-width:0} .nl b{font-weight:500}
 .nr{display:flex;align-items:center;gap:8px;flex-shrink:0}
 .nver{font-size:11px;color:var(--mut);margin:3px 0 0 42px} .nver b{color:var(--tx);font-weight:500} .nver a{color:var(--ac);cursor:pointer}
 .kids{margin:5px 0 0 24px;border-left:1px solid #1c2438}
 .bfwrap{margin:6px 0 0 24px}
 .bftoggle{font-size:11px;color:var(--mut);cursor:pointer;user-select:none}
 .bfiles{margin:4px 0 0;border-left:1px solid #1c2438;padding-left:8px}
 .bfhead{font-size:11px;margin:0 0 4px}
 .rgrp{margin:0 0 6px} .rgrp .rh{display:flex;justify-content:space-between;font-size:11px;padding:2px 0;gap:8px}
 .ftab{width:100%;border-collapse:collapse;font-size:11px;margin:2px 0 0}
 .ftab th{text-align:left;color:var(--mut);font-weight:500;padding:2px 6px;border-bottom:1px solid var(--bd);white-space:nowrap}
 .ftab td{padding:2px 6px;border-bottom:1px solid #131a28;overflow-wrap:anywhere;vertical-align:top}
 .ftab th.r,.ftab td.r{text-align:right;white-space:nowrap}
 .krow{display:flex;align-items:center;justify-content:space-between;padding:4px 0 4px 12px;font-size:12px}
 .krow label{display:flex;align-items:center;gap:6px;cursor:pointer;min-width:0}
 .kr{display:flex;align-items:center;gap:8px} .kr .mut{font-size:10px}
 .pdetail{margin:6px 2px 2px 18px;padding:9px 11px;background:#0c1018;border:1px solid var(--bd);border-radius:7px;font-size:11px;line-height:1.6}
 .pdetail a{color:var(--ac)} .pdsec{margin-bottom:7px}
 .pdh{color:var(--mut);font-size:10px;text-transform:uppercase;letter-spacing:.04em;margin-bottom:3px}
 .pdrow{display:flex;justify-content:space-between;gap:12px;padding:1px 0} .pdrow>span:first-child{color:var(--tx)}
 .acts{display:flex;gap:8px} .ic{color:var(--mut);cursor:pointer;text-decoration:none;font-size:13px} .ic:hover{color:var(--ac)}
 .caret{display:inline-block;width:12px;color:var(--mut);cursor:pointer;font-size:13px;text-align:center}
 .mbadge{font-size:10px;padding:1px 7px;border-radius:6px;background:#0c1018;color:var(--mut)}
 .mbadge[data-m="자동"]{color:#7fd1ff} .mbadge[data-m="선택"]{color:#7ee0a0}
 .info{font-size:10px} .dash{color:var(--bd);width:13px;display:inline-block;text-align:center}
</style>
<header><div style="width:9px;height:9px;border-radius:99px;background:var(--ac)"></div>
 <div><h1>CUVIA Build Studio</h1><div class=sub id=bh>지도데이터 빌드 콘솔</div></div>
 <a href="/style" target="_blank" rel="noopener" style="margin-left:auto;color:var(--ac);text-decoration:none;border:1px solid var(--bd);border-radius:8px;padding:7px 13px;font-size:13px">🎨 스타일 디자인 →</a></header>
<div class=wrap>
 <div>
  <div class=panel><h2>자동 수집 목록</h2>
   <div class=sub style="font-size:12px;color:var(--mut);margin-bottom:10px;line-height:1.6">✔ 체크 = 다음 수집 대상 · ☐ 미체크 = 직전 수집분 재사용. 자동(다운로드)·선택(생활편의)·수동(업로드) 혼합. 항목별 업로드·드롭도 가능.</div>
   <div id=collect class=ds></div>
   <div style="display:flex;gap:8px;margin-top:12px;flex-wrap:wrap">
     <button id=collectBtn>⬇ 자동수집 시작 (순차)</button>
     <button class=ghost id=dlSelBtn>⬇ 다운로드(내 PC로)</button></div></div>
  <div class=panel style="margin-top:14px"><h2>빌드 프로필 · 이력</h2>
   <div id=builds class=ds></div></div>
 </div>
 <div>
  <div class=panel><h2>빌드 대상 <span class=mut style="font-weight:400;font-size:12px">— 변경된 것만 자동 선택 · 최신은 건너뜀</span></h2>
   <div id=checks></div>
   <div style="display:flex;gap:8px;margin-top:12px">
     <button id=run>빌드 시작</button>
     <button class=ghost id=forceAll title="모든 타겟 체크(최신 무시하고 전체 재빌드)">강제 재빌드(전체)</button></div></div>
  <div class=panel style="margin-top:14px"><h2>빌드 진행률</h2><div id=cards></div>
   <h2 style="margin-top:14px">실시간 로그</h2><pre id=log></pre></div>
 </div>
</div>
<script>
const $=s=>document.querySelector(s), cards={}, bars={}, sts={};
let TARGETS=[];
function tbadge(f){return f==='fresh'?'<span class="tb fresh">↻ 최신</span>'
  :f==='stale'?'<span class="tb stale">⟳ 변경됨</span>'
  :f==='missing'?'<span class="tb miss">⊘ 산출물 없음</span>'
  :f==='always'?'<span class="tb always">항상</span>':'';}
// 최신(fresh)인 타겟은 기본 체크해제 — 그대로 두면 빌드 시 건너뜀. 다시 체크하면 강제 재빌드.
function loadTargets(){return fetch('/api/targets').then(r=>r.json()).then(d=>{
  TARGETS=d.targets.filter(t=>t.kind[0]!=='_');
  $('#checks').innerHTML=TARGETS.map(t=>`<label class=row><input type=checkbox value="${t.kind}" ${t.fresh==='fresh'?'':'checked'}> ${t.label} ${tbadge(t.fresh)}${t.dep?`<span class=chip>← ${t.dep}</span>`:''}</label>`).join('');
});}
let _tt; function refreshTargetsSoon(){clearTimeout(_tt);_tt=setTimeout(loadTargets,800);}   // 빌드 종료 후 배지·체크 갱신(디바운스)
loadTargets();
function card(kind,label){if(cards[kind])return;const el=document.createElement('div');el.className='tcard';
 el.innerHTML=`<div class=h><span>${label||kind}</span><span class=st id=st_${kind}>대기</span></div><div class=bar><i id=bar_${kind}></i></div>`;
 $('#cards').appendChild(el);cards[kind]=el;bars[kind]=$('#bar_'+kind);sts[kind]=$('#st_'+kind);}
const LBL={};
function lbl(k){const t=TARGETS.find(x=>x.kind===k);return t?t.label:k}
function setStatus(k,s,p){card(k,lbl(k));if(p!=null)bars[k].style.width=Math.round(p*100)+'%';
 if(s){const m={queued:'대기',running:'진행중',done:'완료',error:'오류',skipped:'건너뜀',fresh:'↻ 최신(재사용)'};sts[k].textContent=m[s]||s;sts[k].className='st '+s;}}
function logln(t){const p=$('#log');p.textContent+=t+'\n';p.scrollTop=p.scrollHeight}
const es=new EventSource('/api/events');
es.onmessage=e=>{const d=JSON.parse(e.data);
 if(d.snapshot){for(const k in d.snapshot)setStatus(k,d.snapshot[k].status,d.snapshot[k].progress);return}
 setStatus(d.kind,d.status,d.progress); if(d.line)logln('['+d.kind+'] '+d.line);
 if(d.status==='done'||d.status==='error'||d.status==='skipped'){loadBuilds();if(d.kind==='collect')loadCollect();else refreshTargetsSoon();}};
function fmt(d){const x=String(d||'').replace(/\D/g,'');return x.length===8?`${x.slice(0,4)}-${x.slice(4,6)}-${x.slice(6,8)}`:x.length===6?`${x.slice(0,4)}-${x.slice(4,6)}`:(d||'−');}
function srcStatus(s){return s.status==='update'?'🔴 업데이트 있음':(s.status==='current'?'🟢 최신':'—');}
function vbadge(s){if(!s.uploadable)return '';
  const m={ok:'🟢 검증 OK',warn:'🟡 검증 경고',fail:'🔴 검증 실패',pending:'⏳ 검증 대기'},v=s.validation;
  const lab=v?(m[v]||v):(s.file?'⏳ 검증 대기':'<span class=mut>미업로드</span>');
  const btn=s.file?` <a onclick="validateSource('${s.key}')">[${v&&v!=='pending'?'재검증':'검증'}]</a>`:'';
  return `<div class=src-meta>${lab}${s.validation_msg?` · <span class=mut>${s.validation_msg}</span>`:''}${btn}</div>`;}
let SRC=[], COL=[];
function modeChip(it){const m=it.collectable?(it.method==='localdata_facility'?'선택':'자동'):'수동';return `<span class=mbadge data-m="${m}">${m}</span>`;}
function acts(it){let h='';
  if(it.url)h+=`<a href="${it.url}" target=_blank rel=noopener title=출처 class=ic>↗</a>`;
  if(it.collectable||it.current||it.key.includes(':'))h+=`<a onclick="dlItem('${it.key}')" title="개별 다운로드(내 PC)" class=ic>⬇</a>`;
  if(it.uploadable||it.key.includes(':'))h+=`<a onclick="upItem('${it.key}')" title="개별 업로드" class=ic>⬆</a>`;
  return `<span class=acts>${h}</span>`;}
function kidRow(c,pk){return `<div class=krow><label><input type=checkbox class=ck value="${c.key}" onchange="onKid('${pk}')" ${c.default_collect?'checked':''}> ${c.name}</label><span class=kr><span class=mut>수집 ${c.current?fmt(c.current):'—'} · 최신 ${fmt(c.latest)} <a onclick="checkItem('${c.key}',this)">조회</a></span>${acts(c)}</span></div>`;}
function renderNode(it){const kids=it.children||[],hasKids=kids.length>0;
  let cb;
  if(it.method==='localdata_facility')cb=`<input type=checkbox class=pk id=pk_${it.key} onchange="toggleAll('${it.key}',this.checked)">`;
  else if(it.collectable)cb=`<input type=checkbox class="ck${hasKids?' pk':''}" id=pk_${it.key} value="${it.key}" ${hasKids?`onchange="toggleAll('${it.key}',this.checked)"`:''} ${it.default_collect?'checked':''}>`;
  else cb='<span class=dash>—</span>';
  const car=hasKids?`<a onclick="toggleKids('${it.key}')" class=caret id=cr_${it.key}>›</a>`:'<span class=caret></span>';
  const info=hasKids?(it.method==='localdata_facility'?`${kids.filter(k=>k.default_collect).length}/${kids.length} · facility`:`${kids.length} 시·도`):'';
  return `<div class=node ${it.uploadable?`data-key="${it.key}"`:''}>
   <div class=nrow><span class=nl>${cb}${car}<b>${it.name}</b></span><span class=nr>${modeChip(it)}${info?`<span class="mut info">${info}</span>`:''}${acts(it)}</span></div>
   <div class=nver>현재 <b>${fmt(it.current)}</b> <a onclick="setVer('${it.key}','current')">✎</a> · 최신 <b>${fmt(it.latest)}</b> ${it.auto?`<a onclick="checkLatest('${it.key}',this)">조회</a>`:''} · ${srcStatus(it)}${it.file?` · <span class=mut>📄${it.file}</span>`:''}</div>
   ${it.uploadable?`<div class=bar id=ub_${it.key} style="display:none"><i id=ubi_${it.key}></i></div>`:''}
   ${it.uploadable&&!hasKids?`<div class=bfwrap><span class=bftoggle id=bft_${it.key} onclick="toggleBF('${it.key}')">▸ 파일 현황</span><div id=bf_${it.key} class=bfiles style="display:none"></div></div>`:''}
   ${hasKids?`<div id=kids_${it.key} class=kids style="display:none">${kids.map(k=>kidRow(k,it.key)).join('')}</div>`:''}</div>`;}
function bf_bytes(n){return n>=1073741824?gb(n):n>=1048576?(n/1048576).toFixed(1)+'MB':(n/1024).toFixed(0)+'KB';}
function bfBadge(asof,latest){if(!asof)return '<span class=mut>기준일 미상</span>';if(!latest)return '<span class=mut>—</span>';
  const a=String(asof).replace(/\D/g,''),b=String(latest).replace(/\D/g,''),k=Math.min(a.length,b.length);
  if(!k)return '<span class=mut>—</span>';return b.slice(0,k)>a.slice(0,k)?'🔴 구버전':'🟢 최신';}
function toggleBF(key){const e=document.getElementById('bf_'+key),t=document.getElementById('bft_'+key);
  const o=e.style.display==='none';e.style.display=o?'':'none';if(t)t.textContent=(o?'▾':'▸')+' 파일 현황';
  if(o&&!e.dataset.loaded){e.dataset.loaded='1';loadBF(key);}}
function loadBF(key){const e=document.getElementById('bf_'+key);e.innerHTML='<span class=mut>불러오는 중…</span>';
  fetch('/api/sources/files?key='+encodeURIComponent(key)).then(r=>r.json()).then(d=>{
    if(d.error){e.innerHTML='<span class=mut>'+d.error+'</span>';return;}
    if(!d.total){e.innerHTML='<span class=mut>업로드된 파일 없음 — 폴더/파일을 끌어다 놓거나 ⬆ 로 올리세요</span>';return;}
    const ftab=fs=>`<table class=ftab><thead><tr><th>파일</th><th>기준일</th><th class=r>크기</th><th class=r>업로드</th><th>상태</th></tr></thead><tbody>${
      fs.map(f=>`<tr><td>📄 ${f.file}</td><td class=mut>${fmt(f.asof)}</td><td class="mut r">${bf_bytes(f.size)}</td><td class="mut r">${(f.uploaded_at||'').slice(0,16)}</td><td>${bfBadge(f.asof,d.latest)}</td></tr>`).join('')}</tbody></table>`;
    const head=`<div class="mut bfhead">총 ${d.total}개 · 출처 갱신일 ${fmt(d.latest)}</div>`;
    const body=d.grouped?(d.regions||[]).map(rg=>`<div class=rgrp><div class=rh><b>${rg.name||'미분류'} <span class=mut>(${rg.n})</span></b><span class=mut>${fmt(rg.asof)} · ${bfBadge(rg.asof,d.latest)}</span></div>${ftab(rg.files)}</div>`).join('')
      :ftab((d.regions[0]||{files:[]}).files);
    e.innerHTML=head+body;
  }).catch(err=>{e.innerHTML='<span class=mut>실패: '+err+'</span>';});}
function toggleKids(key){const e=document.getElementById('kids_'+key),c=document.getElementById('cr_'+key);if(e){const o=e.style.display==='none';e.style.display=o?'':'none';if(c)c.textContent=o?'⌄':'›';}}
function toggleAll(key,on){document.querySelectorAll('#kids_'+key+' input.ck').forEach(c=>c.checked=on);syncParent(key);}
function onKid(key){syncParent(key);}
function syncParent(key){const p=document.getElementById('pk_'+key);if(!p)return;const ks=[...document.querySelectorAll('#kids_'+key+' input.ck')];if(!ks.length)return;const on=ks.filter(c=>c.checked).length;p.checked=on===ks.length;p.indeterminate=on>0&&on<ks.length;}
function checkItem(key,a){if(a)a.textContent='…';fetch('/api/collect/check',{method:'POST',body:JSON.stringify({key})}).then(r=>r.json()).then(d=>{if(d.error)alert('최신 조회 실패: '+d.error);loadCollect();}).catch(e=>{alert('실패: '+e);loadCollect();});}
function dlItem(key){window.open('/api/collect/download?key='+encodeURIComponent(key),'_blank');}
function upItem(key){const inp=document.createElement('input');inp.type='file';inp.multiple=true;inp.onchange=()=>{const fs=[...inp.files];if(!fs.length)return;key.includes(':')?upSub(key,fs):uploadAll(key,fs);};inp.click();}
async function upSub(key,files){logln('⇧ '+key+' 업로드 '+files.length+'개…');
  for(const f of files){await new Promise(res=>{const x=new XMLHttpRequest();
    x.open('POST','/api/collect/upload?key='+encodeURIComponent(key)+'&name='+encodeURIComponent(f.name));
    x.onload=()=>{try{const d=JSON.parse(x.responseText);logln(d.error?'  ✗ '+d.error:'  ✓ '+f.name+' (sha '+d.sha+')');}catch(_){logln('  ✗ 응답오류 '+x.status);}res();};
    x.onerror=()=>{logln('  ✗ 네트워크');res();};x.send(f);});}
  loadCollect();}
function loadCollect(){
  const prevChk={};document.querySelectorAll('#collect input.ck').forEach(c=>{if(c.value)prevChk[c.value]=c.checked;});  // 재렌더 전 체크상태 보존(업로드·검증 시 선택 초기화 방지)
  Promise.all([fetch('/api/collect/catalog').then(r=>r.json()),fetch('/api/sources').then(r=>r.json())]).then(([cd,sd])=>{
  if(cd.build_home)$('#bh').textContent='BUILD_HOME: '+cd.build_home;
  const sv=Object.fromEntries((sd.sources||[]).map(s=>[s.key,s]));
  COL=(cd.items||[]).map(it=>{const s=sv[it.key]||{};return Object.assign({},it,{url:s.url,status:s.status,latest:s.latest||it.latest,current:s.current||it.current,validation:s.validation,validation_msg:s.validation_msg,file:s.file,auto:s.auto||it.auto});});
  SRC=COL;
  $('#collect').innerHTML=COL.map(renderNode).join('');
  document.querySelectorAll('#collect input.ck').forEach(c=>{if(c.value in prevChk)c.checked=prevChk[c.value];});  // 직전 선택 복원(최초 렌더는 prevChk 비어 default 유지)
  COL.forEach(it=>{if((it.children||[]).length)syncParent(it.key);});  // 부모 체크/indeterminate 재계산
  bindDrops();});}
function startCollect(){let sel=[...document.querySelectorAll('#collect input.ck:checked')].map(x=>x.value).filter(Boolean);
  if(sel.includes('localdata'))sel=sel.filter(k=>!k.startsWith('localdata:'));
  if(!sel.length)return alert('수집할 항목을 체크하세요');
  logln('▶ 자동수집 '+sel.length+'건 순차 시작…');
  fetch('/api/collect/start',{method:'POST',body:JSON.stringify({selected:sel})}).then(r=>r.json()).then(d=>{if(d.error)return alert(d.error);logln('  큐: '+(d.started||[]).join(', '));});}
function setVer(key,field){const v=prompt((field==='current'?'현재(빌드에 쓴)':'최신')+' 기준일 입력 — 예: 202605 또는 2026-06-19');
  if(v==null)return; fetch('/api/sources/version',{method:'POST',body:JSON.stringify({key,field,value:v})})
   .then(r=>r.json()).then(d=>{if(d.error)alert(d.error);loadCollect();});}
function checkLatest(key,a){if(a)a.textContent='조회중…';
  fetch('/api/sources/check',{method:'POST',body:JSON.stringify({key})}).then(r=>r.json()).then(d=>{
   if(d.error)alert('최신 조회 실패: '+d.error); loadCollect();}).catch(e=>{alert('실패: '+e);loadCollect();});}
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
  setTimeout(()=>{const b=$('#ub_'+key);if(b)b.style.display='none';},900);loadCollect();}
function validateSource(key,quiet){return fetch('/api/sources/validate',{method:'POST',body:JSON.stringify({key})})
  .then(r=>r.json()).then(d=>{if(d.error){logln('✗ '+key+' 검증 실패: '+d.error);return;}
    const ic={ok:'🟢',warn:'🟡',fail:'🔴'}[d.status]||'•';
    logln(ic+' '+key+' 검증: '+d.status+(d.msg?' — '+d.msg:''));if(!quiet)loadCollect();})
  .catch(e=>logln('✗ '+key+' 검증 오류: '+e));}
function bindDrops(){document.querySelectorAll('.node[data-key]').forEach(el=>{const key=el.getAttribute('data-key');
  ['dragenter','dragover'].forEach(ev=>el.addEventListener(ev,e=>{e.preventDefault();e.stopPropagation();el.style.borderColor='var(--ac)';}));
  ['dragleave','dragend','drop'].forEach(ev=>el.addEventListener(ev,e=>{e.preventDefault();e.stopPropagation();el.style.borderColor='';}));
  el.addEventListener('drop',e=>gatherFiles(e.dataTransfer).then(fs=>uploadAll(key,fs)));});}
function gb(n){return (n/1073741824).toFixed(2)+'GB';}
let CUR_BUNDLE={},PROF=[],PROF_PG=0,PROF_NQ='',PROF_FROM='',PROF_TO='';
function loadBuilds(){Promise.all([fetch('/api/builds').then(r=>r.json()),fetch('/api/profiles').then(r=>r.json())]).then(([d,pd])=>{CUR_BUNDLE=d;PROF=pd.profiles||[];buildProfPanel();});}
function buildProfPanel(){const c=CUR_BUNDLE.current_bundle||{};
  const dl=c.exists?`<a href="/api/download/bundle" style="color:var(--ac)">⬇ 현재 번들 (${gb(c.size)})</a>`:'<i class=mut>번들 없음 — 빌드/패키징을 먼저</i>';
  const hist=(CUR_BUNDLE.builds||[]).map(b=>`<div style="font-size:11px;padding:3px 0;color:var(--mut)"><b style="color:var(--tx)">${b.version}</b> · ${(b.built_at||'').replace('T',' ').slice(0,16)} · ${gb(b.bundle_bytes||0)}${b.qc?' · QC '+b.qc:''}</div>`).join('');
  $('#builds').innerHTML=`<div style="margin-bottom:8px">${dl}</div>
   <div style="display:flex;align-items:center;gap:6px;margin:8px 0 2px;flex-wrap:wrap"><input id=profQ placeholder="이름 검색…" style="flex:1;min-width:130px;background:#0c1018;border:1px solid var(--bd);border-radius:6px;color:var(--tx);padding:5px 8px;font-size:12px"><span id=profCnt class=mut style="font-size:11px"></span></div>
   <div style="display:flex;align-items:center;gap:5px;margin:0 0 6px;font-size:11px;color:var(--mut);flex-wrap:wrap"><span>📅 빌드일</span><input type=date id=profFrom style="background:#0c1018;border:1px solid var(--bd);border-radius:6px;color:var(--tx);padding:3px 6px;font-size:11px;color-scheme:dark"><span>~</span><input type=date id=profTo style="background:#0c1018;border:1px solid var(--bd);border-radius:6px;color:var(--tx);padding:3px 6px;font-size:11px;color-scheme:dark"><a onclick="clearProfDate()" class=ic title="날짜 초기화">✕</a></div>
   <div id=profRows></div><div id=profPager></div>
   ${hist?`<div class=mut style="font-size:11px;margin:10px 0 2px">패키징 이력</div>${hist}`:''}`;
  const qi=$('#profQ');qi.value=PROF_NQ;qi.oninput=e=>{PROF_NQ=e.target.value;PROF_PG=0;renderProfRows();};
  const ff=$('#profFrom'),ft=$('#profTo');ff.value=PROF_FROM;ft.value=PROF_TO;
  ff.onchange=e=>{PROF_FROM=e.target.value;PROF_PG=0;renderProfRows();};ft.onchange=e=>{PROF_TO=e.target.value;PROF_PG=0;renderProfRows();};
  renderProfRows();}
function profMatch(p){const nq=PROF_NQ.trim().toLowerCase();
  if(nq && !(p.name||'').toLowerCase().includes(nq) && !(p.git||'').toLowerCase().includes(nq)) return false;
  const c=(p.created||'').slice(0,10);   // YYYY-MM-DD 빌드 생성일
  if(PROF_FROM && c<PROF_FROM) return false;
  if(PROF_TO && c>PROF_TO) return false;
  return true;}
function renderProfRows(){const PS=5,active=!!(PROF_NQ.trim()||PROF_FROM||PROF_TO);
  const filt=PROF.filter(profMatch);
  const pages=Math.max(1,Math.ceil(filt.length/PS));PROF_PG=Math.max(0,Math.min(PROF_PG,pages-1));
  const page=filt.slice(PROF_PG*PS,PROF_PG*PS+PS);
  if($('#profCnt'))$('#profCnt').textContent=filt.length+'개';
  $('#profRows').innerHTML=page.map(p=>`<div class=node style="padding:7px 2px" id=pf_${p.id}>
     <div class=nrow><span class=nl><a onclick="toggleDetail('${p.id}')" class=caret id=pc_${p.id}>›</a><b class=pname onclick="toggleDetail('${p.id}')" style="cursor:pointer">${p.name}</b></span><span class=nr>
       <a onclick="editProfile('${p.id}')" class=ic title=이름수정>✎</a>
       <a onclick="loadProfile('${p.id}')" class=ic title="불러오기 (이 파일집합으로 복원)">📂</a>
       ${p.has_bundle?`<a href="/api/profiles/bundle?id=${p.id}" class=ic title="번들 다운로드">⬇</a>`:''}
       <a onclick="delProfile('${p.id}')" class=ic title=삭제>✕</a></span></div>
     <div class=nver>${p.created}${p.git?' · '+p.git:''} · 소스 ${Object.keys(p.sources||{}).length}${p.bundle_bytes?' · 번들 '+gb(p.bundle_bytes):''}</div>
     <div id=pd_${p.id} class=pdetail style="display:none">${profDetailHtml(p)}</div></div>`).join('')||`<i class=mut>${active?'검색/필터 결과 없음':'저장된 프로필 없음 — 빌드(패키징) 완료 시 자동저장'}</i>`;
  const cur=PROF_PG+1,nums=pageList(cur,pages).map(n=>n==='…'?'<span class=mut>…</span>':`<a onclick="profGoto(${n})" style="cursor:pointer;padding:1px 6px;border-radius:5px;${n===cur?'color:#06121f;background:var(--ac);font-weight:600':'color:var(--mut)'}">${n}</a>`).join(' ');
  $('#profPager').innerHTML=pages>1?`<div style="display:flex;align-items:center;justify-content:center;gap:6px;margin-top:8px;font-size:12px;flex-wrap:wrap">${cur>1?'<a onclick="profPage(-1)" class=ic>‹ 이전</a>':'<span class=mut style="opacity:.35">‹ 이전</span>'} ${nums} ${cur<pages?'<a onclick="profPage(1)" class=ic>다음 ›</a>':'<span class=mut style="opacity:.35">다음 ›</span>'}</div>`:'';}
function pageList(cur,total){const win=2,set=new Set([1,total]);for(let i=cur-win;i<=cur+win;i++)if(i>=1&&i<=total)set.add(i);const arr=[...set].filter(n=>n>=1&&n<=total).sort((a,b)=>a-b),out=[];let prev=0;for(const n of arr){if(n-prev>1)out.push('…');out.push(n);prev=n;}return out;}
function profPage(d){PROF_PG+=d;renderProfRows();}
function profGoto(n){PROF_PG=n-1;renderProfRows();}
function clearProfDate(){PROF_FROM='';PROF_TO='';const ff=$('#profFrom'),ft=$('#profTo');if(ff)ff.value='';if(ft)ft.value='';PROF_PG=0;renderProfRows();}
function keyName(k){const i=k.indexOf(':'),sk=i<0?k:k.slice(0,i),sub=i<0?'':k.slice(i+1);const top=(COL||[]).find(c=>c.key===sk);if(!sub)return top?top.name:sk;const kid=((top&&top.children)||[]).find(c=>c.key===k);return (top?top.name:sk)+' · '+(kid?kid.name:sub);}
function profDetailHtml(p){
  const srcs=Object.entries(p.sources||{}).map(([k,s])=>`<div class=pdrow><span>${keyName(k)}</span><span class=mut>${s.current?fmt(s.current):'—'}${s.file?' · '+s.file:''}${s.sha?' · sha '+String(s.sha).slice(0,8):''}</span></div>`).join('')||'<span class=mut>기록된 소스 없음</span>';
  const tg=(p.targets||[]).length?(p.targets).map(t=>lbl(t)).join(', '):'<span class=mut>기록 없음 (수동 저장 프로필)</span>';
  const bundle=p.bundle_bytes?`보관됨 (${gb(p.bundle_bytes)}) · <a href="/api/profiles/bundle?id=${p.id}">다운로드</a> <span class=mut>· BUILD_HOME/manifests/${p.id}/cuvia-map-bundle.tgz</span>`:'<span class=mut>없음 (빌드/패키징 완료 시 보관)</span>';
  return `<div class=pdsec><div class=pdh>소스 파일 버전 (빌드에 쓴 것)</div>${srcs}</div>
   <div class=pdsec><div class=pdh>빌드 정제 체크</div><div>${tg}</div></div>
   <div class=pdsec><div class=pdh>번들</div><div>${bundle}</div></div>
   <div class=mut style="font-size:10px">id ${p.id}${p.qc?' · QC '+p.qc:''}</div>`;}
function toggleDetail(id){const e=document.getElementById('pd_'+id),c=document.getElementById('pc_'+id);if(e){const o=e.style.display==='none';e.style.display=o?'':'none';if(c)c.textContent=o?'⌄':'›';}}
function editProfile(id){const el=document.querySelector('#pf_'+id+' .pname');if(!el)return;const cur=el.textContent;
  el.innerHTML=`<input id=ed_${id} value="${cur.replace(/"/g,'&quot;')}" style="background:#0c1018;border:1px solid var(--ac);border-radius:5px;color:var(--tx);padding:2px 6px;font-size:13px;width:72%">`;
  const inp=document.getElementById('ed_'+id);inp.focus();inp.select();let done=false;
  const save=()=>{if(done)return;done=true;const n=inp.value.trim();if(!n||n===cur){renderProfRows();return;}
    fetch('/api/profiles/rename',{method:'POST',body:JSON.stringify({id,name:n})}).then(r=>r.json()).then(d=>{if(d.error)alert(d.error);loadBuilds();});};
  inp.onkeydown=e=>{if(e.key==='Enter'){e.preventDefault();save();}else if(e.key==='Escape'){done=true;renderProfRows();}};inp.onblur=save;}
function loadProfile(id){if(!confirm('이 프로필의 파일집합으로 복원할까요? (현재 staged 덮어씀)'))return;
  fetch('/api/profiles/load',{method:'POST',body:JSON.stringify({id})}).then(r=>r.json()).then(d=>{if(d.error)return alert(d.error);logln('↻ 프로필 불러옴: '+d.name+' (복원 '+d.restored+'건) — 갱신할 항목만 체크 후 빌드');loadCollect();});}
function delProfile(id){if(!confirm('프로필을 삭제할까요? (보관 번들 + 미참조 store 파일 정리)'))return;fetch('/api/profiles/delete',{method:'POST',body:JSON.stringify({id})}).then(r=>r.json()).then(d=>{if(d.gc_removed)logln('🗑 store 정리: '+d.gc_removed+'개 ('+gb(d.gc_freed||0)+' 회수)');loadBuilds();});}
loadCollect(); loadBuilds();
$('#collectBtn').onclick=startCollect; $('#dlSelBtn').onclick=()=>alert('선택 항목 내 PC 다운로드 — 다음 단계 연결 예정');
$('#forceAll').onclick=()=>{document.querySelectorAll('#checks input').forEach(c=>c.checked=true);logln('⟳ 전체 체크 — 최신 무시하고 강제 재빌드');};
function runBuild(t){
 const A={staged:'⬆ 새 데이터 적재',ok:'✓ 적재됨',reused:'↻ 직전 데이터 사용','missing':'⚠ 데이터 없음',partial:'⚠ 일부만 적재(오류)','no-tool':'⚠ 추출도구 없음',error:'✗ 오류'};
 fetch('/api/build',{method:'POST',body:JSON.stringify({targets:t})}).then(r=>r.json()).then(d=>{
   (d.prepared||[]).forEach(p=>logln('  📦 '+p.key+': '+(A[p.action]||p.action)+(p.n?' ('+p.n+'개)':'')+(p.msg?' — '+p.msg:'')));
   (d.fresh||[]).forEach(k=>logln('  ↻ '+lbl(k)+': 최신 — 건너뜀(재사용)'));
   logln('▶ 큐: '+((d.queued||[]).map(lbl).join(', ')||'없음 — 빌드할 변경 없음'));loadCollect();refreshTargetsSoon();});}
$('#run').onclick=()=>{const t=[...document.querySelectorAll('#checks input:checked')].map(x=>x.value);
 if(!t.length)return alert('빌드할 대상이 없습니다.\n(모두 최신이면, 다시 빌드할 항목을 체크하거나 [강제 재빌드(전체)]를 누르세요)');
 // 사전점검 — 필요한 소스 데이터 누락/검증실패면 경고 팝업(그래도 진행 가능)
 fetch('/api/build/check',{method:'POST',body:JSON.stringify({targets:t})}).then(r=>r.json()).then(c=>{
   const miss=c.missing||[],inval=c.invalid||[];
   if(miss.length||inval.length){
     let msg='';
     if(miss.length)msg+='⚠ 데이터가 없습니다:\n  · '+miss.map(m=>m.name).join('\n  · ')+'\n\n';
     if(inval.length)msg+='⚠ 검증 경고/실패:\n  · '+inval.map(m=>m.name+' ('+m.validation+')').join('\n  · ')+'\n\n';
     msg+='이대로 빌드를 진행할까요? (해당 데이터는 비거나 직전 분으로 빌드됩니다)';
     if(!confirm(msg)){logln('⏹ 빌드 취소 — 누락/오류: '+[...miss.map(m=>m.name),...inval.map(m=>m.name)].join(', '));return;}
     logln('⚠ 누락 무시하고 진행: '+[...miss.map(m=>m.name),...inval.map(m=>m.name)].join(', '));
   }
   runBuild(t);
 }).catch(e=>{if(confirm('소스 사전점검 실패('+e+').\n그래도 빌드를 진행할까요?'))runBuild(t);});};
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
