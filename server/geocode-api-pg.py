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
# "12"(전남광주통합특별시)는 **DB 에 존재하지 않는 코드**다 — 안 A 는 응답 경계에서만 치환하므로
# emd_cd/bcode 는 46/29 로 남는다. 여기 등록하는 이유는 사용자가 신 시도명으로 검색했을 때
# 토큰을 인식하기 위함이고, 실제 좁힘 직전에 SIDO_ALIAS_CODES 로 46·29 로 되돌린다(§B-3).
SIDO_NM = {
    # "12"(전남광주) 항목은 여기 적지 않는다 — 약칭 문자열 단일정의점(SIDO_ABBR)이 아직 정의되기
    # 전이라, 여기 리터럴을 또 쓰면 약칭이 두 곳에 존재하게 된다. 아래 SIDO_MERGED_ABBR 확정 직후
    # SIDO_NM[SIDO_MERGED_CODE] 로 파생 주입한다(§B-3).
    "11": ("서울",), "26": ("부산",), "27": ("대구",), "28": ("인천",),
    "29": ("광주",), "30": ("대전",), "31": ("울산",), "36": ("세종",), "41": ("경기",),
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
    # 12: 전남광주통합특별시(46+29 통합). 안 A 응답 치환용 — DB 에는 이 코드가 없다.
    "12": "전남광주통합특별시",
}
SIDO_ABBR = {
    "서울특별시": "서울", "부산광역시": "부산", "대구광역시": "대구", "인천광역시": "인천",
    "광주광역시": "광주", "대전광역시": "대전", "울산광역시": "울산", "세종특별자치시": "세종",
    "경기도": "경기", "충청북도": "충북", "충청남도": "충남", "전라남도": "전남",
    "경상북도": "경북", "경상남도": "경남", "제주특별자치도": "제주",
    "강원특별자치도": "강원", "전북특별자치도": "전북",
    # 통합 시도 약칭 단일정의점(§B-3). 이 한 줄이 SIDO_ABBR(정식→약칭)·아래 SIDO_FULL_BY_ABBR
    # (약칭→정식) 양방향을 동시에 성립시킨다 — 약칭을 다른 곳에 또 적지 말 것.
    "전남광주통합특별시": "전남광주",
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

# ── T018 안 A: 전남광주통합특별시(12) 응답 경계 치환 ─────────────────────────────
# ★★★ 영구 불일치 경고 — 다음 사람이 반드시 읽을 것 ★★★
#   안 A 는 **DB 를 바꾸지 않는다**. address.bcode / parcel.emd_cd / parcel.pnu / lawd_dong.emd_cd /
#   admin_boundary.code 는 앞으로도 계속 **46(전라남도) · 29(광주광역시)** 다.
#   12(전남광주통합특별시) 로 보이는 것은 **오직 이 파일이 응답을 만들 때 치환한 결과**뿐이다.
#   → "DB 가 이미 12 체계"라고 오해하고 SQL 을 짜면 전부 0건이 된다. WHERE 절에는 46/29 를 써야 한다.
#   → 이 불일치는 안 A 가 존재하는 한 영구적이다. 근본 해소는 안 B(파티션 포함 전면 remap)뿐이며,
#     안 B 태스크는 Master 가 별도로 생성한다. 관련 문서: docs/ri-dict-runbook.md,
#     scripts/postgis/build_sido_remap.sql, lawd_ri.exist 의 COMMENT ON COLUMN.
#   → 안 A 적용 후에도 areas[] 는 **두 코드체계가 섞인 배열**이다: type='emd' 는 치환돼 12 로 나가지만
#     type='adm_dong' 은 통계청 행정동코드라 치환 대상이 아니며 그 체계의 29 는 **세종**이다.
SIDO_MERGED_CODE = "12"
SIDO_MERGED_NEW = SIDO_FULL[SIDO_MERGED_CODE]              # '전남광주통합특별시'(단일정의점)
SIDO_MERGED_ABBR = SIDO_ABBR[SIDO_MERGED_NEW]              # 약칭 단일정의점(SIDO_ABBR)에서 파생
# 검색 입력 토큰 인식표에도 신 약칭을 넣는다 — 리터럴 재기입이 아니라 파생이다(§B-3 단일정의점).
SIDO_NM[SIDO_MERGED_CODE] = (SIDO_MERGED_ABBR,)
SIDO_MERGED_OLD = ("전라남도", "광주광역시")               # DB 실측값(address.sido / lawd_dong.sido 전수)
SIDO_MERGED_OLD_ABBR = tuple(SIDO_ABBR[x] for x in SIDO_MERGED_OLD)   # ('전남','광주')
SIDO_MERGED_OLD_CODES = ("46", "29")
# 신 시도코드 → DB 실제 코드. SIDO_NM 이 '12' 를 물어와도 좁힘 직전에 46·29 로 되돌린다(§B-3).
SIDO_ALIAS_CODES = {SIDO_MERGED_CODE: SIDO_MERGED_OLD_CODES}

# 치환표(기동 1회 적재). 미적재 시 전면 비활성 = 현행 46/29 응답 유지(fail-open).
# **부분 치환 금지**: 코드만 12 이고 표기는 '전라남도'(또는 그 반대)인 상태가 아무것도 안 한 것보다 나쁘다.
# 그래서 표기 치환도 코드 치환과 같은 _HAS_SIDO_REMAP 한 개의 스위치에 묶는다.
_SIDO_REMAP = {}          # old_emd8(8자리) → new_emd8(8자리), 623행
_RI_REMAP_EXC = {}        # old_bcode(10자리) → new_bcode(10자리), 리코드까지 재배정되는 4건
_HAS_SIDO_REMAP = False
# A-5 관측용: 리를 보유한 읍면동 emd_cd 집합 + 리 미확정 누적 카운터(Phase 1 잔여 2.16% 추적).
_RI_EMDS = frozenset()
_RI_UNRESOLVED = 0


# ── 표시/응답 헬퍼 (geocode-api.py 와 동일 형태) ─────────────────────
def _g(r, k): return r.get(k)
def norm(s): return re.sub(r"\s+", " ", unicodedata.normalize("NFC", s or "")).strip()
def rnorm(s): return re.sub(r"[.\s]", "", unicodedata.normalize("NFC", s or ""))
def _limit(qs, dflt, cap=50):
    # limit 파라미터 안전 파싱: 비숫자('abc'·'3.5') 입력 시 ValueError 전파로 무응답 소켓끊김(crash) 방지.
    try: return min(max(int((qs.get("limit") or [str(dflt)])[0]), 1), cap)
    except (ValueError, TypeError): return dflt


# ── 안 A 치환 함수 (응답 경계 전용) ───────────────────────────────────
# ※ 위 'T018 안 A' 블록의 영구 불일치 경고를 먼저 읽을 것.
#   DB 는 46/29, 응답만 12. 이 함수들은 **응답을 만드는 순간에만** 호출한다.
#   WHERE 절·조인·필터에 12 를 넣으면 안 된다(0건).
def remap_bcode(code):
    """법정동코드 46/29 → 12 치환. 8자리(읍면동)·10자리(리 포함) 모두 처리.

    매핑표 미적재(_HAS_SIDO_REMAP=False)거나 대상이 아니면 **원값 객체를 그대로** 돌려준다
    (문자열 trim 조차 하지 않는다 — 비대상 경로의 기존 값 형태를 건드리지 않기 위함).
    10자리는 리 코드 재배정 예외(4건)를 먼저 보고, 없으면 앞 8자리만 치환하고 뒤 2자리는 보존한다.
    매핑표에 없는 46/29 코드는 치환하지 않는다(fail-open) — 없는 코드를 지어내지 않는다.
    """
    if not _HAS_SIDO_REMAP or code is None:
        return code
    s = str(code).strip()                                  # char(n) 공백패딩 btrim
    if len(s) not in (8, 10) or not s.isdigit() or s[:2] not in SIDO_MERGED_OLD_CODES:
        return code
    if len(s) == 10:
        n = _RI_REMAP_EXC.get(s)                           # 리코드까지 바뀌는 예외 우선
        if n: return n
        e = _SIDO_REMAP.get(s[:8])
        return (e + s[8:]) if e else code
    return _SIDO_REMAP.get(s) or code


def remap_sido_name(sido):
    """시도 표기 '전라남도'/'광주광역시'(및 약칭) → 통합 표기. 대상 아니면 원값 그대로.

    코드 치환과 **같은 스위치**(_HAS_SIDO_REMAP)에 묶는다 — 표기만 바뀌고 코드가 안 바뀌거나
    그 반대인 상태는 사용자·후속 대조 스크립트 모두에게 지금보다 나쁘다.
    """
    if not _HAS_SIDO_REMAP or sido is None:
        return sido
    s = str(sido).strip()
    if s in SIDO_MERGED_OLD: return SIDO_MERGED_NEW
    if s in SIDO_MERGED_OLD_ABBR: return SIDO_MERGED_ABBR
    return sido


def sido_input_alias(t):
    """검색 입력 역치환(§B-3) — 신 시도명 토큰 → [원토큰, 구 표기…] 후보 목록.

    안 A 는 응답만 12 로 바꾸므로, 사용자가 그 응답을 그대로 검색창에 다시 넣으면(round-trip)
    DB 에 없는 문자열이라 0건이 된다. 여기서 구 표기로 되돌려 양방향을 성립시킨다.
    원토큰을 항상 첫 원소로 남긴다 — 상호·지명이 우연히 '전남광주…'로 시작해도 죽이지 않는다.
    구 표기 토큰('전남'·'전라남도'·'광주')은 확장 없이 그대로 통과 → 기존 질의 회귀 없음.
    _HAS_SIDO_REMAP 에 묶지 않는다: 입력 관용은 치환표 유무와 무관하게 해로울 게 없다.
    """
    if not t: return (t,)
    return (t,) + SIDO_MERGED_OLD if t.startswith(SIDO_MERGED_ABBR) else (t,)


def _region_cond(cols, t):
    """지역토큰 t 를 cols 각각에 ILIKE 매칭하는 (SQL 조각, 인자) 쌍. 신 시도명은 구 표기로 확장.

    반환 조각은 항상 괄호로 감싼 OR — 호출측이 ' AND ' 로 이어붙여도 의미가 깨지지 않는다.
    conds/args 는 동일한 (별칭, 컬럼) 순회 순서로 만들어야 바인딩이 어긋나지 않는다.
    """
    alts = sido_input_alias(t)
    conds = [f"{c} ILIKE %s" for a in alts for c in cols]
    args = [f"%{a}%" for a in alts for _ in cols]
    return "(" + " OR ".join(conds) + ")", args


def _note_ri_unresolved(b_code, ri, where):
    """A-5 관측 가드 — 리 보유 읍면동인데 결과의 리가 미확정이면 stderr 경고(응답 불변).

    Phase 1 리 백필의 잔여 실패분(148,818행 / 2.16%)이 실사용에서 얼마나 노출되는지 추적한다.
    로그 폭주 방지를 위해 1·2·4·8… 번째 발생에서만 출력하고 총계는 카운터로 보존한다.
    """
    global _RI_UNRESOLVED
    if not _RI_EMDS or ri or b_code is None: return
    s = str(b_code).strip()
    if len(s) != 10 or not s.isdigit() or s[8:] != "00": return
    if s[:8] not in _RI_EMDS: return                       # 리 없는 읍면동 → '00' 이 정답
    _RI_UNRESOLVED += 1
    n = _RI_UNRESOLVED
    if n & (n - 1) == 0:                                   # 2의 거듭제곱 회차만 출력
        print(f"geocode-api-pg: NOTE 리 미확정 b_code={s} ({where}) 누적={n}", file=sys.stderr)


# 지역토큰은 _s 가드 후 결합 — 결측 토큰 생략(display_of/_region 과 동일 계약).
# f-string 직결이면 시군구 없는 세종(적재가 빈칸을 NULL 로 넣음)에서 'None' 이 문자열로 박힌다.
# main_no=0 은 유효값이라 _s('0') 로 살아남고, sub_no/bld 결합 규칙은 종전대로.
def addr_str(r):
    # 시도명은 응답 경계에서만 통합 표기로 치환(안 A). r["sido"] 자체(DB 값)는 건드리지 않는다.
    s = " ".join(x for x in (_s(remap_sido_name(r["sido"])), _s(r["sigungu"]), _s(r["emd"]),
                             _s(r["road"]), _s(r["main_no"])) if x)
    if r.get("sub_no"): s += f'-{r["sub_no"]}'
    if r.get("bld"): s += f' ({r["bld"]})'
    return s


def road_str(r):
    # 도로명주소 표기 규정 — 동(洞) 지역은 읍면동을 표기하지 않지만 읍·면 지역은 읍·면을 표기한다.
    # address.emd 는 읍/면/동을 한 칸에 담으므로 접미사로 분기한다: 무조건 빼면 읍·면에서 누락되고
    # (VWorld 대조 149건 중 도로명 불일치 44건의 37건이 이 원인), 무조건 넣으면 동에서 과잉이 된다.
    # 표시용 addr_str 이 emd 를 항상 넣는 것과는 계약이 다르므로 두 함수를 합치지 않는다.
    emd = _s(_g(r, "emd"))
    if emd and not emd.endswith(("읍", "면")): emd = None
    s = " ".join(x for x in (_s(remap_sido_name(r["sido"])), _s(r["sigungu"]), emd,
                             _s(r["road"]), _s(r["main_no"])) if x)
    if r.get("sub_no"): s += f'-{r["sub_no"]}'
    if r.get("bld"): s += f' ({r["bld"]})'
    return s


def parcel_str(r):
    jb = _s(_g(r, "jibun"))
    return " ".join(x for x in (_s(remap_sido_name(r["sido"])), _s(r["sigungu"]),
                                jb or _s(r["emd"])) if x)


# addr_obj().structure 의 키 집합. T019 의 PIP 단독 응답(최근접 포인트는 없고 필지만 있는
# 좌표)도 이 형을 그대로 채워야 소비자가 분기 없이 읽는다. 아래 addr_obj 와 어긋나면
# TestReverseSchemaStable 이 잡는다 — 값이 아니라 **키 집합**이 계약이다.
ADDR_STRUCT_KEYS = ("sido", "sigungu", "emd", "haeng_dong", "ri", "san",
                    "road_name", "main_no", "sub_no", "bld_main_no", "bld_sub_no",
                    "ji_main", "ji_sub", "bld_name", "zipcode", "b_code", "h_code")


def addr_obj(r):
    # 기존 키(road/parcel/zipcode/bld/structure) 전부 보존. structure 에 분해계약 신규 필드 가산.
    # 동결: main_no/sub_no(도로명 건물본/부번, address_road_addr_idx 계약) — 의미·값 불변. bld_* 는 alias.
    jm, js, _san = parse_jibun_nums(_g(r, "jibun"))   # address.jibun best-effort(ji_main/ji_sub)
    _bc = _g(r, "bcode")
    _note_ri_unresolved(_bc, _g(r, "ri"), "addr")      # A-5: 치환 전 원값(46/29 기준)으로 관측
    return {
        "road": road_str(r), "parcel": parcel_str(r),
        "zipcode": _g(r, "postal") or "", "bld": _g(r, "bld") or "",
        "structure": {
            "sido": remap_sido_name(r["sido"]), "sigungu": r["sigungu"], "emd": r["emd"],
            "haeng_dong": _g(r, "haeng_dong"),
            # B-1 반증: address.ri 는 실재하는 컬럼(10-base.sql). 미적재 DB 에서는 _g 가 None 을
            # 돌려주므로 현행과 동일. b_code 는 여기서 손대지 않는다(값은 address.bcode 그대로).
            #   ※ T018 Phase 1(2026-08-10) 이후 끝 2자리는 '00' 고정이 아니다 — 리 지역은
            #     실제 리 코드가 들어온다(6,743,655행 백필). 구 주석의 "'00' 불변"은 무효.
            "ri": _g(r, "ri"),
            "san": None,                              # F4: address 에 san 컬럼 없음 → null
            "road_name": _g(r, "road"),
            "main_no": _g(r, "main_no"), "sub_no": _g(r, "sub_no"),   # 동결
            "bld_main_no": _g(r, "main_no"), "bld_sub_no": _g(r, "sub_no"),  # alias
            "ji_main": jm, "ji_sub": js,
            "bld_name": _g(r, "bld"), "zipcode": _g(r, "postal"),
            # 치환지점 1/4 — address.bcode(46/29) → 12.
            # h_code 는 치환하지 않는다. 다만 그 이유가 "다른 시도코드 체계라서"는 **아니다**(구 주석 오류).
            #   [실측 2026-08-10, address TABLESAMPLE 1%] hcode 앞 2자리 = bcode 앞 2자리 100% 일치
            #   (46→46 12,626 / 29→29 1,195 / 36→36 656 / 47→47 13,653). 즉 address.hcode 는
            #   행안부 행정동코드로 시도 2자리가 법정동코드와 같은 46/29 다.
            # 치환하지 않는 진짜 이유: 안 A 는 **법정동코드 대응표만** 만들었다(lawd_code 원본이
            # 법정동코드뿐). 행정동코드는 시군구·읍면동 재편이 법정동과 다르게 잡힐 수 있어 앞 2자리만
            # 갈아끼우는 것은 Master 가 금지한 방식이다(402건 중 60.70%만 일치 → 약 39% 오답).
            # → 결과적으로 응답 안에서 b_code(12)와 h_code(46/29)가 어긋난다. 안 A 의 알려진 잔여
            #   불일치이며 docs/ri-dict-runbook.md 부록 B 에 기록돼 있다. 해소는 안 B 몫.
            # ※ admin_boundary 의 level='adm_dong' 코드는 또 다른 체계다(통계청: 24=광주·36=전남·29=세종).
            #   법정동코드 / 행안부 행정동코드 / 통계청 행정동코드 셋을 혼동하지 말 것.
            "b_code": remap_bcode(_bc), "h_code": _g(r, "hcode"),
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
def pad_bcode(emd_cd, ri_cd=None):
    """emd_cd(char(8)) + 리코드 → b_code 10자리. btrim 후 len==8·digit 가드, 비정상 시 None(경고 호출측).

    ri_cd 가 확정된 2자리 숫자면 그것을 붙이고, 아니면 '00'(동 지역·리 미확정).
    T018 Phase 1 이후 address.bcode 뒤 2자리는 실제 리 코드를 갖는다. 그러나 여기서
    '00' 을 무조건 떼거나 붙이는 것은 오답이다 — 존재 읍면동 5,067 중 리를 갖는 것은
    1,411(27.8%)뿐이고 나머지 3,656(72.2%)은 리가 없어 '00' 이 정답이다.
    즉 ri_cd 가 확정되지 않으면 '00' 이 유일하게 안전한 값이다(fail-closed).
    """
    if emd_cd is None: return None
    s = str(emd_cd).strip()                          # char(8) 공백패딩 btrim
    if len(s) != 8 or not s.isdigit(): return None   # 길이/숫자 가드 → 10자리 깨짐 차단
    if ri_cd is not None:
        r = str(ri_cd).strip()                       # char(2) 공백패딩 btrim
        if len(r) == 2 and r.isdigit() and r != "00":
            return s + r
    return s + "00"


def parcel_bcode(r):
    """parcel 경로 b_code — 1순위 left(pnu,10), 2순위 pad_bcode(emd_cd, ri_cd).

    pnu 19자리 = 법정동코드10 + 대지구분1 + 본번4 + 부번4 이므로 앞 10자리가 곧 b_code 다.
    [측정 2026-08-10, parcel 39,882,449행 전수] pnu NULL 0건 / left(pnu,8)<>emd_cd 0건 /
    length(pnu)<>19 는 12건. 즉 앞 8자리는 emd_cd 와 완전히 일치하므로 pnu 채택이
    lawd_dong 조인으로 복원한 권위 읍면동을 바꾸지 않는다. 기형 12건만 폴백으로 흘린다.
    앞 8자리 일치를 명시 검사하는 이유: 장래에 pnu 만 remap 되고 emd_cd 가 남는(또는 그 반대)
    상황에서 조용히 다른 읍면동을 반환하지 않게 하기 위함이다(안 B 에서 실제로 발생 가능).
    """
    emd_cd = r.get("emd_cd")
    pnu = (r.get("pnu") or "").strip()
    if len(pnu) >= 10 and pnu[:10].isdigit():
        s = str(emd_cd or "").strip()
        if pnu[:8] == s:                             # 권위 읍면동 불일치 시 pnu 채택 금지
            return pnu[:10]
    return pad_bcode(emd_cd, r.get("ri_cd"))


def pip_jibun(r):
    """parcel 행 → 지번 문자열 '읍면동 [리] [산 ]본번[-부번]'. 번지 미상이면 None.

    T019. **parcel.jibun 문자열을 쓰지 않는 것이 핵심이다** — 그 값은 '825-42구' 처럼
    지목 한 글자가 접미되고 읍면동·리 이름이 없다(address.jibun 은 반대로 '여산면 두여리 85-69'
    형식이라 두 테이블의 jibun 계약이 서로 다르다). 정수 컬럼 san/ji_main/ji_sub 로 재조립하고
    지역명은 lawd_dong/lawd_ri 조인값에서 가져온다.

    display_of 의 kind=='addr' 지번 규칙(L408~)과 같은 조립식이다. 그쪽은 item/row 를 받는
    표시 계층이고 여기는 parcel 행 전용이라 합치지 않는다 — 합치면 display_of 가 순방향
    도로명 경로까지 함께 물고 있어 회귀 반경이 커진다.
    ji_sub 는 0 과 NULL 을 같게 다룬다(둘 다 부번 없음 → '-0' 을 붙이지 않는다).
    """
    jm = r.get("ji_main")
    if jm is None: return None
    js = r.get("ji_sub")
    jb = ("산 " if r.get("san") else "") + f"{jm}" + (f"-{js}" if js else "")
    # 조인 별칭은 ri_nm(=lawd_ri.ri). 호출측이 ri 키로 실어 보내는 경우도 받아 준다.
    ri = _s(r.get("ri_nm")) or _s(r.get("ri"))
    return " ".join(x for x in (_s(r.get("emd")), ri, jb) if x)


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
    sido = _s(remap_sido_name(r.get("sido")))        # 안 A: 표기 경계 치환(코드와 동일 스위치)
    sigungu = _s(r.get("sigungu")); emd = _s(r.get("emd"))
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
        # 리(里)는 읍·면과 번지 사이에 들어간다('월곶면 성동리 263-8'). 리를 빼면 같은 읍·면 안의
        # 다른 리와 구분되지 않아 표시만으로는 검증이 불가능하다. r 에 ri 가 없으면 종전과 동일.
        ri_s = _s(r.get("ri"))
        main = " ".join(x for x in (emd, ri_s, jb) if x) or name
        secondary = _region(r, official=False, with_emd=False)
        full = " ".join(x for x in (_region(r, official=True, with_emd=False), emd, ri_s, jb) if x) or name
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
            "sido": remap_sido_name(sido), "sigungu": sigungu, "emd": emd,
            "haeng_dong": None, "ri": None, "san": None,
            "road_name": _g(r, "road"), "main_no": _g(r, "main_no"), "sub_no": _g(r, "sub_no"),
            "bld_main_no": _g(r, "main_no"), "bld_sub_no": _g(r, "sub_no"),
            "ji_main": None, "ji_sub": None,
            "bld_name": _g(r, "bld"), "zipcode": _g(r, "postal"),
            # 치환지점 2/4 — poi/building 등 비-addr 경로의 bcode. 리는 이 경로에서 항상 None 이라
            #   A-5 관측(_note_ri_unresolved)은 호출하지 않는다(전건 오탐이 된다).
            "b_code": remap_bcode(_g(r, "bcode")), "h_code": _g(r, "hcode"),
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


def _load_sido_remap(cur):
    """안 A 치환표(lawd_sido_remap·lawd_ri_remap_exception) 기동 1회 적재 → 전역 캐시.

    _probe_lawd_ri 와 같은 fail-open 계약: 테이블 부재·조회 실패 시 전역을 전부 비우고 False.
    그러면 remap_* 가 전부 항등함수가 되어 **현행 46/29 응답이 그대로 유지**된다(0건 회귀 없음).
    **all-or-nothing**: 코드표와 예외표 중 하나만 적재된 상태를 허용하지 않는다 — 부분 치환은
    "코드는 12 인데 리 코드는 구 체계"를 만들어 아무것도 안 한 것보다 나쁘다.
    행수는 소수(623 + 4)라 메모리·기동시간 영향은 무시할 수준이다.
    """
    global _SIDO_REMAP, _RI_REMAP_EXC, _HAS_SIDO_REMAP
    try:
        cur.execute("SELECT to_regclass('public.lawd_sido_remap') IS NOT NULL "
                    "AND to_regclass('public.lawd_ri_remap_exception') IS NOT NULL AS ok")
        if not cur.fetchone()["ok"]:
            raise LookupError("remap tables absent")
        # char(n) 공백패딩 제거는 적재 시점에 끝낸다 — 조회 경로에서 매번 strip 하지 않기 위함.
        cur.execute("SELECT btrim(old_emd8) o, btrim(new_emd8) n FROM lawd_sido_remap")
        m = {r["o"]: r["n"] for r in cur.fetchall()}
        cur.execute("SELECT btrim(old_bcode) o, btrim(new_bcode) n FROM lawd_ri_remap_exception")
        x = {r["o"]: r["n"] for r in cur.fetchall()}
        _SIDO_REMAP, _RI_REMAP_EXC, _HAS_SIDO_REMAP = m, x, bool(m)
    except Exception:
        _SIDO_REMAP, _RI_REMAP_EXC, _HAS_SIDO_REMAP = {}, {}, False   # fail-open
    return _HAS_SIDO_REMAP


def _load_ri_emds(cur):
    """A-5 관측용 — 리를 실제로 보유한 읍면동 emd_cd 집합(1,411건) 기동 1회 적재.

    이 집합에 있는 읍면동인데 응답의 ri 가 비어 있으면 Phase 1 백필 잔여 실패(148,818행/2.16%)
    또는 조회 경로의 리 미확정이다. 응답은 바꾸지 않고 stderr 경고만 남긴다(관측 전용).
    lawd_ri 부재 시 빈 집합 → 경고 자체가 비활성(fail-open).
    """
    global _RI_EMDS
    try:
        cur.execute("SELECT DISTINCT btrim(emd_cd) e FROM lawd_ri WHERE exist")
        _RI_EMDS = frozenset(r["e"] for r in cur.fetchall())
    except Exception:
        _RI_EMDS = frozenset()
    return len(_RI_EMDS)


def _acc_road(cur, seg):
    """도로명 조각 누적 — D3-R3 멱등. 이미 누적된 조각이 다시 오면 재누적하지 않는다.

    원본 대장에 도로명이 통짜로 두 번 들어간 행이 실재한다('… 방내시장길 32 방내시장길 32').
    단순 연결이면 road_norm 이 '방내시장길방내시장길' 이 돼 0건 → 지번경로로 조용히 강등되고
    15km 떨어진 읍·면 번지를 자신만만하게 반환했다(NO1·43·81·249).
    """
    if not seg: return cur
    if cur and seg in cur: return cur                        # 이미 들어있는 조각 → 스킵
    return (cur or "") + seg


def parse(q):
    q = re.sub(r"(?<=\d)\.(?=\d)", "", norm(q))
    # D3-R1 입력 정규화 ①: 괄호는 구분자다. '149(맥금동)' 이 한 토큰으로 남으면 아래 ct 정규화가
    # '149맥금동' 을 통째로 dong 에 넣어 번지가 증발한다(NO117/118 683m 이탈).
    q = re.sub(r"[()\[\]{}]", " ", q)
    house = road = dong = ri = bld_dong = zipcode = None; san = False; terms = []
    for t in re.split(r"[\s,]+", q):
        t = t.strip(".·;:'\"")                              # D3-R1 ②: 토큰 양끝 문장부호(꼬리 '.' 등)
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
            road = _acc_road(road, rnorm(mr.group(1)))
            a, b = int(mr.group(2)), int(mr.group(3) or 0)
            if house is None and a <= 99999 and b <= 99999:
                house = (a, b)
            continue
        # 복합 도로명(상위 '○○대로/로' + 하위 'N길/N나길/N번길')을 띄어 입력하면 두 토큰으로 쪼개진다.
        # 덮어쓰면 마지막 '7나길'만 남아 0건 → 누적 연결로 '과천대로7나길'(정식 road_norm) 복원.
        if re.search(r"(로|길)$", t): road = _acc_road(road, rnorm(t)); continue
        # 법정동/리/읍/면/'N가' 접미부와 번지가 공백없이 붙은 토큰('성동리263-8'·'청운동1'·'종로1가15')을
        # 접미부 + 번지로 분해한다. 위 도로(로/길) 규칙보다 반드시 뒤여야 '검단리1길'이 도로로 먼저 소비된다.
        # 접미 앞에 한글 1자 이상을 요구해 '101동5'(건물 동) 같은 토큰은 매칭되지 않게 한다.
        mj = re.fullmatch(r"((?:\d*[가-힣][^\s]*?)(?:동|리|읍|면|가))(산)?(\d+)(?:-(\d+))?", t)
        if mj:
            if mj.group(2): san = True
            a, b = int(mj.group(3)), int(mj.group(4) or 0)
            if house is None and a <= 99999 and b <= 99999: house = (a, b)
            t = mj.group(1)          # 접미부만 남겨 아래 dong/ri 판정으로 흘려보낸다
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
    # 원본 대장에 도로명이 통짜로 반복된 행이 있다('… 방내시장길 32 방내시장길 32').
    # _acc_road 가 대부분 걸러내지만 표기가 미세하게 달라 통과한 잔여분을 정수배 반복이면 1회로 접는다.
    if road:
        mm = re.fullmatch(r"(.+?)\1+", road)
        if mm: road = mm.group(1)
    return {"road": road, "house": house, "terms": terms, "dong": dong, "ri": ri, "san": san,
            "bld_dong": bld_dong, "zipcode": zipcode}


# ── 좌표 → 최근접 도로명주소(역지오코딩/주소부착) ──────────────────
def addr_at(cur, lon, lat, with_meta=False):
    """최근접 address 포인트 1행 → 주소객체. with_meta=True 면 (주소객체, meta) 쌍.

    meta 는 D-1 가드 전용 부가정보다(dist_m: 질의점까지 실거리 m, san: 그 포인트가 산번지인가).
    **기본 반환값·키 집합은 종전과 완전히 같다** — 순방향 주소부착(geocode)이 이 함수를 함께
    쓰므로 시그니처를 깨면 회귀 반경이 커진다. 그래서 추가 정보는 응답 dict 에 넣지 않고
    (넣으면 /reverse JSON 계약에 그대로 새어 나간다) 선택 반환값으로만 뺀다.

    거리는 바깥 SELECT 에서 계산한다 — LIMIT 1 서브쿼리 밖이라 ST_Distance 는 정확히 1행에
    대해서만 평가된다(안쪽 타깃리스트에 두면 KNN 주행 중 스캔되는 모든 행에서 평가될 수 있다).
    """
    # geom && ST_Expand 는 Index Cond 로 내려가 KNN 주행범위를 묶는다(geography 캐스트한
    # ST_DWithin 은 Filter 로만 걸려 반경 내 0건이면 인덱스 전체를 훑고 statement_timeout).
    # 0.035°는 2.5km 의 경도 상한(위도 38.7°에서 0.0288°) 초과분 — 정확도는 ST_DWithin 이 보장.
    cur.execute(
        """SELECT s.*, ST_Distance(s.geom::geography,
                  ST_SetSRID(ST_MakePoint(%s,%s),4326)::geography) AS knn_dist_m
           FROM (SELECT * FROM address
                 WHERE kind='addr' AND geom IS NOT NULL
                   AND geom && ST_Expand(ST_SetSRID(ST_MakePoint(%s,%s),4326), 0.035)
                   AND ST_DWithin(geom::geography, ST_SetSRID(ST_MakePoint(%s,%s),4326)::geography, 2500)
                 ORDER BY geom <-> ST_SetSRID(ST_MakePoint(%s,%s),4326) LIMIT 1) s""",
        (lon, lat, lon, lat, lon, lat, lon, lat))
    r = cur.fetchone()
    if not with_meta:
        return addr_obj(r) if r else None
    if not r:
        return None, {"dist_m": None, "san": False}
    d = _g(r, "knn_dist_m")
    # address 에는 san 컬럼이 없다(addr_obj 의 F4 주석 참조) → jibun 문자열에서 판정한다.
    return addr_obj(r), {"dist_m": (float(d) if d is not None else None),
                         "san": parse_jibun_nums(_g(r, "jibun"))[2]}


# ── 좌표 → 그 점을 **포함하는** 필지(PIP) ──────────────────────────
def parcel_at(cur, lon, lat):
    """ST_Contains(parcel.geom, pt) 로 좌표를 포함하는 필지 1행. 0행·실패 시 None.

    T019 결함의 핵심 처방. addr_at 은 address **포인트** 테이블의 최근접(KNN)이라
    '가장 가까운 다른 번지'를 집는다(원 지번 복원 44.9%). 필지 폴리곤 포함관계는
    좌표에 대해 유일 정답이므로 지번은 이쪽에서 가져와야 한다.

    - KNN 이 아니라 포함관계라 3033546 의 503 함정(geography ST_DWithin 이 Filter 로만
      걸려 인덱스 전주행)에 해당하지 않는다. bbox 선행이 불필요하며 실측 정상상태 ~6ms.
    - sido_cd 를 좌표만으로 알 수 없어 파티션 프루닝은 안 된다(18개 Append). 파티션마다
      GiST 라 문제되지 않는다.
    - **모든 공유 컬럼은 parcel. 로 한정한다** — emd_cd 는 parcel 과 lawd_dong 양쪽에 있어
      한정하지 않으면 ambiguous 로 질의 자체가 죽는다(그러면 아래 폴백에 삼켜져 조용히
      기준선과 같은 값이 나온다 — 이 함수의 가장 위험한 실패 양식이다).
    - ORDER BY 는 결정성 확보용. 같은 점을 포함하는 필지가 둘 이상인 경우(경계 중복·산/일반
      공존)에 산이 아닌 일반 필지, 작은 번지를 우선한다(순방향 parcel 경로 L773 과 같은 취향).

    실패는 삼키고 None 을 돌려 호출측이 현행 KNN 지번을 유지하게 한다(무회귀 보장).
    다만 **트랜잭션 오염을 반드시 풀어야 한다** — psycopg 는 질의 실패 시 트랜잭션을
    abort 상태로 만들고 이후 nearest/areas 질의가 전부 InFailedSqlTransaction 으로 죽는다.
    즉 예외만 삼키면 폴백이 아니라 500 이 된다. connection.transaction() 은 진행 중인
    트랜잭션 안에서 SAVEPOINT 로 동작하므로 이 구간만 되감을 수 있다.
    """
    ri_sel = "lr.ri AS ri_nm, " if _HAS_LAWD_RI else ""
    ri_join = (" LEFT JOIN lawd_ri lr ON lr.emd_cd = parcel.emd_cd "
               "AND lr.ri_cd = substr(parcel.pnu,9,2)::char(2)") if _HAS_LAWD_RI else ""
    sql = ("SELECT parcel.jibun AS jibun, parcel.emd_cd AS emd_cd, "
           "parcel.pnu AS pnu, substr(parcel.pnu,9,2) AS ri_cd, " + ri_sel +
           "parcel.ji_main AS ji_main, parcel.ji_sub AS ji_sub, parcel.san AS san, "
           "ld.sido AS sido, ld.sigungu AS sigungu, ld.emd AS emd "
           "FROM parcel JOIN lawd_dong ld ON ld.emd_cd = parcel.emd_cd" + ri_join +
           " WHERE ST_Contains(parcel.geom, ST_SetSRID(ST_MakePoint(%s,%s),4326))"
           " ORDER BY parcel.san, parcel.ji_main, parcel.ji_sub LIMIT 1")
    tx = getattr(getattr(cur, "connection", None), "transaction", None)
    try:
        if tx is not None:
            with tx():                                  # 실패 시 SAVEPOINT 까지만 되감기
                cur.execute(sql, (lon, lat))
                return cur.fetchone()
        cur.execute(sql, (lon, lat))
        return cur.fetchone()
    except Exception as e:
        # 조용한 무력화를 막기 위해 반드시 남긴다(ambiguous/타임아웃이 여기로 숨는다).
        print(f"geocode-api-pg: NOTE parcel PIP 실패 → KNN 폴백 "
              f"({type(e).__name__}: {str(e)[:120]})", file=sys.stderr)
        return None


# 산번지 폴리곤 결측 판정 거리 임계(m). 벤치 595건 실측 근거:
#  · KNN=산·PIP=일반 인 좌표는 7건뿐이고, 그중 폴리곤 결측인 NO119 만 거리 0.00m,
#    나머지 6건 중 최근접이 32.65m 다 → 5m 는 오탐측 경계에서 6.5배 여유.
#  · 0.5~20m 어느 값을 잡아도 적중은 NO119 1건이고 1차 개선 253건 적중은 0건이다(함정A 회피).
#  · 반대로 임계를 없애면 개선 5건(NO203·507·532·538·586)이 되돌아간다 → 임계는 필수다.
#  · 거리 단독 가드는 금물이다 — 개선 253건 중 최소거리가 1.35m(화전동 841-4→825-42)라
#    '가까우면 KNN' 규칙이면 계획서의 대표 개선사례부터 무너진다. 판별자는 san 뒤집힘이고
#    거리는 안전마진일 뿐이다.
PIP_SAN_GUARD_M = 5.0


def _pip_polygon_missing(p, knn):
    """PIP 를 KNN 으로 교차검증 — '산번지 폴리곤 결측' 신호면 True(=PIP 를 버리고 KNN 유지).

    T019 수정 라운드 1(D-1). 1차 구현은 PIP 가 1행이라도 나오면 무조건 지번을 덮어썼고
    폴백은 parcel_at 이 None 일 때만 있었다. 그런데 산번지는 address 포인트로는 존재하는데
    지적도 폴리곤이 없는 경우가 있다 — 이때 ST_Contains 는 **그 점을 덮는 옆 일반필지**를
    1행으로 돌려준다. 0행이 아니므로 폴백은 설계상 발동할 수 없고, 결과는 조용한 오답이다
    (NO119 파주 하지석동 산54-1 → 465-9). 즉 '0행'이 아니라 '엉뚱한 1행'이 실패 양식이다.

    **반드시 단방향이어야 한다.** 반대 상황(PIP=산 · KNN=일반)은 KNN 이 옆 일반필지를 집은
    평범한 오차이고 PIP 가 정답이다 — 벤치 595건 중 38건이 이쪽이다. 대칭 가드로 만들면
    그 38건이 통째로 회귀한다. 그래서 knn.san 이 참인 경우에만 개입한다.

    거리를 못 구하면(meta 없음·구버전 호출) 개입하지 않는다 — fail-open 이라야 가드 자체가
    새로운 회귀원이 되지 않는다.
    """
    if not knn or not knn.get("san"):
        return False                          # KNN 이 산이 아니면 이 가드의 관할이 아니다
    if bool(p.get("san")):
        return False                          # 양쪽 다 산 → 같은 계열, PIP 가 더 정확하다
    d = knn.get("dist_m")
    return d is not None and d <= PIP_SAN_GUARD_M


def apply_parcel_pip(address, p, knn=None):
    """addr_at(KNN) 주소객체에 parcel_at(PIP) 결과를 병합한다. p 가 못 쓸 값이면 원본 그대로.

    T019 의 병합 규칙. **지번계열과 도로명계열의 출처를 나눈다.**
      - 지번계열(parcel 문자열 · sido/sigungu/emd/ri/san/ji_main/ji_sub/b_code) → PIP
      - 도로명계열(road/zipcode/bld · road_name/main_no/sub_no/bld_*/haeng_dong/h_code) → KNN
    parcel 테이블에는 도로명·우편번호·건물명·행정동이 아예 없으므로 그쪽은 KNN 이 유일 출처다.

    시도·시군구·읍면동을 **지번과 한 덩어리로** 옮기는 것이 요점이다. 지번 문자열만 갈아끼우면
    최근접 포인트가 옆 동이었을 때 '덕양구 도내동 825-42' 같은 잡종이 나온다(825-42 는 화전동).
    같은 이유로 b_code 도 PIP 출처여야 한다 — 안 그러면 코드와 이름이 서로 다른 동을 가리킨다.
    h_code(행정동)는 parcel 에 없으니 KNN 값을 유지한다. b_code 와 h_code 가 어긋날 수 있으나
    이는 안 A 의 기존 잔여 불일치와 같은 성격이다(addr 경로 주석 L282 부근 참조).
    **road(도로명 문자열)도 같은 이유로 KNN 출처다** — 지번계열만 PIP 로 갈아끼우므로 road 는
    최근접 포인트가 속한 도로명이고, PIP 가 옆 동을 가리키면 road 와 지번의 행정구역이 어긋날
    수 있다(D-5). 도로명 문자열에는 동(洞) 토큰이 없어 시도·시군구 수준까지만 교차검증이
    가능하며, 벤치 실측에서는 road 593행 중 시군구 불일치 0·시도 불일치 0 으로 미발현이다.

    address 가 None 이어도 p 만 있으면 주소를 만든다 — 최근접 포인트가 2.5km 안에 없어
    지금까지 address=null 이던 좌표의 순증분이다. 이때도 키 집합은 ADDR_STRUCT_KEYS 로 동일하다.
    """
    jb = pip_jibun(p) if p else None
    if jb is None:
        return address                       # PIP 0행·번지 미상 → 현행 KNN 유지(무회귀)
    if _pip_polygon_missing(p, knn):
        return address                       # 산번지 폴리곤 결측 → PIP 는 옆 필지다. KNN 유지
    ri = _s(p.get("ri_nm")) or _s(p.get("ri"))
    _bc = parcel_bcode(p)
    _note_ri_unresolved(_bc, ri, "reverse_pip")        # A-5: 치환 전 원값으로 관측
    # 치환지점 — pnu/emd_cd 유래 b_code(46/29) → 12. 순방향 parcel 경로(L860)와 같은 규칙.
    pip_st = {"sido": remap_sido_name(p.get("sido")), "sigungu": p.get("sigungu"),
              "emd": p.get("emd"), "ri": ri, "san": bool(p.get("san")),
              "ji_main": p.get("ji_main"), "ji_sub": p.get("ji_sub"),
              "b_code": remap_bcode(_bc)}
    # 지번 문자열은 parcel_str 로 만든다 — addr 경로와 표기 정의점을 하나로 유지하기 위함이다
    # (remap_sido_name 적용·결측 토큰 생략 규칙이 그 안에 있다).
    parcel = parcel_str({"sido": p.get("sido"), "sigungu": p.get("sigungu"),
                         "jibun": jb, "emd": p.get("emd")})
    if address is None:
        st = dict.fromkeys(ADDR_STRUCT_KEYS)           # 없는 값은 전부 None
        st.update(pip_st)
        return {"road": "", "parcel": parcel, "zipcode": "", "bld": "", "structure": st}
    return {**address, "parcel": parcel,
            "structure": {**address["structure"], **pip_st}}


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
            # §B-3 입력 역치환: '12'(전남광주통합특별시)는 DB 에 없는 코드다. 여기서 46·29 로 되돌리지
            #   않으면 신 시도명으로 검색한 사용자가 0건을 받는다. '전남광주…'는 '전남'으로도 접두매칭돼
            #   {12,46} 이 되므로, 확장 없이는 광주 소재 동이 전부 탈락한다(부분적 0건 = 더 나쁜 회귀).
            for _new, _olds in SIDO_ALIAS_CODES.items():
                if _new in sido_hit:
                    sido_hit.discard(_new); sido_hit.update(_olds)
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
            # T018 A-2: pnu 를 SELECT 목록에 올린다. 기존에는 pnu 가 WHERE 절에만 있었으므로
            #   b_code 를 pnu 에서 뽑으려면 이 추가가 필수다(없으면 아래 b_code 식이 조용한 no-op).
            #   조인 추가는 불필요 — pnu 는 parcel 자체 컬럼이다.
            # 리(里)명은 질의 토큰이 아니라 결과 행의 pnu 에서 얻는다. 질의에 리가 없어도
            # 그 필지가 리에 속하면 표시에 리가 나와야 하기 때문(입력 에코가 아닌 DB 사실).
            # R-9 fail-open 유지: 사전 미구축이면 조인 자체를 붙이지 않고 종전 동작 그대로 간다.
            ri_sel = "lr.ri AS ri_nm, " if _HAS_LAWD_RI else ""
            ri_join = (" LEFT JOIN lawd_ri lr ON lr.emd_cd = parcel.emd_cd "
                       "AND lr.ri_cd = substr(parcel.pnu,9,2)::char(2)") if _HAS_LAWD_RI else ""
            sql = ("SELECT parcel.jibun AS jibun, parcel.emd_cd AS emd_cd, "
                   "parcel.pnu AS pnu, substr(parcel.pnu,9,2) AS ri_cd, " + ri_sel +
                   "parcel.ji_main AS ji_main, parcel.ji_sub AS ji_sub, parcel.san AS san, "
                   "ld.sido AS sido, ld.sigungu AS sigungu, ld.emd AS emd, "
                   "ST_X(COALESCE(parcel.geom_pt, ST_PointOnSurface(parcel.geom))) AS lon, "
                   "ST_Y(COALESCE(parcel.geom_pt, ST_PointOnSurface(parcel.geom))) AS lat "
                   "FROM parcel JOIN lawd_dong ld ON ld.emd_cd = parcel.emd_cd" + ri_join +
                   " WHERE parcel.sido_cd = ANY(%s::char(2)[]) AND parcel.emd_cd = ANY(%s::char(8)[]) "
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
            # 질의에 '산'이 없으면 일반 필지를 먼저 준다. 같은 번지에 산(임야)과 일반이 공존하면
            # 임의 순서로는 임야가 먼저 잡혀 수 km 떨어진 산자락을 반환한다(NO161·162·283·288).
            order = "parcel.san, " if not p["san"] else ""
            sql += f" ORDER BY {order}parcel.emd_cd, parcel.ji_sub LIMIT {ADDR_CAP}"
            cur.execute(sql, args)
            for r in cur.fetchall():
                san_b = bool(r["san"])
                # 1순위: 이 행의 pnu 리코드로 조회한 사전값(DB 사실). 2순위: 리 필터가 실제로
                # 걸린 경우의 질의 토큰. 둘 다 없으면 None — 입력토큰을 확인 없이 되돌려주지 않는다.
                _ri = r.get("ri_nm") or (p.get("ri") if ri_cds else None)
                _bc = parcel_bcode(r)
                _note_ri_unresolved(_bc, _ri, "parcel")     # A-5: 치환 전 원값으로 관측
                st = {"sido": remap_sido_name(r["sido"]), "sigungu": r["sigungu"], "emd": r["emd"],
                      "haeng_dong": None, "ri": _ri, "san": san_b,
                      "road_name": None, "main_no": None, "sub_no": None,
                      "bld_main_no": None, "bld_sub_no": None,
                      "ji_main": r["ji_main"], "ji_sub": r["ji_sub"],
                      "bld_name": None, "zipcode": None,
                      # 치환지점 3/4 — pnu/emd_cd 유래 b_code(46/29) → 12.
                      "b_code": remap_bcode(_bc), "h_code": None}
                it = {"name": None, "kind": "addr", "subtype": "parcel", "source": "parcel",
                      "lon": r["lon"], "lat": r["lat"]}
                # ri 를 실어 보낸다 — display_of 는 r 에서 리를 읽어 읍·면과 번지 사이에 넣는다.
                disp = display_of(it, {**r, "ri": _ri})   # parcel 규칙(road 부재) — 지목제거·지역복원
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
            # §B-3 입력 역치환: 토큰별 조건은 AND 로 이어붙으므로, 신 시도명 하나가 안 맞으면 전체가
            #   0건이 된다. _region_cond 가 '전남광주…' → 구 표기(전라남도·광주광역시)까지 OR 로 넓힌다.
            c, a = _region_cond(("sigungu", "sido"), t)
            reg_sql += " AND " + c
            reg_args += a
        cur.execute(
            "SELECT *, ST_X(geom) AS lon, ST_Y(geom) AS lat FROM address "
            f"WHERE kind='addr' AND search_text ILIKE %s AND {num_conds}{reg_sql} "
            f"ORDER BY (search_text ILIKE %s) DESC, sigungu, emd, id LIMIT {ADDR_CAP}",
            (f"%{dong}%", *nums, *reg_args, exact))
        for r in cur.fetchall():
            # 가산도 같은 별칭 규칙을 써야 한다 — WHERE 는 통과했는데 가산이 0 이면 신 시도명으로
            #   검색한 결과만 순위가 밀린다(조용한 품질 저하).
            bonus = 12 * sum(1 for t in region
                             if any(a in (r["sigungu"] or "") or a in (r["sido"] or "")
                                    for a in sido_input_alias(t)))
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
                # §B-3 입력 역치환. 원토큰은 _region_cond 가 항상 첫 별칭으로 남기므로 search_text/bld
                #   매칭(상호명이 우연히 '전남광주…'로 시작하는 경우 포함)은 그대로 유지된다.
                c, a = _region_cond(("search_text", "bld", "sido", "sigungu", "emd"), t)
                conds.append(c); args += a
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
    # T019: 지번은 필지 폴리곤 포함관계(PIP)에서, 도로명·우편번호·건물명은 종전 KNN 에서.
    # 두 질의를 모두 던진다 — PIP 는 정상상태 ~6ms 라 p95 에 유의미하지 않다.
    # nearest[]/areas[] 에는 적용하지 않는다(그쪽은 '주변 무엇'이지 '이 점의 주소'가 아니다).
    # knn_meta(거리·산 여부)는 D-1 교차검증 가드 전용이다 — 응답에는 실리지 않는다.
    address, knn_meta = addr_at(cur, lon, lat, with_meta=True)
    address = apply_parcel_pip(address, parcel_at(cur, lon, lat), knn_meta)
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
    # 치환지점 4/4 — areas[].code.
    # ★ type='emd' 로 한정하는 것이 필수다. admin_boundary 는 level 별로 **다른 코드체계**를 담는다:
    #     level='emd'      → 법정동코드 (29=광주광역시, 46=전라남도)  → 치환 대상
    #     level='adm_dong' → 통계청 행정동코드 (**29 = 세종특별자치시**) → 치환하면 세종이 광주로 둔갑
    #   [실측 2026-08-10, admin_boundary 전량] adm_dong 시도 2자리는 11·21~26·29·31~39 만 나온다.
    #     그 체계에서 24=광주 · 36=전남 · 29=세종 이다(표본: 금곡동 24040740 / 산이면 36600410 /
    #     금남면 29010340). 즉 adm_dong 에는 46 이 아예 없고 29 는 전부 세종이다.
    #   [실측 2026-08-10] 46/29 로 시작하는 admin_boundary 행 649건의 내역:
    #     emd·len8   421(46) + 202(29) = 623  ← 치환 대상(= lawd_sido_remap 행수)
    #     adm_dong·len8 24(29, 세종)          ← 치환 금지
    #     emd·len7      2(46, code='46-1 ??'·'46-2 ??' 기형행) ← 자료품질 결함, 길이가드로 배제
    #   따라서 조건 없는 left(code,2) 판정은 **금지**다. remap_bcode 도 내부에서 길이·숫자·사전존재를
    #   재검사하므로 이 가드는 이중 방어다(어느 한쪽만 남기지 말 것).
    # ※ 안 A 적용 후에도 areas[] 는 두 코드체계가 섞인 배열이다 — emd 는 12, adm_dong 은 29(세종).
    areas = [{"name": a["name"], "type": a["type"],
              "code": remap_bcode(a["code"]) if a["type"] == "emd" else a["code"]}
             for a in cur.fetchall()]
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
    _boot_check()          # 사전·치환표를 실서비스와 동일하게 적재(안 하면 selftest 만 46/29 로 보인다)
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
            has_remap = _load_sido_remap(cur)   # 안 A 치환표(부재 시 46/29 현행 유지)
            n_ri_emds = _load_ri_emds(cur)      # A-5 관측용 리 보유 읍면동 집합
        print(f"geocode-api-pg: lawd_ri: {'present' if has_ri else 'absent'}", file=sys.stderr)
        # 안 A 상태를 기동 로그에 남긴다 — 운영에서 "왜 아직 46 이 나오냐"를 즉시 판별하기 위함.
        print(f"geocode-api-pg: sido remap(안 A): "
              f"{'ON' if has_remap else 'OFF(46/29 유지)'} "
              f"emd={len(_SIDO_REMAP)} ri_exc={len(_RI_REMAP_EXC)} ri_emds={n_ri_emds}",
              file=sys.stderr)
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
