#!/usr/bin/env python3
"""T046 §7 — 배타 11 분류 + 직교 8 플래그(§4.3). **순수 함수**를 검정한다.

PostGIS 도 VWorld 도 부르지 않는다 — 오라클·심판 결과를 인자로 주입한다(§3.1).

이 파일의 첫 번째 클래스가 **C1 재발 방지 장치**다. 11 개 분류 각각에 대해
그 분류를 산출하는 입력이 실재함을 증명한다. 도달 불가능한 분류가 설계에 남아
있으면 측정 결과가 조용히 왜곡된다.

실행: /usr/bin/python3 -m unittest discover -s tests/t046 -t . -v
"""
import unittest

import _ctx  # noqa: F401  (sys.path 주입)

import classify  # noqa: E402
from classify import PREDICATES, classify_one  # noqa: E402

T = 100.0

# ── 합성 입력 ─────────────────────────────────────────────────────────
# 각 분류에 도달하는 최소 입력. 기본값은 '게이트 통과, 지번층, 플래그 없음'.
BASE = dict(
    layer="jibun",
    e1=False,
    e2=False,
    our_addr_count=0,
    d_top1=None,
    d_min5=None,
    oracle="N",
    o_apx=False,
    r_v=None,
    r_m=None,
    T=T,
    # 플래그 입력
    bcode_ours=None,
    bcode_vw=None,
    nonaddr_count=0,
    top1_is_nearest=True,
    san_query=False,
    san_ours=False,
    source=None,
    relax12_used=False,
    norm_applied=False,
)


def mk(**kw):
    obs = dict(BASE)
    obs.update(kw)
    return obs


# 11 분류 각각에 도달하는 입력. 도달가능성 증명의 본체다.
REACHABLE = {
    # A 군 — 우리 응답에 kind='addr' 가 0 건
    1: mk(our_addr_count=0, oracle="A"),
    2: mk(our_addr_count=0, oracle="P"),
    3: mk(our_addr_count=0, oracle="N", o_apx=True),
    4: mk(our_addr_count=0, oracle="N", o_apx=False),
    # B 군 — 응답이 있고 임계 안
    5: mk(our_addr_count=1, oracle="A", d_top1=42.0, d_min5=42.0),
    6: mk(our_addr_count=3, oracle="A", d_top1=250.0, d_min5=80.0),
    # C 군 — 상위 5 후보가 전부 임계 밖
    7: mk(our_addr_count=1, oracle="A", d_top1=900.0, d_min5=900.0, r_v=False, r_m=True),
    8: mk(our_addr_count=1, oracle="A", d_top1=900.0, d_min5=900.0, r_v=True, r_m=False),
    9: mk(our_addr_count=1, oracle="A", d_top1=900.0, d_min5=900.0, r_v=True, r_m=True),
    10: mk(our_addr_count=1, oracle="A", d_top1=900.0, d_min5=900.0, r_v=False, r_m=False),
    11: mk(our_addr_count=1, oracle="A", d_top1=900.0, d_min5=900.0, r_v=None, r_m=None),
}


class TestReachability(unittest.TestCase):
    """① 도달가능성 — 11 분류 전부. C1 재발 방지 장치(§12-1)."""

    def test_every_class_is_reachable(self):
        for cls_no, obs in sorted(REACHABLE.items()):
            v = classify_one(obs)
            self.assertIsNone(v.gate, msg="분류 %d 가 게이트에 걸렸다" % cls_no)
            self.assertEqual(v.cls, cls_no, msg="분류 %d 에 도달하지 못했다" % cls_no)

    def test_all_eleven_classes_are_declared(self):
        self.assertEqual(sorted(PREDICATES.keys()), list(range(1, 12)))
        self.assertEqual(sorted(REACHABLE.keys()), list(range(1, 12)))

    def test_road_layer_reaches_ten_classes(self):
        """도로명층은 분류 3 을 제외한 10 개에 도달한다(3 은 지번 전용)."""
        reached = set()
        road = {
            1: mk(layer="road", our_addr_count=0, oracle="A25"),
            2: mk(layer="road", our_addr_count=0, oracle="P"),
            4: mk(layer="road", our_addr_count=0, oracle="N"),
            5: mk(layer="road", our_addr_count=1, oracle="A25", d_top1=10.0, d_min5=10.0),
            6: mk(layer="road", our_addr_count=3, oracle="A25", d_top1=300.0, d_min5=90.0),
            7: mk(layer="road", our_addr_count=1, oracle="A25", d_top1=900.0,
                  d_min5=900.0, r_v=False, r_m=True),
            8: mk(layer="road", our_addr_count=1, oracle="A25", d_top1=900.0,
                  d_min5=900.0, r_v=True, r_m=False),
            9: mk(layer="road", our_addr_count=1, oracle="A25", d_top1=900.0,
                  d_min5=900.0, r_v=True, r_m=True),
            10: mk(layer="road", our_addr_count=1, oracle="A25", d_top1=900.0,
                   d_min5=900.0, r_v=False, r_m=False),
            11: mk(layer="road", our_addr_count=1, oracle="A25", d_top1=900.0,
                   d_min5=900.0, r_v=None, r_m=None),
        }
        for cls_no, obs in sorted(road.items()):
            self.assertEqual(classify_one(obs).cls, cls_no, msg="road 분류 %d" % cls_no)
            reached.add(cls_no)
        self.assertEqual(len(reached), 10)

    def test_a19_also_lands_in_class_1(self):
        """도로명 A19(같은 PNU 의 다른 건물)도 분류 1 이다(§4.3-c)."""
        self.assertEqual(
            classify_one(mk(layer="road", our_addr_count=0, oracle="A19")).cls, 1
        )


class TestExclusivity(unittest.TestCase):
    """② 배타성 — 어떤 입력에도 술어가 정확히 하나만 참이다."""

    def test_exactly_one_predicate_per_reachable_input(self):
        for cls_no, obs in sorted(REACHABLE.items()):
            hits = [k for k, p in PREDICATES.items() if p(obs)]
            self.assertEqual(
                hits, [cls_no], msg="분류 %d 입력에 술어 %r 이 겹쳤다" % (cls_no, hits)
            )

    def test_exclusivity_over_synthetic_cross_product(self):
        """도달가능 입력만이 아니라 조합 전반에서 배타성이 성립한다."""
        checked = 0
        for layer in ("jibun", "road"):
            for cnt in (0, 1, 5):
                for orc in ("A", "A25", "A19", "P", "N"):
                    for apx in (False, True):
                        for d1, d5 in ((None, None), (10.0, 10.0),
                                       (300.0, 90.0), (900.0, 900.0)):
                            for rv in (None, True, False):
                                for rm in (None, True, False):
                                    obs = mk(layer=layer, our_addr_count=cnt,
                                             oracle=orc, o_apx=apx,
                                             d_top1=d1, d_min5=d5, r_v=rv, r_m=rm)
                                    hits = [k for k, p in PREDICATES.items() if p(obs)]
                                    self.assertEqual(
                                        len(hits), 1,
                                        msg="술어 %r 이 겹치거나 비었다: %r" % (hits, obs),
                                    )
                                    checked += 1
        self.assertGreater(checked, 1000)


class TestGates(unittest.TestCase):
    """③ 게이트 E1·E2 가 분류에 **선행**한다(§4.3). 부재를 성공으로 세지 않는다."""

    def test_e1_blocks_classification(self):
        v = classify_one(mk(e1=True, our_addr_count=0, oracle="A"))
        self.assertEqual(v.gate, "E1")
        self.assertIsNone(v.cls)

    def test_e2_blocks_classification(self):
        v = classify_one(mk(e2=True, our_addr_count=1, oracle="A",
                            d_top1=10.0, d_min5=10.0))
        self.assertEqual(v.gate, "E2")
        self.assertIsNone(v.cls)

    def test_e1_takes_precedence_over_e2(self):
        """둘 다 걸리면 E1 이다 — 외부기준 부재가 먼저다."""
        self.assertEqual(classify_one(mk(e1=True, e2=True)).gate, "E1")


class TestJibunOnlyClass3(unittest.TestCase):
    """④ 분류 3(본번 근사)은 지번 전용이다. 도로명층에 나오면 안 된다."""

    def test_road_layer_never_yields_class_3(self):
        obs = mk(layer="road", our_addr_count=0, oracle="N", o_apx=True)
        v = classify_one(obs)
        self.assertNotEqual(v.cls, 3)
        self.assertEqual(v.cls, 4)          # O=N 이므로 4 로 떨어진다

    def test_predicate_3_is_false_on_road_layer(self):
        obs = mk(layer="road", our_addr_count=0, oracle="N", o_apx=True)
        self.assertFalse(PREDICATES[3](obs))


class TestRefereeAbsence(unittest.TestCase):
    """⑤ 심판 부재 → 11. 2 차 검토 조건 4 — C 군 판별 순서 확정."""

    def test_c_group_order_is_fixed(self):
        """순서를 코드가 선언한다. 11 을 **먼저** 본다."""
        self.assertEqual(tuple(classify.C_GROUP_ORDER), (11, 7, 8, 9, 10))

    def test_both_referees_absent(self):
        obs = mk(our_addr_count=1, oracle="A", d_top1=900.0, d_min5=900.0,
                 r_v=None, r_m=None)
        self.assertEqual(classify_one(obs).cls, 11)

    def test_mixed_absence_still_eleven(self):
        """한쪽만 부재여도 11 이다 — 반쪽 심판으로 7~10 을 가르면 안 된다."""
        for rv, rm in ((None, True), (None, False), (True, None), (False, None)):
            obs = mk(our_addr_count=1, oracle="A", d_top1=900.0, d_min5=900.0,
                     r_v=rv, r_m=rm)
            self.assertEqual(classify_one(obs).cls, 11, msg=(rv, rm))

    def test_none_is_not_coerced_to_false(self):
        """None 을 False 로 뭉개면 분류 10 으로 샌다 — 그 경로가 막혀 있는지 본다."""
        absent = mk(our_addr_count=1, oracle="A", d_top1=900.0, d_min5=900.0,
                    r_v=None, r_m=None)
        false_false = mk(our_addr_count=1, oracle="A", d_top1=900.0, d_min5=900.0,
                         r_v=False, r_m=False)
        self.assertEqual(classify_one(absent).cls, 11)
        self.assertEqual(classify_one(false_false).cls, 10)

    def test_referee_absence_does_not_affect_a_and_b_groups(self):
        """A·B 군은 심판을 보지 않는다 — 부재여도 원래 분류가 나온다."""
        self.assertEqual(classify_one(mk(our_addr_count=0, oracle="A", r_v=None)).cls, 1)
        self.assertEqual(
            classify_one(mk(our_addr_count=1, oracle="A", d_top1=10.0,
                            d_min5=10.0, r_v=None)).cls, 5
        )


class TestThreshold(unittest.TestCase):
    """⑥ 임계 T 경계. 정확히 100.0 m 는 **통과**다(d ≤ T)."""

    def test_exactly_at_threshold_passes(self):
        obs = mk(our_addr_count=1, oracle="A", d_top1=100.0, d_min5=100.0)
        self.assertEqual(classify_one(obs).cls, 5)

    def test_just_over_threshold_falls_through(self):
        obs = mk(our_addr_count=1, oracle="A", d_top1=100.0000001,
                 d_min5=100.0000001, r_v=False, r_m=False)
        self.assertEqual(classify_one(obs).cls, 10)

    def test_class6_boundary(self):
        """top-1 은 밖, 상위 5 최소가 정확히 T → 분류 6."""
        obs = mk(our_addr_count=5, oracle="A", d_top1=500.0, d_min5=100.0)
        self.assertEqual(classify_one(obs).cls, 6)

    def test_threshold_is_a_parameter(self):
        """T 는 인자다 — 25 m·1 km 민감도 분석을 같은 코드로 돌린다(§4.1)."""
        obs = mk(our_addr_count=1, oracle="A", d_top1=50.0, d_min5=50.0, T=25.0,
                 r_v=False, r_m=False)
        self.assertEqual(classify_one(obs).cls, 10)
        obs2 = dict(obs, T=1000.0)
        self.assertEqual(classify_one(obs2).cls, 5)


class TestFlagOrthogonality(unittest.TestCase):
    """⑦⑧ 플래그는 분류와 직교한다(§4.3-e)."""

    def test_f2_can_accompany_classes_1_to_4(self):
        """F2(카테고리 오폴백)는 A 군 네 분류 각각과 동반할 수 있다."""
        for cls_no in (1, 2, 3, 4):
            obs = dict(REACHABLE[cls_no])
            obs["nonaddr_count"] = 2          # kind≠addr 가 있다
            v = classify_one(obs)
            self.assertEqual(v.cls, cls_no, msg=cls_no)
            self.assertIn("F2", v.flags, msg=cls_no)

    def test_f2_requires_zero_addr_and_nonzero_nonaddr(self):
        self.assertNotIn("F2", classify_one(mk(our_addr_count=1, nonaddr_count=2,
                                               oracle="A", d_top1=10.0,
                                               d_min5=10.0)).flags)
        self.assertNotIn("F2", classify_one(mk(our_addr_count=0, nonaddr_count=0,
                                               oracle="A")).flags)

    def test_f1_is_independent_of_distance(self):
        """⑧ F1(법정동코드 불일치)은 거리와 무관하게 전 건에서 판정한다(M6 회귀).

        거리만 다르고 b_code 는 같은 두 입력에서 F1 이 똑같이 서야 한다.
        """
        near = mk(our_addr_count=1, oracle="A", d_top1=1.0, d_min5=1.0,
                  bcode_ours="4611012345", bcode_vw="4611099999")
        far = dict(near, d_top1=5000.0, d_min5=5000.0, r_v=False, r_m=False)
        self.assertIn("F1", classify_one(near).flags)
        self.assertIn("F1", classify_one(far).flags)
        self.assertEqual(classify_one(near).cls, 5)
        self.assertEqual(classify_one(far).cls, 10)

    def test_f1_also_fires_in_a_group_without_any_distance(self):
        """A 군은 거리 자체가 없다 — 그래도 b_code 가 있으면 F1 을 판정한다."""
        obs = mk(our_addr_count=0, oracle="A",
                 bcode_ours="4611012345", bcode_vw="4611099999")
        v = classify_one(obs)
        self.assertEqual(v.cls, 1)
        self.assertIn("F1", v.flags)

    def test_f1_relaxed_by_sido_12_rule(self):
        """2 차 검토 조건 2 — 접두 12 ↔ 46/29 는 완화 시 F1 이 아니다."""
        obs = mk(our_addr_count=0, oracle="A",
                 bcode_ours="1211012345", bcode_vw="4611012345")
        strict = classify_one(obs, relax12=False)
        relaxed = classify_one(obs, relax12=True)
        self.assertIn("F1", strict.flags)
        self.assertNotIn("F1", relaxed.flags)
        self.assertIn("F6", relaxed.flags)

    def test_f3_rank_contamination(self):
        """F3 — 상위 5 안에 임계 내 후보가 있는데 top-1 이 최소거리가 아니다."""
        obs = mk(our_addr_count=5, oracle="A", d_top1=300.0, d_min5=50.0,
                 top1_is_nearest=False)
        v = classify_one(obs)
        self.assertEqual(v.cls, 6)
        self.assertIn("F3", v.flags)

    def test_f4_san_confusion(self):
        obs = mk(our_addr_count=1, oracle="A", d_top1=10.0, d_min5=10.0,
                 san_query=True, san_ours=False)
        self.assertIn("F4", classify_one(obs).flags)
        obs2 = dict(obs, san_ours=True)
        self.assertNotIn("F4", classify_one(obs2).flags)

    def test_f5_source(self):
        for src in ("navi", "parcel"):
            obs = mk(our_addr_count=1, oracle="A", d_top1=10.0, d_min5=10.0, source=src)
            self.assertIn("F5", classify_one(obs).flags, msg=src)

    def test_f7_normalization_changed_value(self):
        obs = mk(our_addr_count=0, oracle="A", norm_applied=True)
        self.assertIn("F7", classify_one(obs).flags)

    def test_flags_do_not_change_class(self):
        """플래그를 전부 켜도 분류는 그대로다 — 직교성의 핵심."""
        for cls_no, base in sorted(REACHABLE.items()):
            loud = dict(base)
            loud.update(nonaddr_count=3, top1_is_nearest=False, san_query=True,
                        san_ours=False, source="parcel", norm_applied=True,
                        bcode_ours="4611012345", bcode_vw="2911012345")
            self.assertEqual(classify_one(loud).cls, cls_no, msg=cls_no)


class TestF8ParcelOutsideAGroup(unittest.TestCase):
    """2 차 검토 조건 3(Major) — 분류 2 를 A 군 밖에서도 계상한다."""

    def test_f8_fires_in_b_group(self):
        """B 군인데 우리 응답이 parcel 폴백이고 오라클이 P 인 건."""
        obs = mk(our_addr_count=1, oracle="P", d_top1=10.0, d_min5=10.0,
                 source="parcel")
        v = classify_one(obs)
        self.assertEqual(v.cls, 5)
        self.assertIn("F8", v.flags)

    def test_f8_fires_in_c_group(self):
        obs = mk(our_addr_count=1, oracle="P", d_top1=900.0, d_min5=900.0,
                 source="parcel", r_v=False, r_m=False)
        v = classify_one(obs)
        self.assertEqual(v.cls, 10)
        self.assertIn("F8", v.flags)

    def test_f8_does_not_fire_in_a_group(self):
        """A 군의 O=P 는 분류 2 로 이미 계상된다 — 이중 계상을 막는다."""
        obs = mk(our_addr_count=0, oracle="P", source="parcel")
        v = classify_one(obs)
        self.assertEqual(v.cls, 2)
        self.assertNotIn("F8", v.flags)

    def test_f8_requires_both_source_and_oracle(self):
        self.assertNotIn("F8", classify_one(mk(our_addr_count=1, oracle="A",
                                               d_top1=10.0, d_min5=10.0,
                                               source="parcel")).flags)
        self.assertNotIn("F8", classify_one(mk(our_addr_count=1, oracle="P",
                                               d_top1=10.0, d_min5=10.0,
                                               source="navi")).flags)


class TestInputContract(unittest.TestCase):
    """필수 필드 누락을 조용히 넘기지 않는다 — 기본값으로 채우면 오판이 숨는다."""

    def test_missing_required_field_raises(self):
        obs = dict(BASE)
        del obs["oracle"]
        with self.assertRaises(KeyError):
            classify_one(obs)

    def test_unknown_layer_raises(self):
        with self.assertRaises(ValueError):
            classify_one(mk(layer="dong"))

    def test_unknown_oracle_value_raises(self):
        with self.assertRaises(ValueError):
            classify_one(mk(oracle="X"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
