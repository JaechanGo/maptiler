#!/usr/bin/env python3
"""CUVIA Build Studio (MVP) — 무의존(파이썬 표준 라이브러리만) 빌드 콘솔.

원천데이터(SHP/CSV)를 기반으로 지오코드 인덱스·타일을 빌드하고, 종류별 진행률을 SSE로
실시간 표시하며, 체크박스로 빌드 대상을 정제하고, 완료물을 폐쇄망 번들로 패키징한다.
이미 검증된 CLI 파이프라인(09/10/11/12/13/package)을 그대로 빌드 엔진으로 구동한다.

기동:  python3 scripts/build-studio.py            # http://localhost:18081
       BUILD_HOME=~/geocode-build PORT=18081 python3 scripts/build-studio.py
"""
import json, os, pathlib, queue, subprocess, threading, time, re, ssl, sqlite3, shutil, zipfile, hashlib, urllib.request, urllib.parse
import errno  # _swap_dir 의 크로스 디바이스(EXDEV) 판별
import pty   # 자식 프로세스를 TTY 에 붙여 외부 도구의 블록버퍼링을 막고 로그를 실시간 스트리밍
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = pathlib.Path(__file__).resolve().parents[1]
BUILD_HOME = pathlib.Path(os.environ.get("BUILD_HOME", os.path.expanduser("~/geocode-build")))
PORT = int(os.environ.get("PORT", "18081"))
TILE_PORT = int(os.environ.get("TILE_PORT", "8080"))   # 미리보기가 스타일을 읽어올 tileserver 포트
STYLE_STUDIO_PORT = os.environ.get("STYLE_STUDIO_PORT", "18082")   # 스타일 편집기(style-studio) — /style 은 여기로 일원화(리다이렉트)
COMPOSE_FILE = os.environ.get("COMPOSE_FILE", str(BUILD_HOME / "deploy/docker-compose.yml"))
# 기본 외부(LAN) 노출(0.0.0.0). 인증이 없으므로 같은 LAN의 누구나 빌드 실행/업로드가 가능함을
# 운영자가 인지해야 함 — 신뢰망에서만 사용하고, 로컬 전용이 필요하면 HOST=127.0.0.1 로 명시.
HOST = os.environ.get("HOST", "0.0.0.0")   # 기본 외부(LAN) 노출 — 무인증 콘솔이므로 신뢰망에서만 사용. 로컬 전용은 HOST=127.0.0.1
MAX_CTRL = 256 * 1024                                           # 제어 API(JSON) 본문 상한
# 업로드는 raw 바디 스트리밍(청크→디스크)이라 용량 상한 없음 — 대용량 내비DB/건물DB 웹 업로드 지원.
DIST = BUILD_HOME / "dist"                       # package.sh 산출물(번들·images·builds.json)
ARTIFACTS = BUILD_HOME / "artifacts"             # 빌드 산출물 스냅샷 보존(geocode.sqlite — 재적재·재수집 teardown 으로부터 보호)
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
    # 건물 SHP zip — 시도별 zip 직접, 또는 17개 시도 zip 을 한 번 더 묶은 중첩 zip(zip-of-zips) 모두 허용(빌드 시 재귀추출).
    zips = [f for f in files if f.suffix.lower() == ".zip"]
    shps = [f for f in files if f.suffix.lower() == ".shp"]
    if zips:
        tot = nested = 0
        for z in zips:
            try:
                with zipfile.ZipFile(z) as zf:
                    names = zf.namelist()
                    tot += sum(1 for n in names if n.lower().endswith(".shp"))
                    nested += sum(1 for n in names if n.lower().endswith(".zip"))
            except Exception:
                return ("warn", f"zip {len(zips)}개 (일부 열기 실패)")
        if tot:    return ("ok", f"zip {len(zips)}개 · shp {tot}개")
        if nested: return ("ok", f"zip {len(zips)}개 · 중첩 zip {nested}개(시도별 묶음 — 빌드 시 재귀추출)")
        return ("warn", f"zip {len(zips)}개지만 .shp/중첩 zip 없음")
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


def _v_style(files):
    # Style Studio 내보내기 = 완성형 style.json(MapLibre v8). 가장 최근 .json 1개를 검증.
    js = [f for f in files if f.suffix.lower() == ".json"]
    if not js:
        return ("fail", "style.json(.json) 파일이 필요합니다")
    f = sorted(js, key=lambda p: p.stat().st_mtime)[-1]
    try:
        d = json.loads(f.read_text(encoding="utf-8"))
    except Exception as e:
        return ("fail", f"JSON 파싱 실패: {str(e)[:80]}")
    if not isinstance(d.get("layers"), list) or not d["layers"]:
        return ("fail", "style.json 형식 아님(layers 배열 없음)")
    extra = f" · {len(js)}개 중 최신({f.name})" if len(js) > 1 else ""
    return ("ok", f"layers {len(d['layers'])}개 · sources {len(d.get('sources') or {})}개{extra}")


_VALIDATORS = {"juso_navi": _v_navi, "sangga": _v_sangga, "localdata": _v_localdata, "building_db": _v_building,
               "boundary_legal": _v_boundary, "boundary_admin": _v_boundary, "boundary_ri": _v_boundary,
               "style": _v_style}


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
# VWorld dsId=18 은 시도 17개 단위 SHP. 실제 파일명 AL_D010_<시도코드2자리>_<YYYYMMDD>(전체분; 변동분=CH_D010).
# zip/내부 SHP/폴더명에서 시도코드·기준일 추출. (구포맷 F_FAC_BUILDING_<코드>_<YYYYMM> 도 호환.)
_SIDO_CANON = {"42": "51", "45": "52"}   # 구코드(강원42·전북45) → 특별자치도 신코드(한 시도=한 그룹)
_BUILDING_SIDO = {
    "11": "서울특별시", "26": "부산광역시", "27": "대구광역시", "28": "인천광역시",
    "29": "광주광역시", "30": "대전광역시", "31": "울산광역시", "36": "세종특별자치시",
    "41": "경기도", "51": "강원특별자치도", "43": "충청북도", "44": "충청남도",
    "52": "전북특별자치도", "46": "전라남도", "47": "경상북도", "48": "경상남도",
    "50": "제주특별자치도",
}
# 코드 없이 한글 시도명만 있을 때 폴백 — 풀네임(_BUILDING_SIDO.values)을 먼저 보고, 없으면 짧은 별칭.
_SIDO_ALIASES = [
    ("11", "서울"), ("26", "부산"), ("27", "대구"), ("28", "인천"), ("29", "광주"),
    ("30", "대전"), ("31", "울산"), ("36", "세종"), ("41", "경기"), ("51", "강원"),
    ("43", "충북"), ("43", "충청북"), ("44", "충남"), ("44", "충청남"),
    ("52", "전북"), ("52", "전라북"), ("46", "전남"), ("46", "전라남"),
    ("47", "경북"), ("47", "경상북"), ("48", "경남"), ("48", "경상남"), ("50", "제주"),
]
_BLD_PAT = re.compile(r"(?:AL_D010|CH_D010|F_FAC_BUILDING)_(\d{2})_(\d{6,8})", re.I)
# 파일 현황 표시에서 제외할 SHP 부속파일(데이터셋=.shp/.zip 만 노출) — 빌드/검증의 rglob 와 무관(표시 전용)
_SHP_SIDECAR = {".shx", ".dbf", ".prj", ".fix", ".cpg", ".qpj", ".sbn", ".sbx", ".qix", ".aih", ".ain", ".xml"}


def _valid_period(s):
    """기준일 추출 — 연도 19/20xx·월 01~12 검증(임의 6/8자리 일련번호 오탐 방지). YYYYMM 또는 YYYYMMDD."""
    m = re.search(r"(?<!\d)((?:19|20)\d{2})(0[1-9]|1[0-2])(\d{2})?(?!\d)", s or "")
    return (m.group(1) + m.group(2) + (m.group(3) or "")) if m else ""


def _sido_codes_from_text(blob):
    """텍스트→시도코드 집합. 공식 풀네임 우선(예 '경기도 광주시'는 경기로 정확분류), 없으면 짧은 별칭(2종↑ 모호→빈집합)."""
    full = {code for code, name in _BUILDING_SIDO.items() if name in blob}
    if full:
        return full
    alias = {code for code, a in _SIDO_ALIASES if a in blob}
    return alias if len(alias) == 1 else set()


def _building_regions(path, rel=""):
    """건물 업로드 파일 → (정렬된 시도코드 리스트, 기준일digits|''). zip 내부 전 멤버 스캔(다중 시도 zip 지원),
    파일명·폴더명(rel)·zip내부명에서 F_FAC_BUILDING_<코드>_<날짜> 우선, 없으면 한글 시도명 폴백."""
    members = []
    if path.suffix.lower() == ".zip":
        try:
            if zipfile.is_zipfile(path):
                with zipfile.ZipFile(path) as zf:
                    members = zf.namelist()
        except Exception:
            members = []
    blob = " ".join([rel, path.name] + members)
    codes, dates = set(), []
    for mm in _BLD_PAT.finditer(blob):
        codes.add(_SIDO_CANON.get(mm.group(1), mm.group(1))); dates.append(mm.group(2))
    if not codes:
        codes = _sido_codes_from_text(blob)
    asof = (max(dates, key=_norm) if dates else "") or _valid_period(path.name) or _valid_period(blob)
    return sorted(codes), asof


def scan_uploaded_files(key):
    """업로드 출처 디렉토리의 파일 단위 현황 — 파일명·기준일·크기·업로드시각·최신화상태(+건물DB는 시도 그룹핑).
    진실원천=디스크 실파일(rglob), 업로드시각은 upload_history 로 보강. 다중 시도 zip 은 시도별로 분해 표시."""
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
    prec = 6 if is_bld else None   # 건물DB=월 전체분 → 월(YYYYMM) 정밀도(일자 갱신일 drift 오판 방지)
    groups = {}; total = 0
    if sdir.is_dir():
        for f in sorted((p for p in sdir.rglob("*") if p.is_file()), key=lambda p: str(p)):
            if f.name.startswith(".") or f.suffix.lower() in _SHP_SIDECAR:
                continue   # 숨김파일(.DS_Store 등)·SHP 사이드카(.shx/.dbf/.prj/.fix) 제외 — 데이터셋(zip/shp)만 표시
            try:
                rel = str(f.relative_to(sdir)); st = f.stat()
            except (OSError, ValueError):
                continue   # 열거~stat 사이 삭제/경합 — 건너뜀
            codes, asof = _building_regions(f, rel) if is_bld else ([], _period_from_name(f.name))
            total += 1
            row = {"file": rel, "asof": asof or None, "size": st.st_size,
                   "uploaded_at": hist.get(rel) or hist.get(f.name)
                                  or time.strftime("%Y-%m-%d %H:%M", time.localtime(st.st_mtime)),
                   "status": _cmp_period(asof, latest, prec)}
            for gk in (codes or [""]):   # 시도 미상=미분류(빈키), 다중 시도=각 그룹에 표시
                groups.setdefault(gk, {"code": gk or None, "files": []})["files"].append(row)
    regions = []
    for gk in sorted(groups, key=lambda k: (k == "", k)):   # 미분류(빈키) 맨 뒤
        g = groups[gk]; asofs = [r["asof"] for r in g["files"] if r["asof"]]
        newest = max(asofs, key=_norm) if asofs else None
        regions.append({"code": g["code"],
                        "name": (_BUILDING_SIDO.get(g["code"]) if g["code"] else None),
                        "asof": newest, "status": _cmp_period(newest, latest, prec),
                        "n": len(g["files"]), "files": g["files"]})
    return {"key": key, "latest": latest, "grouped": is_bld,
            "total": total, "regions": regions}   # total=고유 파일수(다중 시도 파일은 그룹 n 에선 중복 카운트)


def load_builds():
    return _load_json(DIST / "builds.json", [])

def _norm(x):
    return re.sub(r"\D", "", str(x or ""))   # 기준일 비교용 — 숫자만(202605 vs 2026-06-18 호환)

def _cmp_period(cur, lat, prec=None):
    """기준일 vs 최신 → 'current'|'update'|'unknown'. 공통 정밀도 비교(YYYYMM vs YYYYMMDD 동월 오판 방지).
    prec=6 이면 월(YYYYMM) 단위로 절단 비교 — 건물DB(월 전체분)는 일 단위 갱신일과 비교 시 일자 drift 오판 방지."""
    if not cur or not lat:
        return "unknown"
    a, b = _norm(cur), _norm(lat)
    if prec:
        a, b = a[:prec], b[:prec]
    k = min(len(a), len(b))
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


def _unzip_recursive(path, dest, depth=4, on=None):
    """zip 추출 + 내부 중첩 zip 재귀 추출(zip-of-zips: 법정동 LSMD 시도별 묶음 등). 내부 zip은 풀고 제거.
    on(line): 진행 로그 콜백(있으면 추출 단위로 호출 — 큰 zip 적재 중 라이브 표시)."""
    dest = pathlib.Path(dest)
    if on: on(f"   {pathlib.Path(path).name} 추출…")
    with zipfile.ZipFile(path) as z:
        z.extractall(dest)
    for _ in range(depth):
        inners = [p for p in dest.rglob("*.zip") if p.is_file()]
        if not inners:
            break
        for inner in inners:
            try:
                if on: on(f"     ↳ {inner.name} 추출…")
                with zipfile.ZipFile(inner) as z:
                    z.extractall(inner.parent)
                inner.unlink()
            except Exception:
                pass


def prepare_sources(keys=None, emit=None):
    """sources/<key>/ 의 '모든' 업로드 파일을 build_input.dest 로 적재(누적).
    확장자별: .zip=stdlib 추출 / .7z=CLI(있으면) / .tar·.tgz·.txz=stdlib 추출 / 그외(.csv·.txt·.shp 등)=복사.
    업로드가 없으면 기존 staged 재사용(직전 빌드분 또는 BUILD_HOME 직접 배치 데이터).
    emit(line): 적재 진행 로그 콜백 — 큰 소스(navi .7z·building_db 2GB) 추출이 빌드 큐 앞에서 동기로
    돌며 무출력이라 멈춘 듯 보이던 문제 해소(있으면 소스/추출 단위로 라이브 표시)."""
    import shutil, tarfile, zipfile
    def _say(msg):
        if emit:
            try: emit(msg)
            except Exception: pass
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
            _say(f"⬆ {s.get('name', key)} 적재 중… ({len(files)}개)")
            shutil.rmtree(dest, ignore_errors=True); dest.mkdir(parents=True, exist_ok=True)
            tool = shutil.which("7z") or shutil.which("7za") or shutil.which("7zr")
            n = 0; errs = []
            for f in files:
                ext = f.suffix.lower()
                if ext == ".zip":
                    try:
                        _unzip_recursive(f, dest, on=_say); n += 1   # 중첩 zip(zip-of-zips: 법정동 시도별 묶음)도 재귀 추출
                    except Exception as e:
                        errs.append(f"{f.name}: zip {str(e)[:50]}")
                elif ext == ".7z":
                    if not tool:
                        errs.append(f"{f.name}: 7z 없음 — scripts/setup-build-host.sh 실행(p7zip 설치) 후 재빌드, 또는 .txt 직접배치"); continue
                    _say(f"   {f.name} 추출(.7z, 수십초 소요)…")
                    r = subprocess.run([tool, "x", "-y", f"-o{dest}", str(f)], capture_output=True, text=True, timeout=3600)
                    if r.returncode != 0: errs.append(f"{f.name}: {(r.stderr or '')[-80:]}")
                    else: n += 1
                elif ext == ".tar" or f.name.lower().endswith((".tar.gz", ".tgz", ".tar.bz2", ".tbz2", ".tar.xz", ".txz")):
                    try:
                        _say(f"   {f.name} 추출(tar)…")
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
            _say(f"{'✓' if action=='staged' else '⚠'} {s.get('name', key)} 적재 {'완료' if action=='staged' else action} ({n}개)")
            report.append({"key": key, "action": action, "n": n, "dest": str(dest),
                           "msg": "; ".join(errs)[:200] if errs else None})
        except Exception as e:
            report.append({"key": key, "action": "error", "msg": str(e)[:200]})
    return report

# 원천데이터 기본 경로(업로드/환경변수로 덮어쓸 수 있음)
# 빌드 입력 — 기본값은 Build Studio 업로드 적재 경로(prepare_sources). 외부 경로는 환경변수로 덮어쓰기.
SRC_JUSO = os.environ.get("SRC_JUSO", str(BUILD_HOME / "staged/navi"))
SRC_LOCALDATA = os.environ.get("SRC_LOCALDATA", str(BUILD_HOME / "staged/localdata"))
SRC_GIS = os.environ.get("SRC_GIS", str(BUILD_HOME / "staged/gis"))   # 건물 SHP 경로 오버라이드 계약(load-all.sh:88 소비). TARGETS() 미사용은 정상 — test_build_studio.py 가 계약 고정

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
        "terrain": dict(label="지형 음영 타일 (terrain.mbtiles)", dep=None,
            cmd=["bash", str(ROOT/"scripts/03-gen-terrain.sh")]),   # SRTM30m→Terrain-RGB(온라인·정적). 반입본 있으면 freshness=fresh 로 스킵
        "geocode": dict(label="통합 지오코딩 인덱스", dep=["localdata", "facility"],
            cmd=[py, str(ROOT/"scripts/09-gen-geocode.py"), "--src", SRC_JUSO,
                 "--osm", str(BUILD_HOME/"osm.sqlite"), "--poi-csv-dir", str(BUILD_HOME/"poi-all"),
                 "--out", str(BUILD_HOME/"geocode.sqlite"), "--dedup", "er",   # ER 중복제거+건물키 backfill 적용(없으면 09 기본 legacy)
                 "--t018-disposed"]),   # [F1 2026-08-28·T045] T018 리 백필 처분 완료 — G0 해제 조건 5.
                                        #   조건 1·2: ri_backfill_* 폐기 헤더·s3 하드게이트 폐기, 조건 3: load_geocode.py 는
                                        #   ri/bcode 를 무가공 통과(확인), 조건 4: backfill-admin-codes.py PK6 세대 가드.
        # 리(legal-ri)는 if/else 가드로 감싼다 — 아직 한 번도 반입한 적이 없어 보통 디렉터리가 없다.
        #   · `&& … || echo` 로 쓰지 않는 이유: 디렉터리가 있는데 06 이 실패해도 rc 가 0 으로 덮여
        #     areas 단계가 '성공'으로 기록된다. if/else 는 06 의 rc 를 그대로 전파한다.
        #   · 경로에 셸 변수를 쓰지 않는 이유: 이 bash -c 는 BUILD_HOME 을 상속받지 않는다.
        #     `[ -d "$RI" ]` 로 쓰면 항상 거짓이 되어 리가 있어도 영구히 건너뛴다(조용한 실패).
        #   · --name-field RI_NM / --code-field RI_CD 는 **잠정값**이다. 리 SHP 반입 시 ogrinfo 로
        #     확정할 것. 틀리면 06-gen-areas.py:95 의 skip 카운터가 전건 skip 으로 자기진단한다.
        "areas": dict(label="행정구역 경계 (법정동·행정동·리 → areas)", dep="geocode",
            cmd=["bash", "-c",
                 f'python3 "{ROOT/"scripts/06-gen-areas.py"}" --shp "{BUILD_HOME/"sources/boundary/legal"}" --srs EPSG:5186 --name-field EMD_NM --code-field EMD_CD --type legal-dong --db "{BUILD_HOME/"geocode.sqlite"}"'
                 f' && python3 "{ROOT/"scripts/06-gen-areas.py"}" --shp "{BUILD_HOME/"sources/boundary/admin/BND_ADM_DONG_PG.shp"}" --srs EPSG:5186 --name-field ADM_NM --code-field ADM_CD --type admin-dong --db "{BUILD_HOME/"geocode.sqlite"}"'
                 f' && if [ -d "{BUILD_HOME/"sources/boundary/ri"}" ]; then'
                 f' python3 "{ROOT/"scripts/06-gen-areas.py"}" --shp "{BUILD_HOME/"sources/boundary/ri"}"'
                 f' --srs EPSG:5186 --name-field RI_NM --code-field RI_CD --type legal-ri'
                 f' --db "{BUILD_HOME/"geocode.sqlite"}";'
                 f' else echo "(건너뜀) 리 경계 없음: {BUILD_HOME/"sources/boundary/ri"}"; fi']),
        # 하이브리드: 건물·필지·POI·시설은 PostGIS→martin(/dyn) 서빙으로 일원화 → buildings.mbtiles/poi.mbtiles 타깃 폐기.
        # 동적 레이어 = scripts/postgis/load-all.sh(admin·parcel·building·geocode→address/poi·facility) 한 타깃으로.
        # (PostGIS 적재는 빌드호스트에서 compose --profile postgis 기동 후 실행.)
        "load_postgis": dict(label="PostGIS 적재 (필지·건물·POI·시설·행정구역)", dep="geocode",
            cmd=["bash", str(ROOT/"scripts/postgis/load-all.sh")]),
        # dep=load_postgis: PostGIS 적재 후 실행 보장 + 적재 실패 시 qc 스킵(거짓 PASS 차단). qc 는 always=True 라
        #   적재가 최신이면 자동 재사용(강제 재적재 없음). --pg 로 parcel/building 적재 완전성·인덱스까지 검증.
        "qc": dict(label="QC 검증", dep="load_postgis",
            cmd=[py, str(ROOT/"scripts/13-qc-check.py"), "--db", str(BUILD_HOME/"geocode.sqlite"),
                 "--tiles", str(BUILD_HOME/"tiles"), "--style", str(ROOT/"style/style.json"),
                 "--config", str(ROOT/"server/tileserver-config.json"), "--api", "http://localhost:8082", "--pg"]),
        # [T043 검수 M-3] dep=None → dep="geocode".
        #   package.sh:240 은 `cp -c "$BUILD_HOME/geocode.sqlite"` 로 지오코딩 DB 를 번들에 담는다.
        #   dep 이 없으면 geocode 가 G0(전국 재빌드 차단)로 정지해도 package 는 그대로 돌아
        #   **직전 세대 7GB DB** 를 최신 번들인 양 반출한다. 폐쇄망으로 나가면 되돌릴 수 없다.
        #   dep="qc" 가 아니라 dep="geocode" 인 이유: qc 는 PostGIS 기동을 전제하므로
        #   그것을 걸면 DB 가 없는 환경에서 번들 생성 자체가 막힌다(과잉 차단). geocode 라면
        #   fresh 일 때 큐에서 자동으로 빠져 현행 동작이 그대로 유지되고(build-studio.py:914-920),
        #   실제로 실행됐다가 실패/스킵된 경우에만 package 가 선다 — 정확히 막아야 할 경우다.
        "package": dict(label="폐쇄망 번들 패키징", dep="geocode",
            cmd=["bash", str(ROOT/"scripts/package.sh")]),
    }

CANON = ["osm_vector", "osm_sqlite", "dong", "terrain", "localdata", "facility", "geocode", "areas", "load_postgis", "qc", "package"]


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
    "terrain": {"out_only": True, "scripts": ["scripts/03-gen-terrain.sh"],
                "out": [ROOT / "tiles/terrain.mbtiles"]},   # 정적 SRTM 산출물 — 파일 존재=최신(반입본 보존·재다운로드 방지)
    "localdata": {"src": ["localdata"], "scripts": ["scripts/11-build-localdata.py"],
                  "out": [BUILD_HOME / "poi-all/localdata_clean.csv"]},
    "facility": {"src": ["facility"], "scripts": ["scripts/11b-build-facility.py"],
                 "out": [BUILD_HOME / "poi-all/facility_clean.csv"]},
    "geocode": {"src": ["juso_navi", "sangga"], "dep_art": ["osm_sqlite", "localdata", "facility"],
                "scripts": ["scripts/09-gen-geocode.py", "scripts/dedup_er.py"],
                "out": [BUILD_HOME / "geocode.sqlite"]},
    # boundary_ri: 리 SHP 를 새로 올리거나 교체하면 areas 가 stale 로 떨어져 재적재된다.
    #   빠뜨리면 새 경계를 올려도 fresh 판정으로 조용히 건너뛴다.
    "areas": {"src": ["boundary_legal", "boundary_admin", "boundary_ri"], "dep_art": ["geocode"],
              "scripts": ["scripts/06-gen-areas.py"], "out": [BUILD_HOME / "geocode.sqlite"]},
    "load_postgis": {"src": ["parcel", "building_db", "sangga", "localdata", "facility"],
                     "dep_art": ["geocode"],
                     "scripts": ["scripts/postgis/load-all.sh", "scripts/postgis/load_parcel.sh",
                                 "scripts/postgis/load_building.sh", "scripts/postgis/load_geocode.py",
                                 # ── 지번 1급화(A3): 스키마·사전·백필 추적 → 개선 시 stale 인식(조용한 skip 방지) ──
                                 "scripts/postgis/schema/21-parcel-jibun.sql",
                                 "scripts/postgis/build_dong_dict.sql",
                                 "scripts/postgis/build_sigungu_dict.sh",
                                 "scripts/postgis/backfill_parcel_jibun.sql",
                                 "scripts/postgis/backfill_geom_pt.sql",
                                 # ── T8: POI tier 디클러터(스키마·함수·백필) ──
                                 "scripts/postgis/schema/30-poi.sql",
                                 "scripts/postgis/schema/31-poi-mvt.sql",
                                 "scripts/postgis/backfill_poi_tier.py"],
                     "out": []},   # PostGIS 적재(파일 산출물 없음) — src/scripts 시그니처로 재빌드 판정
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
    out_ok = all(pathlib.Path(o).exists() for o in outs)   # outs=[] → True(검사할 파일 없음)
    if m.get("out_only"):   # 정적 외부 산출물(terrain 등) — 파일 존재만으로 최신(시그니처 무시 → 반입본 보존)
        return "fresh" if (outs and out_ok) else "missing"
    if outs and not out_ok:   # 파일 산출물이 정의됐는데 없음 → 재빌드
        return "missing"
    # 파일 산출물 없는 적재형 타깃(load_postgis)은 입력 시그니처로만 최신 판정(DB 적재 멱등)
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


def backup_geocode_artifact(keep=None):
    """geocode.sqlite 를 BUILD_HOME/artifacts/ 에 타임스탬프 스냅샷으로 보존.
    배경: load_postgis 적재 후 다음 빌드의 재수집(staged/navi rmtree)·geocode 재생성이 직전 산출물을
      덮어/지워, 분석·롤백용 geocode.sqlite 가 소실됨(사용자 관찰). 빌드 산출물을 산출 즉시 보존한다.
    멱등: 동일본(size,mtime) 이미 보존됐으면 건너뜀(재빌드 1회당 1스냅샷). 최신 keep 개만 유지.
    keep: 보존 개수(기본 env GEOCODE_BACKUP_KEEP=3; 0 이면 비활성). 반환: 새 스냅샷 경로 | None."""
    if keep is None:
        keep = int(os.environ.get("GEOCODE_BACKUP_KEEP", "3"))
    if keep <= 0:
        return None
    src = BUILD_HOME / "geocode.sqlite"
    if not src.exists() or src.stat().st_size < 1_000_000:   # 미생성/손상 스텁 제외
        return None
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    sig = f"{src.stat().st_size}_{int(src.stat().st_mtime)}"
    sigf = ARTIFACTS / ".last_sig"
    if sigf.exists() and sigf.read_text(encoding="utf-8").strip() == sig:
        return None   # 동일본 이미 보존(중복 복사 방지)
    ts = time.strftime("%Y%m%d-%H%M%S", time.localtime(src.stat().st_mtime))
    dst = ARTIFACTS / f"geocode_{ts}.sqlite"
    shutil.copy2(src, dst)
    sigf.write_text(sig, encoding="utf-8")
    snaps = sorted(ARTIFACTS.glob("geocode_*.sqlite"), key=lambda p: p.stat().st_mtime, reverse=True)
    for old in snaps[keep:]:   # 최신 keep 개 초과분 제거(디스크 보호)
        try: old.unlink()
        except OSError: pass
    return dst


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
    elif kind == "load_postgis":
        # load-all.sh: '━━ <단계> ━━' 헤더 + 시도별 '[i/N]' + 'OK: load-all 완료'
        m = re.search(r"\[(\d+)/(\d+)\]", l)
        if m: return min(0.95, 0.1 + 0.85*int(m.group(1))/max(int(m.group(2)), 1))
        if l.startswith("OK: load-all"): return 1.0
        if l.startswith("━━"): return 0.1
    elif kind == "terrain":   # 03-gen-terrain.sh: '[1/4]'~'[4/4]' + '지형 타일 생성 완료'
        m = re.search(r"\[(\d)/4\]", l)
        if m: return 0.1 + 0.85*int(m.group(1))/4
        if "생성 완료" in l: return 1.0
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
            # 자식 출력을 PTY 로 받아 실시간 스트리밍. 외부 도구(tippecanoe·ogr2ogr·planetiler 등)는
            # stdout 이 파이프면 블록버퍼링되어 로그가 종료 직전까지 안 나온다(진행 여부 알 수 없음).
            # TTY 에 붙으면 라인 버퍼링 → 즉시 출력. \r 진행표시도 라인으로 surface.
            # (PYTHONUNBUFFERED 는 파이썬 자식 보조.) PTY 불가 환경은 파이프로 폴백.
            env = {**os.environ, "PYTHONUNBUFFERED": "1"}
            rc = self._stream(kind, cmd, env)
            if rc != 0: raise RuntimeError(f"종료코드 {rc}")
            j["status"] = "done"; j["progress"] = 1.0
            record_build_sig(kind)   # 성공 시그니처 기록 → 다음 빌드에서 변경없으면 자동 건너뜀
            if kind in ("geocode", "areas"):   # geocode.sqlite 산출 직후 스냅샷 보존(재적재·재수집 teardown 전에)
                try:
                    b = backup_geocode_artifact()
                    if b: self._emit(kind, f"[artifact 보존] {b.name} → {ARTIFACTS}")
                except Exception as e: self._emit(kind, f"[artifact 보존 경고] {e}")
            if kind == "package":   # 빌드(패키징) 완료 → 매니페스트 자동저장(번들 포함)
                try: save_manifest(with_bundle=True)
                except Exception as e: self._emit(kind, f"[프로필 저장 실패] {e}")
        except Exception as e:
            j["status"] = "error"; self._emit(kind, f"[오류] {e}")
        self.publish({"kind": kind, "status": j["status"], "progress": j["progress"]})

    _ANSI = re.compile(r'\x1b\[[0-9;?]*[a-zA-Z]')   # 색/커서 ANSI 이스케이프(TTY 모드에서 도구가 붙임) 제거

    def _stream(self, kind, cmd, env):
        """자식을 PTY 에 붙여 라인 단위 실시간 출력. \\r 진행표시는 라인으로 surface하되 5/s 스로틀.
        PTY 생성/기동 실패 시 파이프로 폴백."""
        try:
            mfd, sfd = pty.openpty()
        except Exception:
            return self._stream_pipe(kind, cmd, env)
        try:
            p = subprocess.Popen(cmd, stdout=sfd, stderr=sfd, stdin=subprocess.DEVNULL,
                                 cwd=str(ROOT), env=env, close_fds=True)
        except Exception:
            os.close(mfd); os.close(sfd)
            return self._stream_pipe(kind, cmd, env)
        os.close(sfd)
        buf = ""; hold = ""; lr = [0.0]   # lr: \r 진행표시 마지막 emit 시각(스로틀)
        def feed(s, final=False):
            nonlocal buf, hold
            buf += hold + s; hold = ""
            if not final and buf.endswith("\r"):   # 청크 경계에 걸린 \r\n 보호(\r 만 다음으로 미룸)
                buf = buf[:-1]; hold = "\r"
            buf = buf.replace("\r\n", "\n")
            toks = re.split(r'([\r\n])', buf)
            buf = toks.pop()                       # 마지막 토큰 = 미완성 tail
            it = iter(toks)
            for text in it:
                sep = next(it, "")
                if sep == "\r":                    # in-place 진행표시 → 최대 5/s
                    now = time.monotonic()
                    if now - lr[0] < 0.2: continue
                    lr[0] = now
                text = self._ANSI.sub("", text)
                if text: self._emit(kind, text)
            if final:
                tail = self._ANSI.sub("", buf)
                if tail.strip(): self._emit(kind, tail)
                buf = ""
        try:
            while True:
                try: chunk = os.read(mfd, 65536)
                except OSError: break              # 자식 종료(mac PTY: EIO)
                if not chunk: break                # EOF(linux)
                feed(chunk.decode("utf-8", "replace"))
        finally:
            try: os.close(mfd)
            except OSError: pass
        feed("", final=True)
        return p.wait()

    def _stream_pipe(self, kind, cmd, env):
        """PTY 불가 환경 폴백 — 파이프 라인 스트리밍(외부 도구는 버퍼링될 수 있음)."""
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             text=True, bufsize=1, cwd=str(ROOT), env=env)
        for line in p.stdout:
            self._emit(kind, line.rstrip("\n"))
        return p.wait()

MGR = Manager()


# ── 내용주소 저장소(store) + 자동수집(collector) ─────────────────────
# store/<sha[:2]>/<sha> 로 같은 내용 1벌 저장(중복 0, 영구 보관). 수집은 항목 1건씩 순차
# (Referer·백오프·HTML 차단감지) → SHA 비교(동일=staged 유지·재빌드 생략) → staged 추출/배치 → DB(현재일·sha=staged_sig).
STORE_DIR = BUILD_HOME / "store"
FACILITY_CATALOG = ROOT / "scripts" / "facility-catalog.json"
LOCALDATA_REGIONS = ROOT / "scripts" / "localdata-regions.json"
_COLLECT_LOCK = threading.Lock()
# 파괴 구간(교체) 진입 대기 상한. 수집은 수십 분이 걸리므로 무한 대기 대신 즉시 거절해
# 사용자에게 "지금은 수집 중"을 알린다(C-11: 락은 파괴 구간에만, 다GB 수신 루프는 락 밖).
_COLLECT_LOCK_WAIT = 2.0
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


def _payload_integrity(path):
    """페이로드 건전성 판정(매직바이트 ↔ 구조 정합) — 네트워크/HTTP 비의존 순수함수.
    수집(다운로드) 시점에 절단/손상 페이로드를 검지하기 위한 단일 판정원(run_collect·_extract_into 공유, DRY).
    반환: (verdict, detail)
      healthy   : 정상 아카이브(zip/tar/7z/gzip) 또는 아카이브 매직이 아닌 평문(csv/txt/pbf/shp 등) — 통과
      truncated : 아카이브 매직이 있으나 구조 불완전(EOCD/중앙디렉토리 결손, gzip 말미 절단 등) — 거부
      corrupt   : 0바이트 / 매직-구조 모순 / 식별불가 손상 — 거부
      unknown   : 판정 불가(7z 검증도구 없음 등) — 호출측 정책에 위임(보수적 통과)
    경로만 받으므로 임시파일만으로 단위테스트 가능. is_zipfile/is_tarfile 은 말미 EOCD/헤더 수준만 읽어 GB급도 저비용."""
    import tarfile
    p = pathlib.Path(path)
    try:
        size = p.stat().st_size
    except OSError as e:
        return ("corrupt", f"stat 실패: {e}")
    if size == 0:
        return ("corrupt", "0바이트")
    with open(p, "rb") as fh:
        head = fh.read(8)
    # ① ZIP: local header(PK\x03\x04) / 빈 zip EOCD(PK\x05\x06) / data descriptor(PK\x07\x08)
    if head[:4] in (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08"):
        if zipfile.is_zipfile(p):
            return ("healthy", "zip")
        return ("truncated", "ZIP 매직이나 EOCD/중앙디렉토리 결손 — 절단 의심")
    # ② 7z: 도구 있으면 무결성검증(t), 없으면 unknown(기존 추출분기에 위임)
    if head[:6] == b"7z\xbc\xaf\x27\x1c":
        tool = shutil.which("7z") or shutil.which("7za") or shutil.which("7zr")
        if not tool:
            return ("unknown", "7z 매직 — 무결성검증 도구 없음")
        try:
            r = subprocess.run([tool, "t", str(p)], capture_output=True, timeout=600)
            return ("healthy", "7z") if r.returncode == 0 else ("corrupt", "7z 무결성검증 실패")
        except Exception as e:
            return ("unknown", f"7z 검증 예외: {str(e)[:80]}")
    # ③ gzip: 말미까지 스트리밍 디코드 시도(상한까지). 성공 healthy / 실패 truncated / 상한초과 unknown
    if head[:2] == b"\x1f\x8b":
        import gzip as _gz
        cap = 256 << 20   # 디코드 상한(대용량 회피) — 초과 시 말미 미확인 → unknown
        try:
            read = 0
            with _gz.open(p, "rb") as g:
                while True:
                    chunk = g.read(1 << 20)
                    if not chunk:
                        return ("healthy", "gzip")
                    read += len(chunk)
                    if read >= cap:
                        return ("unknown", "gzip — 디코드 상한 초과로 말미 미확인")
        except Exception as e:
            return ("truncated", f"gzip 디코드 실패 — 절단 의심: {str(e)[:80]}")
    # ④ tar(비압축): magic 이 아닌 ustar 헤더 — is_tarfile 로 판정(gzip-tar 은 ③에서 처리)
    try:
        if tarfile.is_tarfile(p):
            return ("healthy", "tar")
    except Exception:
        pass
    # ⑤ 아카이브 매직 아님 → 평문(csv/txt/shp/pbf 등). 평문 본문 절단은 스코프 외(_http_download CL 대조가 보완)
    return ("healthy", "non-archive")


def _collect_integrity_gate(tmp, item_key, sz):
    """수집(다운로드) 직후·store_put 전 무결성 게이트. truncated/corrupt 면 tmp(.part) 폐기 후 raise.
    raise 가 run_collect 의 try 말미 staged_sig 갱신을 건너뛰게 해 다음 회차 자동 재수집(자기치유)을 유도하고,
    store_put 미호출로 손상물의 store 영구보관을 차단한다. tmp 명시 제거로 .part 잔존도 방지.
    참고(Note 3): extract 모드의 stale 한 손상 staged 는 정상 재수집 성공 시 run_collect 의
    `(not allreused) or (not _nonempty_dir(dest))` 분기 rmtree+재추출로 자연 치유된다(추가 코드 불요)."""
    verdict, detail = _payload_integrity(tmp)
    if verdict in ("truncated", "corrupt"):
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise RuntimeError(f"수집 무결성 거부({verdict}): {item_key} — {detail} ({sz/1e6:.1f}MB)")


def _assert_full_receipt(item_key, shas, expected_n):
    """부분 수신 차단 — 목록 개수와 실제 수신 개수의 **동수 불변식**만 본다.

    절대 개수를 상수로 박지 않는다: parcel 은 원본 파일 수가 258→262 로 변동하므로
    "몇 개여야 한다"는 판정은 정상 증감에서 오탐이 된다. 볼 수 있는 건 "이번 회차에
    목록으로 뽑은 만큼 전부 받아냈는가" 뿐이다.

    **파괴적 교체 전에** 불러야 한다 — 반쪽 수신물로 staged 를 갈아끼우는 게 이번 사고다.
    """
    if len(shas) != expected_n:
        raise RuntimeError(f"부분 수신: {item_key} — {len(shas)}/{expected_n}파일만 수신, 교체 중단")


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
                # 헤더는 응답 컨텍스트가 닫히기 전 취득(절단 대조용). identity/헤더부재일 때만 엄격 대조.
                cl_raw = r.headers.get("Content-Length")
                ce = (r.headers.get("Content-Encoding") or "").strip().lower()
                with open(dest, "wb") as o:
                    head = r.read(1 << 16)
                    low = head[:300].lstrip().lower()
                    if low.startswith(b"<!doctype html") or b"<html" in low:
                        txt = re.sub(rb'\s+', b' ', re.sub(rb'<[^>]+>', b' ', head)).strip().decode("utf-8", "replace")[:160]
                        raise RuntimeError(f"파일 아님(HTML 에러페이지) — 세션/URL 확인 · 응답본문:[{txt}]")
                    o.write(head)
                    while True:
                        chunk = r.read(1 << 20)
                        if not chunk:
                            break
                        o.write(chunk)
            # 크기 대조는 with open 블록이 닫힌 뒤(flush 후) 디스크 크기로 수행.
            # transport gzip(CE=gzip 등) 또는 chunked(CL 부재) 는 압축/미선언이라 대조 생략(오탐 방지) →
            # 절단은 _payload_integrity(매직↔구조 정합)가 보완. 재시도 루프 안이라 raise 시 백오프 재시도됨.
            written = os.path.getsize(dest)
            # A1: 0바이트는 '성공적으로 0바이트를 받았다'가 아니라 실패다. CL=0 이면 아래
            # 대조를 정상 통과해버리고(수신 0 == 선언 0), CL 부재면 대조 자체가 생략된다 —
            # 이번 사고의 262건이 전부 이 틈으로 빠져나갔다. 반환값이 아니라 **예외**여야
            # 재시도 백오프와 상위 실패처리(A5 배지·A6 status)가 걸린다.
            if written == 0:
                raise RuntimeError(f"0바이트 수신: 빈 응답은 성공이 아니다  [url={url}]")
            if cl_raw is not None and ce in ("", "identity"):
                try:
                    expected = int(cl_raw.strip())
                except (ValueError, AttributeError):
                    expected = None
                if expected is not None and written != expected:
                    raise RuntimeError(f"다운로드 절단: 수신 {written}B / 선언 {expected}B  [url={url}]")
            return written
        except Exception as e:
            last = e
            if attempt < retries - 1:
                time.sleep(3 * (attempt + 1))
    raise RuntimeError(f"다운로드 실패({retries}회): {str(last)[:220]}  [url={url}]")


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
    # zip/tar/7z 어느 추출 분기에도 안 든 페이로드 — 아카이브 매직을 가진 손상물(절단ZIP 등)이
    # 확장자 없는 <sha> 라는 이유로 .csv 로 둔갑·staged 잠입하는 구멍을 봉쇄(2차 방어).
    v, d = _payload_integrity(src)
    if v in ("truncated", "corrupt"):
        raise RuntimeError(f"손상 아카이브 추출 거부({v}): {d}  [src={src}]")
    # healthy 평문(아카이브 매직 없음)만 .csv 폴백 복사 유지(11/11b 가 *.csv glob — 계약 보존).
    nm = orig_name or src.name
    if "." not in nm: nm += ".csv"
    shutil.copy2(src, dest_dir / nm)


# ── 원자적 디렉터리 교체(_swap_dir) ────────────────────────────────────
# 사고 재현 경로: `rmtree(dest)` 로 원본을 **먼저 지우고** 새로 채우다 실패하면 남는 게 없다.
# 여기서는 순서를 뒤집는다 — 옆에 채우고(.incoming), 검증하고, 그 다음에야 교체한다.
_SWAP_INCOMING_SUFFIX = ".incoming"
_SWAP_OLD_SUFFIX = ".old"
_SWAP_PID_NAME = ".swap-pid.json"


def _proc_start_key(pid):
    """PID 의 프로세스 시작시각 문자열. 조회 실패/미존재면 None.

    C-12: PID 는 재사용된다. 고아 판정을 PID 생존만으로 내리면, 재사용된 남의 PID 를
    보고 '작업 중'이라 오판해 고아가 영원히 남거나, 반대로 살아있는 작업을 지운다.
    시작시각을 함께 대조하는 두 번째 열쇠다(표준 라이브러리만 — psutil 비의존)."""
    try:
        r = subprocess.run(["ps", "-o", "lstart=", "-p", str(int(pid))],
                           capture_output=True, text=True, timeout=10)
    except Exception:
        return None
    return (r.stdout or "").strip() or None


def _swap_pid_write(staging):
    """`.incoming` 안에 소유자 표식(PID + 시작시각)을 남긴다."""
    p = pathlib.Path(staging) / _SWAP_PID_NAME
    pid = os.getpid()
    p.write_text(json.dumps({"pid": pid, "start": _proc_start_key(pid), "at": time.time()}),
                 encoding="utf-8")
    return p


def _swap_owner_alive(staging):
    """`.incoming` 의 소유 프로세스가 아직 살아있는가(= 손대면 안 되는가).

    판정 불가(표식 손상 아님 / 시작시각 조회 실패)면 **살아있다**로 본다 —
    오판이 파괴가 아니라 잔재를 남기는 쪽으로 기울게 하는 편향이다.
    반대로 표식 자체가 없으면 고아로 본다(정상 경로는 항상 표식을 쓴다)."""
    f = pathlib.Path(staging) / _SWAP_PID_NAME
    try:
        info = json.loads(f.read_text(encoding="utf-8"))
        pid = int(info.get("pid"))
    except Exception:
        return False                      # 표식 없음/깨짐 → 고아
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False                      # 죽음 → 고아
    except PermissionError:
        pass                              # 살아있으나 타 사용자 소유 — 시작시각으로 가린다
    except OSError:
        return True                       # 판정 불가 → 보수적
    want = info.get("start")
    got = _proc_start_key(pid)
    if not want or got is None:
        return True                       # 시작시각을 못 얻는 플랫폼 → 보수적
    return got == want


def _dir_stats(root, skip=()):
    """root 하위 **정규 파일**의 (개수, 바이트합). 심링크는 세지 않는다."""
    skip = {os.path.realpath(str(s)) for s in skip}
    n_files = n_bytes = 0
    for dp, _dn, fns in os.walk(str(root)):
        for nm in fns:
            fp = os.path.join(dp, nm)
            if os.path.realpath(fp) in skip or os.path.islink(fp):
                continue
            try:
                n_bytes += os.stat(fp).st_size
            except OSError:
                continue
            n_files += 1
    return n_files, n_bytes


def _rm_path(p, ignore_errors=False):
    """디렉터리/파일 어느 쪽이든 제거(교체 잔재 정리 전용)."""
    p = pathlib.Path(p)
    if p.is_dir() and not p.is_symlink():
        shutil.rmtree(p, ignore_errors=ignore_errors)
        return
    try:
        p.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        if not ignore_errors:
            raise


def _assert_same_device(*paths):
    """존재하는 경로들이 같은 파일시스템인지 검증한다(C-7 잔여 — EXDEV 전제 강제).

    `.incoming`/`.old` 를 `dest.parent` 안에 만드는 설계상 rename 양끝은 늘 같은
    디렉터리 엔트리라 EXDEV 는 구조적으로 불가하다. 다만 dest 자체가 마운트포인트인
    경우가 남으므로 전제를 코드로 못박는다 — 복사 폴백을 두지 않는 이유는 §docstring 참조."""
    devs = {}
    for p in paths:
        try:
            devs[str(p)] = os.stat(str(p)).st_dev
        except OSError:
            continue
    if len(set(devs.values())) > 1:
        raise RuntimeError(
            "교체 거부 — 서로 다른 파일시스템이라 원자적 rename 불가(EXDEV): "
            + ", ".join(f"{k}(dev={v})" for k, v in devs.items()))


def _swap_replace(src, dst):
    """rename 한 번. EXDEV 는 원인이 드러나는 메시지로 바꿔 올린다(무성 폴백 금지)."""
    try:
        os.replace(str(src), str(dst))
    except OSError as e:
        if getattr(e, "errno", None) == errno.EXDEV:
            raise RuntimeError(
                f"교체 중단 — 크로스 디바이스(EXDEV) rename 실패: {src} → {dst}. "
                "dest 와 같은 볼륨에 스테이징을 잡아야 한다") from e
        raise


def _swap_rollback_old(old, on=None):
    """① `.old` 처리. dest 가 없으면 롤백(원본 회수), 있으면 완료된 교체의 잔재로 보고 폐기."""
    old = pathlib.Path(old)
    if not (old.exists() or old.is_symlink()):
        return []
    dest = old.parent / old.name[: -len(_SWAP_OLD_SUFFIX)]
    if not (dest.exists() or dest.is_symlink()):
        _swap_replace(old, dest)                       # ①직후 사망 → 원본 복구
        if on: on(f"   복구: {old.name} → {dest.name} 롤백(원본 회수)")
        return [("rollback", str(dest))]
    _rm_path(old, ignore_errors=True)                  # ②완료 후 사망 → 정상 종료 처리
    if on: on(f"   정리: 완료된 교체의 잔재 {old.name} 삭제")
    return [("drop-old", str(old))]


def _swap_sweep_incoming(staging, on=None):
    """② `.incoming` 처리. 소유자가 살아있으면 절대 손대지 않는다."""
    staging = pathlib.Path(staging)
    if not (staging.exists() or staging.is_symlink()):
        return []
    if _swap_owner_alive(staging):
        return [("keep-incoming", str(staging))]
    _rm_path(staging, ignore_errors=True)
    if on: on(f"   정리: 고아 스테이징 {staging.name} 삭제")
    return [("drop-incoming", str(staging))]


def _swap_recover(dest, on=None):
    """dest 한 곳의 교체 잔재를 복구한다.

    C-13: **`.old` 롤백이 `.incoming` 스윕보다 먼저다.** 순서를 뒤집으면
    'dest 부재 ∧ 두 잔재 공존'(= ① 직후 사망) 상태에서 되돌릴 원본을 잃는다."""
    dest = pathlib.Path(dest)
    acts = _swap_rollback_old(dest.parent / (dest.name + _SWAP_OLD_SUFFIX), on=on)
    acts += _swap_sweep_incoming(dest.parent / (dest.name + _SWAP_INCOMING_SUFFIX), on=on)
    return acts


def _sweep_swap_residue(roots, on=None, max_depth=4):
    """프로세스 **시동 시 1회** 호출용 전역 스윕. 실행 중에는 부르지 마라.

    C-13 을 전역 수준에서도 지킨다 — `.old` 를 전량 되돌린 뒤에야 `.incoming` 으로 넘어간다.
    BUILD_HOME 전체 워크(수백만 파일)를 피하려고 깊이를 제한한다(dest 는 전부 얕다)."""
    if isinstance(roots, (str, pathlib.PurePath)):
        roots = [roots]
    olds, incs = [], []
    for root in roots:
        root = pathlib.Path(root)
        if not root.is_dir():
            continue
        base = str(root).rstrip(os.sep).count(os.sep)
        for dp, dns, _fns in os.walk(str(root)):
            # 이름 수집이 깊이 프루닝보다 **먼저** — 순서를 뒤집으면 경계 깊이의 잔재를 놓친다.
            for nm in list(dns):
                if nm.endswith(_SWAP_OLD_SUFFIX):
                    olds.append(pathlib.Path(dp) / nm); dns.remove(nm)
                elif nm.endswith(_SWAP_INCOMING_SUFFIX):
                    incs.append(pathlib.Path(dp) / nm); dns.remove(nm)
            if dp.count(os.sep) - base >= max_depth:
                dns[:] = []
    acts = []
    for p in olds:                      # ① 전량 롤백 먼저
        acts += _swap_rollback_old(p, on=on)
    for p in incs:                      # ② 그 다음 스윕
        acts += _swap_sweep_incoming(p, on=on)
    return acts


def _swap_dir(dest, fill, *, keep_old=False, on=None):
    """dest 를 **파괴하지 않고** 통째로 교체한다(원자적 교체 프리미티브).

    호출자가 `_COLLECT_LOCK` 을 보유한 상태로 호출한다 — 이 함수는 **락을 절대 획득하지
    않는다**(C-6). `threading.Lock` 은 재진입 불가라, 이미 락을 쥔 `run_collect` 안에서
    여기서 다시 잡으면 그 자리에서 자기교착한다. 상호배제는 전적으로 호출자 책임이다.

    dest     : pathlib.Path — 최종 목적지 디렉터리
    fill     : callable(staging: pathlib.Path) -> None
               staging 에 내용물을 채우는 콜백. 예외를 던지면 staging 만 지우고 재전파한다.
    keep_old : True 면 `.old` 백업을 남긴다(디버깅용, 기본 False)
    on       : 진행 로그 콜백(선택) — `_unzip_recursive` 와 같은 관행

    반환: (n_files, n_bytes) — 교체된 내용물의 실측치
    실패: staging 만 삭제하고 예외 재전파. **dest 는 처음부터 끝까지 무손상.**

    순서 — ① `dest.parent/<name>.incoming` 에 전량 채움 → ② 비어있지 않음 검증
           → ③ dest → `.old` rename → ④ `.incoming` → dest rename → ⑤ `.old` 삭제.
    ③④ 사이에서 죽으면 dest 가 잠깐 사라지지만, 시동 스윕(`_sweep_swap_residue`)의
    `.old` 롤백이 원본을 되돌린다.

    EXDEV: 스테이징을 `dest.parent` 안에 만들어(C-7) rename 양끝을 같은 디렉터리 엔트리로
    고정하므로 크로스 디바이스는 구조적으로 발생하지 않는다. 그럼에도 전제를 `_assert_same_device`
    로 검사하고 EXDEV 를 명확한 예외로 올린다. **복사 기반 폴백은 두지 않는다** — 3.6GB 복사는
    원자성을 잃어 이 함수가 막으려는 '교체 도중 반쪽 상태'를 되살리기 때문이다.
    """
    dest = pathlib.Path(dest)
    parent = dest.parent
    parent.mkdir(parents=True, exist_ok=True)
    staging = parent / (dest.name + _SWAP_INCOMING_SUFFIX)
    old = parent / (dest.name + _SWAP_OLD_SUFFIX)

    # 0) 이전 시도의 잔재부터 — C-13 과 같은 순서(.old 롤백 → .incoming 스윕)를 쓴다.
    _swap_rollback_old(old, on=on)
    if staging.exists() or staging.is_symlink():
        if _swap_owner_alive(staging):
            raise RuntimeError(f"교체가 이미 진행 중이다(살아있는 소유자): {staging}")
        _swap_sweep_incoming(staging, on=on)
    if staging.exists() or staging.is_symlink():
        raise RuntimeError(f"스테이징 정리 실패 — 수동 확인 필요: {staging}")

    staging.mkdir(parents=True)
    pidf = _swap_pid_write(staging)
    _assert_same_device(staging, parent, dest)

    try:
        fill(staging)
        n_files, n_bytes = _dir_stats(staging, skip=[pidf])
        if n_files <= 0 or n_bytes <= 0:
            raise RuntimeError(
                f"교체 거부 — 스테이징이 비었다(파일 {n_files}개 / {n_bytes}바이트): {dest}")
    except BaseException:
        _rm_path(staging, ignore_errors=True)      # dest 는 아직 손도 대지 않았다
        raise

    _rm_path(pidf, ignore_errors=True)             # 표식은 dest 로 넘기지 않는다
    dest_there = dest.exists() or dest.is_symlink()
    if dest_there:
        _swap_replace(dest, old)                   # ③ 원본 대피
    try:
        _swap_replace(staging, dest)               # ④ 신품 게시
    except BaseException:
        if dest_there and not (dest.exists() or dest.is_symlink()):
            os.replace(str(old), str(dest))        # ④ 실패 → 즉시 롤백
            if on: on(f"   롤백: {old.name} → {dest.name}(교체 실패, 원본 회수)")
        _rm_path(staging, ignore_errors=True)
        raise
    if dest_there and not keep_old:
        _rm_path(old, ignore_errors=True)          # ⑤ 여기서 죽어도 시동 스윕이 정리한다
    if on: on(f"   교체 완료: {dest.name} ({n_files}개 / {n_bytes:,}바이트)")
    return n_files, n_bytes


def _swap_file(dest, src, *, on=None):
    """파일형 목적지(osm `.pbf` 등)를 **파괴하지 않고** 교체한다 — `_swap_dir` 의 파일 짝.

    기존 `shutil.copy2(sp, dest)` 는 dest 를 먼저 truncate 하고 쓰기 때문에, 복사 도중
    죽거나 원본이 반쪽이면 dest 가 그 자리에서 깨진다. 형제 `.incoming` 에 먼저 받고
    크기 게이트를 통과한 뒤에만 `os.replace` 로 갈아끼워 그 창을 없앤다.

    `_swap_dir` 과 같은 계약 — **락을 절대 획득하지 않는다**(C-6). 상호배제는 호출자 몫.
    스테이징이 `dest.parent` 안이라 EXDEV 는 구조적으로 발생하지 않는다(C-7).

    반환: (1, n_bytes)
    실패: 스테이징만 삭제하고 예외 재전파. **dest 는 무손상.**
    """
    dest = pathlib.Path(dest); src = pathlib.Path(src)
    parent = dest.parent
    parent.mkdir(parents=True, exist_ok=True)
    staging = parent / (dest.name + _SWAP_INCOMING_SUFFIX)
    _rm_path(staging, ignore_errors=True)          # 이전 시도의 잔재
    _assert_same_device(staging, parent, dest)
    try:
        shutil.copy2(str(src), str(staging))
        n_bytes = staging.stat().st_size
        if n_bytes <= 0:
            raise RuntimeError(f"교체 거부 — 원본이 0바이트다: {src}")
    except BaseException:
        _rm_path(staging, ignore_errors=True)      # dest 는 아직 손도 대지 않았다
        raise
    _swap_replace(staging, dest)                   # 원자적 교체(같은 디렉터리 → EXDEV 불가)
    if on: on(f"   교체 완료: {dest.name} ({n_bytes:,}바이트)")
    return 1, n_bytes


def _vworld_cookie():
    """VWorld 로그인 세션 쿠키 — 환경변수 VWORLD_COOKIE 우선, 없으면 BUILD_HOME/.secrets/vworld_cookie.
    사용자가 브라우저 로그인 후 JSESSIONID(=...; ...) 를 붙여넣어 둔다(만료 시 재주입)."""
    ck = os.environ.get("VWORLD_COOKIE")
    if ck:
        return ck.strip()
    f = BUILD_HOME / ".secrets" / "vworld_cookie"
    try:
        if f.exists():
            return f.read_text(encoding="utf-8").strip()
    except OSError:
        pass
    return None


def _collect_headers(src):
    """수집 메서드별 추가 헤더 — vworld_session 이면 로그인 쿠키+Referer. 그 외 None."""
    if (src.get("collect") or {}).get("method") == "vworld_session":
        ck = _vworld_cookie()
        if not ck:
            raise RuntimeError("VWorld 세션 쿠키 없음 — Build Studio UI(🔑 PJSESSIONID·vworld) 또는 "
                               f"환경변수 VWORLD_COOKIE / {BUILD_HOME}/.secrets/vworld_cookie 설정")
        # Referer 는 해당 dsId 다운로드센터 페이지(src.url) — VWorld 가 Referer 검사함
        return {"Cookie": ck, "Referer": src.get("url") or "https://www.vworld.kr/dtmk/"}
    return None


def _vworld_list_filenos(ds, col):
    """VWorld 다운로드센터 목록(서버렌더 HTML)을 쿠키로 받아 listFnc.download('ds','fileNo','sizeKB') 에서
    fileNo 자동 추출 → file_nos 하드코딩 불필요(전국 시군구 + 갱신 대응).
    level=sigungu(기본, 시군구 단위 파일)|sido(시도 통합 1파일)|all. 파일명 'LSMD_CONT_LDREG_<시도>[_<시군구>].zip'."""
    cookie = _vworld_cookie()
    if not cookie:
        raise RuntimeError("VWorld 세션 쿠키 없음(목록 조회) — VWORLD_COOKIE 또는 .secrets/vworld_cookie")
    url = col.get("list_url") or (
        f"https://www.vworld.kr/dtmk/dtmk_ntads_s002.do?dsId={ds}&datIde={ds}&svcCde=MK&datPageSize=1000")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Cookie": cookie,
                                               "Referer": url})
    html = urllib.request.urlopen(req, timeout=60, context=ssl._create_unverified_context()).read().decode("utf-8", "replace")
    nos = _vworld_parse_filenos(html, col.get("level", "sigungu"))
    if not nos:   # 0개 = 원인 구분(다운로드 버튼 유무)으로 명확한 조치 안내
        has_btn = "listFnc.download" in html
        raise RuntimeError(
            "VWorld 목록 fileNo 0개 — " + (
                "다운로드 버튼은 있으나 level 필터 결과 0(level/페이지 구조 확인)" if has_btn else
                "목록 HTML에 다운로드 버튼 없음 → 세션 쿠키 만료(로그인 페이지) 또는 list_url 오류. "
                "Build Studio 에서 VWorld 쿠키(PJSESSIONID·vworld) 재입력 후 재시도")
            + f"  [url={url} · html={len(html)}B]")
    return nos


def _vworld_parse_filenos(html, level="sigungu"):
    """다운로드센터 HTML → 행정구역별 (dsFileId, fileNo) 쌍 리스트. (네트워크 분리 — 단위테스트 가능)
    항목 <li> 단위로 listFnc.download('<dsFileId>','<fileNo=dsFileSq>','<size>') 의 1·2번째 인자.
      다운로드는 ds_id=dsFileId(1번째)·fileNo=dsFileSq(2번째). 연속지적은 dsFileId 가 페이지 dsId(30563)와
      같지만 건물은 '20171128DS00010' 이라 페이지 dsId(18)로는 일반페이지가 응답됨 → 반드시 1번째 인자 사용.
    행정구역 = <span class="sigunguNm1">전체명(서울특별시·경기도 …) 우선, 없으면 <div class="tit min"> 파일명 템플릿.
    시도판별 = 전체시도명 접미(특별시/광역시/특별자치시/특별자치도/도) 또는 파일명 약어(시군구 분할 '_' 없음).
    ★갱신 누적: 한 데이터셋에 같은 행정구역의 구버전이 쌓이므로(건물=204건) 행정구역별 '최신 1건'(목록 상단=최신,
    첫 등장)만 취한다 — 결과적으로 시도 17건/시군구 N건."""
    out = []; seen = set()
    for blk in re.split(r'<li\b', html):              # 다운로드 항목 1개 = <li> 블록(목록은 최신순)
        mf = re.search(r"listFnc\.download\(\s*'([^']*)'\s*,\s*'(\d+)'", blk)
        if not mf:
            continue
        fid, fno = mf.group(1), mf.group(2)           # (dsFileId, fileNo=dsFileSq) — 다운로드는 ds_id=dsFileId 사용
        mr = (re.search(r'class="sigunguNm1">\s*([^<]+?)\s*</span>', blk)
              or re.search(r'class="tit min">\s*([^<]+?)\s*</div>', blk))
        rg = re.sub(r'\s+', ' ', mr.group(1)).strip() if mr else ""
        rg = re.sub(r'^.*?(?:LDREG_|AL_D\d+_)', '', rg)        # 파일명 템플릿 접두 제거(연속지적 등)
        rg = re.sub(r'\.zip$', '', rg, flags=re.I)
        rg = re.sub(r'_\d{6,8}$', '', rg).strip()             # 날짜 접미 제거
        is_sido = bool(re.match(r'^[가-힣]{2,}(특별시|광역시|특별자치시|특별자치도|도)$', rg)) \
                  or (rg != "" and "_" not in rg and not re.search(r'[시군구]$', rg))
        # 세종(단층 시도)은 시군구 하위파일이 없어 sigungu level 에서도 포함해야 누락 안 됨
        if level == "sigungu" and is_sido and "세종" not in rg: continue
        if level == "sido" and not is_sido: continue
        if rg in seen: continue                               # 같은 행정구역의 구버전(갱신 누적) 제거 — 최신 1건만
        seen.add(rg); out.append((fid, fno))
    return out


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
    if method == "direct":   # 무인증 직접 다운로드(민방위 대피시설 등). col.url → dest 추출/복사.
        return src, [col["url"]], BUILD_HOME / src.get("build_input", {}).get("dest", "staged/facility_src"), col.get("mode", "extract")
    if method == "datago_filedown":   # data.go.kr 2단계: selectFileDataDownload.do(메타) → fileDownload.do(파일)
        # 메타 파라미터: 상가=publicDataDetailPk(uddi), 경찰/소방=fileDetailSn. 둘 다 지원.
        q = f"?recommendDataYn=Y&publicDataPk={col.get('publicDataPk')}"
        if col.get("publicDataDetailPk"):
            q += f"&publicDataDetailPk={urllib.parse.quote(col['publicDataDetailPk'])}"
        if col.get("fileDetailSn"):
            q += f"&fileDetailSn={col['fileDetailSn']}"
        meta_url = col.get("detail_url", "https://www.data.go.kr/tcs/dss/selectFileDataDownload.do") + q
        req = urllib.request.Request(meta_url, headers={"User-Agent": "Mozilla/5.0", "Referer": src.get("url", "")})
        meta = json.loads(urllib.request.urlopen(req, timeout=30, context=ssl._create_unverified_context()).read().decode("utf-8", "replace"))
        if not (meta.get("status") and meta.get("atchFileId")):
            raise RuntimeError(f"data.go.kr 메타 조회 실패(atchFileId 없음) — PK={col.get('publicDataPk')} 변경/차단/IP 확인")
        dnm = (meta.get("dataSetFileDetailInfo") or {}).get("dataNm", skey)
        url = ("https://www.data.go.kr/cmm/cmm/fileDownload.do"
               + f"?atchFileId={meta['atchFileId']}&fileDetailSn={col.get('fileDetailSn', meta.get('fileDetailSn', '1'))}&dataNm={urllib.parse.quote(dnm)}")
        return src, [url], BUILD_HOME / src.get("build_input", {}).get("dest", "poi-all/sangga"), "extract"
    if method == "vworld_session":   # VWorld 다운로드센터 — 로그인 세션 쿠키로 downloadResourceFile.do GET
        base = col.get("download_url", "https://www.vworld.kr/dtmk/downloadResourceFile.do")
        ds = str(col.get("ds_id", ""))
        manual = [str(n) for n in (col.get("file_nos") or [])]
        # ds_id 파라미터 = listFnc.download 1번째 인자(dsFileId). 연속지적은 페이지 dsId(30563)와 같지만
        # 건물은 dsFileId('20171128DS00010')라 페이지 dsId(18)로는 일반페이지가 응답됨 → 첫 인자를 그대로 사용.
        if manual:
            specs = [(ds, n) for n in manual]                # 수동 fileNo 는 config ds_id 와 페어
        elif ds or col.get("list_url"):
            specs = _vworld_list_filenos(ds, col)            # [(dsFileId, fileNo), ...]
        else:
            specs = []
        if not specs:
            raise RuntimeError(f"vworld_session({skey}): 다운로드 항목 0개 — ds_id/list_url 또는 쿠키(목록 자동조회) 확인.")
        urls = [f"{base}?ds_id={urllib.parse.quote(a)}&fileNo={urllib.parse.quote(b)}" for a, b in specs]
        return src, urls, BUILD_HOME / src.get("build_input", {}).get("dest", "staged/gis"), "extract"
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
        total = len(selected); done = 0; changed = []; failed = 0
        for item_key in selected:
            try:
                src, urls, dest, mode = _collect_plan(item_key)
                hdrs = _collect_headers(src)   # vworld_session 이면 로그인 세션 쿠키 주입
                expected_n = len(urls)   # A2 동수 불변식 기준 — 루프 진입 전에 확정한다
                MGR._emit("collect", f"⇩ {item_key} 다운로드 ({expected_n}파일)…")
                shas = []
                if mode == "file":
                    destp = pathlib.Path(dest); destp.parent.mkdir(parents=True, exist_ok=True)
                    tmp = tmpdir / (item_key.replace(":", "_") + ".part")
                    sz = _http_download(urls[0], tmp, headers=hdrs)
                    _collect_integrity_gate(tmp, item_key, sz)   # 다운로드 직후·store_put 전 무결성 게이트
                    sha, reused = store_put(tmp); shas.append(sha)
                    _assert_full_receipt(item_key, shas, expected_n)   # 교체 전 마지막 관문
                    if not reused or not destp.exists():
                        # copy2 는 dest 를 먼저 truncate 한다 — 형제 .incoming 경유 원자 교체로 대체.
                        _swap_file(destp, store_path(sha), on=lambda m: MGR._emit("collect", f"  {m}"))
                    MGR._emit("collect", f"  {sz/1e6:.1f}MB sha={sha[:8]}{' (변경없음)' if reused else ' → 배치'}")
                else:
                    allreused = True
                    for i, u in enumerate(urls):
                        tmp = tmpdir / (item_key.replace(":", "_") + f"_{i}.part")
                        sz = _http_download(u, tmp, headers=hdrs)
                        _collect_integrity_gate(tmp, item_key, sz)   # 다운로드 직후·store_put 전 무결성 게이트(루프 내)
                        sha, reused = store_put(tmp); shas.append(sha); allreused = allreused and reused
                        MGR._emit("collect", f"  파일{i+1}/{expected_n} {sz/1e6:.1f}MB sha={sha[:8]}{' (변경없음)' if reused else ''}")
                    _assert_full_receipt(item_key, shas, expected_n)   # 교체 전 마지막 관문
                    if (not allreused) or (not _nonempty_dir(pathlib.Path(dest))):
                        # 예전엔 rmtree(dest) 를 **먼저** 때린 뒤 추출했다 — 추출물이 0건이면
                        # 그대로 빈 디렉터리가 남았다(이번 사고). 형제 .incoming 에 다 채운
                        # 뒤에만 갈아끼운다. 채우다 실패하면 dest 는 손도 대지 않는다.
                        def _fill(staging, _shas=list(shas)):
                            for s in _shas:
                                _extract_into(store_path(s), staging)
                        _swap_dir(dest, _fill, on=lambda m: MGR._emit("collect", f"  {m}"))
                cur_sha = ",".join(shas); prev = ver.get(item_key, {}).get("staged_sig")
                rec = ver.setdefault(item_key, {})
                rec["staged_sig"] = cur_sha; rec["current"] = time.strftime("%Y-%m-%d")
                rec["checked_at"] = time.strftime("%Y-%m-%d %H:%M")
                save_versions({item_key: rec})
                changed.append(item_key) if cur_sha != prev else None
                MGR._emit("collect", f"  {'✓ 갱신' if cur_sha != prev else '= 변경 없음'}: {item_key}")
                # save_versions 뒤에 둔다 — 그쪽이 _STATE_COLS 전량을 rec 값으로 덮어써
                # 먼저 쓰면 낡은 배지로 되돌아간다.
                _set_validation(item_key, "collect_ok",
                                f"{len(shas)}파일 수집 {'갱신' if cur_sha != prev else '변경 없음'}")
            except Exception as e:
                failed += 1
                MGR._emit("collect", f"  ✗ {item_key}: {str(e)[:140]}")
                # 실패를 상태 컬럼에 남겨 **낡은 `ok` 배지를 덮는다** — 남겨두면 UI 상
                # 정상으로 보여 소실이 무증상으로 굳는다(이번 사고의 고착 원인).
                try:
                    _set_validation(item_key, "collect_failed", str(e)[:200])
                except Exception as e2:   # 상태 기록 실패가 원래 원인을 가리지 않게
                    MGR._emit("collect", f"  ! 상태 기록 실패: {str(e2)[:100]}")
            done += 1
            MGR.jobs["collect"]["progress"] = done / max(total, 1)
            MGR.publish({"kind": "collect", "status": "running", "progress": done / max(total, 1)})
            time.sleep(6)   # 차단 회피 간격
        if 'osm' in changed:   # OSM 변경 시 변환(planetiler→korea.mbtiles, osm.sqlite, 동) 자동 enqueue
            bt = (next((s for s in load_sources() if s['key'] == 'osm'), {}).get('collect') or {}).get('build_targets') or []
            if bt:
                MGR._emit("collect", f"  ▶ OSM 변환 빌드 enqueue: {', '.join(bt)}")
                MGR.enqueue(bt)
        # A6: 예외 없이 끝나도 item 실패가 있으면 error 다. 예전엔 262건 전부 실패해도
        # done 이라 UI·SSE 어디에도 사고가 드러나지 않았다. 예외 이탈 시엔 여기 도달
        # 자체를 못 하므로 _collect_guarded 의 error 와 경합하지 않는다.
        st = "error" if failed else "done"
        MGR.jobs["collect"]["status"] = st
        MGR._emit("collect", f"실패 {failed}건 — 수집 종료(변경 {len(changed)}/{total})" if failed
                  else f"OK: 수집 완료 — 변경 {len(changed)}/{total}")
        MGR.publish({"kind": "collect", "status": st, "progress": 1.0})
        return changed


def _collect_guarded(selected):
    """run_collect 를 감싸 예외 시에도 'collect' 종료(error) 상태를 반드시 publish.
    안 그러면 클라이언트가 수집을 'running' 으로 오인해 빌드 버튼이 영구 비활성화된다."""
    try:
        run_collect(selected)
    except Exception as e:
        j = MGR.jobs.get("collect") or {"progress": 0.0, "log": [], "st": {}}
        j["status"] = "error"; MGR.jobs["collect"] = j
        try: MGR._emit("collect", f"[오류] 수집 중단: {str(e)[:160]}")
        except Exception: pass
        MGR.publish({"kind": "collect", "status": "error", "progress": j.get("progress", 0.0)})


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


class _UploadRejected(Exception):
    """업로드 거부 — `code` 는 그대로 HTTP 상태로 나간다."""

    def __init__(self, msg, code=400):
        super().__init__(msg)
        self.code = code


def _apply_upload(key, name, dest, tmp, written, nbytes):
    """수신이 끝난 임시파일을 **검증한 뒤에만** staged 로 적재한다(C-9).

    예전에는 게이트가 하나도 없었다. 연결이 끊겨 `written < nbytes` 여도 그대로
    `rmtree(dest)` → 반쪽 파일 추출 → 성공 응답이 나갔고, `_record_upload` 도 부르지
    않아 파일을 갈아끼워도 옛 `ok` 검증 배지가 남았다. 수집 경로만 고치면 같은 사고가
    이 통로로 그대로 재현된다.

    락은 **파괴 구간에만** 잡는다(C-11). 다GB 스트리밍과 무결성 검사를 락 안에 넣으면
    업로드가 도는 내내 수집이 통째로 막힌다. 수신 루프는 아예 이 함수 밖(핸들러)에 있고,
    여기서도 `store_put` 까지는 락 없이 진행한다. `_swap_dir`/`_swap_file` 은 락을 잡지
    않으므로(C-6) 획득 지점은 아래 한 곳뿐이다.

    반환: sha — 실패는 전부 `_UploadRejected`. **어느 실패 경로에서도 dest 는 무손상.**
    """
    if nbytes <= 0:
        raise _UploadRejected("본문 길이 없음 — 빈 업로드는 적재하지 않는다", 400)
    if written != nbytes:
        raise _UploadRejected(
            f"수신 불완전 — {written:,}/{nbytes:,}바이트만 도착했다. 기존 파일은 그대로 두었다", 400)
    try:
        _collect_integrity_gate(tmp, key, written)     # truncated/corrupt 면 tmp 폐기 후 raise
    except RuntimeError as e:
        raise _UploadRejected(f"업로드 무결성 거부: {str(e)[:120]}", 400) from e

    STORE_DIR.mkdir(parents=True, exist_ok=True)
    sha, _ = store_put(tmp)                            # 여기까지 dest 는 손도 대지 않았다
    sp = store_path(sha)

    if not _COLLECT_LOCK.acquire(timeout=_COLLECT_LOCK_WAIT):
        raise _UploadRejected("수집이 진행 중이라 적재할 수 없다 — 수집이 끝난 뒤 다시 시도하라", 409)
    try:
        if dest.suffix:                                # 파일형(osm .pbf)
            _swap_file(dest, sp)
        else:
            _swap_dir(dest, lambda staging: _extract_into(sp, staging))
        _record_upload(key, name, written)             # 이력 + 검증배지 pending 리셋
        # `_record_upload` 가 건드린 뒤에 읽어야 pending 이 살아남는다
        # (`validation_status` 도 `_STATE_COLS` 라 옛 스냅샷을 쓰면 방금 리셋한 배지를 되돌린다).
        ver = load_versions(); rec = ver.setdefault(key, {})
        rec["staged_sig"] = sha
        rec["current"] = time.strftime("%Y-%m-%d"); rec["file"] = name
        save_versions({key: rec})
    finally:
        _COLLECT_LOCK.release()
    return sha


def load_manifest(mid):
    """프로필 SHA들을 store 에서 staged 로 재구성 + DB 상태 복원 → 다음 빌드가 그 파일로.

    **원본이 store 에 없으면 dest 에 손도 대지 않는다**(C-8). 예전에는 `rmtree(dest)` 를
    먼저 하고 `sp.exists()` 를 나중에 봤기 때문에, store 가 빈 프로필 하나를 불러오는 것만으로
    staged 약 5.0GB 가 소리 없이 사라졌다. 게다가 서명 기록이 `if` 블록 **바깥**이라 복원하지도
    않은 키에 `staged_sig` 가 찍혔고, 다음 수집이 그 거짓 서명을 보고 "이미 최신"이라 판단해
    재수집조차 하지 않았다 — 소실이 영구화되는 경로다.

    반환: {"id", "name", "restored": n, "skipped": [{"key","reason","missing","total"}, …]}
    """
    p = MANIFESTS_DIR / f"{mid}.json"
    if not p.is_file():
        raise RuntimeError("프로필 없음")
    man = json.loads(p.read_text(encoding="utf-8"))
    restored = 0; skipped = []

    def _skip(key, reason, missing=0, total=0):
        skipped.append({"key": key, "reason": reason, "missing": missing, "total": total})

    # 수집과 동시에 돌면 서로의 staged 를 덮는다 — 파괴 구간을 락으로 감싼다(C-11).
    # `_swap_dir`/`_swap_file` 은 락을 잡지 않으므로(C-6) 여기가 유일한 획득 지점이다.
    if not _COLLECT_LOCK.acquire(timeout=_COLLECT_LOCK_WAIT):
        raise RuntimeError("수집이 진행 중이라 복원할 수 없다 — 수집이 끝난 뒤 다시 시도하라")
    try:
        ver = load_versions()
        for key, info in (man.get("sources") or {}).items():
            sha = (info or {}).get("sha"); dest = _item_dest(key)
            if not sha or not dest:
                _skip(key, "원본 서명 없음" if not sha else "알 수 없는 항목")
                continue
            shas = [s for s in str(sha).split(",") if s]
            miss = [s for s in shas if not store_path(s).exists()]
            if not shas or miss:
                # ① 선검증 — 파일형·디렉터리형 **양쪽** 모두. 하나라도 없으면 그 키는 통째로
                #    건너뛴다(반쪽 복원 금지). dest 는 이 시점까지 한 번도 건드리지 않았다.
                _skip(key, "store 에 원본 없음", missing=len(miss) or 1, total=len(shas))
                continue
            try:
                if dest.suffix:            # 파일형(osm .pbf) — `.incoming` 경유 원자적 교체
                    _swap_file(dest, store_path(shas[0]))
                else:                      # 디렉터리형 — `.incoming` 에 전량 추출 후 교체
                    def fill(staging, _shas=shas):
                        for s in _shas:
                            _extract_into(store_path(s), staging)
                    _swap_dir(dest, fill)
            except Exception as e:
                # ② 교체가 깨져도 dest 무손상이 두 프리미티브의 계약이다. 서명은 남기지 않는다.
                _skip(key, f"복원 실패: {str(e)[:80]}", missing=0, total=len(shas))
                continue
            restored += 1
            # ③ 서명·상태 기록은 **성공한 키에만**(옛 1578 을 `if` 안으로 들여쓴 자리).
            rec = ver.setdefault(key, {}); rec["staged_sig"] = sha
            rec["current"] = info.get("current"); rec["file"] = info.get("file")
            save_versions({key: rec})
    finally:
        _COLLECT_LOCK.release()
    return {"id": mid, "name": man.get("name"), "restored": restored, "skipped": skipped}


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
                status = _cmp_period(cur, lat, 6 if s["key"] == "building_db" else None)   # 건물DB=월 정밀도
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
        if self.path == "/api/secrets/vworld":   # VWorld 쿠키 설정여부만(값 미노출)
            return self._json({"set": bool(_vworld_cookie())})
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
            # 적재(prepare_sources)는 빌드 큐 앞에서 동기로 도는데, 큰 소스(navi .7z·building_db 2GB) 추출이
            # 무출력이라 멈춘 듯 보였음 → 진행을 SSE(kind=prepare)로 라이브 스트리밍(클라는 로그만 표시).
            def _pemit(line): MGR.publish({"kind": "prepare", "status": "running", "line": line})
            _pemit("⬆ 업로드 소스 적재 점검…")
            prep = prepare_sources(emit=_pemit)   # 업로드 데이터 적재(미업로드는 직전 재사용)
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
            threading.Thread(target=_collect_guarded, args=(sel,), daemon=True).start()
            return self._json({"ok": True, "started": sel})
        if self.path == "/api/secrets/vworld":   # VWorld 세션 쿠키 저장 — SSCSID·vworld 2값 → 'SSCSID=…; vworld=…'. repo 밖·gitignore.
            n = int(self.headers.get("Content-Length", "0"))
            if n > MAX_CTRL: return self._json({"error": "본문 과대"}, 413)
            b = json.loads(self.rfile.read(n) or "{}")
            ck = (b.get("cookie") or "").strip()   # 전체 Cookie 헤더 직접 입력도 허용(하위호환)
            if not ck:
                parts = []
                if (b.get("pjsessionid") or "").strip(): parts.append("PJSESSIONID=" + b["pjsessionid"].strip())
                if (b.get("vworld") or "").strip(): parts.append("vworld=" + b["vworld"].strip())
                ck = "; ".join(parts)
            sd = BUILD_HOME / ".secrets"; sd.mkdir(parents=True, exist_ok=True)
            f = sd / "vworld_cookie"
            if ck:
                f.write_text(ck, encoding="utf-8")
                try: os.chmod(f, 0o600)
                except OSError: pass
                return self._json({"ok": True, "set": True})
            f.unlink(missing_ok=True)   # 빈 값 = 삭제
            return self._json({"ok": True, "set": False})
        if self.path.startswith("/api/collect/upload"):   # 항목별 사용자 정의 파일 업로드 → staged 직접 적재
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            key = (q.get("key") or [""])[0]; name = _safe_relpath((q.get("name") or [""])[0])
            dest = _item_dest(key)
            if not dest: return self._json({"error": "알 수 없는 항목"}, 400)
            if not name: return self._json({"error": "파일명 누락"}, 400)
            tmpdir = BUILD_HOME / "tmp"; tmpdir.mkdir(parents=True, exist_ok=True)
            tmp = tmpdir / ("up_" + key.replace(":", "_") + "_" + name.replace("/", "_"))
            nbytes = int(self.headers.get("Content-Length", "0")); written = 0
            # 수신은 락 **밖**에서(C-11). 다GB 업로드가 도는 내내 수집이 막히면 안 된다.
            try:
                with open(tmp, "wb") as o:
                    rem = nbytes
                    while rem > 0:
                        ch = self.rfile.read(min(1 << 20, rem))
                        if not ch: break
                        o.write(ch); rem -= len(ch); written += len(ch)
            except Exception as e:
                _rm_path(tmp, ignore_errors=True)
                return self._json({"error": f"수신 실패: {str(e)[:120]}"}, 500)
            try:
                sha = _apply_upload(key, name, dest, tmp, written, nbytes)
            except _UploadRejected as e:
                _rm_path(tmp, ignore_errors=True)
                return self._json({"error": str(e), "size": written, "expected": nbytes}, e.code)
            except Exception as e:
                _rm_path(tmp, ignore_errors=True)
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
 .wrap>div{min-width:0}
 @media(max-width:1100px){.wrap{grid-template-columns:1fr}}
 .panel{background:var(--card);border:1px solid var(--bd);border-radius:12px;padding:14px 16px}
 .panel h2{font-size:13px;margin:0 0 10px;font-weight:600;color:var(--mut);text-transform:uppercase;letter-spacing:.04em}
 label.row{display:flex;align-items:center;gap:8px;padding:5px 0;cursor:pointer}
 .bar{height:7px;background:#0c1018;border-radius:99px;overflow:hidden;margin-top:6px}
 .bar>i{display:block;height:100%;width:0;background:var(--ac);transition:width .3s}
 .tcard{padding:11px 13px;border:1px solid var(--bd);border-radius:9px;margin-bottom:9px}
 .tcard .h{display:flex;justify-content:space-between;align-items:center;font-size:13px}
 .tcard .hr{display:flex;align-items:center;gap:7px}
 .retry{display:none;background:#1a2230;border:1px solid var(--bd);color:#cfe3ff;border-radius:6px;font-size:11px;padding:2px 8px;cursor:pointer}
 .retry:hover{border-color:var(--ac)} .retry:disabled{opacity:.45;cursor:default}
 .st{font-size:11px;padding:2px 8px;border-radius:99px;background:#0c1018;color:var(--mut)}
 .st.running{color:#7fd1ff} .st.done{color:#7ee0a0} .st.error{color:#ff8585} .st.queued{color:#d9c07a} .st.skipped{color:#9aa3ad} .st.fresh{color:#7ee0a0}
 .tb{display:inline-block;white-space:nowrap;font-size:10px;border-radius:6px;padding:1px 6px;margin-left:4px}
 .tb.fresh{color:#7ee0a0;background:#0e1a12} .tb.stale{color:#d9c07a;background:#1a160c} .tb.miss{color:#ff8585;background:#1a0e0e} .tb.always{color:#9aa3ad;background:#0c1018}
 button{background:var(--ac);color:#06121f;border:0;border-radius:8px;padding:9px 16px;font-weight:600;cursor:pointer}
 button.ghost{background:transparent;color:var(--tx);border:1px solid var(--bd)}
 button:disabled{opacity:.5;cursor:default}
 pre{background:#0a0e16;border:1px solid var(--bd);border-radius:9px;padding:10px;height:230px;
   overflow:auto;font:11px/1.45 ui-monospace,Menlo,monospace;color:#bcd;white-space:pre-wrap;overflow-wrap:anywhere;margin:10px 0 0}
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
     <button class=ghost id=dlSelBtn>⬇ 다운로드(내 PC로)</button></div>
   <div style="margin-top:12px;border-top:1px solid var(--bd);padding-top:10px;font-size:12px">
     <div style="color:var(--mut);margin-bottom:6px">🔑 VWorld 세션 쿠키 <span id=vwState class=mut></span></div>
     <div style="display:flex;gap:6px;flex-wrap:wrap">
       <input id=vwPjsess type=password placeholder="PJSESSIONID 값" style="flex:1;min-width:130px;background:#0c1018;border:1px solid var(--bd);border-radius:6px;color:var(--tx);padding:6px 9px;font-size:12px">
       <input id=vwVworld type=password placeholder="vworld 값" style="flex:1;min-width:130px;background:#0c1018;border:1px solid var(--bd);border-radius:6px;color:var(--tx);padding:6px 9px;font-size:12px">
       <button class=ghost id=vwSave>저장</button></div>
     <div class=mut style="font-size:10px;margin-top:5px">연속지적·건물(VWorld) 수집용. 로그인 후 DevTools→Application→Cookies→vworld.kr 에서 <b>PJSESSIONID</b>·<b>vworld</b> 값 복사. 만료 시 재입력.</div></div></div>
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
 el.innerHTML=`<div class=h><span>${label||kind}</span><span class=hr><button class=retry id=rt_${kind} onclick="retry('${kind}')" title="이 단계만 다시 실행 — 완료된 상위 단계는 재사용(건너뜀)">↻ 재시도</button><span class=st id=st_${kind}>대기</span></span></div><div class=bar><i id=bar_${kind}></i></div>`;
 $('#cards').appendChild(el);cards[kind]=el;bars[kind]=$('#bar_'+kind);sts[kind]=$('#st_'+kind);}
const LBL={};
function lbl(k){const t=TARGETS.find(x=>x.kind===k);return t?t.label:k}
const JOB={}; let optBuild=false, optCollect=false;   // JOB: kind→상태. opt*: 클릭 직후 SSE 도착 전 낙관적 비활성화
function buildBusy(){return optBuild||Object.keys(JOB).some(k=>k!=='collect'&&(JOB[k]==='queued'||JOB[k]==='running'));}
function collectBusy(){return optCollect||JOB['collect']==='queued'||JOB['collect']==='running';}
function anyBusy(){return buildBusy()||collectBusy();}   // 타겟 체크 초기화 게이팅(완전 idle 일 때만)
function updateBusy(){
 const bb=buildBusy(), cb=collectBusy(), any=bb||cb;
 const run=$('#run'); if(run){run.disabled=any; run.textContent=bb?'⏳ 빌드 중…':'빌드 시작';}   // '빌드 중…'은 실제 빌드 때만(수집 중엔 '빌드 시작'·비활성)
 const fa=$('#forceAll'); if(fa)fa.disabled=any;
 document.querySelectorAll('.retry').forEach(b=>{b.disabled=any;});   // 빌드/수집 중엔 재시도 비활성(중복 트리거 방지)
 const col=$('#collectBtn'); if(col){col.disabled=any; col.textContent=cb?'⏳ 진행 중…':'⬇ 자동수집 시작 (순차)';}
}
function setStatus(k,s,p){card(k,lbl(k));if(p!=null)bars[k].style.width=Math.round(p*100)+'%';
 if(s){JOB[k]=s;const m={queued:'대기',running:'진행중',done:'완료',error:'오류',skipped:'건너뜀',fresh:'↻ 최신(재사용)'};sts[k].textContent=m[s]||s;sts[k].className='st '+s;
   const rt=$('#rt_'+k); if(rt)rt.style.display=(s==='error'||s==='skipped')?'inline-block':'none';}}   // 실패/건너뜀 단계에만 재시도 노출
function logln(t){const p=$('#log');let s=p.textContent+t+'\n';const MAX=800,a=s.split('\n');if(a.length>MAX)s=a.slice(-MAX).join('\n');p.textContent=s;p.scrollTop=p.scrollHeight}   // DOM 줄 수 상한(MAX) — 노이즈 폭주 시 브라우저 정지 방지
const es=new EventSource('/api/events');
es.onmessage=e=>{const d=JSON.parse(e.data);
 if(d.snapshot){for(const k in d.snapshot)setStatus(k,d.snapshot[k].status,d.snapshot[k].progress);optBuild=optCollect=false;updateBusy();return}
 if(d.kind==='prepare'){if(d.line)logln('[적재] '+d.line);return;}   // 업로드 소스 적재 진행 — 로그만(카드·busy·optBuild 불변)
 setStatus(d.kind,d.status,d.progress); if(d.line)logln('['+d.kind+'] '+d.line);
 if(d.kind==='collect')optCollect=false; else optBuild=false;   // 실제 상태 도착 → 해당 낙관 플래그 해제
 updateBusy();
 if(d.status==='done'||d.status==='error'||d.status==='skipped'){loadBuilds();if(d.kind==='collect')loadCollect();else if(!anyBusy())refreshTargetsSoon();}};   // 타겟 체크 초기화는 전체 종료 후에만
function fmt(d){const x=String(d||'').replace(/\D/g,'');return x.length===8?`${x.slice(0,4)}-${x.slice(4,6)}-${x.slice(6,8)}`:x.length===6?`${x.slice(0,4)}-${x.slice(4,6)}`:(d||'−');}
function srcStatus(s){return s.status==='update'?'🔴 업데이트 있음':(s.status==='current'?'🟢 최신':'—');}
function vbadge(s){if(!s.uploadable)return '';
  // collect_* 2키: run_collect(A5)가 같은 축에 기록한다. 없으면 m[v]||v 로 떨어져
  // 아이콘·색 없는 회색 영문이 되는데, 가장 눈에 띄어야 할 수집 실패가 가장 안 띈다.
  const m={ok:'🟢 검증 OK',warn:'🟡 검증 경고',fail:'🔴 검증 실패',pending:'⏳ 검증 대기',
           collect_ok:'🟢 수집 OK',collect_failed:'🔴 수집 실패'},v=s.validation;
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
function esc(s){return String(s==null?'':s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}
function bf_bytes(n){return n>=1073741824?gb(n):n>=1048576?(n/1048576).toFixed(1)+'MB':(n/1024).toFixed(0)+'KB';}
function bfBadge(asof,latest,prec){if(!asof)return '<span class=mut>기준일 미상</span>';if(!latest)return '<span class=mut>—</span>';
  let a=String(asof).replace(/\D/g,''),b=String(latest).replace(/\D/g,'');if(prec){a=a.slice(0,prec);b=b.slice(0,prec);}
  const k=Math.min(a.length,b.length);
  if(!k)return '<span class=mut>—</span>';return b.slice(0,k)>a.slice(0,k)?'🔴 갱신본 있음':'🟢 최신';}
function toggleBF(key){const e=document.getElementById('bf_'+key),t=document.getElementById('bft_'+key);
  const o=e.style.display==='none';e.style.display=o?'':'none';if(t)t.textContent=(o?'▾':'▸')+' 파일 현황';
  if(o&&!e.dataset.loaded){e.dataset.loaded='1';loadBF(key);}}
function loadBF(key){const e=document.getElementById('bf_'+key);e.innerHTML='<span class=mut>불러오는 중…</span>';
  fetch('/api/sources/files?key='+encodeURIComponent(key)).then(r=>r.json()).then(d=>{
    if(d.error){e.innerHTML='<span class=mut>'+d.error+'</span>';return;}
    if(!d.total){e.innerHTML='<span class=mut>업로드된 파일 없음 — 폴더/파일을 끌어다 놓거나 ⬆ 로 올리세요</span>';return;}
    const pr=d.grouped?6:0;   // 건물DB(grouped)=월 정밀도 비교(일자 갱신일 drift 오판 방지)
    const ftab=fs=>`<table class=ftab><thead><tr><th>파일</th><th>기준일</th><th class=r>크기</th><th class=r>업로드</th><th>상태</th></tr></thead><tbody>${
      fs.map(f=>`<tr><td>📄 ${esc(f.file)}</td><td class=mut>${fmt(f.asof)}</td><td class="mut r">${bf_bytes(f.size)}</td><td class="mut r">${esc((f.uploaded_at||'').slice(0,16))}</td><td>${bfBadge(f.asof,d.latest,pr)}</td></tr>`).join('')}</tbody></table>`;
    const head=`<div class="mut bfhead">총 ${d.total}개 · 출처 갱신일 ${fmt(d.latest)}</div>`;
    const body=d.grouped?(d.regions||[]).map(rg=>`<div class=rgrp><div class=rh><b>${rg.name||'미분류'} <span class=mut>(${rg.n})</span></b><span class=mut>${fmt(rg.asof)} · ${bfBadge(rg.asof,d.latest,pr)}</span></div>${ftab(rg.files)}</div>`).join('')
      :ftab((d.regions[0]||{files:[]}).files);
    e.innerHTML=head+body;
  }).catch(err=>{e.innerHTML='<span class=mut>실패: '+err+'</span>';});}
function toggleKids(key){const e=document.getElementById('kids_'+key),c=document.getElementById('cr_'+key);if(e){const o=e.style.display==='none';e.style.display=o?'':'none';if(c)c.textContent=o?'⌄':'›';}}
function toggleAll(key,on){document.querySelectorAll('#kids_'+key+' input.ck').forEach(c=>c.checked=on);syncParent(key);}
function onKid(key){syncParent(key);}
function syncParent(key){const p=document.getElementById('pk_'+key);if(!p)return;const ks=[...document.querySelectorAll('#kids_'+key+' input.ck')];if(!ks.length)return;const on=ks.filter(c=>c.checked).length;p.checked=on===ks.length;p.indeterminate=on>0&&on<ks.length;}
function checkItem(key,a){if(a)a.textContent='…';fetch('/api/collect/check',{method:'POST',body:JSON.stringify({key})}).then(r=>r.json()).then(d=>{if(d.error)alert('최신 조회 실패: '+d.error);loadCollect();}).catch(e=>{alert('실패: '+e);loadCollect();});}
function dlItem(key){window.open('/api/collect/download?key='+encodeURIComponent(key),'_blank');}
// Safari/Firefox 는 DOM 에 연결되지 않은 file input 의 .click() 으로 파일 다이얼로그를 열지 않음 → body 에 잠깐 붙였다 제거.
function openFilePicker(multiple,dir,cb){const inp=document.createElement('input');inp.type='file';inp.multiple=!!multiple;
  if(dir)inp.webkitdirectory=true;inp.style.display='none';document.body.appendChild(inp);
  inp.onchange=()=>{const fs=[...inp.files];inp.remove();cb(fs);};
  window.addEventListener('focus',function h(){setTimeout(()=>{if(inp.isConnected&&!inp.files.length)inp.remove();},300);window.removeEventListener('focus',h);});  // 취소 시 정리
  inp.click();}
function upItem(key){openFilePicker(true,false,fs=>{if(!fs.length)return;key.includes(':')?upSub(key,fs):uploadAll(key,fs);});}
async function upSub(key,files){logln('⇧ '+key+' 업로드 '+files.length+'개…');
  for(const f of files){await new Promise(res=>{const x=new XMLHttpRequest();
    x.open('POST','/api/collect/upload?key='+encodeURIComponent(key)+'&name='+encodeURIComponent(f.name));
    x.onload=()=>{try{const d=JSON.parse(x.responseText);logln(d.error?'  ✗ '+d.error:'  ✓ '+f.name+' (sha '+d.sha+')');}catch(_){logln('  ✗ 응답오류 '+x.status);}res();};
    x.onerror=()=>{logln('  ✗ 네트워크');res();};x.send(f);});}
  loadCollect();}
function loadCollect(preserve=true){
  // 재렌더 전 체크상태 보존(업로드·검증 시 선택 초기화 방지). 프로필 로드/최초 렌더는 preserve=false 로 default_collect 적용.
  const prevChk={};if(preserve)document.querySelectorAll('#collect input.ck').forEach(c=>{if(c.value)prevChk[c.value]=c.checked;});
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
  logln('▶ 자동수집 '+sel.length+'건 순차 시작…'); optCollect=true; updateBusy();   // 즉시 수집-비활성화(SSE running 도착 전 중복 클릭 방지)
  fetch('/api/collect/start',{method:'POST',body:JSON.stringify({selected:sel})}).then(r=>r.json()).then(d=>{if(d.error){optCollect=false;updateBusy();return alert(d.error);}logln('  큐: '+(d.started||[]).join(', '));}).catch(e=>{optCollect=false;updateBusy();alert('수집 시작 실패: '+e);});}
function setVer(key,field){const v=prompt((field==='current'?'현재(빌드에 쓴)':'최신')+' 기준일 입력 — 예: 202605 또는 2026-06-19');
  if(v==null)return; fetch('/api/sources/version',{method:'POST',body:JSON.stringify({key,field,value:v})})
   .then(r=>r.json()).then(d=>{if(d.error)alert(d.error);loadCollect();});}
function checkLatest(key,a){if(a)a.textContent='조회중…';
  fetch('/api/sources/check',{method:'POST',body:JSON.stringify({key})}).then(r=>r.json()).then(d=>{
   if(d.error)alert('최신 조회 실패: '+d.error); loadCollect();}).catch(e=>{alert('실패: '+e);loadCollect();});}
function pickFiles(key,dir){openFilePicker(true,dir,fs=>{if(fs.length)uploadAll(key,fs);});}
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
  fetch('/api/profiles/load',{method:'POST',body:JSON.stringify({id})}).then(r=>r.json()).then(d=>{if(d.error)return alert(d.error);
    var sk=d.skipped||[];
    logln('↻ 프로필 불러옴: '+d.name+' (복원 '+d.restored+'건 / 건너뜀 '+sk.length+'건) — 갱신할 항목만 체크 후 빌드');
    if(sk.length){sk.forEach(function(s){logln('   ⚠ 건너뜀 '+s.key+': '+s.reason+(s.total?' ('+s.missing+'/'+s.total+' 누락)':''));});
      alert('건너뛴 항목 '+sk.length+'건 — store 에 원본이 없어 복원하지 못했습니다.\n기존 staged 파일은 그대로 두었습니다. 해당 항목은 다시 수집해야 합니다:\n\n'+sk.map(function(s){return '· '+s.key+' — '+s.reason;}).join('\n'));}
    loadCollect(false);});}
function delProfile(id){if(!confirm('프로필을 삭제할까요? (보관 번들 + 미참조 store 파일 정리)'))return;fetch('/api/profiles/delete',{method:'POST',body:JSON.stringify({id})}).then(r=>r.json()).then(d=>{if(d.gc_removed)logln('🗑 store 정리: '+d.gc_removed+'개 ('+gb(d.gc_freed||0)+' 회수)');loadBuilds();});}
function loadVw(){fetch('/api/secrets/vworld').then(r=>r.json()).then(d=>{const s=$('#vwState');if(s)s.textContent=d.set?'· 설정됨 ✓':'· 미설정';});}
function saveVw(){const j=$('#vwPjsess'),b=$('#vwVworld');fetch('/api/secrets/vworld',{method:'POST',body:JSON.stringify({pjsessionid:j.value,vworld:b.value})}).then(r=>r.json()).then(d=>{j.value='';b.value='';loadVw();logln(d.set?'🔑 VWorld 쿠키 저장됨':'🔑 VWorld 쿠키 삭제됨');}).catch(e=>alert('실패: '+e));}
loadCollect(); loadBuilds(); loadVw();
$('#collectBtn').onclick=startCollect; $('#dlSelBtn').onclick=()=>alert('선택 항목 내 PC 다운로드 — 다음 단계 연결 예정'); $('#vwSave').onclick=saveVw;
$('#forceAll').onclick=()=>{document.querySelectorAll('#checks input').forEach(c=>c.checked=true);logln('⟳ 전체 체크 — 최신 무시하고 강제 재빌드');};
function runBuild(t){
 const A={staged:'⬆ 새 데이터 적재',ok:'✓ 적재됨',reused:'↻ 직전 데이터 사용','missing':'⚠ 데이터 없음',partial:'⚠ 일부만 적재(오류)','no-tool':'⚠ 추출도구 없음',error:'✗ 오류'};
 fetch('/api/build',{method:'POST',body:JSON.stringify({targets:t})}).then(r=>r.json()).then(d=>{
   (d.prepared||[]).forEach(p=>logln('  📦 '+p.key+': '+(A[p.action]||p.action)+(p.n?' ('+p.n+'개)':'')+(p.msg?' — '+p.msg:'')));
   (d.fresh||[]).forEach(k=>logln('  ↻ '+lbl(k)+': 최신 — 건너뜀(재사용)'));
   logln('▶ 큐: '+((d.queued||[]).map(lbl).join(', ')||'없음 — 빌드할 변경 없음'));loadCollect();optBuild=false;updateBusy();}).catch(e=>{optBuild=false;updateBusy();logln('✗ 빌드 시작 실패: '+e);});}
// 빌드 시작 시엔 '빌드 대상' 체크박스를 건드리지 않는다(사용자 선택 유지). 타겟 목록 갱신(=fresh 자동 체크해제)은
// 모든 잡이 끝난 뒤(es.onmessage 의 !anyBusy())에만 1회 수행 → 진행 중 초기화/중간 재렌더 방지.
// 사전점검(소스 누락/검증) → 빌드 — #run(체크된 다수)·retry(단일 단계) 공용 경로
function triggerBuild(t){
 optBuild=true; updateBusy();   // 즉시 빌드-비활성화 — 사전점검·확인 대화 중 중복 클릭/타겟 체크 초기화 방지
 fetch('/api/build/check',{method:'POST',body:JSON.stringify({targets:t})}).then(r=>r.json()).then(c=>{
   const miss=c.missing||[],inval=c.invalid||[];
   if(miss.length||inval.length){
     let msg='';
     if(miss.length)msg+='⚠ 데이터가 없습니다:\n  · '+miss.map(m=>m.name).join('\n  · ')+'\n\n';
     if(inval.length)msg+='⚠ 검증 경고/실패:\n  · '+inval.map(m=>m.name+' ('+m.validation+')').join('\n  · ')+'\n\n';
     msg+='이대로 빌드를 진행할까요? (해당 데이터는 비거나 직전 분으로 빌드됩니다)';
     if(!confirm(msg)){logln('⏹ 빌드 취소 — 누락/오류: '+[...miss.map(m=>m.name),...inval.map(m=>m.name)].join(', '));optBuild=false;updateBusy();return;}
     logln('⚠ 누락 무시하고 진행: '+[...miss.map(m=>m.name),...inval.map(m=>m.name)].join(', '));
   }
   runBuild(t);
 }).catch(e=>{if(confirm('소스 사전점검 실패('+e+').\n그래도 빌드를 진행할까요?'))runBuild(t);else{optBuild=false;updateBusy();}});}
$('#run').onclick=()=>{const t=[...document.querySelectorAll('#checks input:checked')].map(x=>x.value);
 if(!t.length)return alert('빌드할 대상이 없습니다.\n(모두 최신이면, 다시 빌드할 항목을 체크하거나 [강제 재빌드(전체)]를 누르세요)');
 triggerBuild(t);};
// 단일 단계 재시도 — 그 단계만 명시 빌드(의존성은 fresh면 자동 건너뜀). 오류/건너뜀 카드의 [↻ 재시도] 버튼.
function retry(kind){if(anyBusy())return;logln('↻ 재시도: '+lbl(kind)+' (완료 단계는 재사용)');triggerBuild([kind]);}
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
