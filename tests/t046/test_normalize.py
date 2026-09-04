#!/usr/bin/env python3
"""T046 §7 — 주소 정규화(§4.2 규칙 1~12) 단위 테스트. 순수 함수, 외부 의존 없음.

핵심은 두 가지다.
  · 부재는 성공이 아니다 — 빈 문자열·None 은 **불일치**로 계수한다(규칙 12).
  · 지번 비교는 문자열이 아니라 `(san, ji_main, ji_sub)` 3-튜플로 한다(규칙 9).
    이것이 §4.3 오라클의 PNU 키와 정확히 같은 정보다(M1).

실행: /usr/bin/python3 -m unittest discover -s tests/t046 -t . -v
"""
import unicodedata
import unittest

import _ctx  # noqa: F401  (sys.path 주입)

from normalize import (  # noqa: E402
    bcode_match,
    bcode_relaxed,
    is_basement,
    jibun_match,
    norm_road,
    norm_sido,
    norm_text,
    parse_bld_no,
    parse_jibun,
    parse_jibun_detail,
    sido_code_match,
    sido_code_relaxed,
    sido_match,
    strip_spaces,
)

# §4.2 규칙 4 — 시도명 이형 사전. 좌우가 같은 시도를 가리켜야 한다.
SIDO_PAIRS = [
    ("충청남도", "충남"),
    ("충청북도", "충북"),
    ("전라남도", "전남"),
    ("전라북도", "전북"),
    ("전북특별자치도", "전라북도"),
    ("경상남도", "경남"),
    ("경상북도", "경북"),
    ("강원특별자치도", "강원도"),
    ("제주특별자치도", "제주도"),
    ("서울특별시", "서울"),
    ("부산광역시", "부산"),
    ("세종특별자치시", "세종"),
]

# 동형 광역시 5 개(§4.2 규칙 4 의 '동형' 항).
SIDO_METRO = [
    ("대구광역시", "대구"),
    ("인천광역시", "인천"),
    ("광주광역시", "광주"),
    ("대전광역시", "대전"),
    ("울산광역시", "울산"),
]


class TestTextNorm(unittest.TestCase):
    """규칙 1·2·3 — NFC, 공백."""

    def test_nfc_normalization(self):
        """NFD 로 분해된 한글을 NFC 로 합성한다(규칙 1)."""
        nfd = unicodedata.normalize("NFD", "충청남도")
        self.assertNotEqual(nfd, "충청남도")          # 전제 확인: 정말 다른 바이트열
        self.assertEqual(norm_text(nfd), "충청남도")

    def test_collapses_whitespace(self):
        """앞뒤 공백 제거, 연속 공백 1 개로 축약(규칙 2)."""
        self.assertEqual(norm_text("  충남   예산군  사천리  "), "충남 예산군 사천리")
        self.assertEqual(norm_text("탭\t과\n줄바꿈"), "탭 과 줄바꿈")

    def test_strip_spaces_removes_all(self):
        """비교는 공백을 제거한 뒤 수행한다(규칙 3)."""
        self.assertEqual(strip_spaces(" 충남 예산군 "), "충남예산군")
        self.assertEqual(strip_spaces("산 102"), "산102")

    def test_none_and_empty(self):
        """None·빈 문자열은 빈 문자열로 떨어진다 — 예외를 던지지 않는다."""
        self.assertEqual(norm_text(None), "")
        self.assertEqual(norm_text(""), "")
        self.assertEqual(strip_spaces(None), "")


class TestSido(unittest.TestCase):
    """규칙 4 — 시도명 이형 사전 12 쌍."""

    def test_twelve_variant_pairs(self):
        for a, b in SIDO_PAIRS:
            self.assertEqual(norm_sido(a), norm_sido(b), msg="%s vs %s" % (a, b))
            self.assertTrue(sido_match(a, b), msg="%s vs %s" % (a, b))

    def test_metro_short_forms(self):
        for a, b in SIDO_METRO:
            self.assertTrue(sido_match(a, b), msg="%s vs %s" % (a, b))

    def test_distinct_sido_do_not_match(self):
        """이형 사전이 서로 다른 시도를 합쳐 버리지 않는다."""
        self.assertFalse(sido_match("충청남도", "충청북도"))
        self.assertFalse(sido_match("전라남도", "경상남도"))
        self.assertFalse(sido_match("서울특별시", "세종특별자치시"))

    def test_empty_is_mismatch(self):
        """규칙 12 — 부재는 성공이 아니다."""
        self.assertFalse(sido_match("", "충남"))
        self.assertFalse(sido_match(None, "충남"))
        self.assertFalse(sido_match("", ""))


class TestSidoCode12(unittest.TestCase):
    """규칙 5 — 전남광주통합특별시(시도코드 12) 완화. on·off 양쪽을 고정한다."""

    def test_relax_on_matches_both_parents(self):
        self.assertTrue(sido_code_match("12", "46", relax12=True))   # 전남
        self.assertTrue(sido_code_match("12", "29", relax12=True))   # 광주
        self.assertTrue(sido_code_match("46", "12", relax12=True))   # 대칭

    def test_relax_off_is_strict(self):
        self.assertFalse(sido_code_match("12", "46", relax12=False))
        self.assertFalse(sido_code_match("12", "29", relax12=False))

    def test_relax_does_not_leak_to_other_codes(self):
        """완화는 12↔{29,46} 에만 적용된다. 대조군 전북 52 는 그대로 엄격하다."""
        self.assertTrue(sido_code_match("52", "52", relax12=True))
        self.assertFalse(sido_code_match("52", "46", relax12=True))
        self.assertFalse(sido_code_match("29", "46", relax12=True))  # 광주≠전남

    def test_relaxed_flag_reports_only_when_needed(self):
        """F6 카운터용 — 엄격 불일치였는데 완화로 일치한 건만 True."""
        self.assertTrue(sido_code_relaxed("12", "46"))
        self.assertTrue(sido_code_relaxed("12", "29"))
        self.assertFalse(sido_code_relaxed("12", "12"))   # 엄격으로 이미 일치
        self.assertFalse(sido_code_relaxed("52", "46"))   # 완화해도 불일치


class TestBcode(unittest.TestCase):
    """2 차 검토 조건 2(Major) — F1 판정에 규칙 5 완화를 적용한다."""

    def test_strict_by_default(self):
        """기본값은 엄격이다 — 완화 수치와 엄격 수치를 병기해야 하므로."""
        self.assertFalse(bcode_match("1211012345", "4611012345"))
        self.assertTrue(bcode_match("4611012345", "4611012345"))

    def test_relax_on_accepts_12_vs_29_and_46(self):
        """접두 12 ↔ 29/46 이고 **나머지 8 자리가 같으면** 일치로 인정한다."""
        self.assertTrue(bcode_match("1211012345", "4611012345", relax12=True))
        self.assertTrue(bcode_match("1211012345", "2911012345", relax12=True))
        self.assertTrue(bcode_match("4611012345", "1211012345", relax12=True))

    def test_relax_requires_remaining_eight_digits_equal(self):
        """뒤 8 자리가 다르면 완화해도 불일치다 — 완화가 판정을 삼키지 않는다."""
        self.assertFalse(bcode_match("1211012345", "4611099999", relax12=True))

    def test_relaxed_flag(self):
        self.assertTrue(bcode_relaxed("1211012345", "4611012345"))
        self.assertFalse(bcode_relaxed("4611012345", "4611012345"))
        self.assertFalse(bcode_relaxed("1211012345", "4611099999"))

    def test_length_and_absence(self):
        """10 자리가 아니거나 부재면 불일치(규칙 12)."""
        self.assertFalse(bcode_match("46110", "46110"))
        self.assertFalse(bcode_match(None, "4611012345"))
        self.assertFalse(bcode_match("", ""))


class TestParseJibun(unittest.TestCase):
    """규칙 6·7·8·9 — 지번을 (san, ji_main, ji_sub) 3-튜플로."""

    def test_plain(self):
        self.assertEqual(parse_jibun("200-1"), (False, 200, 1))
        self.assertEqual(parse_jibun("200"), (False, 200, 0))

    def test_strips_land_category_suffix(self):
        """규칙 6 — 숫자 뒤 한글 1~3 자 제거."""
        self.assertEqual(parse_jibun("200-1전"), (False, 200, 1))
        self.assertEqual(parse_jibun("200-1 대"), (False, 200, 1))
        self.assertEqual(parse_jibun("200-1잡종지"), (False, 200, 1))   # 3 자
        self.assertEqual(parse_jibun("산102임"), (True, 102, 0))

    def test_does_not_strip_four_hangul(self):
        """한글 4 자 이상은 지목이 아니다 — 제거하면 과잉 정규화다."""
        self.assertIsNone(parse_jibun("200-1아무개말"))

    def test_san_prefix_forms(self):
        """규칙 7 — 산 접두는 공백 유무·번지 접미와 무관하게 같은 값."""
        expected = (True, 102, 0)
        for s in ("산102", "산 102", "산102번지", "산 102 번지"):
            self.assertEqual(parse_jibun(s), expected, msg=s)

    def test_san_with_sub(self):
        self.assertEqual(parse_jibun("산102-3"), (True, 102, 3))

    def test_strips_bunji_suffix(self):
        """규칙 8 — 번지/번/호 접미 제거."""
        self.assertEqual(parse_jibun("200-1번지"), (False, 200, 1))
        self.assertEqual(parse_jibun("200번"), (False, 200, 0))
        self.assertEqual(parse_jibun("200-1호"), (False, 200, 1))

    def test_sub_absent_is_zero(self):
        """규칙 9 — 부번 없음은 0 이다. None 이 아니다(PNU 0 패딩과 같은 정보)."""
        self.assertEqual(parse_jibun("200")[2], 0)
        self.assertEqual(parse_jibun("200-0")[2], 0)

    def test_absence_returns_none(self):
        """규칙 12 — 빈 문자열·None·비수치는 파싱 실패(None)."""
        for s in ("", "   ", None, "없음", "-", "산"):
            self.assertIsNone(parse_jibun(s), msg=repr(s))

    def test_jibun_match_treats_absence_as_mismatch(self):
        """부재끼리도 일치가 아니다 — 부재는 성공이 아니다."""
        self.assertTrue(jibun_match("200-1전", "200-1"))
        self.assertTrue(jibun_match("산102임", "산 102"))
        self.assertFalse(jibun_match("200-1", "200-2"))
        self.assertFalse(jibun_match("산102", "102"))     # 산/비산은 다른 지번
        self.assertFalse(jibun_match(None, None))
        self.assertFalse(jibun_match("", ""))


class TestParseJibunDetail(unittest.TestCase):
    """F7 — 정규화가 실제로 값을 바꿨는지 보고한다(§4.3-e)."""

    def test_reports_changed_when_normalization_applied(self):
        value, flags = parse_jibun_detail("산102임")
        self.assertEqual(value, (True, 102, 0))
        self.assertTrue(flags["changed"])

    def test_reports_unchanged_for_canonical_input(self):
        value, flags = parse_jibun_detail("200-1")
        self.assertEqual(value, (False, 200, 1))
        self.assertFalse(flags["changed"])

    def test_reports_which_rule_fired(self):
        """어떤 규칙이 발동했는지 구분한다 — 진단에 쓴다."""
        _, flags = parse_jibun_detail("200-1전")
        self.assertTrue(flags["land_category"])
        _, flags = parse_jibun_detail("산 102")
        self.assertTrue(flags["san"])
        _, flags = parse_jibun_detail("200-1번지")
        self.assertTrue(flags["bunji"])


class TestRoad(unittest.TestCase):
    """규칙 10·11 — 도로명은 접미를 유지한다. 건물번호는 숫자쌍."""

    def test_keeps_suffixes(self):
        """`대로`/`로`/`길` 을 잘라내면 서로 다른 도로가 합쳐진다."""
        self.assertEqual(norm_road("세종대로"), "세종대로")
        self.assertEqual(norm_road("세종로"), "세종로")
        self.assertNotEqual(norm_road("세종대로"), norm_road("세종로"))

    def test_keeps_numbered_gil(self):
        """`NN번길` 은 도로명의 일부다 — 숫자를 건물번호로 오인하면 안 된다."""
        self.assertEqual(norm_road("서초대로 78번길"), "서초대로78번길")
        self.assertNotEqual(norm_road("서초대로78번길"), norm_road("서초대로"))

    def test_removes_spaces_only(self):
        self.assertEqual(norm_road(" 세종  대로 "), "세종대로")

    def test_absence(self):
        self.assertEqual(norm_road(None), "")
        self.assertEqual(norm_road(""), "")

    def test_parse_bld_no(self):
        self.assertEqual(parse_bld_no("175"), (175, 0))
        self.assertEqual(parse_bld_no("175-3"), (175, 3))
        self.assertEqual(parse_bld_no(" 175 - 3 "), (175, 3))
        self.assertIsNone(parse_bld_no(""))
        self.assertIsNone(parse_bld_no(None))

    def test_basement_detected(self):
        """`지하` 접두 건은 §2.4 에서 표본에서 제외된다 — 판별만 한다."""
        self.assertTrue(is_basement("지하 175"))
        self.assertTrue(is_basement("지하175-3"))
        self.assertFalse(is_basement("175"))
        self.assertFalse(is_basement(None))


if __name__ == "__main__":
    unittest.main(verbosity=2)
