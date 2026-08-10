#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""지번 주소 정규화 — 행안부 595건 채점 자산의 표기 결함 흡수 계층 (T024).

배경: 채점 자산의 기대 지번에 세 종류의 표기 결함이 섞여 있어, 정상 출력이 오답으로
      집계된다. 이 모듈은 기대값과 우리 출력 **양쪽에 동일하게** 적용되는 대칭
      정규화를 제공한다. 한쪽만 정규화하면 새로운 편향이 생긴다.

  A  법정동 접미사 뒤 번지 공백   `성동리263-8`  ↔ `성동리 263-8`
  B  `-0` 접미                    `617-0`        ↔ `617`
  C-1 `산` 접두 공백              `산 282-1`     ↔ `산282-1`

구조: norm_jibun = post_norm ∘ base_norm.
      post_norm 은 기저 정규화의 **출력만** 받는 순수 후처리다. 그래서
      기저(a) == 기저(b) 이면 norm_jibun(a) == norm_jibun(b) 가 함수 정의상
      따라온다 — 즉 **기존 하네스에서 통과하던 쌍은 절대 깨지지 않는다**.
      이 보존은 측정이 아니라 합성 구조에서 나오므로, 덤프에 통과분 원문이
      없어도 증명된다.

      적용 순서는 C-1 → A → B 다. 역지오 하네스의 기존 정규화가
      `C-1 ∘ 기저` 이므로, C-1 을 맨 앞에 두면 그 기준선의 통과분 보존도
      마찬가지로 구조적으로 성립한다. 순서를 A → B → C-1 로 바꿔도 595건
      결과는 같지만 이 증명이 사라진다.

경계: 이 모듈은 `산` 의 유무를 절대 없애지 않는다. `norm(…산282-1) != norm(…282-1)`
      은 이 태스크의 최상위 불변식이다. 산번지와 일반번지는 서로 다른 필지이고,
      전국 parcel 에서 두 형태가 함께 존재하는 쌍이 100만 단위로 있다.
      통과율을 올리려고 이 불변식을 완화해서는 안 된다.

사용:
      from addr_norm import norm_jibun, norm_road
      norm_jibun(기대값) == norm_jibun(우리출력)

의존: 표준 라이브러리(re)만. 서버·DB 호출 없음.
"""
import re

__all__ = ["SIDO_ALIAS", "base_norm", "rule_a", "rule_b", "rule_c1",
           "post_norm", "norm_jibun", "norm_road", "RULES"]

# 2023~2024 개편 명칭 → 채점 자산이 쓰는 구 명칭. 기존 하네스와 동일하게 유지한다.
SIDO_ALIAS = {
    "강원특별자치도": "강원도",
    "전북특별자치도": "전라북도",
    "제주특별자치도": "제주도",
    "세종특별자치시": "세종시",
}

_PAREN = re.compile(r"\([^)]*\)")
_WS = re.compile(r"\s+")

# A: 법정동 접미사 뒤에 곧바로 숫자가 붙으면 공백을 넣는다.
#    `로`·`길`은 의도적으로 제외한다 — 포함하면 `충무로1가` 가 `충무 로1가` 로 파괴된다.
_RE_A = re.compile(r"(리|동|읍|면|가)(\d)")

# B: 말미의 `-0` 만 제거한다. lookahead 가 `617-01`·`617-0-2` 를 보호하고,
#    리터럴 `-0` 이라 `617-10` 은 애초에 매치되지 않는다.
_RE_B = re.compile(r"-0(?=\s|$)")

# C-1: `산` 과 번지 사이 공백을 없앤다.
#      (?<![가-힣]) 이 `중산동`·`산황동` 을, (?=\d) 가 `백석산길` 을 막는다.
_RE_C1 = re.compile(r"(?<![가-힣])산\s+(?=\d)")


def base_norm(s):
    """기저 정규화 — 괄호 주석 제거, 시도 별칭 통일, 공백 축약.

    기존 두 하네스(roundtrip_all.py·reverse_google.py)의 `norm()` 이 공통으로
    하던 일이다. 여기서 새 규칙을 섞지 않는다.
    """
    s = _PAREN.sub("", s or "")
    for new, old in SIDO_ALIAS.items():
        s = s.replace(new, old)
    return _WS.sub(" ", s).strip()


def rule_a(s):
    """규칙 A — 법정동 접미사(리·동·읍·면·가) 뒤 번지 공백 삽입."""
    return _RE_A.sub(r"\1 \2", s)


def rule_b(s):
    """규칙 B — 말미 `-0` 접미 제거. 부번의 0 은 건드리지 않는다."""
    return _RE_B.sub("", s)


def rule_c1(s):
    """규칙 C-1 — `산` 접두와 번지 사이 공백 흡수. 지명의 `산` 은 제외."""
    return _RE_C1.sub("산", s)


RULES = (("C-1", rule_c1), ("A", rule_a), ("B", rule_b))


def post_norm(s):
    """순수 후처리 g — 기저 정규화 결과만 입력으로 받는다.

    이 함수가 기저 정규화를 다시 호출하지 않는다는 점이 보존 증명의 전제다.
    """
    for _, fn in RULES:
        s = fn(s)
    return s


def norm_jibun(s):
    """지번 비교용 정규화. 기대값과 출력에 **똑같이** 적용해야 한다."""
    return post_norm(base_norm(s))


def norm_road(s):
    """도로명 비교용 정규화 — 기저 정규화만 적용한다.

    도로명에는 `리/동/읍/면/가` 접미사 뒤 번지도, `-0` 접미도, `산` 접두도
    나타나지 않는다. 지번 규칙을 도로명에 얹으면 얻는 것 없이 위험만 는다.
    """
    return base_norm(s)


def _selftest():
    """모듈 단독 점검. 상세 검증은 scripts/test_addr_norm.py 가 한다."""
    cases = [
        ("경기도 김포시 월곶면 성동리263-8", "경기도 김포시 월곶면 성동리 263-8"),
        ("경상북도 의성군 단촌면 구계리 617-0", "경상북도 의성군 단촌면 구계리 617"),
        ("경상북도 의성군 단촌면 구계리 617-10", "경상북도 의성군 단촌면 구계리 617-10"),
        ("강원특별자치도 인제군 기린면 방동리 산 282-1", "강원도 인제군 기린면 방동리 산282-1"),
        ("인천광역시 영종구 중산동 1354-1", "인천광역시 영종구 중산동 1354-1"),
        ("서울특별시 중구 충무로1가 25", "서울특별시 중구 충무로1가 25"),
    ]
    bad = [(src, want, norm_jibun(src)) for src, want in cases if norm_jibun(src) != want]
    inv = norm_jibun("강원도 인제군 기린면 방동리 산282-1") == \
        norm_jibun("강원도 인제군 기린면 방동리 282-1")
    for src, want, got in bad:
        print(f"✗ {src!r}\n   기대 {want!r}\n   실제 {got!r}")
    if inv:
        print("✗ 최상위 불변식 붕괴: 산번지와 일반번지가 같아졌다")
    ok = not bad and not inv
    print("✓ addr_norm selftest 통과" if ok else "✗ addr_norm selftest 실패")
    return 0 if ok else 1


if __name__ == "__main__":
    import sys
    sys.exit(_selftest())
