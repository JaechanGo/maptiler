#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""지번 정규화 규칙(addr_norm) 단위 테스트 — T024 채점 자산 정정.

배경: 행안부 595건 채점 자산의 기대 지번에 표기 결함이 섞여 있어, 정상 출력이
      오답으로 집계된다. 규칙 A(법정동 접미사 뒤 공백)·B(`-0` 접미)·C-1(`산` 접두
      공백)로 흡수하되, **결과를 좋게 만들려고 규칙을 넓히는 것**을 막는 것이
      이 테스트의 본체다. 최상위 불변식은 `norm(산X) != norm(X)` 이다.

사용:
      python3 scripts/test_addr_norm.py            # 전체
      python3 scripts/test_addr_norm.py -v         # 케이스별
      T024_XLSX=... T024_RT_DUMP=... python3 scripts/test_addr_norm.py

판정: 실패 0 이어야 한다. 전수 단언(595건)·회귀 가드(D/E/C-2)가 포함되므로
      원본 xlsx 와 왕복 덤프가 없으면 해당 테스트는 skip 되고 그 사실이 출력된다.
      skip 은 통과가 아니다 — impl-report 에 skip 수를 반드시 기록한다.

의존: 표준 라이브러리 + openpyxl(원본 xlsx 읽기 전용). 서버·DB 호출 없음.
"""
import csv
import json
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from addr_norm import (  # noqa: E402
    base_norm,
    post_norm,
    norm_jibun,
    norm_road,
    rule_a,
    rule_b,
    rule_c1,
)

HERE = Path(__file__).resolve().parent
CORRECTIONS = Path(os.environ.get("T024_CORRECTIONS", HERE / "595-corrections.csv"))

XLSX = Path(os.environ.get(
    "T024_XLSX",
    os.path.expanduser(
        "~/Downloads/행정안전부 제공_7월 호우피해지역 현황_위경도산출(2026.8.5_Rev0.2).xlsx"),
))
RT_DUMP = Path(os.environ.get(
    "T024_RT_DUMP",
    "/private/tmp/claude-501/-Users-jaechango-cudo-Desktop-mac-maptiler"
    "/cf4cd315-bd4b-4b91-af21-641746de3b79/scratchpad/roundtrip_after_t019.json",
))

EVIDENCE_KINDS = {"grammar", "db-absence-of-form", "unique", "string-defect", "none"}

# 계획 §3.4 의 범위 밖 목록 (T021·T019 소관). 규칙이 이들을 "고쳐서는" 안 된다.
D_INCHEON = ["209", "210", "243", "244", "245", "246", "247", "248", "249", "250", "251"]
C2_DEFERRED = ["283", "342", "345", "348", "352", "353", "483"]


# ---------------------------------------------------------------- 자료 적재
def _load_expected():
    """원본 xlsx 의 기대 지번 595건. 반드시 read_only=True (원본 불변 보장)."""
    try:
        import openpyxl
    except ImportError:  # pragma: no cover
        return None
    if not XLSX.exists():
        return None
    ws = openpyxl.load_workbook(XLSX, data_only=True, read_only=True)["Sheet1"]
    out = []
    for r in ws.iter_rows(min_row=4, values_only=True):
        no = "" if r[1] is None else str(r[1]).strip()
        if not no:
            continue
        out.append((no, "" if r[5] is None else str(r[5]).strip()))
    return out


def _load_rt_bad():
    """왕복 덤프의 불일치 79건 {no: (기대, 우리 출력)}."""
    if not RT_DUMP.exists():
        return None
    d = json.load(open(RT_DUMP))
    return {b["no"]: (b["in"], b["out"] or "") for b in d["rt_bad"]}


def _load_corrections():
    if not CORRECTIONS.exists():
        return None
    with open(CORRECTIONS, encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


EXPECTED = _load_expected()
RT_BAD = _load_rt_bad()
ROWS = _load_corrections()


# ---------------------------------------------------------------- §10.1 규칙 B
class TestRuleB(unittest.TestCase):
    """`-0` 접미 제거. 부번을 파괴하지 않는 것이 최우선."""

    BASE = "경상북도 의성군 단촌면 구계리 "

    def test_trailing_zero_removed(self):
        self.assertEqual(norm_jibun(self.BASE + "617-0"), self.BASE + "617")

    def test_617_01_untouched(self):
        """`-0` 이 부번의 접두일 때 파괴 금지."""
        self.assertEqual(norm_jibun(self.BASE + "617-01"), self.BASE + "617-01")

    def test_617_10_untouched(self):
        """★ `-0` 이 부번 끝자리일 때 파괴 금지 — 최우선 케이스."""
        self.assertEqual(norm_jibun(self.BASE + "617-10"), self.BASE + "617-10")

    def test_617_0_2_untouched(self):
        """말미가 아니면 미적용."""
        self.assertEqual(norm_jibun(self.BASE + "617-0-2"), self.BASE + "617-0-2")

    def test_bonbun_zero_boundary(self):
        self.assertEqual(norm_jibun(self.BASE + "0-0"), self.BASE + "0")

    def test_trailing_space_composition(self):
        """꼬리 공백 정규화(f)와의 합성."""
        self.assertEqual(norm_jibun(self.BASE + "617-0 "), self.BASE + "617")

    def test_idempotent(self):
        for s in ("617-0", "617-10", "617-01", "617-0-2", "0-0"):
            with self.subTest(s=s):
                once = norm_jibun(self.BASE + s)
                self.assertEqual(norm_jibun(once), once)

    def test_rule_b_is_pure_postprocessor(self):
        """rule_b 는 문자열 → 문자열 순수 함수여야 한다."""
        s = self.BASE + "617-0"
        self.assertEqual(rule_b(s), rule_b(s))


# ---------------------------------------------------------------- §10.2 규칙 A
class TestRuleA(unittest.TestCase):
    """법정동 접미사(리·동·읍·면·가) 뒤 번지 공백 삽입. `로`·`길`은 절대 제외."""

    def test_ri_basic(self):
        self.assertEqual(norm_jibun("경기도 김포시 월곶면 성동리263-8"),
                         "경기도 김포시 월곶면 성동리 263-8")

    def test_idempotent(self):
        s = "경기도 김포시 월곶면 성동리 263-8"
        self.assertEqual(norm_jibun(s), s)

    def test_ro_not_applied_chungmuro(self):
        """★ `충무로1가` 의 `로` 에 적용되면 법정동명이 파괴된다."""
        s = "서울특별시 중구 충무로1가 25"
        self.assertEqual(norm_jibun(s), "서울특별시 중구 충무로1가 25")

    def test_ro_not_applied_sejongdaero(self):
        """★ 도로명 `세종대로110` 불변."""
        s = "서울특별시 종로구 세종대로110"
        self.assertEqual(norm_jibun(s), s)

    def test_gil_not_applied(self):
        s = "서울특별시 종로구 자하문로7길12"
        self.assertEqual(norm_jibun(s), s)

    def test_ri_with_bubun(self):
        self.assertEqual(norm_jibun("경기도 안성시 일죽면 신락리858-14"),
                         "경기도 안성시 일죽면 신락리 858-14")

    def test_eup_child_ri(self):
        self.assertEqual(norm_jibun("경기도 평택시 팽성읍 안정리183-13"),
                         "경기도 평택시 팽성읍 안정리 183-13")

    def test_dong_token(self):
        """표본 0건인 `동` 토큰 — R-1 근거를 테스트로 고정."""
        self.assertEqual(norm_jibun("경기도 파주시 하지석동12"),
                         "경기도 파주시 하지석동 12")

    def test_eup_myeon_ga_tokens(self):
        cases = [
            ("강원도 인제군 인제읍5", "강원도 인제군 인제읍 5"),
            ("강원도 홍천군 화촌면7", "강원도 홍천군 화촌면 7"),
            ("서울특별시 중구 충무로1가25", "서울특별시 중구 충무로1가 25"),
        ]
        for src, want in cases:
            with self.subTest(src=src):
                self.assertEqual(norm_jibun(src), want)

    def test_rule_a_alone(self):
        self.assertEqual(rule_a("성동리263-8"), "성동리 263-8")
        self.assertEqual(rule_a("충무로1가"), "충무로1가")


# ---------------------------------------------------------------- §10.3 규칙 C-1
class TestRuleC1(unittest.TestCase):
    """`산` 접두 공백 흡수. 지명의 `산` 오탐 금지가 핵심."""

    def test_san_space_absorbed(self):
        self.assertEqual(norm_jibun("강원도 인제군 기린면 방동리 산 282-1"),
                         "강원도 인제군 기린면 방동리 산282-1")

    def test_idempotent(self):
        s = "강원도 인제군 기린면 방동리 산282-1"
        self.assertEqual(norm_jibun(s), s)

    def test_does_not_invent_san(self):
        s = "강원도 인제군 기린면 방동리 282-1"
        self.assertEqual(norm_jibun(s), s)

    def test_san_vs_general_lot_are_distinct(self):
        """★★ 이 태스크 최상위 불변식 — 산번지와 일반번지는 절대 같지 않다."""
        self.assertNotEqual(norm_jibun("강원도 인제군 기린면 방동리 산282-1"),
                            norm_jibun("강원도 인제군 기린면 방동리 282-1"))
        self.assertNotEqual(norm_jibun("강원도 인제군 기린면 방동리 산 282-1"),
                            norm_jibun("강원도 인제군 기린면 방동리 282-1"))

    def test_jungsan_dong_false_positive(self):
        """★ `중산동` 의 `산` 오탐 금지."""
        s = "인천광역시 영종구 중산동 1354-1"
        self.assertEqual(norm_jibun(s), s)

    def test_sanhwang_dong_false_positive(self):
        """★ `산황동` 오탐 금지."""
        s = "충청남도 천안시 산황동 12"
        self.assertEqual(norm_jibun(s), s)

    def test_baekseoksan_gil_false_positive(self):
        s = "경기도 고양시 백석산길 21"
        self.assertEqual(norm_jibun(s), s)

    def test_dong_child_san_lot(self):
        self.assertEqual(norm_jibun("경기도 파주시 하지석동 산 54-1"),
                         "경기도 파주시 하지석동 산54-1")

    def test_san_without_digit_untouched(self):
        s = "강원도 인제군 기린면 방동리 산"
        self.assertEqual(norm_jibun(s), s)

    def test_c3_double_contamination_left_to_table(self):
        """`산282-1  산` 은 규칙이 손대지 않는다 — C-3 은 정정표 담당."""
        got = norm_jibun("강원도 인제군 기린면 방동리 산282-1  산")
        self.assertEqual(got, "강원도 인제군 기린면 방동리 산282-1 산")
        self.assertNotEqual(got, "강원도 인제군 기린면 방동리 산282-1")

    def test_rule_c1_alone(self):
        self.assertEqual(rule_c1("방동리 산 282-1"), "방동리 산282-1")
        self.assertEqual(rule_c1("중산동 12"), "중산동 12")


# ---------------------------------------------------------------- §10.4 합성·불변식
class TestComposition(unittest.TestCase):
    """post-composition 단조성: g 는 f 출력만 받는 순수 후처리여야 한다."""

    def test_norm_is_exactly_g_after_f(self):
        """★ 합격선 #1 의 함수 수준 증명.

        norm_jibun == post_norm ∘ base_norm 이 항등으로 성립하면
        f(a)==f(b) ⟹ norm(a)==norm(b) 가 수학적으로 따라온다.
        """
        samples = [
            "경기도 김포시 월곶면 성동리263-8",
            "경상북도 의성군 단촌면 구계리 617-0",
            "강원특별자치도 인제군 기린면 방동리 산 282-1",
            "서울특별시 중구 충무로1가 25 (구주소)",
            "  전북특별자치도  완주군   봉동읍 1-0 ",
        ]
        for s in samples:
            with self.subTest(s=s):
                self.assertEqual(norm_jibun(s), post_norm(base_norm(s)))

    def test_monotonicity_property(self):
        """f 출력이 같은 두 입력은 norm 출력도 같다 (단조성 직접 확인)."""
        pairs = [
            ("강원특별자치도 인제군 기린면 방동리 산 282-1",
             "강원도 인제군 기린면 방동리 산 282-1"),
            ("서울특별시 중구 충무로1가 25(비고)",
             "서울특별시  중구   충무로1가 25"),
        ]
        for a, b in pairs:
            with self.subTest(a=a):
                self.assertEqual(base_norm(a), base_norm(b))
                self.assertEqual(norm_jibun(a), norm_jibun(b))

    def test_post_norm_does_not_reenter_f(self):
        """g 가 f 를 내부에서 다시 부르면 합성 구조가 깨진다."""
        import inspect
        src = inspect.getsource(post_norm)
        self.assertNotIn("base_norm", src)

    @unittest.skipIf(EXPECTED is None, f"원본 xlsx 없음: {XLSX}")
    def test_idempotent_over_595(self):
        for no, e in EXPECTED:
            with self.subTest(no=no):
                once = norm_jibun(e)
                self.assertEqual(norm_jibun(once), once)

    @unittest.skipIf(EXPECTED is None, f"원본 xlsx 없음: {XLSX}")
    def test_monotonicity_over_595(self):
        for no, e in EXPECTED:
            with self.subTest(no=no):
                self.assertEqual(norm_jibun(e), post_norm(base_norm(e)))

    @unittest.skipIf(EXPECTED is None, f"원본 xlsx 없음: {XLSX}")
    def test_san_invariant_over_595(self):
        """★ 합격선 #3(b) 전수 단언 (R-2) — 산 포함 전 행."""
        n = 0
        for no, e in EXPECTED:
            if "산" not in e:
                continue
            n += 1
            with self.subTest(no=no, e=e):
                self.assertNotEqual(norm_jibun(e), norm_jibun(e.replace("산", "", 1)))
        self.assertGreater(n, 100, "산 포함 표본이 비정상적으로 적다")

    def test_road_uses_separate_function(self):
        """도로명은 별도 함수. norm_jibun 을 도로명에 적용해도 파괴가 없어야 한다."""
        roads = [
            "서울특별시 종로구 세종대로 110",
            "서울특별시 종로구 자하문로7길 12",
            "경기도 고양시 백석산길 21",
            "충청남도 천안시 서북구 번영로 156",
        ]
        for r in roads:
            with self.subTest(r=r):
                self.assertEqual(norm_road(r), r)
                self.assertEqual(norm_jibun(r), r)


# ---------------------------------------------------------------- §10.4 정정표
class TestCorrectionsTable(unittest.TestCase):
    """정정표는 문서·감사용이고 채점은 규칙이 한다 — 이 분리를 테스트로 고정."""

    COLS = ["no", "expected_raw", "expected_fixed", "category",
            "handler", "applied_to_scoring", "evidence_kind", "evidence"]

    @unittest.skipIf(ROWS is None, f"정정표 없음: {CORRECTIONS}")
    def test_columns(self):
        self.assertEqual(list(ROWS[0].keys()), self.COLS)

    @unittest.skipIf(ROWS is None, "정정표 없음")
    def test_row_count_at_least_51(self):
        """완료 기준: 정정표 51건 이상."""
        self.assertGreaterEqual(len(ROWS), 51)

    @unittest.skipIf(ROWS is None or EXPECTED is None, "정정표 또는 xlsx 없음")
    def test_every_no_exists_in_595(self):
        valid = {no for no, _ in EXPECTED}
        for r in ROWS:
            with self.subTest(no=r["no"]):
                self.assertIn(r["no"], valid)

    @unittest.skipIf(ROWS is None or EXPECTED is None, "정정표 또는 xlsx 없음")
    def test_expected_raw_matches_xlsx(self):
        """정정표의 원 기대값이 원본과 한 글자도 다르면 안 된다."""
        exp = dict(EXPECTED)
        for r in ROWS:
            with self.subTest(no=r["no"]):
                self.assertEqual(r["expected_raw"], exp[r["no"]])

    @unittest.skipIf(ROWS is None, "정정표 없음")
    def test_no_duplicate_rows(self):
        nos = [r["no"] for r in ROWS]
        self.assertEqual(len(nos), len(set(nos)))

    @unittest.skipIf(ROWS is None, "정정표 없음")
    def test_handler_values(self):
        for r in ROWS:
            with self.subTest(no=r["no"]):
                self.assertIn(r["handler"], ("rule", "table", "deferred"))

    @unittest.skipIf(ROWS is None, "정정표 없음")
    def test_rule_rows_never_applied_to_scoring(self):
        """handler=rule 행은 채점 치환에 쓰이지 않는다 (§6.2 규약)."""
        for r in ROWS:
            if r["handler"] == "rule":
                with self.subTest(no=r["no"]):
                    self.assertEqual(r["applied_to_scoring"], "0")

    @unittest.skipIf(ROWS is None, "정정표 없음")
    def test_deferred_rows_have_blank_fixed(self):
        """보류 항목의 정정값이 비어 있어야 새어 들어가지 않는다."""
        n = 0
        for r in ROWS:
            if r["handler"] == "deferred":
                n += 1
                with self.subTest(no=r["no"]):
                    self.assertEqual(r["expected_fixed"], "")
                    self.assertEqual(r["applied_to_scoring"], "0")
        self.assertEqual(n, 8, "보류는 C-2 7건 + C-4(NO358) 1건 = 8행")

    @unittest.skipIf(ROWS is None, "정정표 없음")
    def test_exactly_one_row_applied_to_scoring(self):
        """NO358 보류 지시 반영 후 채점 적용은 NO334 단 1행."""
        applied = [r for r in ROWS if r["applied_to_scoring"] == "1"]
        self.assertEqual([r["no"] for r in applied], ["334"])
        self.assertEqual(applied[0]["handler"], "table")

    @unittest.skipIf(ROWS is None or RT_BAD is None, "정정표 또는 왕복 덤프 없음")
    def test_applied_rows_actually_match_after_fix(self):
        """정정표 자체 검증 — 정정값을 정규화하면 우리 출력과 일치해야 한다."""
        for r in ROWS:
            if r["applied_to_scoring"] != "1":
                continue
            with self.subTest(no=r["no"]):
                self.assertIn(r["no"], RT_BAD)
                _, ours = RT_BAD[r["no"]]
                self.assertEqual(norm_jibun(r["expected_fixed"]), norm_jibun(ours))

    @unittest.skipIf(ROWS is None, "정정표 없음")
    def test_evidence_kind_is_one_of_five(self):
        for r in ROWS:
            with self.subTest(no=r["no"]):
                self.assertIn(r["evidence_kind"], EVIDENCE_KINDS)

    @unittest.skipIf(ROWS is None, "정정표 없음")
    def test_applied_rows_have_no_coordinate_basis(self):
        """★ 합격선 #11 (R-10) — 채점에 적용되는 행의 근거에 좌표·PIP·필지부존재 0건.

        보류(deferred) 행의 **보류 사유** 서술에는 "독립 좌표원 확보 후 재판단"
        처럼 좌표라는 낱말이 등장할 수 있다. 금지 대상은 좌표를 **정정 근거로
        인용**하는 것이므로 검사는 applied_to_scoring=1 행에 건다.
        """
        banned = ("coord", "pip", "distance", "거리", "좌표", "absence-of-parcel", "부존재")
        for r in ROWS:
            if r["applied_to_scoring"] != "1":
                continue
            with self.subTest(no=r["no"]):
                blob = (r["evidence_kind"] + " " + r["evidence"]).lower()
                for b in banned:
                    self.assertNotIn(b, blob)

    @unittest.skipIf(ROWS is None, "정정표 없음")
    def test_evidence_non_empty(self):
        for r in ROWS:
            with self.subTest(no=r["no"]):
                self.assertTrue(r["evidence"].strip(), "근거 공란 금지 (추정치 채우기 방지)")


# ---------------------------------------------------------------- §10.5 회귀 가드
@unittest.skipIf(RT_BAD is None, f"왕복 덤프 없음: {RT_DUMP}")
class TestRegressionGuards(unittest.TestCase):
    """범위 밖(D 인천·E 로직·C-2)이 규칙으로 '고쳐지지' 않음을 고정."""

    def _matches(self, no):
        e, o = RT_BAD[no]
        return norm_jibun(e) == norm_jibun(o)

    def test_D_incheon_still_mismatched(self):
        """인천 11건은 T021 소관 — 규칙이 손대면 안 된다."""
        for no in D_INCHEON:
            with self.subTest(no=no):
                self.assertIn(no, RT_BAD)
                self.assertFalse(self._matches(no), f"NO{no} 이 규칙으로 해소됨 — 범위 침범")

    def test_E_logic_still_mismatched(self):
        """왕복 축에서 실제로 잔존하는 로직 오류 5건. 규칙이 은폐하면 안 된다.

        계획 §3.4 의 E 12건 중 355·356·384 는 왕복 축에서 순수 `산` 공백차이며
        (test_E_reclassified_is_pure_spacing 이 문자열로 증명), 357·368·549·550 은
        왕복 축 기준선에서 이미 통과라 이 목록에 들어갈 수 없다. 축 차이이지
        계획의 오류가 아니다 — impl-report §3 참조.
        """
        for no in ("304", "343", "367", "438", "513"):
            with self.subTest(no=no):
                self.assertIn(no, RT_BAD)
                self.assertFalse(self._matches(no), f"NO{no} 이 규칙으로 해소됨 — 로직 오류 은폐")

    def test_E_reclassified_is_pure_spacing(self):
        """계획이 E 로 둔 355·356·384 는 `산` 공백 하나만 다르다 — 로직 오류가 아니다.

        규칙이 '고친' 것이 아니라 애초에 같은 필지였음을 문자열로 증명한다.
        (통과율 역산이 아니라 문자열 동일성이 근거다.)
        """
        for no in ("355", "356", "384"):
            with self.subTest(no=no):
                e, o = RT_BAD[no]
                self.assertEqual(e.replace(" ", ""), o.replace(" ", ""),
                                 "공백 외 차이가 있으면 C-1 재분류는 무효다")
                self.assertNotEqual(e, o)

    def test_E_axis_only_rows_absent_from_roundtrip(self):
        """계획 E 의 357·368·549·550 은 왕복 축 기준선에서 이미 통과 (규칙 무관)."""
        for no in ("357", "368", "549", "550"):
            with self.subTest(no=no):
                self.assertNotIn(no, RT_BAD)

    def test_C2_never_resolved_by_rules(self):
        """★ M1 고정 — C-2 7건은 규칙·정정표 어느 쪽으로도 해소되지 않는다."""
        for no in C2_DEFERRED:
            with self.subTest(no=no):
                if no in RT_BAD:
                    self.assertFalse(self._matches(no),
                                     f"NO{no} 이 규칙으로 해소됨 — 산/일반 불변식 붕괴")

    def test_C2_bystanders_pass_at_baseline_not_by_rule(self):
        """283·342·483 은 왕복 축 기준선부터 통과 — 규칙 효과가 아니다."""
        for no in ("283", "342", "483"):
            with self.subTest(no=no):
                self.assertNotIn(no, RT_BAD)

    def test_C2_residuals_keep_san_distinction(self):
        """345·348·352·353 은 기대=일반번지 / 우리=산번지. 규칙이 같게 만들면 안 된다."""
        for no in ("345", "348", "352", "353"):
            with self.subTest(no=no):
                e, o = RT_BAD[no]
                self.assertNotIn("산", e)
                self.assertIn("산", o)
                self.assertNotEqual(norm_jibun(e), norm_jibun(o))

    def test_C4_no358_deferred_not_resolved(self):
        """NO358 은 보류 — 규칙으로도 정정표로도 해소되지 않는다."""
        self.assertIn("358", RT_BAD)
        self.assertFalse(self._matches("358"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
