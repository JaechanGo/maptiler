"""시도·시군구·읍면동 파싱 — 원천 주소 문자열에서 '검증된' 지역 토큰만 꺼낸다.

왜 이 모듈이 있나 ([실측 2026-09-02 .244]):
  11-build-localdata.py·11b-build-facility.py 가 각자 `addr.split()[0]` 을 시도로 채택했다.
  인허가·공공시설 원천 주소는 비정형이라 — 약칭('전남 장흥군'), 시도 누락('장전동 ***번지'),
  붙여쓰기('서울특별시마포구'), 오타('대전광역대전광역시') — 그대로 시도 컬럼에 들어갔고
  localdata_clean.csv 시도명이 270종(정규 17), PostGIS address 에 biz 1,188행·facility 90행이 오염됐다.
  QC 의 시도 커버리지는 '< 16 이면 FAIL' 한쪽만 봐서 종류가 **늘어난** 오염은 통과시켰다.

원칙:
  1) 정규 명칭(현행 17) + 원천 잔존 구명칭(전라남도·광주광역시 등)은 **보존** — DB 는 원천을 보존하고
     표시 치환은 API(geocode-api-pg remap_sido_name)가 맡는 기존 설계('안 A')를 바꾸지 않는다.
  2) 약칭·붙여쓰기는 규칙으로 정규화한다(근거가 문자열 안에 있으므로 추정이 아니다).
  3) 그래도 못 살리면 원천이 함께 준 개방자치단체코드(시도 단위 6xx0000)로만 복구한다.
  4) 그것도 없으면 **빈값** — 동 이름을 시도로 올리느니 비워 둔다. 비-addr 은 API 가 좌표 PIP 로 채운다.
"""
import json, os, re, unicodedata

_N = lambda s: unicodedata.normalize("NFC", s or "").strip()

CANON_SIDO = frozenset({
    "서울특별시", "부산광역시", "대구광역시", "인천광역시", "대전광역시", "울산광역시",
    "세종특별자치시", "경기도", "강원특별자치도", "충청북도", "충청남도",
    "전북특별자치도", "전남광주통합특별시", "경상북도", "경상남도", "제주특별자치도",
    "광주광역시",   # 통합 이전 코드체계(6290000)·원천에 여전히 실림 — 현행 취급(API 가 표시 치환)
})
# 개편 전 명칭 — 원천에 잔존. 유효로 인정하되 CANON 과는 분리(집합 겹침 금지 — 테스트가 고정).
LEGACY_SIDO = frozenset({"전라남도", "전라북도", "강원도", "제주도", "세종시"})
VALID_SIDO = CANON_SIDO | LEGACY_SIDO

# 약칭·변형 → 정규/구명칭. 약칭의 뜻은 원천이 쓴 당시 명칭이므로 구명칭으로 편다(전남→전라남도).
SIDO_ALIAS = {
    "서울": "서울특별시", "서울시": "서울특별시", "부산": "부산광역시", "부산시": "부산광역시",
    "대구": "대구광역시", "대구시": "대구광역시", "인천": "인천광역시", "인천시": "인천광역시",
    "광주": "광주광역시", "대전": "대전광역시", "대전시": "대전광역시", "울산": "울산광역시",
    "울산시": "울산광역시", "세종": "세종특별자치시", "경기": "경기도", "강원": "강원특별자치도",
    "충북": "충청북도", "충남": "충청남도", "전북": "전북특별자치도", "전남": "전라남도",
    "경북": "경상북도", "경남": "경상남도", "제주": "제주특별자치도", "전남광주": "전남광주통합특별시",
    "경상북": "경상북도", "강원특별자": "강원특별자치도",   # 절단 잔재(실측 facility)
}

# 개방자치단체코드(시도 단위) → 시도. localdata-regions.json(수집용 17) + 통합 신코드.
def _load_org2sido():
    m = {"6130000": "전남광주통합특별시"}   # 2026 통합 신코드 — regions.json 은 수집 URL 용이라 손대지 않는다
    p = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "localdata-regions.json")
    try:
        for r in json.load(open(p, encoding="utf-8")).get("regions", []):
            m.setdefault(str(r.get("orgCode", "")), r.get("name", ""))
    except Exception:
        pass
    return m
ORG2SIDO = _load_org2sido()

_SGG_RE = re.compile(r"(시|군|구)$")
_EMD_RE = re.compile(r"(동|읍|면|리|가)$")
_LONGEST = sorted(VALID_SIDO, key=len, reverse=True)


def is_valid_sido(s):
    return bool(s) and _N(s) in VALID_SIDO


def _split_sido(tok):
    """토큰 → (시도, 남는 조각). 정규/구명칭 그대로·약칭·붙여쓰기('서울특별시마포구') 순으로 해석."""
    t = _N(tok)
    if t in VALID_SIDO: return t, ""
    if t in SIDO_ALIAS: return SIDO_ALIAS[t], ""
    for name in _LONGEST:                      # 붙여쓰기: 가장 긴 이름부터 접두 일치
        if t.startswith(name) and len(t) > len(name):
            return name, t[len(name):]
    # 약칭 접두('대전…')로는 풀지 않는다 — '대전광역대전광역시' 같은 오타를 대전광역시+'광역대전광역시'로
    # 잘못 살린다(테스트로 고정). 문자열 근거가 없으면 개방자치단체코드 폴백에 맡긴다.
    return "", ""


def _sgg_emd(tokens):
    """시도 뒤 토큰들 → (시군구, 읍면동). 시군구는 시/군/구로 끝나는 것만 인정('***번지' 차단)."""
    sgg = ""; rest = tokens
    if tokens and _SGG_RE.search(tokens[0]):
        sgg = tokens[0]; rest = tokens[1:]
        if rest and rest[0].endswith("구") and sgg.endswith("시"):   # 시+구(수원시 영통구) — navi sigungu 포맷
            sgg += " " + rest[0]; rest = rest[1:]
    emd = next((x for x in rest[:3] if _EMD_RE.search(x)), "")
    return sgg, emd


def parse_region_kr(*addrs, org_code=None):
    """주소 문자열들(지번·도로명 순) 중 첫 해석 가능한 것에서 (시도, 시군구, 읍면동). 실패는 빈값.

    시도가 문자열에서 안 나오면 org_code(개방자치단체코드, 시도 단위 6xx0000)로만 복구하고,
    그 경우 시군구·읍면동은 첫 토큰을 버린 나머지에서 다시 뽑는다(오타 토큰이 시군구로 새지 않게).
    """
    org_sido = ORG2SIDO.get(str(org_code).strip(), "") if org_code else ""
    for a in addrs:
        t = _N(a).split()
        if not t: continue
        sido, rem = _split_sido(t[0])
        if sido:
            tail = ([rem] if rem else []) + t[1:]
            sgg, emd = _sgg_emd(tail)
            return sido, sgg, emd
        if org_sido:                            # 문자열은 못 믿지만 코드는 믿는다 — 첫 토큰은 버린다
            sgg, emd = _sgg_emd(t[1:])
            return org_sido, sgg, emd
    return "", "", ""


__all__ = ["CANON_SIDO", "LEGACY_SIDO", "VALID_SIDO", "SIDO_ALIAS", "ORG2SIDO",
           "is_valid_sido", "parse_region_kr"]
