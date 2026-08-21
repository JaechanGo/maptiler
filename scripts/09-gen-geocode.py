#!/usr/bin/env python3
"""통합 지오코딩 인덱스 빌드 — 도로명주소(내비게이션용DB) + OSM(역·지명·도로·POI·동) 병합.

출력: ~/geocode-build/geocode.sqlite  (단일 DB; 주소 검색 + 역/POI/지명 이름 검색 + 역지오코딩)
- 주소(kind='addr')   : 내비게이션용DB match_build_*.txt, 좌표 EPSG:5179→4326(순수파이썬)
- OSM(kind=type)      : 기존 geocode.sqlite(07 산출물)에서 복사 — station/place/dong/road/poi/biz + areas
통합 스키마:
  places(id,kind,name,subtype, sido,sigungu,emd,ri,road,road_norm,main_no,sub_no,bld,postal,haeng_dong,bd_mgt_sn, lon,lat)
  places_fts(name, region, road, bld)   ← addr→region/road/bld, OSM→name
  place_rtree, areas, area_rtree, meta
※ 산출물(GB급)은 iCloud 밖 로컬에 둔다.
"""
import argparse, collections, io, json, math, os, pathlib, pwd, re, sqlite3, sys, time, unicodedata

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))        # PYTHONSAFEPATH=1 대비
from _common.textnorm import biznrm_nfc as biznrm, norm, rnorm        # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[1]
# 202607 원천 기준 16종. 원천에서 광주광역시·전라남도는 `jeonnamgwangju` 한 파일로 통합돼 있다.
# 202605 유물(17종: gwangju·jeonnam 분리)을 그대로 두면 match_*_jeonnamgwangju.txt 를 아무도
# 열지 않아 전국 건물의 12.4%(1,327,372행)가 **무증상으로** 빠진다. 목록을 바꿀 때는
# `command ls ~/geocode-build/staged/navi/match_build_*.txt` 로 실물과 대조하라.
SIDO = ["seoul","busan","daegu","incheon","daejeon","ulsan","sejong","gyunggi",
        "gangwon","chungbuk","chungnam","jeonbuk","jeonnamgwangju","gyeongbuk","gyeongnam","jeju"]
CW_LD, CW_OSM = {}, {}   # cat-crosswalk.json (canonical 카테고리 매핑) — main()에서 로드, add_biz/add_osm 이 사용

# ---- 원천 설계 PK (T043 / 근거 T042 `docs/원천PK-조인-전국검증.md`) -----------
# 도로명주소 원천이 match_build ↔ match_jibun 을 잇도록 **설계해 둔** 복합키다.
#   PK1 도로명코드 · PK2 지하여부 · PK3 건물본번 · PK4 건물부번 · PK6 주소관할읍면동코드
# PK5(지번일련번호, jibun c[12])는 키가 아니라 대표지번 선별 술어로만 쓴다(c[12]=="0").
# [T042 전국 실측] 적중 10,722,641 / 10,722,641 = 100.00%, 미적중 0, 16/16 시도.
#   같은 표본에서 구 건물관리번호 조인은 59.89% — 원천이 지번을 주소 단위로 싣기 때문에
#   생기는 **구조적 상한**이며 버그가 아니다. PK4 까지만 쓰면 전국 10키가 충돌하고, PK6 을
#   더하면 충돌 0 이 된다(§A-2).
PK6_JIBUN = (8, 9, 10, 11, 19)
PK6_BUILD = (4, 6,  7,  8,  0)

# ---- 빌드 안전 게이트 (T043) --------------------------------------------------
# 산출물 교체(tmp.replace(out)) 앞에 서는 관문. 0행/부분 산출이 7GB 정본을 덮는 사고를 막는다.
CANONICAL_HOMES = ("~/geocode-build",)   # 정본 후보 디렉터리. 확장할 때는 여기에만 추가한다.

# ---- G3·G4 기준선 (202605 정본 실측) ------------------------------------------
# 뽑은 방법 — `sqlite3` 로 정본을 **읽기전용**으로 열고 시도별로 센 값이다:
#   con = sqlite3.connect("file:" + os.path.expanduser("~/geocode-build/geocode.sqlite")
#                         + "?mode=ro", uri=True)
#   con.execute("SELECT sido, count(*) FROM places WHERE kind='addr' GROUP BY sido")
# 정본의 `sido` 는 17종(202605 당시 광주·전남 분리)이고 slug 는 16종이므로,
# **jeonnamgwangju 만** 두 시도의 합으로 유도한다 — 나머지 15종은 단일 시도 실측이다.
#   전라남도 1,159,723 + 광주광역시 163,664 = 1,323,387
# 이 유도가 옳다는 방증: 원천 `match_build_jeonnamgwangju.txt` 는 1,327,372 행이고
# 유도값과의 비 0.99700 은 다른 15종의 (정본 실측 ÷ 원천 행수) 비 0.992~1.000 대와 같은 대역이다.
#
# [T043 재검수] 직전 판(15종)의 값 12종은 정본 실측이 **아니었다**(출처 불명).
#   예: seoul 621,255 ↔ 실측 592,882 / gyunggi 2,443,510 ↔ 실측 1,650,227 /
#       jeju 174,120 ↔ 실측 261,557. `--only gyunggi` 나 `--only jeju` 같은 정상
#   부분 빌드가 창 밖으로 떨어져 **오탐**을 냈고, 전국 합계는 우연히 창 안에 들어
#   게이트가 무력했다. 16종 전부를 위 한 가지 방법으로 다시 뽑아 동질화했다.
BASELINE_ADDR = {
    "seoul": 592882, "busan": 379669, "daegu": 291230, "incheon": 248953,
    "daejeon": 139244, "ulsan": 162517, "sejong": 55846, "gyunggi": 1650227,
    "gangwon": 593703, "chungbuk": 621383, "chungnam": 886180, "jeonbuk": 801085,
    "jeonnamgwangju": 1323387,      # ← 유일한 유도값(전남+광주 합). 위 주석 참조
    "gyeongbuk": 1409925, "gyeongnam": 1268759, "jeju": 261557,
}                                   # 16종 합 10,686,547 = 정본 kind='addr' 전체와 일치
BASELINE_OSM = 594704     # 정본 실측: OSM 유래 kind 합(poi 406,064 + road 165,601 + place 21,668 + station 1,371)
BASELINE_POI = 5000876    # 정본 실측: --poi-csv-dir 유래 kind 합(biz 4,914,112 + facility 86,764)

# 행수 게이트는 **세 층**이고 층마다 무너지는 조건이 다르다. 하나가 뚫려도 다음이 선다.
#   1층 비례 창   GATE_LO~GATE_HI — `--min-rows-ratio/--max-rows-ratio` 로 넓힐 수 있다(운영 판단)
#   2층 비례 바닥 GATE_FLOOR      — **명령줄로 조절 불가**. 창을 아무리 넓혀도 남는 바닥
#   3층 절대 바닥 GATE_MIN_PER_SIDO — 기준선이 아예 없어도 서는 유일한 층
GATE_LO, GATE_HI = 0.98, 1.10      # 행수 허용 창 기본값(-2% ~ +10%)
GATE_FLOOR = 0.93                  # 2층 — 창을 넓히더라도 남는 비례 바닥(조절 불가)
# 3층 — 시도 1종당 최소 addr 행수. 기준선 유무·창 설정과 **무관**하게 항상 적용한다.
#   가장 작은 시도(sejong)가 55,846 행이므로 10,000 은 정상 빌드를 절대 건드리지 않는다.
#   이 층이 있어야 "기준선 없는 시도가 섞여 G3·G4 가 SKIP → 0행 산출이 통과" 가 막힌다.
GATE_MIN_PER_SIDO = 10000
LON_LO, LON_HI, LAT_LO, LAT_HI = 124.0, 132.0, 33.0, 39.0

# 판정 4값: PASS / FAIL / SKIP / N/A.
#   SKIP 은 "판정할 수 없었다"이고 이는 **실패로 계수한다**(--allow-gate-skip 으로만 해제).
#   N/A 는 "판정 대상이 아니다"이고 실패가 아니다(현재 G6 만 해당).
GateResult = collections.namedtuple("GateResult", "name verdict actual expected note")

# ---- 리(里) 키 충돌 관측 (T018 A-4) — **폐지됨** ---------------------------
# 여기에는 RI_KEY_COLLISIONS / RI_COLLISION_ROWS / RI_COLLISION_SAMPLE 세 상수가 있었다.
# mgt[:10] 파생키가 한 키에 서로 다른 리를 몰아넣는 것(붕괴 키 2,699개)을 세려던 계수기다.
# T043 에서 조인키를 원천 설계 PK6 으로 바꾸면서 그 붕괴가 구조적으로 사라졌고(중복은 G9 가
# 0 을 강제한다), 세 상수는 아무도 읽지 않는 채로 남아 있었다.
# **선언만 남은 계수기는 "세고 있다"는 거짓 인상을 준다**(T043 검수 Minor-5). 그래서 지웠다.
# 지금 이 자리의 역할은 state["pk_dup"] → 게이트 G9 가 대신한다 — 계수기를 없애지 말라는
# T042 §B-1 주의는 계수 자체가 아니라 "붕괴를 침묵하지 말라"는 뜻이므로 그대로 지켜진다.

# ---- EPSG:5179(UTM-K) → WGS84 (Snyder inverse TM, 무의존) ----
_a=6378137.0; _f=1/298.257222101; _e2=2*_f-_f*_f; _ep2=_e2/(1-_e2)
_lat0=math.radians(38.0); _lon0=math.radians(127.5); _k0=0.9996; _FE=1e6; _FN=2e6
_M0=_a*((1-_e2/4-3*_e2**2/64-5*_e2**3/256)*_lat0-(3*_e2/8+3*_e2**2/32+45*_e2**3/1024)*math.sin(2*_lat0)
        +(15*_e2**2/256+45*_e2**3/1024)*math.sin(4*_lat0)-(35*_e2**3/3072)*math.sin(6*_lat0))
_e1=(1-math.sqrt(1-_e2))/(1+math.sqrt(1-_e2)); _den=_a*(1-_e2/4-3*_e2**2/64-5*_e2**3/256)
def utmk_to_wgs84(E,N):
    mu=(_M0+(N-_FN)/_k0)/_den
    p=(mu+(3*_e1/2-27*_e1**3/32)*math.sin(2*mu)+(21*_e1**2/16-55*_e1**4/32)*math.sin(4*mu)
       +(151*_e1**3/96)*math.sin(6*mu)+(1097*_e1**4/512)*math.sin(8*mu))
    sp=math.sin(p); C=_ep2*math.cos(p)**2; T=math.tan(p)**2
    N1=_a/math.sqrt(1-_e2*sp*sp); R1=_a*(1-_e2)/(1-_e2*sp*sp)**1.5; D=(E-_FE)/(N1*_k0)
    lat=p-(N1*math.tan(p)/R1)*(D**2/2-(5+3*T+10*C-4*C**2-9*_ep2)*D**4/24
        +(61+90*T+298*C+45*T**2-252*_ep2-3*C**2)*D**6/720)
    lon=_lon0+(D-(1+2*T+C)*D**3/6+(5-2*C+28*T-3*C**2+8*_ep2+24*T**2)*D**5/120)/math.cos(p)
    return round(math.degrees(lon),6), round(math.degrees(lat),6)

# norm·rnorm 은 scripts/_common/textnorm.py 가 정본이다(위 import).
_DORO_RE = re.compile(r"(\S*(?:대로|로|길))\s+(\d+)(?:-(\d+))?")
_GIL_SP = re.compile(r"([로가])\s+(\d+(?:번|가)?길)")   # '퇴계로 34길' 처럼 번호길 앞 띄어쓰기 → 붙임(안 그러면 '퇴계로'+34번지로 오독)
def _mainno(s):                 # 결정5: 본번 int4 안전정수화(length<=7). 비정상 자릿수·비숫자→None(매칭 불성립). int4 가드 제거 금지(Global Constraint).
    if not s or not s.isdigit() or len(s) > 7: return None
    return int(s)
def _subno(s):                  # 결정5: 부번 안전정수화. 비정상·결측→0(기존 int(... or 0) 동작 보존 + main_no 와 동일 자릿수 가드).
    if not s or not s.isdigit() or len(s) > 7: return 0
    return int(s)
def parse_doro(s):
    # 도로명주소 → (도로명, 도로명정규화=rnorm, 건물본번, 건물부번). 상세주소(층/호)·미파싱은 무시. navi 조인키(rnorm·본/부번)와 동형.
    m = _DORO_RE.search(_GIL_SP.sub(r"\1\2", norm(s).split(",")[0]))
    if not m: return (None,None,None,None)
    mno = _mainno(m.group(2))                              # 본번 int4 가드 — 비정상이면 미파싱 취급
    if mno is None: return (None,None,None,None)
    return (m.group(1), rnorm(m.group(1)), mno, _subno(m.group(3)))


# ── 도로명 사전(gazetteer) 매칭 — navi 의 '실재 도로명'에 최장일치. 정규식이 더러운 입력(붙여쓰기·지하·시군구붙음·
#    내부공백·가지번호)을 '추측'하다 깨지는 것을 방지(개념: geocoder-kr/postcodify 의 타입정규화·사전조인을 navi 로 자체구현).
GAZ = {}                                   # build_gazetteer 가 채움: {"road":{(시도,시군구):[road_norm…]}, "sgg":{시도:[시군구…]}}
_JIHA = re.compile(r"지하|지상|지층")        # 도로명·번호 사이 인필('창경궁로 지하 81') 제거용
def parse_doro_gaz(s):
    """도로명주소 → (도로명, road_norm, 본번, 부번, 시도, 시군구). navi 도로명 사전 최장일치.
    붙여쓰기('대학로120')·지하('창경궁로 지하 81')·시군구붙음('성동구난계로')·내부공백('센텀 7로')·가지번호('여의대방로59나길')
    견고. 시군구는 화이트리스트 접두분리로 보정(세종=시군구 빈값). 사전 미구축/미매칭이면 regex parse_doro 로 폴백(시도·시군구 None)."""
    road = GAZ.get("road")
    if road:
        a = norm(s).split(",")[0]                          # 상세주소(쉼표 뒤) 제거
        t = a.split()
        if len(t) >= 2:
            sido = t[0]; sgg = ""
            for cand in GAZ["sgg"].get(sido, []):          # 시군구 화이트리스트 최장 접두('성동구난계로'→'성동구')
                if t[1] == cand or " ".join(t[1:3]) == cand or t[1].startswith(cand):
                    sgg = cand; break
            roads = road.get((sido, sgg)) or (road.get((sido, "")) if sido.startswith("세종") else None)
            if roads:
                rn = _JIHA.sub("", rnorm(a))               # '지하' 제거 후 정규화
                pre = rnorm(sido) + rnorm(sgg)             # 시도+시군구 접두를 먼저 consume → 남은 데서 도로명 검색
                if rn.startswith(pre): rn = rn[len(pre):]  # ('종로'가 '종로구' 안에서 잘못 매칭되는 충돌 방지)
                for rd in roads:                           # 남은 부분에서 실재 도로명 최장일치(가지번호길까지 정확)
                    if rd and rd in rn:
                        m = re.match(r"(\d+)(?:-(\d+))?", rn[rn.find(rd) + len(rd):])
                        if m:
                            mno = _mainno(m.group(1))        # 본번 int4 가드 — 비정상이면 폴백(regex parse_doro)
                            if mno is None: break
                            return (rd, rd, mno, _subno(m.group(2)), sido, sgg)
                        break
    r, rnm, mno, sno = parse_doro(s)                       # 폴백
    return (r, rnm, mno, sno, None, None)


# ── 지번 폴백 — 도로명으로 못 푸는 시설(도로칸에 지번 기입·지번만 보유)을 법정동+본/부번으로 navi 조인.
_JIBUN_RE = re.compile(r"(\S*[동리가])\s+(산)?\s*(\d+)(?:-(\d+))?")
def _navi_jibun_key(sido, sgg, jibun_text):
    # navi addr 의 jibun 텍스트('혜화동 1-21'·'신영동 산 2-13') → (시도,시군구,법정동norm,산,본번,부번)
    m = _JIBUN_RE.search(norm(jibun_text or ""))
    if not m: return None
    mno = _mainno(m.group(3))                              # 지번 본번 int4 가드 — 비정상이면 키 불성립
    if mno is None: return None
    return (sido, sgg, rnorm(m.group(1)), 1 if m.group(2) else 0, mno, _subno(m.group(4)))
def _facility_jibun_key(jibun_src):
    # 시설 지번주소(또는 도로명칸에 기입된 지번) → 조인키. 시군구는 사전 화이트리스트로 보정(navi 포맷 '수원시 영통구').
    a = norm(jibun_src or ""); t = a.split()
    if len(t) < 3: return None
    sido = t[0]; sgg = ""
    for cand in GAZ.get("sgg", {}).get(sido, []):
        if t[1] == cand or " ".join(t[1:3]) == cand or t[1].startswith(cand):
            sgg = cand; break
    return _navi_jibun_key(sido, sgg, a)

def _norm_sgg(sido, sgg):       # 결정4(M1): 시군구 navi 표기 정규화. 완전일치만 치환, 빈값(세종)·미매칭은 원본 유지(승급형 접두매칭 금지).
    if not sgg: return sgg                        # 빈값(세종 등) 그대로 — 임의 '구' 오승급 차단
    for cand in GAZ.get("sgg", {}).get(sido, []):
        if sgg == cand: return cand               # 완전일치만 치환(접두/최장일치 승급 금지)
    return sgg                                    # 미매칭 → 원본 유지(하위호환)

def _split_emd_ri(jibun_txt, sido, sgg):   # 결정2: 지번 텍스트 → (법정동 bjd, 리 ri). 동지역 ri=None. 좌표 PIP 아님(순수 파싱).
    t = norm(jibun_txt or "").split()
    if len(t) < 2: return (None, None)
    i = 1 if t[0] == sido else 0               # 시도 consume
    rest = t[i:]
    if sgg:                                    # 시군구 consume (1~2토큰; GAZ 화이트리스트와 동일 규칙)
        sgg_tok = sgg.split()
        if rest[:len(sgg_tok)] == sgg_tok: rest = rest[len(sgg_tok):]
    bjd = ri = None
    for j, tok in enumerate(rest):
        if bjd is None and tok.endswith(("동", "읍", "면", "가")):   # (m1) 법정동 = endswith 종결 토큰
            bjd = tok
            if j+1 < len(rest) and rest[j+1].endswith("리"): ri = rest[j+1]   # 다음 토큰이 리(면/읍 하위)면 채움
            break
    return (bjd, ri)
# biz 중복(대표) 판정 키 = biznrm. 정본은 scripts/_common/textnorm.py 의 biznrm_nfc 다(위 import).
# 여기는 NFC, dedup_er.py 는 NFKC 로 실재 발산(전수 9,061행)하며 의도된 것 — 근거와
# 과거 허위주석("12-build-poi.sh _nrm 와 동일")의 전말은 textnorm 모듈 docstring 에 옮겼다.
def search_text(name, is_station):
    name=norm(name); v={name, name.replace(' ','')}
    if is_station:
        base=name[:-1] if name.endswith('역') else name
        v|={base, base+'역', base.replace(' ',''), (base+'역').replace(' ','')}
    return ' '.join(x for x in v if x)

SCHEMA = """
  PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF; PRAGMA cache_size=-1048576; PRAGMA temp_store=MEMORY;
  CREATE TABLE places(id INTEGER PRIMARY KEY, kind TEXT, name TEXT, subtype TEXT,
    sido TEXT,sigungu TEXT,emd TEXT,ri TEXT,road TEXT,road_norm TEXT,main_no INTEGER,sub_no INTEGER,
    bld TEXT,postal TEXT,haeng_dong TEXT,bd_mgt_sn TEXT,bcode TEXT,hcode TEXT, phone TEXT,opened TEXT, jibun TEXT,cat1 TEXT,cat2 TEXT, source TEXT,is_primary INTEGER, lon REAL,lat REAL);
  CREATE VIRTUAL TABLE places_fts USING fts5(name, region, road, bld,
    content='places', content_rowid='id', tokenize='unicode61', prefix='2 3');
  CREATE VIRTUAL TABLE place_rtree USING rtree(id,minlon,maxlon,minlat,maxlat);
  CREATE TABLE areas(id INTEGER PRIMARY KEY, name TEXT, type TEXT, rings TEXT, code TEXT);
  CREATE VIRTUAL TABLE area_rtree USING rtree(id,minlon,maxlon,minlat,maxlat);
"""

def pk6_jibun(c):
    """match_jibun 한 행에서 설계 PK6 튜플을 뽑는다."""
    return tuple(c[i] for i in PK6_JIBUN)


def pk6_build(c):
    """match_build 한 행에서 설계 PK6 튜플을 뽑는다."""
    return tuple(c[i] for i in PK6_BUILD)


def assemble_jibun(c, ri):
    """지번 표기 조립. **구 :218-219 와 바이트 단위로 동일한 문자열**을 낸다. 규칙을 바꾸지 마라.

    조인 키만 바꾸는 것이 이번 변경의 범위다. 여기서 표기까지 손대면 A/B 대조에서
    '조인 교체의 효과'와 '표기 변경의 효과'가 섞여 어느 쪽도 측정할 수 없게 된다.
    """
    dong = f"{c[3]} {ri}" if ri else c[3]
    san = "산 " if c[5] == "1" else ""
    bu = c[7]
    return f"{dong} {san}{c[6]}" + (f"-{bu}" if bu and bu != "0" else "")


def load_jibun(src, sido, state):
    # match_jibun_<시도>.txt → (PK6→대표지번, PK6→리(里), PK6→법정동코드) 세 dict.
    # c[0]=법정동코드(시군구5+읍면동3+리2), c[3]=법정동(면), c[4]=리(시골만; 동지역 빈값),
    # c[5]=산, c[6]=본번, c[7]=부번, c[12]=지번일련번호(PK5), c[18]=건물관리번호, c[19]=PK6.
    # ri dict 는 navi addr 의 ri 컬럼(X5) 산출용, 법정동코드 dict 는 bcode 컬럼용(변경 B).
    # ★ 정정(T018, 2026-08-10) — 구 주석의 "법정동코드(mgt[:10]) 단위 리 1:1(면지역만 존재)"은 **거짓**이다.
    #   mgt 는 건물관리번호이고 앞 10자리는 **건물 등록 시점의 법정동코드로 동결**돼 이후 개편을
    #   반영하지 않으며, 끝 2자리(리 자리)도 이 파일에서는 사실상 구분자 역할을 못 한다.
    #   [측정 T018 §A-2] mgt[:10] 키 19,058개 중 **2,699개**가 서로 다른 리를 2개 이상 포함 →
    #   setdefault 가 첫 값만 남기고 **2,698개 키가 붕괴**했다. 이것이 lawd_ri 오염의 근본 원인이다.
    # ★ 정합률 수치 주의 (T018 검증 반영, 2026-08-10) — **아래 두 값을 직접 비교하지 말 것.**
    #   · 구 기준선 97.08% (6,884,543건 중 201,298건 불일치)
    #     → **[재현 실패 — 산출 정의 불명]**. 이 수치는 저장소 전역에 **산문으로만** 존재하고
    #       정의 SQL 이 어디에도 없다. 정의를 6가지(V1~V6)로 바꿔 전수 재시도했으나 어느 것도
    #       재현하지 못했다. 이력으로만 남긴다.
    #   · 실측 재구성치 **96.9384%** (대상 6,892,473 / 불일치 211,017)
    #     → 정의 V1 을 명시한 값: kind='addr' AND ri 비어있지 않음 AND jibun 존재 →
    #       jibun 첫 토큰 == ri. 불일치 분해 = 지번에 리 토큰 자체가 없음 145,317
    #       + 리 토큰이 있으나 ri 와 다름 65,700.
    #   두 값은 **정의가 다르므로 비교가 성립하지 않는다.** "97.08% → 96.94% = 회귀"로 읽으면 오독이다.
    #   근거: 검증 보고서 `verification-report.md` §4-1·§4-2 (기준선 재현 실패 절).
    # ※ 이번 수정의 범위는 **관측뿐**이다 — 값 선택 규칙(첫 값 유지)은 그대로 두어 산출물이 바뀌지
    #   않게 한다. 키를 무엇으로 바꿀지(예: mgt[:10]+리명, 지번 기반)는 원천 재적재를 수반하므로
    #   후속 태스크에서 결정한다. 지금은 "조용히 뭉개던 것"을 "세어서 알리는 것"으로만 바꾼다.
    # ★ 후속 처분(T043, 2026-08-21) — 위 A-4 가 남긴 "키를 무엇으로 바꿀지"에 답이 나왔다.
    #   원천이 **설계해 둔** 복합키(PK6)가 있었다. mgt[:10] 파생키를 버리고 PK6 으로 바꾼다.
    #   · mgt[:10] 붕괴 키 2,699개 → PK6 에서는 **0**(중복은 게이트 G9 가 0 을 강제).
    #   · 조인 적중 59.89% → **100.00%**(T042 전국 실측, 미적중 0).
    #   · 대표지번 없는 건물이 사라지므로 `_derive_jibun` 파생 경로는 호출수 0 이 되어 삭제했다.
    #   A-4 의 관측 상수 3종은 사문이 되어 삭제했다(:85 주석에 계보를 남겼다).
    #   실제 계수는 state["pk_dup"](→ G9)가 대신한다. 계수기를 없애지 말라는 T042 §B-1 주의 이행.
    jd, rid, bcd = {}, {}, {}
    p = src / f"match_jibun_{sido}.txt"
    if not p.exists():
        return jd, rid, bcd                     # 침묵 — 이 침묵은 게이트 G2 가 실패로 승격시킨다
    with io.open(p, encoding="cp949", errors="replace") as fh:   # (Minor-6) ResourceWarning 제거
        for line in fh:
            c = line.rstrip("\n").split("|")
            if len(c) < 20: continue            # c[19]=PK6 을 읽으므로 19 가 아니라 20 이다
            if c[12] != "0": continue           # 대표지번(PK5=지번일련번호 0)만 — T042 §A-6
            k = pk6_jibun(c)
            if k in jd:
                state["pk_dup"] += 1            # G9 가 0 을 요구한다. 첫 값을 유지한다.
                continue
            ri = c[4].strip() or None           # 리(里) — 면 단위 지번에만 존재(동지역은 빈값→None)
            jd[k] = assemble_jibun(c, ri)
            rid[k] = ri
            bcd[k] = c[0]                       # 변경 B: 리 2자리를 포함한 지번측 법정동코드
    return jd, rid, bcd

def add_juso(db, src, only, state):
    pid = state["pid"]; seen = state["seen"]
    for s in effective_sido(only):
        path = src / f"match_build_{s}.txt"
        if not path.exists():
            print(f"  (건너뜀) {path.name} 없음", file=sys.stderr); continue   # 게이트 G2 가 잡는다
        jd, rid, bcd = load_jibun(src, s, state)
        st=time.time(); n0=pid; pb=[]; fb=[]; rb=[]
        with io.open(path, encoding="cp949", errors="replace") as fh:   # (Minor-6) ResourceWarning 제거
            for line in fh:
                c=line.rstrip("\n").split("|")
                if len(c)<27: continue
                try: E=float(c[25]); N=float(c[26])
                except ValueError: continue
                mgt=c[10]
                if mgt in seen: continue
                seen.add(mgt)
                lon,lat=utmk_to_wgs84(E,N)
                if not (LON_LO<=lon<=LON_HI and LAT_LO<=lat<=LAT_HI): continue
                k=pk6_build(c)                                       # 원천 설계 PK6 조인(T042: 전국 적중 100.00%)
                jb=jd.get(k)
                if jb is None:
                    state["pk_miss"]+=1                              # G8 이 0 을 요구한다
                    continue
                ri_v=rid.get(k)
                bv=bcd.get(k)
                bcode_v=c[0] if bv is None else bv                   # 빈 문자열에는 폴백하지 않는다(도달불가 방어분기)
                pid+=1; road=c[5]; rn=rnorm(road); mno=int(c[7] or 0); sno=int(c[8] or 0)
                bld=" ".join(dict.fromkeys([x for x in (c[11],c[19]) if x.strip()]))
                pb.append((pid,'addr',None,None,c[1],c[2],c[3],ri_v,road,rn,mno,sno,bld,c[9],c[14],mgt,bcode_v,c[13],None,None,jb,None,None,'navi',1,lon,lat))  # bcode=지번측 법정동코드(변경 B)·hcode=c[13]·ri=리(X5, PK6키)
                fb.append((pid,'',f"{c[1]} {c[2]} {c[3]} {c[14]}",f"{road} {rn}",bld))
                rb.append((pid,lon,lon,lat,lat))
                if len(pb)>=50000:
                    _flush(db,pb,fb,rb); pb.clear(); fb.clear(); rb.clear()
        _flush(db,pb,fb,rb)
        print(f"  addr:{s:10s} +{pid-n0:>8,}  ({time.time()-st:.1f}s)", file=sys.stderr)
    state["pid"]=pid

def add_osm(db, osm_path, state):
    if not pathlib.Path(osm_path).exists():
        print(f"  (건너뜀) OSM {osm_path} 없음 — 주소 전용으로 빌드", file=sys.stderr); return
    pid=state["pid"]; o=sqlite3.connect(f"file:{osm_path}?mode=ro", uri=True); st=time.time(); n0=pid
    pb=[]; fb=[]; rb=[]
    for name,typ,sub,lon,lat in o.execute("SELECT name,type,subtype,lon,lat FROM places"):
        if lon is None or lat is None: continue
        pid+=1
        oc=CW_OSM.get(sub or ''); oc1,oc2=(oc[0],oc[1] or None) if oc else (None,None)   # canonical 카테고리
        pb.append((pid,typ,name,sub,None,None,None,None,None,None,None,None,None,None,None,None,None,None,None,None,None,oc1,oc2,'osm',1,lon,lat))  # +ri자리 None(X5)
        fb.append((pid,search_text(name, typ=='station'),'','',''))
        rb.append((pid,lon,lon,lat,lat))
        if len(pb)>=50000: _flush(db,pb,fb,rb); pb.clear(); fb.clear(); rb.clear()
    _flush(db,pb,fb,rb)
    # 영역(행정동 등) 그대로 복사 — 역지오코딩 포함영역용
    try:
        db.execute(f"ATTACH DATABASE '{osm_path}' AS o")
        db.execute("INSERT INTO areas SELECT * FROM o.areas")
        db.execute("INSERT INTO area_rtree SELECT * FROM o.area_rtree")
        na=db.execute("SELECT count(*) FROM areas").fetchone()[0]
        db.execute("DETACH DATABASE o")
    except sqlite3.OperationalError as e:
        na=0; print(f"  (areas 복사 스킵: {e})", file=sys.stderr)
    o.close(); state["pid"]=pid
    print(f"  osm: +{pid-n0:>8,} · areas {na:,}  ({time.time()-st:.1f}s)", file=sys.stderr)

def add_biz(db, csvdir, state):
    # 소상공인 상가(상권)정보 CSV(시도별) → kind='biz'. 경도/위도 이미 WGS84.
    # facility(생활편의) 중 좌표 없는 행(주소만)은 pending 으로 모아 반환 → geocode_facilities 가 navi 로 지오코딩.
    import csv, glob
    pid=state["pid"]; st=time.time(); n0=pid; pb=[]; fb=[]; rb=[]; pending=[]
    for path in sorted(glob.glob(os.path.join(csvdir,"**","*.csv"), recursive=True)):
        base = os.path.basename(path)   # 출처·종류 구분(파일명)
        if base == 'facility_clean.csv': src, kind = 'facility', 'facility'   # 생활편의시설 — biz 와 분리 적재
        elif base == 'localdata_clean.csv': src, kind = 'localdata', 'biz'
        else: src, kind = 'sangga', 'biz'
        with open(path, encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                nm=(row.get("상호명") or "").strip()
                if not nm or nm in ("업소명없음", "상호명없음", "-", "."): continue   # 원본 플레이스홀더 제외
                biz=(row.get("상권업종소분류명") or "").strip()
                sido=(row.get("시도명") or "").strip(); sgg=(row.get("시군구명") or "").strip()
                haeng=(row.get("행정동명") or "").strip() or None   # CSV 행정동명 → haeng_dong 자리/폴백 전용(emd 아님; M2)
                phone=(row.get("전화번호") or "").strip() or None; opened=(row.get("인허가일자") or "").strip() or None
                cat1=(row.get("상권업종대분류명") or "").strip() or None
                cat2=(row.get("상권업종중분류명") or "").strip() or None
                if kind=='facility':     # 시설=좌표없으면 이 파싱이 곧 좌표 → navi 도로명 사전 최장일치(더러운입력 견고) + 시군구 보정
                    road,rn,mno,sno,g_sido,g_sgg = parse_doro_gaz(row.get("도로명주소") or "")
                    if g_sido: sido=g_sido
                    if g_sgg is not None: sgg=g_sgg
                else:                    # 상가/인허가=이미 좌표보유(주소 깨끗) → 기존 regex(건물키 조인용)
                    road,rn,mno,sno = parse_doro(row.get("도로명주소") or "")
                jibun_txt=(row.get("지번주소") or "").strip() or None
                sgg = _norm_sgg(sido, sgg)                          # 결정4(M1): 시군구 navi 표기 정규화(완전일치만; g_sgg 보정 후 입력)
                bjd, ri = _split_emd_ri(jibun_txt, sido, sgg)       # 결정1·2: 지번 텍스트 → 법정동(bjd)/리(ri). 첫 원소=법정동
                if src=='localdata':                                   # localdata 카테고리를 canonical 로 표준화
                    cc=CW_LD.get(f"{cat1}|{biz}")
                    if cc: cat1,cat2=cc[0],(cc[1] or None)
                try: lon=float(row.get("경도") or ""); lat=float(row.get("위도") or "")
                except ValueError: lon=lat=None
                if lon is None or not (124<=lon<=132 and 33<=lat<=39):
                    if kind=='facility':                               # 좌표없는 시설 → 도로명 또는 지번으로 지오코딩 대기
                        jibun_src = jibun_txt or ((row.get("도로명주소") or "").strip() or None)   # 지번칸 없으면 도로명칸(지번 기입분)
                        if (rn and mno is not None) or jibun_src:
                            pending.append((nm,biz,sido,sgg,haeng,road,rn,mno,sno,jibun_txt,cat1,cat2,jibun_src))  # rec[4]=haeng(행정동명) — emit 폴백 전용(M2)
                    continue
                pid+=1
                pb.append((pid,kind,nm,biz,sido,sgg,bjd,ri,road,rn,mno,sno,biz,None,haeng,None,None,None,phone,opened,jibun_txt,cat1,cat2,src,0,round(lon,6),round(lat,6)))  # emd=bjd(법정동)·ri=ri·haeng_dong=haeng(B2)
                fb.append((pid,nm," ".join(x for x in (sido,sgg,bjd,ri,haeng) if x),'',biz))   # FTS region: 시도·시군구·법정동·리·행정동(navi region 과 일관)
                rb.append((pid,lon,lon,lat,lat))
                if len(pb)>=50000: _flush(db,pb,fb,rb); pb.clear(); fb.clear(); rb.clear()
    _flush(db,pb,fb,rb)
    print(f"  biz: +{pid-n0:,}  ({time.time()-st:.1f}s)", file=sys.stderr); state["pid"]=pid
    return pending


def geocode_facilities(db, pending, state):
    # 좌표 없는 facility 행 → navi 좌표. 1) 도로명 사전키 조인  2) 실패 시 지번(법정동+본/부번) 폴백.
    # navi 미존재 주소는 좌표없는 POI 라 버린다(insert 안 함). FTS·rtree 까지 정상행만 적재.
    # rec = (nm,biz,sido,sgg,haeng,road,rn,mno,sno,jibun_txt,cat1,cat2,jibun_src)  ← rec[4]=CSV 행정동명(haeng), emd 폴백 전용(M2)
    if not pending: return
    st=time.time()
    db.execute("CREATE INDEX IF NOT EXISTS idx_addr_key ON places(sido,sigungu,road_norm,main_no,sub_no) WHERE kind='addr'")
    cur=db.cursor(); pb=[]; fb=[]; rb=[]
    def emit(rec, lon, lat, n_bjd, n_ri, n_haeng):     # navi addr 매칭행의 법정동(n_bjd)/리(n_ri)/행정동(n_haeng) 캐리(결정3)
        nm,biz,sido,sgg,haeng_csv,road,rn,mno,sno,jibun_txt,cat1,cat2,_js = rec   # rec[4]=haeng_csv(CSV 행정동명)
        haeng = n_haeng or haeng_csv                  # navi c[14] 우선, null이면 CSV 행정동명 폴백(결정3/M2)
        state["pid"]+=1; pid=state["pid"]
        pb.append((pid,'facility',nm,biz,sido,sgg,n_bjd,n_ri,road,rn,mno,sno,biz,None,haeng,None,None,None,None,None,jibun_txt,cat1,cat2,'facility',1,round(lon,6),round(lat,6)))  # emd=n_bjd·ri=n_ri·haeng_dong=haeng(B2)
        fb.append((pid,nm," ".join(x for x in (sido,sgg,n_bjd,n_ri,haeng) if x),'',biz)); rb.append((pid,lon,lon,lat,lat))
        if len(pb)>=50000: _flush(db,pb,fb,rb); pb.clear(); fb.clear(); rb.clear()
    n_road=0; unmatched=[]
    for rec in pending:                                  # 1) 도로명 조인
        sido,sgg,rn,mno,sno = rec[2],rec[3],rec[6],rec[7],rec[8]
        r=cur.execute("""SELECT lon,lat,emd,ri,haeng_dong FROM places WHERE kind='addr'
            AND sido=? AND sigungu=? AND road_norm=? AND main_no=? AND sub_no=? ORDER BY id LIMIT 1""",
            (sido,sgg,rn,mno,sno)).fetchone() if (rn and mno is not None) else None
        if r: emit(rec, r[0], r[1], r[2], r[3], r[4]); n_road+=1   # r[2]=법정동→n_bjd, r[3]=ri, r[4]=haeng_dong
        elif rec[12]:                                    # 도로 실패 → 지번키 후보
            k=_facility_jibun_key(rec[12])
            if k: unmatched.append((rec,k))
    _flush(db,pb,fb,rb); pb.clear(); fb.clear(); rb.clear()
    n_jibun=0
    if unmatched:                                        # 2) 지번 폴백 — 필요한 시군구의 addr 만 스캔해 지번키→좌표
        need={k for _,k in unmatched}; need_sgg={(k[0],k[1]) for k in need}
        sggs=sorted({g for _,g in need_sgg}); ph=",".join("?"*len(sggs)); jc={}
        for s,g,jb,lo,la,e,ri_,hd in db.execute(
                f"SELECT sido,sigungu,jibun,lon,lat,emd,ri,haeng_dong FROM places WHERE kind='addr' AND jibun IS NOT NULL AND sigungu IN ({ph})", sggs):
            if (s,g) not in need_sgg: continue            # sigungu 동명이(시도 다름) 걸러냄
            kk=_navi_jibun_key(s,g,jb)
            if kk in need and kk not in jc: jc[kk]=(lo,la,e,ri_,hd)   # e=법정동(emd)→n_bjd, ri_=리, hd=haeng_dong 캐리
        for rec,k in unmatched:
            co=jc.get(k)
            if co: emit(rec, co[0], co[1], co[2], co[3], co[4]); n_jibun+=1   # co[2]=법정동→n_bjd
        _flush(db,pb,fb,rb)
    db.execute("DROP INDEX IF EXISTS idx_addr_key")
    print(f"  [geocode] facility 도로 {n_road:,} + 지번폴백 {n_jibun:,} = {n_road+n_jibun:,}/{len(pending):,} ({time.time()-st:.1f}s)", file=sys.stderr)


def build_gazetteer(db):
    """navi addr(kind='addr')에서 도로명 사전(GAZ) 구축 — parse_doro_gaz 가 더러운 주소를 실재 도로명에 최장일치시킴.
    add_juso 직후(navi 적재 완료) 1회 호출. (시도,시군구)→도로명집합, 시도→시군구목록(화이트리스트)."""
    import collections
    st=time.time(); road=collections.defaultdict(set); sgg=collections.defaultdict(set)
    for s,g,rn in db.execute("SELECT DISTINCT sido,sigungu,road_norm FROM places WHERE kind='addr' AND road_norm IS NOT NULL"):
        road[(s,g)].add(rn); sgg[s].add(g)
    GAZ["road"]={k:sorted(v,key=len,reverse=True) for k,v in road.items()}
    GAZ["sgg"]={k:sorted(v,key=len,reverse=True) for k,v in sgg.items()}
    print(f"  [gazetteer] 도로명 {sum(len(v) for v in road.values()):,}개 · {len(road):,}시군구 ({time.time()-st:.0f}s)", file=sys.stderr)

def _flush(db,pb,fb,rb):
    if not pb: return
    db.executemany("INSERT INTO places VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",pb)
    db.executemany("INSERT INTO places_fts(rowid,name,region,road,bld) VALUES(?,?,?,?,?)",fb)
    db.executemany("INSERT INTO place_rtree VALUES(?,?,?,?,?)",rb)

def write_taxonomy(db, out_path):
    """biz의 대>중>소 분류 트리를 style/poi-taxonomy.json 으로 재생성(스튜디오 티어/아이콘 목록용)."""
    import collections
    tree = collections.OrderedDict(); cnt = collections.Counter()
    q = ("SELECT cat1,cat2,subtype,count(*) c FROM places WHERE kind IN ('biz','facility') AND cat1 IS NOT NULL "
         "GROUP BY cat1,cat2,subtype ORDER BY cat1, c DESC")
    for cat1, cat2, sub, c in db.execute(q):
        cnt[cat1] += c
        t = tree.setdefault(cat1, collections.OrderedDict())
        m = t.setdefault(cat2 or "(기타)", [])
        if sub and sub not in m:
            m.append(sub)
    ordered = collections.OrderedDict(sorted(tree.items(), key=lambda kv: -cnt[kv[0]]))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps({"cat1_order": list(ordered.keys()), "tree": ordered}, ensure_ascii=False),
                        encoding="utf-8")


# ==== 빌드 안전 게이트 (T043) =================================================
# 규약: 모든 게이트는 `tmp.replace(out)` **앞에서** 판정한다. DB 를 읽을 때는 커밋·종료 후
#       `file:{tmp}?mode=ro` 로 **다시 열고** 읽자마자 닫는다. 게이트는 절대 쓰지 않는다.
#       하나라도 실패하면 tmp 를 남기고 out 은 건드리지 않은 채 exit 2.

def effective_sido(only):
    """이번 실행이 실제로 훑을 시도 목록. add_juso 와 게이트가 **같은 식**을 써야 한다.
    두 곳에서 따로 계산하면 G0 가 실제 처리 범위와 어긋난다."""
    return [x for x in SIDO if (not only or x in only)]


def _home_dirs():
    """정본 후보 디렉터리 경로 집합.

    `expanduser()` 는 `$HOME` 을 **그대로 믿는다**. cron·systemd·`sudo -H`·
    `docker run -e HOME=...` 이 전부 `$HOME` 을 바꿔 놓을 수 있고, 그때 `~/geocode-build`
    는 엉뚱한 곳을 가리켜 정본이 정본으로 인식되지 않는다(T043 검수 C-1: 전 게이트 PASS
    · exit 0 · 정본 교체까지 관통). 그래서 환경변수와 무관한
    `pwd.getpwuid(os.getuid()).pw_dir` 를 **항상** 후보에 더한다.

    BUILD_HOME 은 '추가'이지 '대체'가 아니다 — 환경변수는 후보를 늘릴 수만 있고
    정본 경로를 후보에서 **뺄 수는 없다**.
    """
    prefixes = []
    h = os.environ.get("HOME")
    if h:
        prefixes.append(h)
    try:
        prefixes.append(pwd.getpwuid(os.getuid()).pw_dir)   # $HOME 과 무관한 진짜 홈
    except (KeyError, OSError):
        pass
    homes = set()
    for c in CANONICAL_HOMES:
        if c == "~" or c.startswith("~/"):
            tail = c[2:]
            for p in prefixes:
                homes.add(pathlib.Path(p) / tail if tail else pathlib.Path(p))
        else:
            homes.add(pathlib.Path(c))
    bh = os.environ.get("BUILD_HOME")
    if bh:
        homes.add(pathlib.Path(bh).expanduser())
    return homes


def _abspath(p):
    """존재하지 않아도 죽지 않는 절대경로화."""
    try:
        return pathlib.Path(p).expanduser().resolve()
    except OSError:
        return pathlib.Path(os.path.abspath(os.path.expanduser(str(p))))


def _dir_id(p):
    """디렉터리의 (st_dev, st_ino). 없으면 None."""
    try:
        st = os.stat(str(p))
    except OSError:
        return None
    return (st.st_dev, st.st_ino)


def normalize_arg_paths(args):
    """경로 인자를 **한 지점에서 1회** 정규화한다 (T043 2차 검수 N-1).

    같은 경로 문자열을 두고 소비 지점마다 술어가 갈렸다 —
    `gate_inputs` 는 `Path(x).expanduser().exists()`, `gate_rowcounts` 와
    `add_osm` 은 `Path(x).exists()` 를 썼다. `--osm ~/geocode-build/osm.sqlite`
    처럼 셸이 확장하지 않은 **리터럴 틸데**를 주면(Docker `-e`, systemd
    `Environment=`, JSON/YAML 설정에서 흔하다) G2 는 "있다", 나머지 둘은
    "없다"로 판정이 쪼개진다. 그 결과 osm 594,704 행이 통째로 빠진 산출물이
    11개 게이트를 전부 통과한다.

    술어를 지점마다 고치지 않는 이유: 앞으로 늘어날 **네 번째 소비 지점**을
    못 덮는다. 입력을 정규화하면 그 지점이 어떤 술어를 쓰든 같은 답을 본다.

    `--out` 도 같은 구조다. 정규화하지 않으면 `targets_canonical()` 이 판정한
    경로와 `tmp.replace(out)` 이 실제로 쓰는 경로가 갈라진다. `BUILD_HOME` 이
    리터럴 틸데면 기본 `--out` 이 상대경로 `~/geocode-build/geocode.sqlite` 가
    되어 작업 디렉터리 아래 `./~/geocode-build` 를 만든다.

    **G0 는 이 정규화로 바뀌지 않는다.** `targets_canonical()` 은 내부에서
    `_abspath()` → `.expanduser().resolve()` 를 이미 거치므로 정규화 전후의
    판정이 동일하다 — 약화도, 정당한 부분 빌드에 대한 과잉차단도 아니다.
    바뀌는 것은 **실제로 쓰는 경로가 게이트가 판정한 경로와 일치**한다는 점뿐이다.

    빈 문자열은 건드리지 않는다(`Path("").expanduser()` 는 `.` 이 되어 없던
    경로를 있는 것으로 만든다).
    """
    if args.osm:
        args.osm = str(pathlib.Path(args.osm).expanduser())
    if args.out:
        args.out = str(pathlib.Path(args.out).expanduser())
    return args


def targets_canonical(out_path):
    """산출 경로가 정본 디렉터리(또는 그 하위)를 겨누는가.

    두 판정의 **합집합**이다. 뒤엣것이 앞엣것을 좁히지 않으므로 폴백이 새 구멍이 되지 않는다.
      (a) 아이노드 판정 — `$HOME` 변조·심볼릭링크·상대경로·`..` 를 한꺼번에 무력화한다.
          같은 디렉터리를 어떤 경로로 부르든 (st_dev, st_ino) 는 같기 때문이다.
      (b) 경로 문자열 판정 — 정본 디렉터리가 아직 없는 **첫 빌드**용 폴백.
          디렉터리가 없으면 stat 이 실패해 (a)가 아무것도 못 하므로 반드시 필요하다.
    """
    homes = _home_dirs()
    hids = {i for i in (_dir_id(h) for h in homes) if i is not None}
    out = _abspath(out_path)
    chain = [out.parent, *out.parent.parents]
    for d in chain:                                   # (a) 아이노드
        if _dir_id(d) in hids:
            return True
    hstr = {_abspath(h) for h in homes}
    return any(d in hstr for d in chain)              # (b) 경로 문자열


def is_full_rebuild(args, eff):
    """전국 재빌드인가. 인자의 겉모습(--only 유무)이 아니라 **의미**로 판정한다."""
    covers_all = set(eff) >= set(SIDO)                     # (a) 범위가 전국을 덮는가
    return covers_all or targets_canonical(args.out)       # (b) 정본을 겨누는가


def _fail(name, actual, expected, note=""):
    return GateResult(name, "FAIL", actual, expected, note)


def _ok(name, actual, expected, note=""):
    return GateResult(name, "PASS", actual, expected, note)


def gate_full_rebuild(args, eff):
    """G0 — 전국 재빌드 차단. T018 리 백필 처분(F1) 전까지 정본 갱신을 막는다."""
    n = "G0 전국재빌드차단"
    a = f"범위={len(eff)}/{len(SIDO)} 시도, out={args.out}"
    if not is_full_rebuild(args, eff):
        return _ok(n, a, "부분빌드")
    if getattr(args, "t018_disposed", False):
        return _ok(n, a, "-", "전국빌드 — --t018-disposed 로 해제됨")
    return _fail(n, a, "부분빌드", "전국 재빌드 차단")


def gate_args(args, only):
    """G1 — 인자 정합. `--only` 슬러그가 전부 SIDO 에 있는가.

    `--src` 누락은 argparse(required=True)가 exit 2 로 먼저 잡으므로 여기서 다시 보지 않는다.
    `--src` 경로의 실재 여부는 G2 가 파일 단위로 판정한다.
    """
    n = "G1 인자정합"
    bad = sorted(set(only) - set(SIDO)) if only else []
    if bad:
        return _fail(n, f"미상 슬러그 {bad}", "SIDO 소속", f"허용: {','.join(SIDO)}")
    cnt = f"--only 슬러그 {len(only)}종 유효" if only else "--only 미지정(전국)"
    return _ok(n, f"--src 지정, {cnt}", "-")


def gate_inputs(src, eff, args=None):
    """G2 — 입력 존재. 선택된 모든 시도에 build/jibun 이 **둘 다** 있어야 한다.
    add_juso/load_jibun 의 조용한 건너뜀·빈 dict 반환을 실패로 승격시키는 것이 이 게이트의 일이다.

    [T043 검수 Minor-2] `--osm` 도 여기서 함께 본다. add_osm 은 경로가 없으면
    "(건너뜀) OSM ... 없음" 한 줄만 남기고 지나가고, gate_rowcounts 는 그때 G3 기준선에서
    BASELINE_OSM(594,704)을 **빼 버린다**. 그래서 `--osm` 경로에 오타가 나면 osm 유래 행이
    통째로 사라진 산출물이 낮아진 기준선에 맞춰 G3 를 통과한다 — 기준선을 공격자(또는 오타)가
    고를 수 있다는 뜻이다.

    다만 **모든 osm 부재를 실패로 삼으면 과잉 차단**이 된다. `--osm` 은 미지정 시
    $BUILD_HOME/osm.sqlite 를 관례적으로 집는데, 그 파일이 없는 환경(첫 빌드·배포서버·CI)에서
    주소전용 빌드는 정당한 실행이다. 그래서 두 경우에만 실패로 승격시킨다.

      · `--osm` 을 **명시했는데** 그 경로가 없다 — 오타이거나 기준선 낮추기 시도다.
      · **정본을 겨누는 빌드**인데 osm 이 없다 — 정본은 osm 유래 594,704 행을 담아야 하는
        산출물이므로, 그것이 통째로 빠진 채 낮아진 기준선으로 G3 를 통과해서는 안 된다.

    명시 여부는 main() 이 parse_args 직후에 세우는 args.osm_explicit 로 판별한다. argparse
    기본값을 그대로 두면 "지정했는가"를 사후에 알 수 없어 이 구분 자체가 불가능하다."""
    n = "G2 입력존재"
    nb = [s for s in eff if not (src / f"match_build_{s}.txt").exists()]
    nj = [s for s in eff if not (src / f"match_jibun_{s}.txt").exists()]
    have = f"build {len(eff)-len(nb)}/{len(eff)}, jibun {len(eff)-len(nj)}/{len(eff)}"
    osm = getattr(args, "osm", None) if args is not None else None
    gone = bool(osm) and not pathlib.Path(osm).expanduser().exists()
    explicit = bool(getattr(args, "osm_explicit", False)) if args is not None else False
    out = getattr(args, "out", None) if args is not None else None
    canon = bool(out) and targets_canonical(out)
    osm_missing = gone and (explicit or canon)
    if gone:
        have += ", osm 0/1" + ("" if osm_missing else "(주소전용 — 정본 아님·미지정)")
    if nb or nj or osm_missing:
        why = []
        if nb or nj:
            why.append(f"없음 build={nb} jibun={nj}")
        if osm_missing:
            why.append(("--osm 을 지정했으나 " if explicit else "정본을 겨눈 빌드인데 ")
                       + f"{osm} 이 없다 — G3 기준선이 조용히 낮아진다")
        return _fail(n, have, "전부 존재", " · ".join(why))
    return _ok(n, have, "전부 존재")


def gate_ratios(args):
    """게이트 임계 3종을 인자 우선으로 확정한다. (창하한, 창상한, 시도당 절대하한)"""
    lo = getattr(args, "min_rows_ratio", None)
    hi = getattr(args, "max_rows_ratio", None)
    mp = getattr(args, "min_rows_per_sido", None)
    lo = GATE_LO if lo is None else float(lo)
    hi = GATE_HI if hi is None else float(hi)
    # 3층은 **올릴 수만 있다**. 명령줄로 바닥을 낮출 수 있으면 바닥이 아니기 때문이다.
    mp = GATE_MIN_PER_SIDO if mp is None else max(GATE_MIN_PER_SIDO, int(mp))
    return lo, hi, mp


def gate_rowcounts(n_places, n_addr, eff, args):
    """G3·G4 — 행수. 세 층이 **각각 독립으로** 반증한다.

      1층 비례 창    [lo·base, hi·base]   — `--min/max-rows-ratio` 로 조절 가능
      2층 비례 바닥  GATE_FLOOR·base      — 조절 불가. 창을 넓혀도 남는다
      3층 절대 바닥  min_per_sido·시도수  — 기준선이 없어도, 창과 무관하게 항상 선다

    기본값에서는 1층 하한(0.98)이 2층(0.93)보다 높아 2층이 가려진다. 그것이 정상이다 —
    2층은 `--min-rows-ratio 0.10` 처럼 창을 넓혔을 때 비로소 일하는 최후 방벽이기 때문이다.
    (T043 검수 Minor-1 이 "GATE_FLOOR 는 사문"이라 지적했으나, 창이 인자화된 지금
     사문이 아니다. 다만 주석의 "-2%~+10%" 고정 서술은 거짓이었으므로 바로잡았다.)

    기준선 없는 시도가 섞이면 1·2층은 계산할 수 없어 SKIP 이지만, **3층은 그대로 판정한다** —
    검수 C-2 가 실증한 "0행 전국 산출이 SKIP 을 타고 통과" 는 여기서 FAIL 로 끊긴다.
    SKIP 은 `--allow-gate-skip` 으로 넘길 수 있어도 FAIL 은 넘길 수 없다.
    """
    lo, hi, min_per = gate_ratios(args)
    abs_min = min_per * len(eff)          # 3층 — 기준선과 무관
    miss = [s for s in eff if s not in BASELINE_ADDR]

    if miss:
        why = f"기준선 없는 시도 {miss} — 비례 판정 불가"
        g3 = GateResult("G3 places 행수", "SKIP", f"{n_places:,}", "?", why)
        if n_addr < abs_min:
            g4 = _fail("G4 addr 행수", f"{n_addr:,}", f">={abs_min:,}",
                       f"{why} · **절대하한 미달**({min_per:,}/시도 × {len(eff)}종)")
        else:
            g4 = GateResult("G4 addr 행수", "SKIP", f"{n_addr:,}", "?",
                            f"{why} · 절대하한 {abs_min:,} 은 통과")
        return g3, g4

    base_addr = sum(BASELINE_ADDR[s] for s in eff)
    comp = [f"addr {base_addr:,}"]
    base_places = base_addr
    if args.osm and pathlib.Path(args.osm).exists():
        base_places += BASELINE_OSM; comp.append(f"osm {BASELINE_OSM:,}")
    if getattr(args, "poi_csv_dir", None):
        base_places += BASELINE_POI; comp.append(f"poi/biz {BASELINE_POI:,}")
    win = lambda b: (int(b * lo), int(b * hi))
    plo, phi = win(base_places); alo, ahi = win(base_addr)
    floor = int(base_addr * GATE_FLOOR)
    pct = f"{(lo-1)*100:+.0f}%~{(hi-1)*100:+.0f}%"

    g3n = f"{n_places:,}  (기준 {base_places:,} = {' + '.join(comp)}, {pct})"
    g3 = (_ok if plo <= n_places <= phi else _fail)(
        "G3 places 행수", g3n, f"{plo:,}~{phi:,}")

    # 세 층을 따로 세워, 어느 층이 무너졌는지 판정문에 남긴다.
    #   이렇게 해야 각 임계 상수를 **단독으로** 반증하는 시험을 쓸 수 있다(검수 M-2).
    broke = []
    if not (alo <= n_addr <= ahi):
        broke.append(f"창({alo:,}~{ahi:,})")
    if n_addr < floor:
        broke.append(f"비례바닥({floor:,}=×{GATE_FLOOR})")
    if n_addr < abs_min:
        broke.append(f"절대바닥({abs_min:,}={min_per:,}×{len(eff)})")
    g4n = f"{n_addr:,}  (기준 {base_addr:,}, 비례바닥 {floor:,}, 절대바닥 {abs_min:,})"
    g4 = (_ok(  "G4 addr 행수", g4n, f"{alo:,}~{ahi:,}") if not broke
          else _fail("G4 addr 행수", g4n, f"{alo:,}~{ahi:,}", "미달/초과: " + " · ".join(broke)))
    return g3, g4


def gate_coords(con):
    """G5 — 좌표 범위. 검사 범위는 kind 무관 **전 행**(addr·osm·biz·facility 모두)."""
    n = "G5 좌표범위"
    q = con.execute(f"""SELECT count(*) FROM places
        WHERE lon IS NULL OR lat IS NULL
           OR lon NOT BETWEEN {LON_LO} AND {LON_HI}
           OR lat NOT BETWEEN {LAT_LO} AND {LAT_HI}""").fetchone()[0]
    return (_ok(n, "이탈 0", 0, "(전 kind 대상)") if q == 0
            else _fail(n, f"이탈 {q:,}행", 0, "(전 kind 대상)"))


def gate_areas(con, args):
    """G6 — areas 적재. `--areas` 를 요구하지 않은 실행에서는 N/A(=판정 대상 아님)."""
    n = "G6 areas"
    if getattr(args, "no_areas", False):
        return GateResult(n, "N/A", "--no-areas", "-", "")
    if not args.areas:
        return GateResult(n, "N/A", "--areas 미지정", "-", "")
    q = con.execute("SELECT count(*) FROM areas").fetchone()[0]
    return _ok(n, f"{q:,}행", ">0") if q > 0 else _fail(n, 0, ">0", args.areas)


def gate_integrity(con):
    """G7 — 무결성."""
    n = "G7 무결성"
    v = con.execute("PRAGMA integrity_check").fetchone()[0]
    return _ok(n, v, "ok") if v == "ok" else _fail(n, v, "ok")


def gate_pk_miss(state):
    """G8 — PK6 미적중. **조정 불가**: T042 전국 실측이 미적중 0 이었다. 0 이 아니면 원천 해석이 틀린 것이다."""
    q = state.get("pk_miss", 0)
    n = "G8 PK6 미적중"
    return (_ok(n, f"pk_miss={q}", 0, "[조정불가]") if q == 0
            else _fail(n, f"pk_miss={q:,}", 0, "[조정불가]"))


def gate_pk_dup(state):
    """G9 — PK6 키 중복. **조정 불가**: PK6 은 대표지번 안에서 유일해야 한다(T042 §A-2 충돌 0)."""
    q = state.get("pk_dup", 0)
    n = "G9 PK6 키중복"
    return (_ok(n, f"dup={q}", 0, "[조정불가]") if q == 0
            else _fail(n, f"dup={q:,}", 0, "[조정불가]"))


def gate_ri_bcode(con):
    """G10 — ri ⟺ bcode 리자리 정합. **조정 불가**.

    검사 대상은 kind='addr' 뿐이다. biz·facility 는 ri 를 가지면서 bcode 가 비어 있고
    (정본 실측 998,671행), NULL 3값 논리 때문에 전 행을 대상으로 삼으면 판정이 뒤집힌다.
    SQLite 에는 right() 가 없으므로 substr(bcode,-2) 를 쓴다(SQLite 3.53.4 실측).

    [T043 검수 Minor-4] 술어를 **대칭**으로 고쳤다. 직전 판은 ri 쪽만 `IS NOT NULL` 로
    NULL 을 흡수하고 bcode 쪽은 `COALESCE` 로 흡수해, bcode 가 NULL·짧은 문자열일 때
    좌우의 처리 방식이 달랐다. 이제 양쪽 모두 "값이 있고 비어 있지 않은가"로 통일한다:
      좌 = ri 가 NULL 도 '' 도 아닌가
      우 = bcode 끝 두 자리가 두 자리로 존재하고 '00' 이 아닌가
    `length(bcode)>=2` 를 명시해 한 자리 bcode 가 우변에서 조용히 참이 되는 길을 막는다.
    """
    n = "G10 ri↔리자리 정합"
    q = con.execute("""SELECT count(*) FROM places
        WHERE kind='addr'
          AND (ri IS NOT NULL AND ri <> '')
           <> (bcode IS NOT NULL AND length(bcode) >= 2 AND substr(bcode,-2) <> '00')
        """).fetchone()[0]
    return (_ok(n, "위반 0행", 0, "[조정불가]") if q == 0
            else _fail(n, f"위반 {q:,}행", 0, "[조정불가]"))


def run_gates(tmp, args, state, eff, src):
    """G2~G10 판정. tmp 는 **커밋·종료된 뒤** 읽기전용으로 다시 열린다."""
    res = [gate_inputs(src, eff, args)]
    con = sqlite3.connect(f"file:{tmp}?mode=ro", uri=True)
    try:
        n_places = con.execute("SELECT count(*) FROM places").fetchone()[0]
        n_addr = con.execute("SELECT count(*) FROM places WHERE kind='addr'").fetchone()[0]
        res.extend(gate_rowcounts(n_places, n_addr, eff, args))
        res.append(gate_coords(con))
        res.append(gate_areas(con, args))
        res.append(gate_integrity(con))
    finally:
        con.close()
    res.append(gate_pk_miss(state))
    res.append(gate_pk_dup(state))
    con = sqlite3.connect(f"file:{tmp}?mode=ro", uri=True)
    try:
        res.append(gate_ri_bcode(con))
    finally:
        con.close()
    return res


def gates_failed(results, allow_skip):
    """SKIP 은 실패다 — '판정하지 못했다'를 통과로 읽으면 게이트를 두는 의미가 없다.
    --allow-gate-skip 을 준 실행에서만 실패에서 빠진다. N/A 는 애초에 판정 대상이 아니다."""
    return any(r.verdict == "FAIL" or (r.verdict == "SKIP" and not allow_skip) for r in results)


def _w(s):
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in s)


def _pad(s, n):
    return s + " " * max(1, n - _w(s))


def print_gates(results, allow_skip, halted=False, elapsed=None):
    lead = "[게이트]  "
    ind = " " * _w(lead)
    for i, r in enumerate(results):
        d = str(r.actual)
        print((lead if i == 0 else ind) + _pad(r.name, 22) + _pad(r.verdict, 7)
              + (_pad(d, 38) + r.note if r.note else d), file=sys.stderr)
    c = collections.Counter(r.verdict for r in results)
    tail = "판정 중단" if halted else (
        "산출물 교체 중단" if gates_failed(results, allow_skip) else "산출물 교체 진행")
    el = f" · {elapsed:.1f}s" if elapsed is not None else ""
    print(f"[결과]    {len(results)} 게이트: PASS {c['PASS']} / N/A {c['N/A']} / "
          f"FAIL {c['FAIL']} / SKIP {c['SKIP']}{el}  →  {tail}", file=sys.stderr)


def main():
    ap=argparse.ArgumentParser()
    # --src 기본값 폐지(T043). 구 기본값은 202605 원천을 가리켰고 그 폴더는 이미 없다. 인자를 빠뜨린
    # 실행이 "주소 0행" DB 를 조용히 만들어 7GB 정본을 덮어쓰는 사고 경로였다. 이제 argparse 가 exit 2 로 막는다.
    ap.add_argument("--src", required=True, help="내비게이션용DB match_*.txt 폴더(필수)")
    # --osm 은 sentinel(None) 로 받는다. 기본값을 argparse 에 박아 두면 "사용자가 지정했는가"를
    # 사후에 알 수 없고, 그러면 G2 가 기본 경로 부재를 전부 오타로 오인해 주소전용 빌드를
    # 통째로 막는다(과잉 차단). 아래 parse_args 직후에 osm_explicit 을 세우고 기본값을 채운다.
    ap.add_argument("--osm", default=None,
                    help="OSM 보조 DB. 미지정이면 $BUILD_HOME(또는 ~/geocode-build)/osm.sqlite 를 "
                         "쓰되, 없으면 주소전용 빌드로 조용히 진행한다(G2 는 그 경우를 실패로 세지 않는다)")
    ap.add_argument("--out", default=os.path.join(os.environ.get("BUILD_HOME") or os.path.expanduser("~/geocode-build"), "geocode.sqlite"))
    ap.add_argument("--only")
    ap.add_argument("--poi-csv-dir", help="소상공인 상가(상권)정보 CSV 폴더(시도별)")
    ap.add_argument("--no-areas", action="store_true",
                    help="행정경계 자동 적재를 끈다(게이트 G6 는 N/A). 주의: --osm 이 실재하면 add_osm 이 "
                         "OSM 쪽 areas 를 무조건 복사하므로 이 옵션만으로 areas 가 빈다고 보장되지 않는다")
    ap.add_argument("--allow-gate-skip", action="store_true",
                    help="판정 불가(SKIP) 게이트를 실패로 세지 않는다. FAIL 은 해제하지 못한다")
    # ---- 게이트 임계 (T043 검수 Minor-3 / 원 태스크 §'하한값은 명령줄 인자로 조절 가능하게')
    #      기본은 엄격하게 두고, 인자는 **느슨하게 여는 쪽으로만** 쓰이게 설계한다.
    #      · 창(1층)은 자유롭게 조절 가능 — 원천 세대가 바뀌면 정당하게 넓혀야 하기 때문이다.
    #      · 비례바닥 GATE_FLOOR(2층)는 **인자가 없다**. 창을 넓혀도 남는 것이 존재 이유다.
    #      · 절대바닥(3층)은 인자가 있으나 **올리는 방향만** 반영한다(gate_ratios 참조).
    ap.add_argument("--min-rows-ratio", type=float, default=None, metavar="R",
                    help=f"G3·G4 행수 창의 하한 계수(기본 {GATE_LO}). 넓히면 비례바닥 "
                         f"{GATE_FLOOR}(조절 불가)가 대신 선다")
    ap.add_argument("--max-rows-ratio", type=float, default=None, metavar="R",
                    help=f"G3·G4 행수 창의 상한 계수(기본 {GATE_HI})")
    ap.add_argument("--min-rows-per-sido", type=int, default=None, metavar="N",
                    help=f"G4 절대 하한: 시도 1종당 최소 addr 행수(기본 {GATE_MIN_PER_SIDO:,}). "
                         f"기준선 유무와 무관하게 항상 적용된다. 기본값보다 낮추는 값은 무시한다")
    ap.add_argument("--taxonomy-out", default=None, metavar="PATH",
                    help="POI 분류 트리 산출 경로. 미지정이면 **정본을 갱신한 빌드에서만** "
                         "style/poi-taxonomy.json 을 재생성한다(검수 M-1)")
    ap.add_argument("--t018-disposed", action="store_true",
                    help="T018 리(里) 백필 처분 완료 선언 — 게이트 G0(전국 재빌드 차단)를 해제한다")
    ap.add_argument("--source-label", default="unknown",
                    help="meta.source 에 적을 원천 세대(예: 2026.07). 미지정이면 unknown")
    ap.add_argument("--dedup", choices=["legacy","er"], default="legacy",
                    help="biz 표시용 중복제거: legacy=정규화상호+좌표3자리 1패스(기본), er=엔티티해상도(dedup_er.py: 셀이웃 블로킹+등급가중점수+union-find)")
    ap.add_argument("--areas", help="행정경계 areas.sqlite(06-gen-areas 산출) — 역지오코딩 동 폴리곤 적재")
    args=ap.parse_args()
    # --osm 명시 여부를 여기서 고정한다(G2 가 읽는다). 기본 경로는 그 뒤에 채운다.
    args.osm_explicit = args.osm is not None
    if args.osm is None:
        args.osm = os.path.join(os.environ.get("BUILD_HOME")
                                or os.path.expanduser("~/geocode-build"), "osm.sqlite")
    # 경로 인자 1회 정규화(검수 N-1). 게이트·적재·교체가 **같은 경로**를 보게 만드는 유일한 지점이다.
    # 이 줄을 지우면 리터럴 틸데 입력에서 G2 와 add_osm 의 판정이 갈린다.
    normalize_arg_paths(args)
    only=set(args.only.split(",")) if args.only else None
    eff=effective_sido(only)
    # ---- 사전 게이트 (T043) — tmp 를 만들기도 전에 판정한다.
    #      G1 이 먼저다: --only 오타로 eff 가 비면 G0 의 covers_all 이 False 가 되어 전국빌드를
    #      부분빌드로 오판할 수 있다. 인자가 성립한 뒤라야 범위 판정이 의미를 갖는다.
    g1=gate_args(args, only)
    if g1.verdict!="PASS":
        print_gates([g1], args.allow_gate_skip, halted=True)
        print(f"  {g1.name}: {g1.actual} (기대 {g1.expected}) — {g1.note}", file=sys.stderr)
        sys.exit(1)
    g0=gate_full_rebuild(args, eff)
    if g0.verdict!="PASS":
        print_gates([g0,g1], args.allow_gate_skip, halted=True)
        print("\n  G0 — 전국 재빌드는 차단돼 있다. T018 리(里) 백필 처분이 끝나지 않았다.\n"
              "  해제 조건(전부 충족해야 --t018-disposed 를 붙일 수 있다):\n"
              "    1. T018 리 백필 처분 후속 태스크 완료 — scripts/postgis/ri_backfill_* 5종의\n"
              "       `right(bcode,2)='00'` 전제 정리(변경 B 이후 이 전제는 거짓이다)\n"
              "    2. ri_backfill_s3_todo.sql 하드 게이트(6,743,655 ± 34,000) 재산정 또는 폐기\n"
              "    3. scripts/postgis/load_geocode.py 의 ri/bcode 수용 확인\n"
              "    4. backfill-admin-codes.py 와의 bcode 의미 충돌 정리(후속 F3)\n"
              "    5. 위 4항 완료 후 build-studio.py 의 TARGETS()['geocode'] cmd 에\n"
              "       --t018-disposed 추가 (현행 :598-601. 라인은 밀릴 수 있으니 키로 찾을 것)\n"
              f"  (판정 근거: {g0.note})", file=sys.stderr)
        sys.exit(2)
    cwp=pathlib.Path(__file__).resolve().parent/"cat-crosswalk.json"   # 카테고리 표준화 매핑(localdata/osm→canonical)
    if cwp.exists():
        _cw=json.load(open(cwp,encoding="utf-8")); CW_LD.update(_cw.get("localdata",{})); CW_OSM.update(_cw.get("osm",{}))
        print(f"  cat-crosswalk: localdata {len(CW_LD)} · osm {len(CW_OSM)}", file=sys.stderr)
    out=pathlib.Path(args.out); out.parent.mkdir(parents=True, exist_ok=True)
    if not args.areas and not args.no_areas:             # 관례 경로의 행정경계 자동 적재(06-gen-areas 산출 areas.sqlite)
        cand = out.parent / "areas.sqlite"
        if cand.exists(): args.areas = str(cand)
    tmp=out.with_suffix(".sqlite.tmp"); tmp.unlink(missing_ok=True)
    db=sqlite3.connect(tmp); db.executescript(SCHEMA)
    t0=time.time(); state={"pid":0,"seen":set(),"pk_miss":0,"pk_dup":0}
    print(f"[통합 지오코드 빌드] juso={args.src}\n  osm={args.osm}", file=sys.stderr)
    add_juso(db, pathlib.Path(args.src), only, state)
    build_gazetteer(db)   # navi 도로명 사전 — 시설 주소(더러운 입력) 견고 파싱용(parse_doro_gaz)
    add_osm(db, args.osm, state)
    if args.areas and pathlib.Path(args.areas).exists():     # 행정경계 폴리곤(06-gen-areas) — reverse point-in-polygon용
        ar = sqlite3.connect(f"file:{args.areas}?mode=ro", uri=True)   # ATTACH 아님: rtree 는 cross-attach INSERT 시 'database is locked'
        db.executemany("INSERT INTO areas(id,name,type,rings,code) VALUES(?,?,?,?,?)",
                       ar.execute("SELECT id,name,type,rings,code FROM areas"))
        db.executemany("INSERT INTO area_rtree VALUES(?,?,?,?,?)",
                       ar.execute("SELECT id,minlon,maxlon,minlat,maxlat FROM area_rtree"))
        ar.close()
        print(f"  areas ← {args.areas}: {db.execute('SELECT count(*) FROM areas').fetchone()[0]:,}", file=sys.stderr)
    pending_fac = add_biz(db, args.poi_csv_dir, state) if args.poi_csv_dir else []
    geocode_facilities(db, pending_fac, state)   # 좌표없는 시설(주소만) → navi 도로명주소 지오코딩
    # biz 중복(상가↔LOCALDATA 같은 점포) 표시용 대표 선정 — 출처(source)는 모두 보존하되 그룹당 1건만 is_primary=1.
    db.create_function("nrm", 1, biznrm)   # facility 충돌숨김(아래)에서도 사용 → dedup 방식과 무관하게 등록
    if args.dedup == "er":
        # 엔티티해상도: (선택)건물키 backfill → 셀이웃 블로킹 + 등급가중점수 + 주소/좌표 TF + union-find (dedup_er.py).
        sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
        from dedup_er import dedup_er
        # navi 조인 backfill: biz 행에 건물키(bd_mgt_sn) — sido+sigungu+road_norm+본/부번 일치 navi addr 의 최근접값.
        # 미파싱·미일치는 NULL 유지(dedup_er가 좌표밀도 TF로 폴백). 부분인덱스 후 상관서브쿼리(수백만행→수분), 끝나면 인덱스 제거.
        db.execute("CREATE INDEX IF NOT EXISTS idx_addr_key ON places(sido,sigungu,road_norm,main_no,sub_no) WHERE kind='addr'")
        # 조인키(sido+sigungu+road_norm+본/부번)가 같은 건물을 특정 → 매칭 addr 은 동일 bd_mgt_sn.
        # 좌표 최근접 tiebreak 은 서브쿼리 ORDER BY 에서 바깥 places.lon/lat 을 참조해야 하는데,
        # 낮은 버전 SQLite(빌드 호스트)가 ORDER BY 절의 바깥참조를 해석 못해 "no such column" 으로 실패.
        # 거의 동일 결과인 결정적 inner 정렬(bd_mgt_sn)로 대체 → 전 버전 호환.
        db.execute("""UPDATE places SET bd_mgt_sn=(
            SELECT a.bd_mgt_sn FROM places a WHERE a.kind='addr'
              AND a.sido=places.sido AND a.sigungu=places.sigungu
              AND a.road_norm=places.road_norm AND a.main_no=places.main_no AND a.sub_no=places.sub_no
            ORDER BY a.bd_mgt_sn LIMIT 1)
          WHERE kind='biz' AND road_norm IS NOT NULL AND main_no IS NOT NULL""")
        db.execute("DROP INDEX IF EXISTS idx_addr_key")
        nbiz = db.execute("SELECT count(*) FROM places WHERE kind='biz'").fetchone()[0]
        nmat = db.execute("SELECT count(*) FROM places WHERE kind='biz' AND bd_mgt_sn IS NOT NULL").fetchone()[0]
        print(f"  [backfill] biz 건물키(bd_mgt_sn) 매칭 {nmat:,}/{nbiz:,} ({100*nmat/max(nbiz,1):.0f}%)", file=sys.stderr)
        dedup_er(db)
    else:
        # legacy: 정규화상호 + 좌표3자리(≈90~110m) 그룹당 1건만 is_primary=1. 우선순위 LOCALDATA>sangga, 동률은 작은 id.
        db.execute("""UPDATE places SET is_primary=1 WHERE id IN (
            SELECT id FROM (SELECT id, ROW_NUMBER() OVER (
                PARTITION BY nrm(name), round(lon,3), round(lat,3)
                ORDER BY CASE source WHEN 'localdata' THEN 0 WHEN 'sangga' THEN 1 ELSE 2 END, id) rn
              FROM places WHERE kind='biz') WHERE rn=1)""")
    # 생활편의시설(kind='facility')은 biz 와 별도 — 전부 표시(is_primary=1). 단 낚시터·세차장은 상가/인허가(biz) 와 겹치면 숨김.
    db.execute("UPDATE places SET is_primary=1 WHERE kind='facility'")
    # 충돌숨김은 '어떤 biz 행이든' 동일 nrm+좌표면 적용(b.is_primary 조건 제거) — ER 대표가 이름다른 출처로
    # 승급돼도 견고. nrm(=biznrm)은 corp/branch 미제거라 legacy/er 양쪽에서 동일 동작.
    db.execute("""UPDATE places SET is_primary=0 WHERE kind='facility' AND subtype IN ('낚시터','세차장')
        AND EXISTS (SELECT 1 FROM places b WHERE b.kind='biz'
          AND nrm(b.name)=nrm(places.name) AND round(b.lon,3)=round(places.lon,3) AND round(b.lat,3)=round(places.lat,3))""")
    db.execute("CREATE TABLE meta(k TEXT,v TEXT)")
    db.executemany("INSERT INTO meta VALUES(?,?)", [("places",str(state["pid"])),("srid","4326"),
        ("source",f"내비게이션용DB {args.source_label} + OSM"),("source_label",args.source_label),
        ("built_s",f"{time.time()-t0:.0f}")])
    db.execute("INSERT INTO places_fts(places_fts) VALUES('optimize')")
    # ---- 산출물 교체 직전 게이트 (T043) ----------------------------------------
    # 순서를 바꾸지 마라. commit → close → 게이트 → (실패면 exit) → replace.
    # close 를 게이트보다 먼저 하는 이유: 게이트는 tmp 를 읽기전용으로 **다시 열어** 읽는다.
    # 열린 쓰기 핸들이 남아 있으면 미커밋 페이지를 읽거나 잠금에 걸린다.
    # replace 를 try 밖에 두는 이유: 게이트가 예외로 죽었을 때 그 예외가 정본 교체로
    # 흘러가면 안 된다. 판정하지 못한 빌드는 통과가 아니다.
    db.commit(); db.close()
    gt=time.time()
    try:
        results=run_gates(tmp, args, state, eff, pathlib.Path(args.src))
    except Exception as e:
        print(f"[게이트]  판정 중 예외 — {type(e).__name__}: {e}", file=sys.stderr)
        print(f"[결과]    판정 실패  →  산출물 교체 중단. tmp 를 남긴다: {tmp}", file=sys.stderr)
        sys.exit(2)
    results=[g0,g1]+results
    print_gates(results, args.allow_gate_skip, elapsed=time.time()-gt)
    if gates_failed(results, args.allow_gate_skip):
        for r in results:
            if r.verdict=="FAIL" or (r.verdict=="SKIP" and not args.allow_gate_skip):
                print(f"  {r.name}: {r.actual} (기대 {r.expected}) {r.note}", file=sys.stderr)
        print(f"  정본은 건드리지 않았다. 조사용 산출물: {tmp}", file=sys.stderr)
        sys.exit(2)
    tmp.replace(out)
    sz=out.stat().st_size/1048576
    print("="*56); print(f"OK: {out}  총 {state['pid']:,}건 · {sz:.0f}MB · {time.time()-t0:.0f}s")
    # (T043) 구 '리 키 충돌' 경고 블록 삭제 — mgt[:10] 키에서만 나던 현상이고, PK6 조인에서는
    #        중복이 게이트 G9(조정불가, 0 강제)로 승격됐다. 경고로 흘려보내던 것을 실패로 바꾼 것이다.
    # ---- 카테고리 분류 트리(스튜디오 티어/아이콘 목록용)
    # [T043 검수 M-1] 직전 판은 **모든** 빌드에서 저장소 추적 파일 style/poi-taxonomy.json 을
    # 덮어썼다. `--only sejong` 같은 임시 빌드 한 번이면 POI 가 없어 파일이
    # {"cat1_order": [], "tree": {}} 로 비워지고, 그대로 커밋되면 스튜디오 아이콘 목록이 사라진다.
    # 이제 저장소 파일은 **정본을 갱신한 빌드**에서만 건드린다. 임시 빌드가 이 파일을 쓰려면
    # --taxonomy-out 으로 자기 경로를 **명시**해야 한다.
    tax_out = pathlib.Path(args.taxonomy_out).expanduser() if args.taxonomy_out else None
    if tax_out is None and targets_canonical(out):
        tax_out = ROOT / "style" / "poi-taxonomy.json"
    if tax_out is None:
        print("  (taxonomy 생략: 정본 빌드가 아니다 — 쓰려면 --taxonomy-out 을 지정하라)")
    else:
        try:
            rdb = sqlite3.connect(f"file:{out}?mode=ro", uri=True)
            try:
                write_taxonomy(rdb, tax_out)
            finally:
                rdb.close()
            print(f"  taxonomy → {tax_out}")
        except Exception as e:
            print(f"  (taxonomy 재생성 스킵: {e})", file=sys.stderr)

if __name__=="__main__":
    main()
