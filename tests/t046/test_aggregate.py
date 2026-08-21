#!/usr/bin/env python3
"""T046 §7 — 집계. 신뢰구간·설계효과·분모 분리·사후 가중·판정 B 회귀.

§7 이 못박은 케이스:
  - SRS CI 반폭
  - 가중 CI 반폭 `1.96·√(ΣW_h²·p(1−p)/n_h)` 와 `deff` 산출(§2.2 값 재현)
  - 분모 분리(거리 = 양측응답, 완전실패 = 표본전체, 임계통과 = 양측응답)
  - 사후 가중 합계
  - 분류 8 의 유효 분모에서 `source='parcel'` 지번 건이 빠지는지
  - 개별 주소 문자열이 출력에 섞이지 않는지(판정 B 회귀 테스트)

`aggregate.py` 는 외부 호출이 없는 순수 집계층이다. 이 파일도 그렇다.

실행: /usr/bin/python3 -m unittest discover -s tests/t046 -t . -v
"""
import math
import unittest

import _ctx  # noqa: F401  (sys.path 주입)

import aggregate  # noqa: E402


# ─────────────────────────────────────────────────────────────────────────────
# §1.5 시도 × 도농 교차표 — 지번층 모집단 N_h 의 출처
# 이 값들은 `sample.py` 가 1 패스로 다시 산출한다(§2.4 M10). 여기서는 §2.2 의
# 통계 근거를 재현하기 위한 **고정 대조표**로만 쓴다.
# ─────────────────────────────────────────────────────────────────────────────
SIDO_CROSS = {
    "seoul": (612609, 0),
    "daejeon": (124053, 0),
    "busan": (320796, 18735),
    "daegu": (203315, 51913),
    "incheon": (160491, 53881),
    "ulsan": (73692, 49396),
    "gyunggi": (617111, 695705),
    "jeju": (79016, 99070),
    "jeonbuk": (168611, 389787),
    "jeonnamgwangju": (258865, 709022),
    "gangwon": (135142, 412342),
    "gyeongnam": (228967, 717697),
    "chungbuk": (101656, 325877),
    "gyeongbuk": (202032, 666952),
    "chungnam": (105051, 577426),
    "sejong": (2264, 30735),
}

TOTAL_JIBUN = 8192209


def jibun_strata_pop():
    """공집합(0)을 뺀 지번층 30 개의 `N_h` 맵."""
    pop = {}
    for sido, (urban, rural) in SIDO_CROSS.items():
        if urban:
            pop["%s:urban:jibun" % sido] = urban
        if rural:
            pop["%s:rural:jibun" % sido] = rural
    return pop


# §2.2 가 적은 값들. 여기에 재현 대상이 전부 있다.
PLAN_SUM_W_SQ = 0.05824361
PLAN_DEFF = 1.7473
PLAN_EFFECTIVE_N = 3434
PLAN_SRS = {200: 0.06929646, 800: 0.03464823, 6000: 0.01265175, 12000: 0.00894613}
PLAN_WEIGHTED_ATYPE = 0.01672381      # n_h = 200, H = 30
PLAN_WEIGHTED_ALL = 0.01182552        # 두 주소유형 합산
PLAN_RATIO_TO_SRS = 1.321858
PLAN_WEIGHT_SPREAD = 317.0
PLAN_FPC = 0.955020
PLAN_FPC_SIMPLE = 0.954809
PLAN_F = 0.0883392


# ─────────────────────────────────────────────────────────────────────────────
# 합성 판정 레코드
# ─────────────────────────────────────────────────────────────────────────────

REC_BASE = dict(
    sid="s0000",
    sido="gyunggi",
    urban="urban",
    atype="jibun",
    stratum="gyunggi:urban:jibun",
    gate=None,
    cls=5,
    flags=(),
    vw_status="OK",
    our_addr_count=1,
    our_result_count=1,
    d_top1=30.0,
    d_min5=30.0,
    oracle="A",
    o_apx=False,
    source="navi",
    r_v=None,
    r_m=None,
    relax12_used=False,
)


def rec(**kw):
    r = dict(REC_BASE)
    r.update(kw)
    if "stratum" not in kw:
        r["stratum"] = "%s:%s:%s" % (r["sido"], r["urban"], r["atype"])
    return r


class TrapDict(dict):
    """금지 필드를 **읽는 순간** 터진다. 판정 B 를 코드 경로에서 강제한다."""

    def __getitem__(self, key):
        if key in aggregate.FORBIDDEN_RECORD_FIELDS:
            raise AssertionError("집계층이 금지 필드 %r 을 읽었다" % key)
        return dict.__getitem__(self, key)

    def get(self, key, default=None):
        if key in aggregate.FORBIDDEN_RECORD_FIELDS:
            raise AssertionError("집계층이 금지 필드 %r 을 읽었다" % key)
        return dict.get(self, key, default)


# ─────────────────────────────────────────────────────────────────────────────
# 신뢰구간
# ─────────────────────────────────────────────────────────────────────────────

class TestSRSInterval(unittest.TestCase):
    """§2.2 — 단순임의추출 가정에서의 95% 신뢰구간 반폭."""

    def test_z_is_1_96(self):
        self.assertEqual(aggregate.Z, 1.96)

    def test_reproduces_plan_halfwidths(self):
        for n, want in PLAN_SRS.items():
            self.assertAlmostEqual(aggregate.srs_halfwidth(n), want, places=7, msg=n)

    def test_worst_case_p_is_one_half(self):
        """p 를 안 주면 최악(p=0.5)을 쓴다. 어떤 p 도 그보다 넓을 수 없다."""
        worst = aggregate.srs_halfwidth(1000)
        for p in (0.01, 0.2, 0.5, 0.8, 0.99):
            self.assertLessEqual(aggregate.srs_halfwidth(1000, p=p), worst + 1e-15)
        self.assertAlmostEqual(aggregate.srs_halfwidth(1000, p=0.5), worst, places=15)

    def test_halves_when_n_quadruples(self):
        self.assertAlmostEqual(
            aggregate.srs_halfwidth(200) / aggregate.srs_halfwidth(800), 2.0, places=12)

    def test_zero_n_is_none(self):
        self.assertIsNone(aggregate.srs_halfwidth(0))


class TestWeightedInterval(unittest.TestCase):
    """사후 가중 뒤의 CI 와 설계효과. 층 가중이 317 배로 벌어진 데 대한 대가다."""

    def setUp(self):
        self.pop = jibun_strata_pop()

    def test_thirty_strata_and_total_rows(self):
        self.assertEqual(len(self.pop), 30)
        self.assertEqual(sum(self.pop.values()), TOTAL_JIBUN)

    def test_sum_w_squared_matches_plan(self):
        self.assertAlmostEqual(
            aggregate.sum_w_squared(self.pop), PLAN_SUM_W_SQ, places=8)

    def test_weights_sum_to_one(self):
        w = aggregate.weights(self.pop)
        self.assertAlmostEqual(sum(w.values()), 1.0, places=12)
        self.assertEqual(len(w), 30)

    def test_weight_spread_is_317x(self):
        w = aggregate.weights(self.pop)
        self.assertAlmostEqual(max(w.values()) / min(w.values()),
                               PLAN_WEIGHT_SPREAD, places=2)

    def test_design_effect_matches_plan(self):
        deff = aggregate.design_effect(len(self.pop), aggregate.sum_w_squared(self.pop))
        self.assertAlmostEqual(deff, PLAN_DEFF, places=4)

    def test_effective_n_matches_plan(self):
        """6,000 / 1.7473 = 3,433.85 → 3,434. 계획은 반올림을 썼다."""
        deff = aggregate.design_effect(len(self.pop), aggregate.sum_w_squared(self.pop))
        self.assertEqual(aggregate.effective_n(6000, deff), PLAN_EFFECTIVE_N)

    def test_weighted_halfwidth_per_address_type(self):
        sw2 = aggregate.sum_w_squared(self.pop)
        self.assertAlmostEqual(aggregate.weighted_halfwidth(sw2, n_h=200),
                               PLAN_WEIGHTED_ATYPE, places=7)

    def test_weighted_halfwidth_overall(self):
        sw2 = aggregate.sum_w_squared(self.pop)
        self.assertAlmostEqual(aggregate.weighted_halfwidth(sw2, n_h=400),
                               PLAN_WEIGHTED_ALL, places=7)

    def test_weighted_is_sqrt_deff_times_srs(self):
        """가중 CI / SRS CI = √deff. 두 계산이 서로를 검산한다."""
        sw2 = aggregate.sum_w_squared(self.pop)
        deff = aggregate.design_effect(len(self.pop), sw2)
        ratio = aggregate.weighted_halfwidth(sw2, n_h=200) / aggregate.srs_halfwidth(6000)
        self.assertAlmostEqual(ratio, math.sqrt(deff), places=10)
        self.assertAlmostEqual(ratio, PLAN_RATIO_TO_SRS, places=5)

    def test_equal_strata_give_deff_one(self):
        """층이 균등하면 설계효과가 1 이다 — 공식의 음성 대조군."""
        equal = dict(("s%d" % i, 1000) for i in range(30))
        self.assertAlmostEqual(
            aggregate.design_effect(30, aggregate.sum_w_squared(equal)), 1.0, places=12)


class TestFPC(unittest.TestCase):
    """유한모집단 보정 — **적용하지 않고** 각주로만 남긴다(§2.2)."""

    def test_not_applied(self):
        self.assertFalse(aggregate.FPC_APPLIED)

    def test_values_match_plan_for_smallest_stratum(self):
        n_pop, n = SIDO_CROSS["sejong"][0], 200
        self.assertAlmostEqual(aggregate.fpc(n_pop, n), PLAN_FPC, places=6)
        self.assertAlmostEqual(aggregate.fpc_simple(n_pop, n), PLAN_FPC_SIMPLE, places=6)
        self.assertAlmostEqual(n / float(n_pop), PLAN_F, places=6)

    def test_fpc_is_one_when_population_is_huge(self):
        self.assertAlmostEqual(aggregate.fpc(10 ** 9, 200), 1.0, places=6)

    def test_halfwidth_does_not_secretly_apply_fpc(self):
        """`srs_halfwidth` 는 모집단을 인자로 받지 않는다 — 보정이 끼어들 여지가 없다."""
        self.assertAlmostEqual(aggregate.srs_halfwidth(200), PLAN_SRS[200], places=7)


# ─────────────────────────────────────────────────────────────────────────────
# 분모 분리
# ─────────────────────────────────────────────────────────────────────────────

class TestDenominators(unittest.TestCase):
    """§4 — 지표마다 분모가 다르다. 섞으면 수치가 조용히 틀어진다."""

    def setUp(self):
        self.records = [
            rec(sid="a", gate=None, cls=5, vw_status="OK", our_addr_count=1),
            rec(sid="b", gate=None, cls=6, vw_status="OK", our_addr_count=3),
            rec(sid="c", gate=None, cls=1, vw_status="OK", our_addr_count=0,
                d_top1=None, d_min5=None),
            rec(sid="d", gate="E1", cls=None, vw_status="NOT_FOUND",
                our_addr_count=1, d_top1=None, d_min5=None),
            rec(sid="e", gate="E2", cls=None, vw_status="OK",
                our_addr_count=0, d_top1=None, d_min5=None),
        ]

    def test_d0_excludes_both_gates(self):
        d = aggregate.denominators(self.records)
        self.assertEqual(d.total, 5)
        self.assertEqual(d.e1, 1)
        self.assertEqual(d.e2, 1)
        self.assertEqual(d.d0, 3)

    def test_both_responded_requires_both_sides(self):
        """양측 응답 = VWorld `status==OK` **그리고** 우리 `kind='addr'` ≥ 1."""
        d = aggregate.denominators(self.records)
        self.assertEqual(d.both_responded, 2)   # a, b (c 는 우리 무응답)

    def test_distance_denominator_is_both_responded(self):
        d = aggregate.denominators(self.records)
        self.assertEqual(aggregate.DENOM_OF["distance"](d), d.both_responded)

    def test_threshold_pass_denominator_is_both_responded(self):
        d = aggregate.denominators(self.records)
        self.assertEqual(aggregate.DENOM_OF["threshold_pass"](d), d.both_responded)

    def test_total_failure_denominator_is_whole_sample(self):
        """완전 실패율만 표본 전체가 분모다 — 게이트 건도 포함한다."""
        d = aggregate.denominators(self.records)
        self.assertEqual(aggregate.DENOM_OF["total_failure"](d), d.total)

    def test_class_table_denominator_is_d0(self):
        d = aggregate.denominators(self.records)
        self.assertEqual(aggregate.DENOM_OF["class_table"](d), d.d0)

    def test_every_metric_declares_its_denominator(self):
        for name in ("distance", "threshold_pass", "total_failure", "class_table"):
            self.assertIn(name, aggregate.DENOM_OF, msg=name)

    def test_rate_on_zero_denominator_is_none_not_zero(self):
        """0/0 을 0% 로 적으면 '완벽하다' 로 읽힌다. None 이어야 한다."""
        self.assertIsNone(aggregate.rate(0, 0))
        self.assertEqual(aggregate.rate(0, 10), 0.0)
        self.assertEqual(aggregate.rate(3, 4), 0.75)


class TestResponseRateDualDefinition(unittest.TestCase):
    """엄격(`kind='addr'`≥1) / 광의(`results`≥1) 두 정의. 차이가 곧 F2 다."""

    def setUp(self):
        self.records = [
            rec(sid="a", our_addr_count=1, our_result_count=1),
            rec(sid="b", our_addr_count=0, our_result_count=2),   # 카테고리 오폴백
            rec(sid="c", our_addr_count=0, our_result_count=0),
            rec(sid="d", our_addr_count=2, our_result_count=5),
        ]

    def test_strict_and_broad_differ(self):
        self.assertEqual(aggregate.response_rate(self.records, strict=True), 0.5)
        self.assertEqual(aggregate.response_rate(self.records, strict=False), 0.75)

    def test_gap_equals_category_fallback_count(self):
        strict = aggregate.response_rate(self.records, strict=True)
        broad = aggregate.response_rate(self.records, strict=False)
        self.assertAlmostEqual((broad - strict) * len(self.records), 1.0, places=12)


# ─────────────────────────────────────────────────────────────────────────────
# 사후 가중
# ─────────────────────────────────────────────────────────────────────────────

class TestPostStratification(unittest.TestCase):

    def test_weighted_rate_differs_from_naive_pooling(self):
        """가중이 실제로 값을 바꾼다 — 안 바뀌면 가중을 안 한 것이다."""
        pop = {"big": 9000, "small": 1000}
        num = {"big": 90, "small": 10}
        den = {"big": 100, "small": 100}
        self.assertAlmostEqual(
            aggregate.post_stratified_rate(num, den, pop), 0.82, places=12)
        self.assertAlmostEqual(
            aggregate.rate(sum(num.values()), sum(den.values())), 0.50, places=12)

    def test_equal_populations_reduce_to_simple_mean(self):
        pop = {"a": 500, "b": 500}
        self.assertAlmostEqual(
            aggregate.post_stratified_rate({"a": 30, "b": 10}, {"a": 100, "b": 100}, pop),
            0.20, places=12)

    def test_strata_with_zero_denominator_are_dropped_and_weights_renormalized(self):
        """관측이 0 인 층은 비율을 만들 수 없다. 남은 층의 가중을 다시 1 로 맞춘다."""
        pop = {"a": 6000, "b": 3000, "c": 1000}
        got = aggregate.post_stratified_rate({"a": 50, "b": 30, "c": 0},
                                             {"a": 100, "b": 100, "c": 0}, pop)
        self.assertAlmostEqual(got, (6000 * 0.5 + 3000 * 0.3) / 9000.0, places=12)

    def test_reports_dropped_strata(self):
        pop = {"a": 6000, "b": 3000, "c": 1000}
        _, dropped = aggregate.post_stratified_rate(
            {"a": 50, "b": 30, "c": 0}, {"a": 100, "b": 100, "c": 0}, pop,
            with_dropped=True)
        self.assertEqual(dropped, ["c"])

    def test_unknown_stratum_raises(self):
        """표본에 있는데 모집단표에 없는 층 = 표본 생성과 집계의 불일치다."""
        with self.assertRaises(KeyError):
            aggregate.post_stratified_rate({"z": 1}, {"z": 2}, {"a": 10})

    def test_all_strata_empty_is_none(self):
        self.assertIsNone(aggregate.post_stratified_rate({"a": 0}, {"a": 0}, {"a": 10}))


# ─────────────────────────────────────────────────────────────────────────────
# 분류 8 의 분모
# ─────────────────────────────────────────────────────────────────────────────

class TestClass8Denominator(unittest.TestCase):
    """지번층에서 우리 답이 `source='parcel'` 이면 심판이 R(M)=참을 강제한다.

    필지 대표점은 정의상 그 필지 안이다. 따라서 그런 건은 분류 8
    (`R(V) ∧ ¬R(M)`)에 **원리상 들어갈 수 없다**. 분모에 남겨두면 분류 8 의
    비율이 구조적으로 과소평가된다. 유효 분모와 전체 분모를 **둘 다** 싣는다(§8-11).
    """

    def setUp(self):
        self.records = [
            rec(sid="c7", cls=7, atype="jibun", source="navi"),
            rec(sid="c8", cls=8, atype="jibun", source="navi"),
            rec(sid="c9", cls=9, atype="jibun", source="parcel"),
            rec(sid="c10", cls=10, atype="jibun", source="parcel"),
            rec(sid="c11", cls=11, atype="jibun", source=None),
            rec(sid="r7", cls=7, atype="road", source="parcel"),   # 도로명은 심판이 다름
            rec(sid="b5", cls=5, atype="jibun", source="parcel"),  # C 군 아님
        ]

    def test_c_group_membership(self):
        self.assertEqual(tuple(aggregate.C_GROUP), (7, 8, 9, 10, 11))

    def test_overall_denominator_is_whole_c_group(self):
        eff, overall = aggregate.class8_denominators(self.records)
        self.assertEqual(overall, 6)

    def test_effective_denominator_drops_jibun_parcel(self):
        eff, overall = aggregate.class8_denominators(self.records)
        self.assertEqual(eff, 4)          # c9, c10 제외

    def test_road_parcel_is_not_dropped(self):
        """도로명층 심판은 출입구·건물중심 거리다. `parcel` 이어도 R(M) 이 강제되지 않는다."""
        eff, _ = aggregate.class8_denominators(
            [rec(sid="r", cls=7, atype="road", source="parcel")])
        self.assertEqual(eff, 1)

    def test_effective_never_exceeds_overall(self):
        eff, overall = aggregate.class8_denominators(self.records)
        self.assertLessEqual(eff, overall)

    def test_both_rates_are_reported(self):
        row = aggregate.class8_row(self.records)
        self.assertEqual(row.count, 1)
        self.assertAlmostEqual(row.rate_effective, 1 / 4.0, places=12)
        self.assertAlmostEqual(row.rate_overall, 1 / 6.0, places=12)


class TestF8Counter(unittest.TestCase):
    """2 차 검토 조건 3 — 분류 2 를 A 군 밖에서도 계상한다."""

    def test_counts_a_group_and_all_group_separately(self):
        records = [
            rec(sid="a", cls=2, oracle="P", source="parcel", flags=()),
            rec(sid="b", cls=5, oracle="P", source="parcel", flags=("F8",)),
            rec(sid="c", cls=9, oracle="P", source="parcel", flags=("F8",)),
            rec(sid="d", cls=5, oracle="A", source="navi", flags=()),
        ]
        row = aggregate.class2_row(records)
        self.assertEqual(row.a_group, 1)
        self.assertEqual(row.all_group, 3)

    def test_all_group_is_never_smaller_than_a_group(self):
        records = [rec(sid="a", cls=2, oracle="P", source="parcel")]
        row = aggregate.class2_row(records)
        self.assertGreaterEqual(row.all_group, row.a_group)


class TestRelax12Counter(unittest.TestCase):
    """2 차 검토 조건 1 — 시도코드 12 완화는 **F6 과 별도 카운터**다."""

    def test_relax_counter_is_separate_from_f6(self):
        records = [
            rec(sid="a", relax12_used=True, flags=()),
            rec(sid="b", relax12_used=False, flags=("F6",)),
            rec(sid="c", relax12_used=True, flags=("F6",)),
        ]
        counts = aggregate.relax12_counts(records)
        self.assertEqual(counts.oracle_relax, 2)
        self.assertEqual(counts.f6_flag, 2)
        self.assertNotEqual(counts.oracle_relax, counts.f6_flag + 1)

    def test_strict_and_relaxed_rates_are_both_produced(self):
        """§8-10 은 완화 전/후 두 수치를 병기하라고 한다."""
        records = [
            rec(sid="a", cls=1, relax12_used=True),
            rec(sid="b", cls=1, relax12_used=False),
            rec(sid="c", cls=4, relax12_used=False),
        ]
        strict, relaxed = aggregate.class_rate_strict_and_relaxed(records, cls=1)
        self.assertAlmostEqual(relaxed, 2 / 3.0, places=12)
        self.assertAlmostEqual(strict, 1 / 3.0, places=12)


# ─────────────────────────────────────────────────────────────────────────────
# 판정 B 회귀
# ─────────────────────────────────────────────────────────────────────────────

class TestNoIndividualAddressesInOutput(unittest.TestCase):
    """§6 — 리포트에 개별 주소 문자열을 싣지 않는다. 집계·분포·건수만."""

    def setUp(self):
        self.pop = jibun_strata_pop()
        self.secrets = ["세종특별자치시 장군면 하봉리 177-4",
                        "경기도 성남시 분당구 판교로 255",
                        "3611035026101770004000001"]
        self.records = []
        for i, s in enumerate(self.secrets):
            self.records.append(rec(sid="s%03d" % i, query=s, vw_text=s,
                                    our_text=s, lon=127.1, lat=37.4))

    def test_forbidden_fields_are_declared(self):
        for f in ("query", "vw_text", "our_text", "lon", "lat"):
            self.assertIn(f, aggregate.FORBIDDEN_RECORD_FIELDS, msg=f)

    def test_render_never_reads_forbidden_fields(self):
        """읽는 순간 터지는 dict 를 넣는다 — 출력에 안 실렸다는 것보다 강한 조건이다."""
        trapped = [TrapDict(r) for r in self.records]
        aggregate.render_report(trapped, self.pop)   # 폭발하면 실패

    def test_rendered_text_contains_no_sample_string(self):
        text = aggregate.render_report(self.records, self.pop)
        for s in self.secrets:
            self.assertNotIn(s, text)

    def test_rendered_text_contains_no_sid(self):
        """식별자도 싣지 않는다 — 표본 파일과 대조하면 개별 주소가 복원된다."""
        text = aggregate.render_report(self.records, self.pop)
        for r in self.records:
            self.assertNotIn(r["sid"], text)

    def test_rendered_text_contains_no_bare_coordinates(self):
        text = aggregate.render_report(self.records, self.pop)
        self.assertNotIn("127.1", text)
        self.assertNotIn("37.4", text)

    def test_render_still_reports_counts(self):
        """검열이 지나쳐 알맹이까지 지우면 안 된다."""
        text = aggregate.render_report(self.records, self.pop)
        self.assertIn("3", text)
        self.assertIn("deff", text.lower())


class TestReportRequiredFigures(unittest.TestCase):
    """§8 리포트 필수 기재 중 집계층이 책임지는 항목."""

    def test_report_states_deff_and_effective_n(self):
        pop = jibun_strata_pop()
        text = aggregate.render_report([rec(sid="x")], pop)
        self.assertIn("1.747", text)
        self.assertIn("3,434", text)

    def test_report_states_gate_counts(self):
        pop = jibun_strata_pop()
        text = aggregate.render_report(
            [rec(sid="a", gate="E1"), rec(sid="b", gate="E2"), rec(sid="c")], pop)
        self.assertIn("E1", text)
        self.assertIn("E2", text)

    def test_report_includes_all_eleven_classes_even_when_zero(self):
        """0 건인 분류를 표에서 빼면 도달가능성 논의가 무너진다."""
        pop = jibun_strata_pop()
        text = aggregate.render_report([rec(sid="a", cls=5)], pop)
        for n in range(1, 12):
            self.assertIn("분류 %d" % n, text, msg=n)


if __name__ == "__main__":
    unittest.main(verbosity=2)
