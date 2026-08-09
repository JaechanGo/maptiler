#!/usr/bin/env python3
"""PostGIS 백엔드 지오코딩/역지오코딩 API (Phase 5 — 5b shadow / 5c 전환).

server/geocode-api.py(SQLite FTS5+R-tree)의 **엔드포인트 계약·응답 형태·스코어링을 그대로 유지**하고
질의층만 PostGIS(pg_trgm·btree·GiST·ST_Contains)로 교체. 프론트(demo/js/search.js) 무변경.

데이터: load_geocode.py 가 geocode.sqlite→PostGIS address(+poi) 로 옮긴 것(좌표·dedup·파싱 그대로 재사용).
검색 인덱스: scripts/postgis/schema/11-address-search.sql.
연결: DATABASE_URL 또는 PG* 환경변수. GEOCODE_PORT(기본 8082).

전환 단계:
  5b(shadow): 별도 포트(8092)로 띄워 SQLite판(8082)과 병행, scripts/13d-geocode-parity.py 로 질의 parity 측정.
  5c(전환):  게이트웨이 /geocode·/reverse upstream 을 이 서비스로 교체.
"""
import json, math, os, re, sys, unicodedata
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

PORT = int(os.environ.get("GEOCODE_PORT", "8082"))
DSN = os.environ.get("DATABASE_URL") or (
    f"host={os.environ.get('PGHOST','localhost')} port={os.environ.get('PGPORT','5433')} "
    f"user={os.environ.get('PGUSER','cuvia')} dbname={os.environ.get('PGDATABASE','cuvia')} "
    f"password={os.environ.get('PGPASSWORD','cuvia')}")
ADDR_CAP = 400
# statement_timeout=3s — 폭주 쿼리 안전망(연결 단위 options, postgresql.conf 전역설정 회피).
# 정상 검색은 지역 trgm 인덱스(11-address-search.sql)로 전부 <1s 이므로 3s 는 비정상만 차단.
# plan_cache_mode=force_custom_plan — psycopg3 는 반복 실행 쿼리를 서버사이드 prepare 로 승격시키는데,
# generic plan 은 좌표값을 몰라 bbox(ST_Expand) 선택도를 1,627행으로 과소추정해 KNN Index Scan 대신
# Parallel Bitmap Heap Scan+Sort 를 고르고 3s 를 넘긴다. 값 기반 플랜 고정으로 KNN 을 보장한다.
POOL = ConnectionPool(DSN, min_size=1, max_size=8,
                      kwargs={"row_factory": dict_row,
                              "options": "-c statement_timeout=3000 -c plan_cache_mode=force_custom_plan"},
                      open=False)

TOKEN_RE = re.compile(r"[^\w가-힣]+", re.UNICODE)

# 시도 코드(emd_cd 앞2)→명칭 변형. 동명중복(전국 교동×18 등) 시 지역토큰으로 시도 좁힘에 사용.
SIDO_NM = {
    "11": ("서울",), "26": ("부산",), "27": ("대구",), "28": ("인천",), "29": ("광주",),
    "30": ("대전",), "31": ("울산",), "36": ("세종",), "41": ("경기",),
    "43": ("충북", "충청북"), "44": ("충남", "충청남"), "46": ("전남", "전라남"),
    "47": ("경북", "경상북"), "48": ("경남", "경상남"), "50": ("제주",),
    "51": ("강원",), "52": ("전북", "전라북"),
}

# 표기 단일출처(X1/§3). 시도코드(emd_cd 앞2)→정식명, 정식명→약칭 양방향.
# 17개 전수 + 특별자치 개편명(강원51/전북52 특별자치도, 세종36 특별자치시, 제주50 특별자치도).
# admin_boundary PIP 의 name 표기형(정식/약칭) 불일치 대비, 약칭 self-매핑도 포함해 변환누락 차단(F5).
SIDO_FULL = {
    "11": "서울특별시", "26": "부산광역시", "27": "대구광역시", "28": "인천광역시",
    "29": "광주광역시", "30": "대전광역시", "31": "울산광역시", "36": "세종특별자치시",
    "41": "경기도", "43": "충청북도", "44": "충청남도", "46": "전라남도",
    "47": "경상북도", "48": "경상남도", "50": "제주특별자치도",
    "51": "강원특별자치도", "52": "전북특별자치도",
}
SIDO_ABBR = {
    "서울특별시": "서울", "부산광역시": "부산", "대구광역시": "대구", "인천광역시": "인천",
    "광주광역시": "광주", "대전광역시": "대전", "울산광역시": "울산", "세종특별자치시": "세종",
    "경기도": "경기", "충청북도": "충북", "충청남도": "충남", "전라남도": "전남",
    "경상북도": "경북", "경상남도": "경남", "제주특별자치도": "제주",
    "강원특별자치도": "강원", "전북특별자치도": "전북",
}
# 약칭 self-매핑(약칭이 들어와도 그대로 통과) + 약칭→정식 역매핑(양방향 완비).
SIDO_ABBR.update({v: v for v in list(SIDO_ABBR.values())})
SIDO_FULL_BY_ABBR = {abbr: full for full, abbr in SIDO_ABBR.items() if full in SIDO_FULL.values()}

CONTRACT_VERSION = "geocode/2"            # 응답 분해 계약 버전(봉투 신호)
REQUIRED_TABLES = ["address", "parcel", "lawd_dong", "lawd_sigungu", "admin_boundary"]
# 리(里) 사전 lawd_ri 존재 여부 — 기동 시 1회 확정하는 모듈 전역(R-9 fail-open).
# REQUIRED_TABLES 에 넣지 않는다: 사전 미구축 상태에서도 /health 는 정상이어야 하고
# 리 필터만 조용히 비활성(현행 동작 유지)되어야 한다. Phase 1a 를 무해하게 만드는 장치.
_HAS_LAWD_RI = False
KIND_LABEL = {"station": "지하철역", "road": "도로", "place": "장소", "dong": "행정동", "poi": ""}
KNOWN_NONADDR = {"poi", "biz", "facility", "place", "dong", "road", "station"}


# ── 표시/응답 헬퍼 (geocode-api.py 와 동일 형태) ─────────────────────
def _g(r, k): return r.get(k)
def norm(s): return re.sub(r"\s+", " ", unicodedata.normalize("NFC", s or "")).strip()
def rnorm(s): return re.sub(r"[.\s]", "", unicodedata.normalize("NFC", s or ""))
def _limit(qs, dflt, cap=50):
    # limit 파라미터 안전 파싱: 비숫자('abc'·'3.5') 입력 시 ValueError 전파로 무응답 소켓끊김(crash) 방지.
    try: return min(max(int((qs.get("limit") or [str(dflt)])[0]), 1), cap)
    except (ValueError, TypeError): return dflt


# 지역토큰은 _s 가드 후 결합 — 결측 토큰 생략(display_of/_region 과 동일 계약).
# f-string 직결이면 시군구 없는 세종(적재가 빈칸을 NULL 로 넣음)에서 'None' 이 문자열로 박힌다.
# main_no=0 은 유효값이라 _s('0') 로 살아남고, sub_no/bld 결합 규칙은 종전대로.
def addr_str(r):
    s = " ".join(x for x in (_s(r["sido"]), _s(r["sigungu"]), _s(r["emd"]),
                             _s(r["road"]), _s(r["main_no"])) if x)
    if r.get("sub_no"): s += f'-{r["sub_no"]}'
    if r.get("bld"): s += f' ({r["bld"]})'
    return s


def road_str(r):
    s = " ".join(x for x in (_s(r["sido"]), _s(r["sigungu"]),
                             _s(r["road"]), _s(r["main_no"])) if x)
    if r.get("sub_no"): s += f'-{r["sub_no"]}'
    if r.get("bld"): s += f' ({r["bld"]})'
    return s


def parcel_str(r):
    jb = _s(_g(r, "jibun"))
    return " ".join(x for x in (_s(r["sido"]), _s(r["sigungu"]), jb or _s(r["emd"])) if x)


def addr_obj(r):
    # 기존 키(road/parcel/zipcode/bld/structure) 전부 보존. structure 에 분해계약 신규 필드 가산.
    # 동결: main_no/sub_no(도로명 건물본/부번, address_road_addr_idx 계약) — 의미·값 불변. bld_* 는 alias.
    jm, js, _san = parse_jibun_nums(_g(r, "jibun"))   # address.jibun best-effort(ji_main/ji_sub)
    return {
        "road": road_str(r), "parcel": parcel_str(r),
        "zipcode": _g(r, "postal") or "", "bld": _g(r, "bld") or "",
        "structure": {
            "sido": r["sido"], "sigungu": r["sigungu"], "emd": r["emd"],
            "haeng_dong": _g(r, "haeng_dong"),
            # B-1 반증: address.ri 는 실재하는 컬럼(10-base.sql). 미적재 DB 에서는 _g 가 None 을
            # 돌려주므로 현행과 동일. b_code 는 여기서 손대지 않는다(결정 A/R-1 — 끝 2자리 '00' 불변).
            "ri": _g(r, "ri"),
            "san": None,                              # F4: address 에 san 컬럼 없음 → null
            "road_name": _g(r, "road"),
            "main_no": _g(r, "main_no"), "sub_no": _g(r, "sub_no"),   # 동결
            "bld_main_no": _g(r, "main_no"), "bld_sub_no": _g(r, "sub_no"),  # alias
            "ji_main": jm, "ji_sub": js,
            "bld_name": _g(r, "bld"), "zipcode": _g(r, "postal"),
            "b_code": _g(r, "bcode"), "h_code": _g(r, "hcode"),
        },
    }


def category_of(r):
    c1 = _g(r, "cat1"); c2 = _g(r, "cat2"); sub = _g(r, "subtype")
    if c1:
        label = " > ".join(x for x in (c1, c2) if x)
        cat = {"primary": c1, "label": label,
               "group": c1, "path": ">".join(x for x in (c1, c2) if x)}  # group/path 가산
        if c2: cat["sub"] = c2                       # 보존(F15)
        return cat
    return {"label": sub} if sub else None


# ── 표기 분해계약 헬퍼 (X1) ──────────────────────────────────────
def pad_bcode(emd_cd):
    """emd_cd(char(8)) → b_code 10자리. btrim 후 len==8·digit 가드, 비정상 시 None(경고 호출측)."""
    if emd_cd is None: return None
    s = str(emd_cd).strip()                          # char(8) 공백패딩 btrim
    if len(s) != 8 or not s.isdigit(): return None   # 길이/숫자 가드 → 10자리 깨짐 차단
    return s + "00"


def parse_jibun_nums(jibun):
    """지번문자열 → (ji_main, ji_sub, san) best-effort. int4 가드(범위초과 시 None)."""
    if not jibun: return (None, None, False)
    s = str(jibun)
    san = bool(re.search(r"산\s*\d", s)) or "산" in re.split(r"[\s]+", s)
    m = re.search(r"(?:산\s*)?(\d+)(?:-(\d+))?", s)
    if not m: return (None, None, san)
    a, b = int(m.group(1)), int(m.group(2) or 0)
    if a > 99999 or b > 99999: return (None, None, san)   # 오버플로 예방(parse :104 가드 재사용)
    return (a, b, san)


def clean_jibun(jibun):
    """지목문자('답','대','전'…) 제거한 '본번[-부번]'(산 접두 보존)."""
    a, b, san = parse_jibun_nums(jibun)
    if a is None: return (str(jibun) if jibun else "").strip()
    pre = "산 " if san else ""
    return f"{pre}{a}-{b}" if b else f"{pre}{a}"


def _s(v):
    """None / 'None' 문자열 / 공백 가드 — 표기 결합용. 유효하면 trim 문자열, 아니면 None."""
    if v is None: return None
    v = str(v).strip()
    return None if v in ("", "None") else v


def _sido_abbr(sido):
    s = _s(sido)
    return SIDO_ABBR.get(s, s) if s else None


def _region(r, official=False, with_emd=False):
    """시도(약칭/정식) [시군구] [emd] 합성. 'None'/결측 토큰 생략(graceful)."""
    sido = _s(r.get("sido")); sigungu = _s(r.get("sigungu")); emd = _s(r.get("emd"))
    sido_disp = (sido if official else _sido_abbr(sido)) if sido else None
    parts = [p for p in (sido_disp, sigungu) if p]
    if with_emd and emd: parts.append(emd)
    return " ".join(parts) if parts else None


def display_of(item, r=None):
    """전 kind 일관 display{main,secondary,full}. 미정의 kind → main=name, secondary=null(§3.2).

    addr 내부 road↔parcel 판별은 subtype 이 아니라 r['road']/r['road_name'] 존재 여부(F3-a):
    address 테이블 도로명/두번째 지번경로는 subtype 미설정이므로 컬럼 존재로 판별해야 누락 없음.
    """
    r = r or {}
    kind = item.get("kind")
    name = _s(item.get("name")) or ""
    road = _s(r.get("road")) or _s(r.get("road_name"))

    if kind == "addr" and road:                      # 도로명 규칙
        main = road
        mno = r.get("main_no")
        if mno is not None and _s(mno):
            sub = r.get("sub_no")
            main += f" {mno}" + (f"-{sub}" if sub else "")   # sub_no=0 → '-0' 미부착
        bld = _s(r.get("bld"))
        if bld: main += f" ({bld})"
        secondary = _region(r, official=False, with_emd=True)
        zc = _s(r.get("postal"))
        if zc and secondary: secondary += f" ({zc})"
        tail = road
        if mno is not None and _s(mno):
            sub = r.get("sub_no"); tail += f" {mno}" + (f"-{sub}" if sub else "")
        if bld: tail += f" ({bld})"
        full = " ".join(x for x in (_region(r, official=True, with_emd=True), tail) if x) or name
        return {"main": main, "secondary": secondary, "full": full}

    if kind == "addr":                               # 지번 규칙(road 부재)
        emd = _s(r.get("emd"))
        jm, js = r.get("ji_main"), r.get("ji_sub")
        san = r.get("san")
        if jm is not None:
            jb = ("산 " if san else "") + f"{jm}" + (f"-{js}" if js else "")
        else:
            jb = clean_jibun(r.get("jibun")) or None
        main = " ".join(x for x in (emd, jb) if x) or name
        secondary = _region(r, official=False, with_emd=False)
        full = " ".join(x for x in (_region(r, official=True, with_emd=False), emd, jb) if x) or name
        return {"main": main, "secondary": secondary, "full": full}

    if kind in KNOWN_NONADDR:                         # 비-addr(라벨/카테고리 + PIP 지역)
        region = _region(r, official=False, with_emd=False)
        # poi/biz/facility=카테고리 경로 우선(§3.2). place/dong/road/station=유형라벨(KIND_LABEL).
        if kind in ("poi", "biz", "facility"):
            cat = item.get("category") or {}
            head = cat.get("path") or cat.get("label") or (KIND_LABEL.get(kind) or None)
        else:
            head = KIND_LABEL.get(kind) or None
        sec_parts = [x for x in (head, region) if x]
        secondary = " · ".join(sec_parts) if sec_parts else None
        full = name + (f" — {region}" if region else "")
        return {"main": name, "secondary": secondary, "full": full}

    return {"main": name, "secondary": None, "full": name}   # 미정의 kind fallback


def nonaddr_structure(r, pip=None):
    """비-addr structure — 자체 컬럼 우선, 결측 시 PIP(area_pip 결과) 병합. 소싱불가 필드 null."""
    pip = pip or {}
    sido = _g(r, "sido") or pip.get("sido")
    sigungu = _g(r, "sigungu") or pip.get("sigungu")
    emd = _g(r, "emd") or pip.get("emd")
    return {
        "structure": {
            "sido": sido, "sigungu": sigungu, "emd": emd,
            "haeng_dong": None, "ri": None, "san": None,
            "road_name": _g(r, "road"), "main_no": _g(r, "main_no"), "sub_no": _g(r, "sub_no"),
            "bld_main_no": _g(r, "main_no"), "bld_sub_no": _g(r, "sub_no"),
            "ji_main": None, "ji_sub": None,
            "bld_name": _g(r, "bld"), "zipcode": _g(r, "postal"),
            "b_code": _g(r, "bcode"), "h_code": _g(r, "hcode"),
        },
    }


def area_pip(cur, lon, lat):
    """좌표 → admin_boundary ST_Contains 로 sido/sigungu 취득(X6). GiST 가속·소수행.
    admin_boundary 0행/미적재 시 빈 dict(graceful). reverse areas(:아래) 패턴 재사용."""
    cur.execute(
        "SELECT level, name FROM admin_boundary "
        "WHERE ST_Contains(geom, ST_SetSRID(ST_MakePoint(%s,%s),4326)) "
        "AND level IN ('sido','sigungu','emd')", (lon, lat))
    out = {}
    for a in cur.fetchall():
        if a["level"] == "sido": out["sido"] = a["name"]
        elif a["level"] == "sigungu": out["sigungu"] = a["name"]
        # emd 레벨: nonaddr_structure() 가 이미 pip.get("emd") 를 읽는다(POI structure backfill).
        # admin_boundary 에 emd 경계가 없으면 행이 안 나와 현행과 동일(fail-open).
        elif a["level"] == "emd": out["emd"] = a["name"]
    return out


def _check_tables(cur):
    """REQUIRED_TABLES 중 부재 테이블 목록(to_regclass, public. 한정·파라미터 바인딩)."""
    cur.execute(
        "SELECT t FROM unnest(%s::text[]) t WHERE to_regclass('public.'||t) IS NULL",
        (REQUIRED_TABLES,))
    return [r["t"] for r in cur.fetchall()]


def _probe_lawd_ri(cur):
    """lawd_ri 존재 여부 1회 평가 → 전역 캐시(_HAS_LAWD_RI).

    REQUIRED_TABLES 에 넣지 않는다 — 사전 미구축 시 부팅 실패/degraded 를 막기 위함(R-9).
    점검 자체가 실패해도 False 로 떨어져 리 필터가 비활성될 뿐 현행 동작을 해치지 않는다.
    """
    global _HAS_LAWD_RI
    try:
        cur.execute("SELECT to_regclass('public.lawd_ri') IS NOT NULL AS ok")
        _HAS_LAWD_RI = bool(cur.fetchone()["ok"])
    except Exception:
        _HAS_LAWD_RI = False                 # fail-open
    return _HAS_LAWD_RI


def parse(q):
    q = re.sub(r"(?<=\d)\.(?=\d)", "", norm(q))
    house = road = dong = ri = bld_dong = zipcode = None; san = False; terms = []
    for t in re.split(r"[\s,]+", q):
        if not t: continue
        if t == "산": san = True; continue                  # 단독 '산'(임야) 표기
        if zipcode is None and re.fullmatch(r"\d{5}", t):    # 5자리 = 신우편번호 후보(번지보다 우선; 도로/동 동반 시 아래서 번지로 승격)
            zipcode = t; continue
        m = re.fullmatch(r"(산)?(\d+)(?:-(\d+))?", t)        # 번지: '산12-3'·'산12'·'12-3'·'5'(산 접두 허용)
        if m:
            if m.group(1): san = True
            a, b = int(m.group(2)), int(m.group(3) or 0)
            if house is None and a <= 99999 and b <= 99999:  # 첫 유효 번지만 채택 + int4 범위 가드(오버플로 500 예방)
                house = (a, b)
            continue
        # 도로명+번지가 공백없이 붙은 토큰('7나길9'·'테헤란로152'·'과천대로7나길9') → 도로(로/길끝) + 끝번지 분리.
        # fullmatch+greedy 라 '로/길로 끝나는 부분 + 순수 끝번지'가 토큰 전체를 덮을 때만 매칭(단지명 오인 차단).
        mr = re.fullmatch(r"(.+(?:로|길))(\d+)(?:-(\d+))?", t)
        if mr:
            road = (road or "") + rnorm(mr.group(1))
            a, b = int(mr.group(2)), int(mr.group(3) or 0)
            if house is None and a <= 99999 and b <= 99999:
                house = (a, b)
            continue
        # 복합 도로명(상위 '○○대로/로' + 하위 'N길/N나길/N번길')을 띄어 입력하면 두 토큰으로 쪼개진다.
        # 덮어쓰면 마지막 '7나길'만 남아 0건 → 누적 연결로 '과천대로7나길'(정식 road_norm) 복원.
        if re.search(r"(로|길)$", t): road = (road or "") + rnorm(t); continue
        ct = re.sub(r"[^\w가-힣]", "", t)
        if not ct: continue
        # 아파트 동번호('2105동'·'101동' = 숫자+동)는 법정동이 아니라 건물 동(棟) → bld 검색축(address.bld) 단서.
        # terms/dong 에서 분리: terms 에 남으면 이름경로 다중토큰 AND 가 POI 0건을 유발(POI엔 동번호 없음)하기 때문.
        if bld_dong is None and re.fullmatch(r"\d+동", ct):
            bld_dong = ct
            continue
        # 법정동/리/읍/면/'N가'(종로1가 등) 토큰 → 지번경로 분기 단서(terms 에도 남겨 지역가산·이름검색에 활용)
        # ※ 읍/면 누락 시 농촌(읍·면) 지번질의 전면 0건이 되므로 반드시 포함. 숫자+동(아파트)은 위에서 이미 분리됨.
        if dong is None and len(ct) >= 2 and (re.search(r"(동|리|읍|면)$", ct) or re.search(r"\d가$", ct)):
            dong = ct
        # 리(里) 토큰은 dong(읍·면·동)이 이미 잡힌 뒤에만 채택한다(R-6 게이팅).
        # 이 게이팅이 없으면 '양촌리'·'투다리' 같은 상호 단독 질의가 리 사전 조회를 유발해
        # POI 경로를 가로챈다(실측 상호 충돌 20,078건). dong 의 의미·우선순위는 불변.
        elif ri is None and dong is not None and ct != dong and len(ct) >= 2 and ct.endswith("리"):
            ri = ct
        terms.append(ct)
    # 5자리 숫자가 도로/법정동과 함께면 우편번호가 아니라 번지(예 '○○로 10524') → house 로 승격.
    if zipcode is not None and house is None and (road or dong):
        house = (int(zipcode), 0); zipcode = None
    return {"road": road, "house": house, "terms": terms, "dong": dong, "ri": ri, "san": san,
            "bld_dong": bld_dong, "zipcode": zipcode}


# ── 좌표 → 최근접 도로명주소(역지오코딩/주소부착) ──────────────────
def addr_at(cur, lon, lat):
    # geom && ST_Expand 는 Index Cond 로 내려가 KNN 주행범위를 묶는다(geography 캐스트한
    # ST_DWithin 은 Filter 로만 걸려 반경 내 0건이면 인덱스 전체를 훑고 statement_timeout).
    # 0.035°는 2.5km 의 경도 상한(위도 38.7°에서 0.0288°) 초과분 — 정확도는 ST_DWithin 이 보장.
    cur.execute(
        """SELECT * FROM address
           WHERE kind='addr' AND geom IS NOT NULL
             AND geom && ST_Expand(ST_SetSRID(ST_MakePoint(%s,%s),4326), 0.035)
             AND ST_DWithin(geom::geography, ST_SetSRID(ST_MakePoint(%s,%s),4326)::geography, 2500)
           ORDER BY geom <-> ST_SetSRID(ST_MakePoint(%s,%s),4326) LIMIT 1""",
        (lon, lat, lon, lat, lon, lat))
    r = cur.fetchone()
    return addr_obj(r) if r else None


def geocode(cur, q, limit):
    p = parse(q); results = []

    # ---- 우편번호 경로 (5자리 신우편번호 → postal 정확매칭, address_postal_idx) ----
    if p["zipcode"]:
        cur.execute("SELECT *, ST_X(geom) AS lon, ST_Y(geom) AS lat FROM address "
                    f"WHERE kind='addr' AND postal=%s ORDER BY sigungu, emd, id LIMIT {ADDR_CAP}",
                    (p["zipcode"],))
        for r in cur.fetchall():
            it = {"name": addr_str(r), "kind": "addr",
                  "lon": r["lon"], "lat": r["lat"], "address": addr_obj(r)}
            it["display"] = display_of(it, r)
            results.append((180, it))

    # ---- 주소 경로 ----
    if p["road"]:
        rn = p["road"]; terms = p["terms"]; h = p["house"]
        def rb(r):
            return 12 * sum(1 for t in terms
                            if t in (r["sido"], r["sigungu"], r["emd"], r.get("haeng_dong") or "")
                            or t in (r["sigungu"] or ""))
        def fetch(extra, nums):
            cur.execute(f"SELECT *, ST_X(geom) AS lon, ST_Y(geom) AS lat FROM address "
                        f"WHERE kind='addr' AND road_norm=%s{extra} LIMIT {ADDR_CAP}", (rn, *nums))
            return cur.fetchall()
        cand = []
        if h:
            r1 = fetch(" AND main_no=%s AND sub_no=%s", (h[0], h[1]))
            if r1:
                cand = [(200 + rb(r), r) for r in r1]
            else:
                r2 = fetch(" AND main_no=%s", (h[0],))
                if r2:
                    cand = [(150 + rb(r), r) for r in r2]
        if not cand:
            cand = [(110 + rb(r), r) for r in fetch("", ())]
        for s, r in cand:
            it = {"name": addr_str(r), "kind": "addr",
                  "lon": r["lon"], "lat": r["lat"], "address": addr_obj(r)}
            it["display"] = display_of(it, r)
            results.append((s, it))

    # ---- 지번 경로 (parcel 테이블 — 법정동명 + 번지 정확매칭) ----
    # 권위 지번 소스(연속지적도 39.6M). 동명을 lawd_dong 으로 emd_cd 정확등가(=) 해소 → parcel(emd_cd,ji_main,ji_sub)
    # 인덱스 정확매칭 → 대표점 ST_PointOnSurface(geom). 동명 정확등가라 2자 동명 Seq Scan 없음. san(임야) 토큰 동반시 가산.
    if not results and p["dong"] and p["house"]:
        h = p["house"]
        cur.execute("SELECT emd_cd FROM lawd_dong WHERE emd = %s", (p["dong"],))
        cds = [r["emd_cd"] for r in cur.fetchall()]
        # 동명중복 좁힘: 지역토큰(시/도)이 특정 시도를 가리키면 그 시도로 한정(엉뚱한 타시도 결과 혼입 방지).
        # 시군구 단위 좁힘은 시군구명 사전 부재로 미적용(후속) — 시도 한정만으로 광역시·도접두 질의 대부분 해소.
        # 리 토큰은 지역토큰(시/도·시군구)이 아니다 → 제외하지 않으면 '청평리'가 sigungu_nm LIKE 에
        # 걸려 엉뚱한 시군구로 좁혀지거나 SIDO_NM 접두매칭 노이즈가 된다.
        region = [t for t in p["terms"] if t != p["dong"] and t != p.get("ri")]
        # 동명중복 좁힘 — 시군구(가장 specific) 우선, 없으면 시도. 지정 지역에 해당 동이 없으면 0건(타지역 동명 혼입 차단).
        # 시군구: lawd_sigungu(내비DB 추출 254개, '수원시 영통구' 형식). 지역토큰이 시군구명 단어와 일치하면 그 시군구(emd_cd 앞5)로 한정.
        sgg_hit = set()
        if region:
            conds = " OR ".join(["(sigungu_nm LIKE %s || '%%' OR sigungu_nm LIKE '%% ' || %s)"] * len(region))
            sa = []
            for t in region:
                sa += [t, t]
            cur.execute("SELECT sigungu_cd FROM lawd_sigungu WHERE " + conds, sa)
            sgg_hit = {r["sigungu_cd"] for r in cur.fetchall()}
        if sgg_hit:
            cds = [c for c in cds if c[:5] in sgg_hit]
        else:
            sido_hit = {code for t in region for code, names in SIDO_NM.items()
                        if any(t.startswith(n) for n in names)}
            if sido_hit:
                cds = [c for c in cds if c[:2] in sido_hit]
        # 리(里) 좁힘 — 시도/시군구 좁힘 뒤, sido_cds 산출 전에 수행한다(cds 를 재정의하므로 순서 고정).
        # R-6 게이팅: p["ri"] 는 dong 이 먼저 잡힌 질의에서만 채워진다(parse()).
        # R-9 fail-open: 사전 미구축(_HAS_LAWD_RI=False) 또는 사전에 없는 리명이면 아무것도 하지 않고
        #                현행 동작(리 무시)을 그대로 유지한다 — 0건 회귀를 만들지 않는다.
        ri_emds = ri_cds = None
        if p.get("ri") and cds and _HAS_LAWD_RI:
            cur.execute(
                "SELECT emd_cd, ri_cd FROM lawd_ri "
                "WHERE ri = %s AND emd_cd = ANY(%s::char(8)[])", (p["ri"], cds))
            pairs = [(r["emd_cd"], r["ri_cd"]) for r in cur.fetchall()]
            if pairs:
                ri_emds = [a for a, _ in pairs]
                ri_cds = [b for _, b in pairs]
                cds = sorted(set(ri_emds))
        if cds:
            sido_cds = list({c[:2] for c in cds})
            # ※ char 캐스팅 필수: char(2)/char(8) 컬럼에 text 배열을 그냥 ANY 하면 파티션 pruning·
            #    parcel_jibun_lookup 인덱스가 무력화돼 전 파티션 Seq Scan(전국 39.6M 시 치명적). 캐스팅하면 parcel_<sido> 1파티션 Index Scan.
            # 좌표: 매칭(보통 1행) 후 대표점. geom_pt(materialized) 있으면 사용, 없으면 즉석 ST_PointOnSurface
            # — 전국 39.6M geom_pt 일괄백필(수시간) 없이도 동작(매칭 소수행만 계산하므로 사실상 무비용).
            # JOIN lawd_dong 으로 sido/sigungu/emd 권위 복원(입력토큰 p["dong"] 금지).
            # 공유 컬럼 emd_cd 가 parcel·lawd_dong 양쪽 존재 → 비수식 참조는 "ambiguous" 쿼리실패.
            # WHERE/ORDER BY 공유 컬럼을 모두 parcel. 로 수식(컬럼 수식 의무, F2).
            # char(2)/char(8) 캐스팅·ji_main/ji_sub 정확매칭·좌표식 COALESCE(geom_pt, ST_PointOnSurface) 보존(F9).
            sql = ("SELECT parcel.jibun AS jibun, parcel.emd_cd AS emd_cd, "
                   "parcel.ji_main AS ji_main, parcel.ji_sub AS ji_sub, parcel.san AS san, "
                   "ld.sido AS sido, ld.sigungu AS sigungu, ld.emd AS emd, "
                   "ST_X(COALESCE(parcel.geom_pt, ST_PointOnSurface(parcel.geom))) AS lon, "
                   "ST_Y(COALESCE(parcel.geom_pt, ST_PointOnSurface(parcel.geom))) AS lat "
                   "FROM parcel JOIN lawd_dong ld ON ld.emd_cd = parcel.emd_cd "
                   "WHERE parcel.sido_cd = ANY(%s::char(2)[]) AND parcel.emd_cd = ANY(%s::char(8)[]) "
                   "AND parcel.ji_main = %s AND parcel.ji_sub = %s")
            args = [sido_cds, cds, h[0], h[1]]
            if ri_cds:
                # R-5: parcel.pnu 는 NOT NULL 이 아니다 → NULL 안전(모르는 필지를 탈락시키지 않는다).
                #      (emd_cd, ri_cd) 를 페어로 묶어 교차조합 오매칭을 차단한다
                #      (emd_cd IN (...) AND ri_cd IN (...) 는 A동+B리 조합을 통과시킨다).
                # pnu 19자리 = 법정동코드10 + 대지구분1 + 본번4 + 부번4 이므로 9~10번째 2자리가 리코드.
                sql += (" AND (parcel.pnu IS NULL OR "
                        "(parcel.emd_cd, substr(parcel.pnu,9,2)::char(2)) IN "
                        "(SELECT e, r FROM unnest(%s::char(8)[], %s::char(2)[]) AS t(e, r)))")
                args += [ri_emds, ri_cds]
            if p["san"]:
                sql += " AND parcel.san = 1"
            sql += f" ORDER BY parcel.emd_cd, parcel.ji_sub LIMIT {ADDR_CAP}"
            cur.execute(sql, args)
            for r in cur.fetchall():
                san_b = bool(r["san"])
                # ri 는 리 필터가 실제로 걸린 경우에만 채운다. 필터가 없으면 이 행이 그 리인지
                # 알 수 없으므로 None 유지(현행) — 입력토큰을 확인 없이 되돌려주지 않는다.
                st = {"sido": r["sido"], "sigungu": r["sigungu"], "emd": r["emd"],
                      "haeng_dong": None, "ri": (p.get("ri") if ri_cds else None), "san": san_b,
                      "road_name": None, "main_no": None, "sub_no": None,
                      "bld_main_no": None, "bld_sub_no": None,
                      "ji_main": r["ji_main"], "ji_sub": r["ji_sub"],
                      "bld_name": None, "zipcode": None,
                      "b_code": pad_bcode(r["emd_cd"]), "h_code": None}
                it = {"name": None, "kind": "addr", "subtype": "parcel", "source": "parcel",
                      "lon": r["lon"], "lat": r["lat"]}
                disp = display_of(it, r)               # parcel 규칙(road 부재) — 지목제거·지역복원
                it["name"] = disp["full"]              # name=display.full(정식, 하위호환 alias)
                it["display"] = disp
                it["address"] = {"road": None, "parcel": disp["full"], "zipcode": None,
                                 "bld": None, "structure": st}
                results.append((200, it))

    # ---- 지번 경로 (법정동/리 + 번지) ----
    # 도로명이 없고 동/리 토큰 + 번지가 있으면 지번주소. addr 행의 search_text 끝(= jibun '법정동 [산] 본번[-부번]')을
    # 동명 부분일치 + 번지 끝고정으로 매칭(둘 다 search_text trgm GIN 가속). 본번만이면 정확본번·부번동반 둘 다 회수.
    # 법정동명은 전국 중복(역삼동·중앙동…)이므로 동 외 지역토큰(시군구/시도)으로 좁히고 가산, 정확본번 우선 결정적 정렬.
    if not results and p["dong"] and p["house"]:
        dong = p["dong"]; h = p["house"]; sep = "산 " if p["san"] else " "
        region = [t for t in p["terms"] if t != dong]
        exact = f"%{sep}{h[0]}-{h[1]}" if h[1] else f"%{sep}{h[0]}"
        if h[1]:
            num_conds = "search_text ILIKE %s"; nums = [exact]
        else:
            num_conds = "(search_text ILIKE %s OR search_text ILIKE %s)"
            nums = [exact, f"%{sep}{h[0]}-%"]
        reg_sql = ""; reg_args = []
        for t in region:
            reg_sql += " AND (sigungu ILIKE %s OR sido ILIKE %s)"
            reg_args += [f"%{t}%", f"%{t}%"]
        cur.execute(
            "SELECT *, ST_X(geom) AS lon, ST_Y(geom) AS lat FROM address "
            f"WHERE kind='addr' AND search_text ILIKE %s AND {num_conds}{reg_sql} "
            f"ORDER BY (search_text ILIKE %s) DESC, sigungu, emd, id LIMIT {ADDR_CAP}",
            (f"%{dong}%", *nums, *reg_args, exact))
        for r in cur.fetchall():
            bonus = 12 * sum(1 for t in region if t in (r["sigungu"] or "") or t in (r["sido"] or ""))
            it = {"name": addr_str(r), "kind": "addr",
                  "lon": r["lon"], "lat": r["lat"], "address": addr_obj(r)}
            it["display"] = display_of(it, r)          # road/road_name 부재 → parcel 규칙(F3-a/b)
            results.append((200 + bonus, it))

    # 도로/지번 경로가 결과를 못 내면 이름+건물명 경로 진입(둘은 병합). 미리 캡처해 bld 경로가
    # results 를 채워도 이름 경로(POI/역)가 함께 돌도록(네이버/카카오식 '주소+장소' 병합).
    name_path = not results

    # ---- 건물명 경로 (address.bld — 아파트 단지명+동(棟) 주소) ----
    # bld 컬럼엔 '다정한마을 2105동(경남아너스빌)' 류 단지명+동(棟)이 30만건. 이 텍스트는 search_text(name+road+jibun)
    # 에 미포함이고 이름 경로는 kind<>'addr' 라 닿지 않으므로 전용 경로 필요. 모든 bld ILIKE 는 address_bld_trgm
    # GIN(연속 ≥3자 trigram) 가속. 동번호(bld_dong) 있으면 정확 AND(점수↑), 없으면 단지명만(일반확장).
    if name_path and p["terms"]:
        bd = p["bld_dong"]
        # 법정동(dong) 토큰은 bld(단지명+동(棟))에 없음(행정구역은 emd/sigungu 별도 컬럼) → bld 매칭에서 빼고
        # 지역 좁힘(emd 정확매칭)에만 사용. 안 그러면 '도곡동 타워팰리스 101동' 처럼 동을 앞에 붙일 때 AND 0건.
        bld_terms = [t for t in p["terms"] if t != p["dong"]]
        # ≥3자(trgm 가능) 토큰이 1개 이상일 때만 진입 — 2자 단독('%XX%')은 trigram 0개라 1570만행 Seq Scan.
        anchors = [t for t in bld_terms if len(t) >= 3]
        if bld_terms and (anchors or (bd and len(bd) >= 3)):
            def bld_fetch(use_dong):
                conds = ["bld ILIKE %s"] * len(bld_terms); a = [f"%{t}%" for t in bld_terms]
                if use_dong and bd:
                    conds.append("bld ILIKE %s"); a.append(f"%{bd}%")
                if p["dong"]:                          # 법정동 동반 시 emd 정확매칭(동명 단지 혼입 방지·지역 좁힘)
                    conds.append("emd = %s"); a.append(p["dong"])
                cur.execute("SELECT *, ST_X(geom) AS lon, ST_Y(geom) AS lat FROM address "
                            "WHERE kind='addr' AND geom IS NOT NULL AND " + " AND ".join(conds) +
                            f" ORDER BY bld LIMIT {ADDR_CAP}", a)
                return cur.fetchall()
            rows = bld_fetch(use_dong=bool(bd)); score = 195 if bd else 150
            if not rows and bd:                        # ③ fallback: 정확 동 0건 → 동 빼고 단지명만(단지라도 반환)
                rows = bld_fetch(use_dong=False); score = 150
            for r in rows:
                it = {"name": addr_str(r), "kind": "addr",
                      "lon": r["lon"], "lat": r["lat"], "address": addr_obj(r)}
                it["display"] = display_of(it, r)      # road 있으면 도로명 규칙(건물명 노출), 없으면 지번 규칙
                results.append((score, it))

    # ---- 이름 경로 (역/지명/POI/건물명) ----
    if p["terms"] and name_path:
        base = {"station": 175, "place": 165, "dong": 160, "poi": 140, "biz": 135, "road": 120, "addr": 130}
        nq = norm(q); multi = len(p["terms"]) >= 2
        # 각 토큰: name 부분일치(단일=prefix, 다중=지역도 허용)
        conds = []; args = []
        for t in p["terms"]:
            if multi:   # search_text(이름+도로명+지번, trgm 인덱스) + 지역 토큰
                conds.append("(search_text ILIKE %s OR bld ILIKE %s OR sido ILIKE %s OR sigungu ILIKE %s OR emd ILIKE %s)")
                args += [f"%{t}%", f"%{t}%", f"%{t}%", f"%{t}%", f"%{t}%"]
            else:       # 단일 토큰 = search_text/건물명. 3자+만 중간검색('%t%'), 2자↓는 prefix('t%')
                # — pg_trgm GIN 은 연속 3글자(trigram)가 있어야 인덱스를 탄다. 2자 infix '%서울%' 는 trigram 0개라
                #   1570만행 Seq Scan(2~11초). 2자↓는 인덱스 타는 prefix 로 유지(기존 동작, 회귀 없음).
                pat = f"%{t}%" if len(t) >= 3 else f"{t}%"
                conds.append("(search_text ILIKE %s OR bld ILIKE %s)")
                args += [pat, pat]
        sql = ("SELECT *, ST_X(geom) AS lon, ST_Y(geom) AS lat FROM address "
               "WHERE kind <> 'addr' AND geom IS NOT NULL AND " + " AND ".join(conds) +
               f" LIMIT {ADDR_CAP}")
        cur.execute(sql, args)
        for r in cur.fetchall():
            disp = addr_str(r) if r["kind"] == "addr" else r["name"]
            s = base.get(r["kind"], 100) + (30 if (r.get("name") or "") == nq else 0)
            item = {"name": disp, "kind": r["kind"], "subtype": _g(r, "subtype"),
                    "lon": r["lon"], "lat": r["lat"]}
            cat = category_of(r)
            if cat: item["category"] = cat
            if _g(r, "phone"): item["phone"] = r["phone"]
            if _g(r, "source"): item["source"] = r["source"]
            results.append((s, item))   # display/structure 는 병합부에서(상위 limit개만, PIP 호출 절약)

    # ---- 병합·정렬·중복 제거 ----
    results.sort(key=lambda x: -x[0])
    out = []; seen = set()
    for s, item in results:
        if item["lon"] is None or item["lat"] is None:
            continue
        k = (item["name"], round(item["lon"], 5), round(item["lat"], 5))
        if k in seen: continue
        seen.add(k); out.append(item)
        if len(out) >= limit: break
    for it in out:
        if it["kind"] != "addr":
            # X6: 비-addr 자체 지역(OSM None) → admin_boundary PIP. 0행/미적재면 빈결과 → 지역 생략(graceful).
            pip = area_pip(cur, it["lon"], it["lat"])
            near = addr_at(cur, it["lon"], it["lat"]) or {}
            # address: 인근 도로명주소(road/parcel/zipcode/bld, 부착 유지) + structure(자체 행정정보=PIP).
            it["address"] = {
                "road": near.get("road"), "parcel": near.get("parcel"),
                "zipcode": near.get("zipcode", ""), "bld": near.get("bld", ""),
                "structure": nonaddr_structure(it, pip)["structure"],
            }
            it["display"] = display_of(it, pip)
    return out


def reverse(cur, lon, lat, limit):
    address = addr_at(cur, lon, lat)
    pt = "ST_SetSRID(ST_MakePoint(%s,%s),4326)"
    # addr_at 과 동일한 이유로 bbox 선행(0.25° > 20km 의 경도 상한 0.2299°).
    cur.execute(
        f"""SELECT *, ST_X(geom) AS lon, ST_Y(geom) AS lat,
                   ST_Distance(geom::geography, {pt}::geography) AS d FROM address
            WHERE geom IS NOT NULL
              AND geom && ST_Expand({pt}, 0.25)
              AND ST_DWithin(geom::geography, {pt}::geography, 20000)
            ORDER BY geom <-> {pt} LIMIT %s""",
        (lon, lat, lon, lat, lon, lat, lon, lat, limit))
    nearest = []
    for r in cur.fetchall():
        nm = addr_str(r) if r["kind"] == "addr" else r["name"]
        item = {"name": nm, "kind": r["kind"], "subtype": _g(r, "subtype"),
                "lon": r["lon"], "lat": r["lat"], "dist_m": round(r["d"], 1)}
        cat = category_of(r)
        if cat: item["category"] = cat
        # 분해계약 부착(geocode 와 동일 헬퍼). addr=자체컬럼, 비-addr=PIP(admin 0행이면 graceful).
        if r["kind"] == "addr":
            item["address"] = addr_obj(r)
            item["display"] = display_of(item, r)
        else:
            pip = area_pip(cur, r["lon"], r["lat"])
            item["address"] = nonaddr_structure(r, pip)   # {"structure": {...}} (자체/PIP)
            item["display"] = display_of(item, pip)
        nearest.append(item)
    cur.execute(
        f"""SELECT name, level AS type, code FROM admin_boundary
            WHERE ST_Contains(geom, {pt})""", (lon, lat))
    areas = [{"name": a["name"], "type": a["type"], "code": a["code"]} for a in cur.fetchall()]
    return {"address": address, "nearest": nearest, "areas": areas}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def _send(self, obj, code=200):
        if getattr(self, "_sent", False):     # 중복 send 가드("이미 송신됨"일 때만 무시)
            return
        self._sent = True
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path); qs = parse_qs(u.query)
        try:
            with POOL.connection() as con, con.cursor() as cur:
                if u.path == "/health":
                    # 필수테이블 점검 → 누락 시 degraded(503). 정상이면 기존 키(ok/places/areas) 보존.
                    missing = _check_tables(cur)
                    if missing:
                        return self._send({"ok": False, "degraded": True,
                                           "missing_tables": missing}, 503)
                    cur.execute("SELECT count(*) c FROM address"); pc = cur.fetchone()["c"]
                    cur.execute("SELECT count(*) c FROM admin_boundary"); ac = cur.fetchone()["c"]
                    return self._send({"ok": True, "places": pc, "areas": ac})
                if u.path == "/geocode":
                    q = (qs.get("q") or [""])[0]
                    limit = _limit(qs, 8)
                    return self._send({"query": q, "contract_version": CONTRACT_VERSION,
                                       "results": geocode(cur, q, limit)})
                if u.path == "/reverse":
                    try:
                        lon = float((qs.get("lon") or [""])[0]); lat = float((qs.get("lat") or [""])[0])
                    except ValueError:
                        return self._send({"error": "lon/lat 필요"}, 400)
                    limit = _limit(qs, 6)
                    return self._send({"lon": lon, "lat": lat, "contract_version": CONTRACT_VERSION,
                                       **reverse(cur, lon, lat, limit)})
                return self._send({"error": "not found",
                                   "endpoints": ["/geocode?q=", "/reverse?lon=&lat=", "/health"]}, 404)
        # C4: 좁은→넓은 순서. OperationalError(503, 연결/가용성)는 ProgrammingError 의 형제이므로 먼저.
        # ProgrammingError(UndefinedTable/Column 등)·기타 psycopg.Error·일반 Exception 은 500 JSON 으로
        # 봉인(빈바디/소켓끊김 방지). 입력 echo 금지, 메시지 절단(str[:120]).
        except psycopg.OperationalError as e:
            return self._send({"error": f"PostGIS 연결 실패: {str(e)[:120]}"}, 503)
        except psycopg.ProgrammingError as e:
            return self._send({"error": f"질의 처리 오류: {str(e)[:120]}"}, 500)
        except psycopg.Error as e:
            return self._send({"error": f"DB 오류: {str(e)[:120]}"}, 500)
        except Exception as e:
            return self._send({"error": f"내부 오류: {str(e)[:120]}"}, 500)


def _selftest():
    POOL.open()
    with POOL.connection() as con, con.cursor() as cur:
        for q in sys.argv[2:] or ["화성시 만세구 3.1만세로 5-3", "강남구 테헤란로 152", "강남역"]:
            print(f"\nQ: {q}")
            for r in geocode(cur, q, 4):
                print(f"   [{r['kind']}] {r['name']} → {r['lon']},{r['lat']}")


def _boot_check():
    """부팅 1회 필수테이블 점검 — 누락 시 stderr 경고만(프로세스 계속, fatal 금지)."""
    try:
        with POOL.connection() as con, con.cursor() as cur:
            missing = _check_tables(cur)
            has_ri = _probe_lawd_ri(cur)     # R-9: 필수테이블 아님 — 존재 여부만 1회 확정
        print(f"geocode-api-pg: lawd_ri: {'present' if has_ri else 'absent'}", file=sys.stderr)
        if missing:
            print(f"geocode-api-pg: WARNING degraded — missing tables: {', '.join(missing)}",
                  file=sys.stderr)
    except Exception as e:                  # 점검 자체 실패도 비치명(경고만)
        print(f"geocode-api-pg: WARNING table check failed: {str(e)[:120]}", file=sys.stderr)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "selftest":
        _selftest(); sys.exit(0)
    POOL.open()
    _boot_check()
    print(f"geocode-api-pg: DSN set, PORT={PORT}", file=sys.stderr)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
