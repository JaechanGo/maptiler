#!/usr/bin/env python3
"""T046 §4.2 — 주소 정규화 규칙 1~12. 순수 함수. 외부 의존 없음.

## 두 개의 축

**부재는 성공이 아니다(규칙 12).** 빈 문자열·`None`·파싱 실패는 전부 **불일치**다.
부재끼리의 비교도 불일치다 — `jibun_match(None, None)` 은 `False`. 부재를 일치로
세면 자료가 없는 구간이 전부 성공으로 둔갑한다.

**지번 비교는 문자열이 아니라 `(san, ji_main, ji_sub)` 3-튜플로 한다(규칙 9).**
이것이 §4.3 오라클의 PNU 키와 정확히 같은 정보다. 문자열로 비교하면 `200-1전`과
`200-1`이 다른 지번이 되고, 부번 없음(`200`)과 부번 0(`200-0`)이 갈린다.

## 시도코드 12 완화(규칙 5)

원천 202607 은 광주와 전남을 `전남광주통합특별시`(시도코드 **12**)로 합쳤다.
VWorld 는 아직 광주 `29` / 전남 `46` 을 쓴다. 완화가 없으면 이 구간이 통째로
불일치로 떨어진다(표본 800 건, 가중 약 11.8 %).

완화는 **접두 2 자리에만** 적용하고 **나머지 8 자리는 엄격히 비교한다** — 완화가
판정을 삼키지 않도록. 기본값은 `relax12=False`(엄격)다. 2 차 검토 조건 2 가
"완화 전/후 두 수치를 병기하라"고 요구하므로, 호출 측이 두 번 물어볼 수 있어야 한다.
"""
import re
import unicodedata

__all__ = [
    "norm_text", "strip_spaces",
    "norm_sido", "sido_match",
    "sido_code_match", "sido_code_relaxed", "SIDO12_PARENTS",
    "bcode_match", "bcode_relaxed",
    "parse_jibun", "parse_jibun_detail", "jibun_match",
    "norm_road", "parse_bld_no", "is_basement",
]

# ── 규칙 1·2·3 — 유니코드·공백 ────────────────────────────────────────
_WS = re.compile(r"\s+")


def norm_text(value):
    """NFC 합성 + 공백 축약 + 앞뒤 제거. `None` 은 빈 문자열."""
    if not value:
        return ""
    return _WS.sub(" ", unicodedata.normalize("NFC", str(value))).strip()


def strip_spaces(value):
    """비교용 — 공백을 **전부** 제거한다(규칙 3)."""
    return _WS.sub("", norm_text(value))


# ── 규칙 4 — 시도명 이형 사전 ─────────────────────────────────────────
# 좌변(이형) → 우변(정규형). 정규형은 2 글자 약칭으로 통일한다.
_SIDO_ALIASES = {
    "서울": ("서울", "서울시", "서울특별시"),
    "부산": ("부산", "부산시", "부산광역시"),
    "대구": ("대구", "대구시", "대구광역시"),
    "인천": ("인천", "인천시", "인천광역시"),
    "광주": ("광주", "광주시", "광주광역시"),
    "대전": ("대전", "대전시", "대전광역시"),
    "울산": ("울산", "울산시", "울산광역시"),
    "세종": ("세종", "세종시", "세종특별시", "세종특별자치시"),
    "경기": ("경기", "경기도"),
    "강원": ("강원", "강원도", "강원특별자치도"),
    "충북": ("충북", "충청북도"),
    "충남": ("충남", "충청남도"),
    "전북": ("전북", "전라북도", "전북특별자치도"),
    "전남": ("전남", "전라남도"),
    "경북": ("경북", "경상북도"),
    "경남": ("경남", "경상남도"),
    "제주": ("제주", "제주도", "제주시", "제주특별자치도"),
    # 원천 202607 의 통합 명칭(시도코드 12). 별개의 정규형으로 둔다 —
    # 여기서 광주/전남에 합쳐 버리면 규칙 5 완화의 on·off 를 구분할 수 없다.
    "전남광주": ("전남광주통합특별시", "전남광주통합시", "전남광주"),
}
_SIDO_LOOKUP = {}
for _canon, _forms in _SIDO_ALIASES.items():
    for _f in _forms:
        _SIDO_LOOKUP[_f] = _canon


def norm_sido(value):
    """시도명을 정규형 약칭으로. 사전에 없으면 공백 제거형을 그대로 돌려준다."""
    key = strip_spaces(value)
    return _SIDO_LOOKUP.get(key, key)


def sido_match(a, b):
    """시도명 일치. 어느 한쪽이라도 부재면 불일치(규칙 12)."""
    na, nb = norm_sido(a), norm_sido(b)
    if not na or not nb:
        return False
    return na == nb


# ── 규칙 5 — 시도코드 12 완화 ─────────────────────────────────────────
SIDO12 = "12"
SIDO12_PARENTS = ("46", "29")   # 전남 우선, 미적중 시 광주(§4.3 조회 순서와 동일)


def _relax_pair(a, b):
    """`{12}` 대 `{29,46}` 조합이면 True. 29↔46 끼리는 아니다."""
    return (a == SIDO12 and b in SIDO12_PARENTS) or (b == SIDO12 and a in SIDO12_PARENTS)


def sido_code_match(a, b, relax12=False):
    """시도코드 2 자리 비교. `relax12` 면 12↔{29,46} 을 일치로 본다."""
    a, b = strip_spaces(a), strip_spaces(b)
    if len(a) != 2 or len(b) != 2:
        return False
    if a == b:
        return True
    return bool(relax12 and _relax_pair(a, b))


def sido_code_relaxed(a, b):
    """완화가 **실제로 판정을 뒤집었는지**. F6 계수기용.

    엄격으로 이미 일치한 건은 False 다 — 완화 발동 건수를 부풀리면
    "완화 없이는 얼마나 떨어지는가"를 잴 수 없다.
    """
    return sido_code_match(a, b, relax12=True) and not sido_code_match(a, b, relax12=False)


# ── 조건 2(Major) — F1 판정용 법정동코드 10 자리 ──────────────────────
def bcode_match(a, b, relax12=False):
    """법정동코드 10 자리 비교. 완화는 **접두 2 자리에만** 적용한다.

    뒤 8 자리는 완화 여부와 무관하게 엄격히 같아야 한다 — 완화가 시군구·읍면동
    불일치까지 덮어 버리면 F1 이 무의미해진다.
    """
    a, b = strip_spaces(a), strip_spaces(b)
    if len(a) != 10 or len(b) != 10:
        return False
    if a == b:
        return True
    if not relax12:
        return False
    return a[2:] == b[2:] and _relax_pair(a[:2], b[:2])


def bcode_relaxed(a, b):
    """완화가 판정을 뒤집은 건만 True. 조건 2 의 '완화 전/후 병기'용."""
    return bcode_match(a, b, relax12=True) and not bcode_match(a, b, relax12=False)


# ── 규칙 6·7·8·9 — 지번 파싱 ──────────────────────────────────────────
# 규칙 8: 번지·번·호 접미. 앞이 숫자로 끝나야 한다 — `없음` 같은 문자열을 건드리지 않는다.
_RE_BUNJI = re.compile(r"^(.*\d)(번지|번|호)$")
# 규칙 6: 지목 접미(한글 1~3 자). 4 자 이상은 지목이 아니므로 손대지 않는다.
_RE_LANDCAT = re.compile(r"^(.*\d)([가-힣]{1,3})$")
# 규칙 9: 최종형. 부번 없음은 0 이다.
_RE_JIBUN = re.compile(r"^(\d+)(?:-(\d+))?$")


def parse_jibun_detail(value):
    """지번 → `((san, ji_main, ji_sub), flags)`. 파싱 실패면 값이 `None`.

    `flags` 는 F7(정규화 발동 여부) 진단용이다. 어떤 규칙이 발동했는지 남겨야
    "정규화가 성공률을 만든 것 아니냐"는 물음에 수치로 답할 수 있다.
    """
    flags = {"san": False, "land_category": False, "bunji": False, "changed": False}
    s = strip_spaces(value)
    if not s:
        return None, flags

    if s.startswith("산"):
        flags["san"] = True
        s = s[1:]

    m = _RE_BUNJI.match(s)
    if m:
        flags["bunji"] = True
        s = m.group(1)
    else:
        m = _RE_LANDCAT.match(s)
        if m:
            flags["land_category"] = True
            s = m.group(1)

    flags["changed"] = flags["san"] or flags["land_category"] or flags["bunji"]

    m = _RE_JIBUN.match(s)
    if not m:
        return None, flags
    return (flags["san"], int(m.group(1)), int(m.group(2) or 0)), flags


def parse_jibun(value):
    """`(san, ji_main, ji_sub)` 또는 파싱 실패 시 `None`."""
    return parse_jibun_detail(value)[0]


def jibun_match(a, b):
    """지번 일치. 어느 한쪽이라도 파싱 실패면 불일치(규칙 12)."""
    pa, pb = parse_jibun(a), parse_jibun(b)
    if pa is None or pb is None:
        return False
    return pa == pb


# ── 규칙 10·11 — 도로명·건물번호 ──────────────────────────────────────
def norm_road(value):
    """도로명은 **접미를 유지한다**(규칙 10).

    `대로`/`로`/`길` 을 잘라내면 `세종대로`와 `세종로`가 같아지고,
    `NN번길` 의 숫자를 건물번호로 오인하면 다른 도로가 합쳐진다.
    공백 제거와 NFC 합성만 한다.
    """
    return strip_spaces(value)


_RE_BLDNO = re.compile(r"^(\d+)(?:-(\d+))?$")


def parse_bld_no(value):
    """건물번호 → `(main, sub)`. 부번 없음은 0. 파싱 실패는 `None`(규칙 11)."""
    m = _RE_BLDNO.match(strip_spaces(value))
    if not m:
        return None
    return int(m.group(1)), int(m.group(2) or 0)


def is_basement(value):
    """`지하` 접두 판별. §2.4 에서 표본 제외 대상이다 — 여기서는 판별만 한다."""
    return strip_spaces(value).startswith("지하")
