#!/usr/bin/env python3
"""T046 §4.3 — 배타 11 분류 + 직교 8 플래그. **순수 함수**. 외부 호출 없음.

오라클·심판 결과를 인자로 받는다. 여기서 DB 나 VWorld 를 부르면 분류 로직을
단위 검정할 수 없고, 검정할 수 없는 분류기는 결과를 신뢰할 근거가 없다.

## 게이트가 분류에 선행한다

`E1`(VWorld 가 `status != "OK"`)·`E2`(우리 8092 가 5xx/타임아웃)는 **분류 대상이
아니다**. 부재를 분류에 흘려보내면 그것이 분류 4(둘 다 없음) 같은 실체 있는
판정으로 둔갑한다. 공통 분모 `D0 = 표본 − E1 − E2` 를 지키기 위해 게이트를
먼저 세운다. 둘 다 걸리면 E1 이다 — 외부기준이 없으면 애초에 비교가 성립하지 않는다.

## 군 구조

    A 군 (our_addr_count == 0)   우리 응답에 kind='addr' 가 없다 → 1·2·3·4
    B 군 (응답 있고 임계 안)                                    → 5·6
    C 군 (상위 5 후보가 전부 임계 밖)                           → 7·8·9·10·11

분류 3(본번 근사)은 **지번 전용**이다. 도로명층에는 본번 근사라는 개념이 없으므로
그 층에서는 4 로 떨어진다.

## 조건 4(Major) — C 군 판별 순서는 11 → 7 → 8 → 9 → 10

§4.3-d 는 "각 군 내부는 순서 의존이 없다"고 했지만 **C 군에 한해 성립하지 않는다.**
심판 `R()` 은 불리언이고 미정의를 반환하지 않는데, 심판 자료 자체가 없는 경우
(`parcel` 에 그 필지가 없거나 도로명 층의 기준점이 없는 경우) `r_v`/`r_m` 이
`None` 으로 온다. `None` 을 `False` 로 뭉개면 그 건이 전부 분류 10(둘 다 실패)으로
새고, "판정할 자료가 없었다"가 "둘 다 틀렸다"로 기록된다.

그래서 **자료 부재를 가장 먼저 판정**한다. `C_GROUP_ORDER` 가 그 순서를 코드로
선언하고 `test_classify.py::test_c_group_order_is_fixed` 가 감시한다.
한쪽만 부재여도 11 이다 — 반쪽 심판으로 7~10 을 가르면 어느 쪽이 틀렸는지
말할 수 없다.

## 플래그는 분류와 직교한다(§4.3-e)

플래그는 분류를 **바꾸지 않는다**. 특히 F1(법정동코드 불일치)은 거리와 무관하게
전 건에서 판정한다 — 거리 임계 안쪽에서만 보면 "가까우니 맞다"가 되어 행정구역이
어긋난 건을 성공으로 센다(M6 회귀).

조건 3(Major)의 **F8** 은 A 군 밖의 `source='parcel' ∧ O=P` 를 잡는다. A 군의
같은 조합은 이미 분류 2 로 계상되므로 F8 을 세우지 않는다 — 이중 계상 방지.
"""
from normalize import bcode_match, bcode_relaxed

__all__ = [
    "classify_one", "Verdict", "PREDICATES", "FLAGS",
    "C_GROUP_ORDER", "CLASS_ORDER", "REQUIRED_FIELDS",
    "LAYERS", "ORACLE_VALUES", "ORACLE_ADDR",
]

LAYERS = ("jibun", "road")
# 지번층 A / 도로명층 A25·A19. 셋 다 "주소가 우리 DB 에 있다"는 같은 사실이다(§4.3-c).
ORACLE_ADDR = ("A", "A25", "A19")
ORACLE_VALUES = ORACLE_ADDR + ("P", "N")

REQUIRED_FIELDS = (
    "layer", "e1", "e2", "our_addr_count", "d_top1", "d_min5",
    "oracle", "o_apx", "r_v", "r_m", "T",
)

# 조건 4 — 심판 자료 부재(11)를 **먼저** 본다. 순서를 코드가 선언한다.
C_GROUP_ORDER = (11, 7, 8, 9, 10)
CLASS_ORDER = (1, 2, 3, 4, 5, 6) + C_GROUP_ORDER


class Verdict(object):
    """`cls`(1~11) 또는 `gate`("E1"/"E2") 중 하나만 값이 있다."""

    __slots__ = ("cls", "flags", "gate")

    def __init__(self, cls=None, flags=(), gate=None):
        self.cls = cls
        self.flags = tuple(flags)
        self.gate = gate

    def __repr__(self):
        return "Verdict(cls=%r, flags=%r, gate=%r)" % (self.cls, self.flags, self.gate)

    def __eq__(self, other):
        return (isinstance(other, Verdict)
                and (self.cls, self.flags, self.gate)
                == (other.cls, other.flags, other.gate))

    def __hash__(self):
        return hash((self.cls, self.flags, self.gate))


# ── 군 판별 보조 ──────────────────────────────────────────────────────
def _a_group(o):
    """우리 응답에 `kind='addr'` 가 0 건."""
    return o["our_addr_count"] == 0


def _within(d, o):
    """`d ≤ T`. 거리 부재는 **임계 안이 아니다**(규칙 12 — 부재는 성공이 아니다)."""
    return d is not None and d <= o["T"]


def _b5(o):
    return _within(o["d_top1"], o)


def _b6(o):
    return (not _b5(o)) and _within(o["d_min5"], o)


def _c_group(o):
    """상위 5 후보가 전부 임계 밖. B 군의 여집합으로 정의해 빈틈을 없앤다."""
    return (not _a_group(o)) and (not _b5(o)) and (not _b6(o))


def _referee_absent(o):
    """한쪽이라도 `None` 이면 부재다. `False`(밖에 있다)와 전혀 다른 사실이다."""
    return o["r_v"] is None or o["r_m"] is None


def _c(o, want_v, want_m):
    return (_c_group(o) and not _referee_absent(o)
            and bool(o["r_v"]) is want_v and bool(o["r_m"]) is want_m)


# ── 배타 술어 11 개 ───────────────────────────────────────────────────
# 각 술어는 군 조건까지 포함한다 — 어떤 입력에도 정확히 하나만 참이다.
PREDICATES = {
    # A 군 — 응답 부재. 오라클이 "그럼 자료는 있었나"를 가른다.
    1: lambda o: _a_group(o) and o["oracle"] in ORACLE_ADDR,
    2: lambda o: _a_group(o) and o["oracle"] == "P",
    3: lambda o: (_a_group(o) and o["oracle"] == "N"
                  and bool(o["o_apx"]) and o["layer"] == "jibun"),
    4: lambda o: (_a_group(o) and o["oracle"] == "N"
                  and not (bool(o["o_apx"]) and o["layer"] == "jibun")),
    # B 군 — 임계 안.
    5: lambda o: (not _a_group(o)) and _b5(o),
    6: lambda o: (not _a_group(o)) and _b6(o),
    # C 군 — 임계 밖. 심판이 누가 틀렸는지 가른다.
    7: lambda o: _c(o, False, True),    # 외부기준이 틀렸다
    8: lambda o: _c(o, True, False),    # 우리가 틀렸다
    9: lambda o: _c(o, True, True),     # 둘 다 필지 안 — 필지가 크다
    10: lambda o: _c(o, False, False),  # 둘 다 필지 밖
    11: lambda o: _c_group(o) and _referee_absent(o),   # 심판 자료 부재
}


# ── 직교 플래그 8 개 ──────────────────────────────────────────────────
def _f1(o, relax12):
    """법정동코드 불일치. **거리와 무관하게** 전 건에서 본다(M6 회귀).

    어느 한쪽이 없으면 판정하지 않는다 — 부재를 불일치로 세면 자료가 없는
    구간이 통째로 F1 이 된다.
    """
    a, b = o.get("bcode_ours"), o.get("bcode_vw")
    if not a or not b:
        return False
    return not bcode_match(a, b, relax12=relax12)


def _f6(o, relax12):
    """조건 2 — 시도코드 12 완화가 **판정을 뒤집은** 건만. F1 과 상호배타적이다."""
    a, b = o.get("bcode_ours"), o.get("bcode_vw")
    if not a or not b or not relax12:
        return False
    return bcode_relaxed(a, b)


FLAGS = {
    "F1": _f1,
    # F2 — 응답은 있는데 전부 kind≠addr 다(카테고리 오폴백).
    "F2": lambda o, r: o["our_addr_count"] == 0 and o.get("nonaddr_count", 0) > 0,
    # F3 — 임계 내 후보가 상위 5 안에 있는데 top-1 이 최소거리가 아니다(순위 오염).
    "F3": lambda o, r: _within(o["d_min5"], o) and not o.get("top1_is_nearest", True),
    # F4 — 질의는 산번지인데 우리 응답은 일반 지번(또는 그 반대).
    "F4": lambda o, r: bool(o.get("san_query")) != bool(o.get("san_ours")),
    # F5 — 우리 응답이 주소 원본이 아니라 폴백 경로다.
    "F5": lambda o, r: o.get("source") in ("navi", "parcel"),
    "F6": _f6,
    # F7 — 정규화가 값을 바꿨다. "정규화가 성공률을 만든 것 아니냐"에 수치로 답한다.
    "F7": lambda o, r: bool(o.get("norm_applied")),
    # F8 — 조건 3: A 군 밖의 parcel 폴백 ∧ O=P. A 군은 분류 2 로 이미 계상된다.
    "F8": lambda o, r: (not _a_group(o) and o["oracle"] == "P"
                        and o.get("source") == "parcel"),
}
FLAG_ORDER = ("F1", "F2", "F3", "F4", "F5", "F6", "F7", "F8")


# ── 입구 ──────────────────────────────────────────────────────────────
def _validate(o):
    """필수 필드 누락을 조용히 넘기지 않는다.

    기본값으로 채우면 "심판이 없었다"가 "심판이 False 였다"로 둔갑하는 식의
    오판이 통계 안에 숨는다. 없으면 죽는 편이 낫다.
    """
    for k in REQUIRED_FIELDS:
        if k not in o:
            raise KeyError("필수 필드가 없다: %r" % (k,))
    if o["layer"] not in LAYERS:
        raise ValueError("알 수 없는 층: %r" % (o["layer"],))
    if o["oracle"] not in ORACLE_VALUES:
        raise ValueError("알 수 없는 오라클 값: %r" % (o["oracle"],))


def classify_one(obs, relax12=False):
    """관측 1 건 → `Verdict`.

    `relax12` 는 F1/F6 판정에만 쓴다. 조건 2 가 "완화 전/후 두 수치를 병기하라"고
    요구하므로 기본은 **엄격**이고, 집계 측이 같은 관측을 두 번 분류해 대조한다.
    """
    _validate(obs)

    # 게이트가 먼저다. 부재는 분류 대상이 아니다.
    if obs["e1"]:
        return Verdict(gate="E1")
    if obs["e2"]:
        return Verdict(gate="E2")

    cls = None
    for k in CLASS_ORDER:
        if PREDICATES[k](obs):
            cls = k
            break
    if cls is None:  # pragma: no cover — 술어가 완전하므로 도달 불가
        raise AssertionError("어느 분류에도 걸리지 않았다: %r" % (obs,))

    flags = tuple(name for name in FLAG_ORDER if FLAGS[name](obs, relax12))
    return Verdict(cls=cls, flags=flags)
