#!/usr/bin/env python3
"""13k-route-drift.py 게이트 단위테스트(네트워크 비의존).

대상 파일명이 하이픈·숫자 시작(13k-route-drift.py)이라 일반 import 불가 →
importlib.util.spec_from_file_location 로 모듈 핸들 확보
(scripts/test_build_studio.py 상단과 동일 기법). 스크립트는 import 시
부작용이 없다(main guard)라 그대로 로드해도 안전하다.

케이스(브리프 T2 지정):
  ① build_ods 결정성 — 같은 seed 두 번 동일·다른 seed 상이, 골든 6건 선두, 도심 접두 U:
  ② evaluate PASS — 도심 합계 1.013·p10 1.005(신호 키 보정본 실측값)
  ③ 합계 0.919(미보정본 실측) → FAIL, 사유에 합계 포함
  ④ p10 0.776(미보정본 실측) → FAIL
  ⑤ A성공·B실패 3%(> 허용 2%) → FAIL
  ⑥ 도심 0건 → FAIL("도심 비교 0건")
  ⑦ 장거리·골든만 크게 틀려도 도심 정상이면 PASS(보고만)
  ⑧ 경계값 — 합계 정확히 1.05 → PASS(비교 포함 경계)
  ⑨ main exit 3 — urlopen monkeypatch 로 연결 실패 흉내 → SystemExit(3)

실행:  python3 scripts/test_route_drift.py
       또는  python3 -m unittest scripts.test_route_drift -v
"""
import importlib.util
import os
import unittest
from unittest import mock   # `import unittest` 만으로는 unittest.mock 이 보장되지 않는다
import urllib.error

# ── 모듈 로드(하이픈·숫자 시작 파일명 대응) ─────────────────────────────────
_HERE = os.path.dirname(os.path.abspath(__file__))
_MOD_PATH = os.path.join(_HERE, "13k-route-drift.py")


def _load():
    spec = importlib.util.spec_from_file_location("route_drift_13k", _MOD_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


MOD = _load()

# 게이트 기본값(C3 과 동일값 — 경계 판정 로직에 영향받지 않게 고정)
MAX_DEV, MIN_P10, MAX_BFAIL = 0.05, 0.85, 0.02


def _row(name, da, tr):
    """compare.py rows 형식(dict) 더미 — db=da*tr, 거리비는 1.0(동일경로)."""
    return dict(name=name, da=float(da), db=float(da) * tr, la=1000.0, lb=1000.0,
                tr=tr, lr=1.0)


def _urban(n, tr):
    """도심 rows n건 — tr 균일."""
    return [_row("U:서울#%d" % i, 1000, tr) for i in range(n)]


class TestBuildOds(unittest.TestCase):
    """① OD 생성 결정성 — 스파이크 compare.py 측정값과의 비교 가능성 근거."""

    def test_same_seed_identical(self):
        self.assertEqual(MOD.build_ods(280, 20261008), MOD.build_ods(280, 20261008))

    def test_different_seed_differs(self):
        self.assertNotEqual(MOD.build_ods(280, 20261008), MOD.build_ods(280, 20261009))

    def test_golden_first_urban_prefix(self):
        ods = MOD.build_ods(70, 20261008)
        self.assertEqual(len(MOD.GOLDEN), 6)
        # 골든 6건이 정확히 선두(이름·순서까지 GOLDEN 순)
        self.assertEqual([o[0] for o in ods[:6]],
                         ["G:%s→%s" % (a, b) for a, _, b, _ in MOD.GOLDEN])
        # 골든 직후는 도심(도시 상자 순) — 접두 U:
        self.assertTrue(ods[6][0].startswith("U:"), ods[6][0])
        urban = [o for o in ods if o[0].startswith("U:")]
        self.assertTrue(urban)
        for name, _, _ in urban:
            self.assertTrue(name.startswith("U:"), name)


class TestEvaluate(unittest.TestCase):
    """②~⑧ 게이트 판정 — 도심만 대상, 장거리·골든은 보고만."""

    def _run(self, rows, fails_b_only=None, n_total=None):
        fails = fails_b_only or []
        n = n_total if n_total is not None else len(rows)
        return MOD.evaluate(rows, fails, n, MAX_DEV, MIN_P10, MAX_BFAIL)

    def test_pass_sum_1_013_p10_1_005(self):
        """② 보정본 실측 수준(합계 1.013·p10 1.005)은 PASS."""
        rows = [_row("U:서울#0", 1000, 1.005), _row("U:서울#1", 1000, 1.005)]
        rows += [_row("U:부산#%d" % i, 1000, 1.015) for i in range(8)]
        # p10(10건): 정렬 후 index 1 → 1.005. 합계 (2*1005+8*1015)/10000 = 1.013
        ok, reasons, stats = self._run(rows)
        self.assertTrue(ok, reasons)
        self.assertEqual(reasons, [])
        self.assertEqual(stats["urban_n"], 10)
        self.assertAlmostEqual(stats["total_ratio"], 1.013, places=6)
        self.assertEqual(stats["p10"], 1.005)
        self.assertEqual(stats["median"], 1.015)
        self.assertEqual(stats["bfail_ratio"], 0.0)

    def test_fail_sum_0_919(self):
        """③ 미보정본 실측(합계 -8.1%) — FAIL, 사유에 합계."""
        ok, reasons, stats = self._run(_urban(10, 0.919))
        self.assertFalse(ok)
        self.assertTrue(any("합계" in r for r in reasons), reasons)
        self.assertFalse(any("p10" in r for r in reasons), reasons)   # p10 0.919 는 통과
        self.assertAlmostEqual(stats["total_ratio"], 0.919, places=6)

    def test_fail_p10_0_776(self):
        """④ 미보정본 실측(p10 0.776) — 합계는 정상이어도 FAIL."""
        rows = [_row("U:서울#%d" % i, 1000, 0.776) for i in range(2)]
        rows += _urban(8, 1.0)
        # 합계 (2*776+8*1000)/10000 = 0.9552 — 허용 [0.95, 1.05] 안. p10 = 0.776.
        ok, reasons, stats = self._run(rows)
        self.assertFalse(ok)
        self.assertEqual(len(reasons), 1, reasons)
        self.assertTrue(any("p10" in r for r in reasons), reasons)
        self.assertFalse(any("합계" in r for r in reasons), reasons)
        self.assertEqual(stats["p10"], 0.776)

    def test_fail_bfail_3pct(self):
        """⑤ A성공·B실패 3건/100건(3%) > 허용 2% — FAIL."""
        fails = [("U:서울#%d" % i, "", "Connection refused") for i in range(3)]
        ok, reasons, stats = self._run(_urban(10, 1.0), fails_b_only=fails, n_total=100)
        self.assertFalse(ok)
        self.assertTrue(any("B실패" in r for r in reasons), reasons)
        self.assertAlmostEqual(stats["bfail_ratio"], 0.03, places=6)

    def test_fail_no_urban(self):
        """⑥ 도심 rows 0건 — 측정 불가 FAIL."""
        rows = [_row("L:서울시청→부산시청", 100000, 1.2), _row("G:춘의역→상동역", 500, 0.9)]
        ok, reasons, stats = self._run(rows)
        self.assertFalse(ok)
        self.assertIn("도심 비교 0건", reasons)
        self.assertEqual(stats["urban_n"], 0)

    def test_pass_with_wild_longdistance_golden(self):
        """⑦ 장거리·골든이 크게 틀려도 도심 정상이면 PASS(보고만)."""
        rows = _urban(10, 1.0)
        rows.append(_row("L:서울시청→부산시청", 100000, 5.0))    # +400% — 보고만
        rows.append(_row("G:서울시청→부산시청", 100000, 0.2))    # -80% — 보고만
        ok, reasons, stats = self._run(rows, n_total=12)
        self.assertTrue(ok, reasons)
        self.assertEqual(stats["urban_n"], 10)

    def test_pass_boundary_sum_exactly_1_05(self):
        """⑧ 경계값 — 합계 정확히 1.05(상한)는 포함(≤)이라 PASS."""
        ok, reasons, stats = self._run(_urban(10, 1.05))
        self.assertTrue(ok, reasons)
        self.assertEqual(stats["total_ratio"], 1.05)


class TestMainExit3(unittest.TestCase):
    """⑨ main 의 exit 3 경로 — 골든 첫 쌍 A 질의가 '연결 실패'면 SKIP."""

    def test_exit3_when_a_unreachable(self):
        with mock.patch("urllib.request.urlopen",
                        side_effect=urllib.error.URLError("Connection refused")):
            with self.assertRaises(SystemExit) as cm:
                MOD.main(["--a", "http://127.0.0.1:9", "--b", "http://127.0.0.1:9",
                          "--profile", "driving"])
        self.assertEqual(cm.exception.code, 3)

    def _http_error(self, code):
        return urllib.error.HTTPError("http://gw/route", code, "err", {}, None)

    def test_probe_5xx_means_baseline_down(self):
        """게이트웨이 502 = 뒤의 osrm 다운 → 기준 사용 불가(재빌드로 복구해야 할 때 교체를 막지 않게)."""
        for code in (502, 503, 504):
            with mock.patch("urllib.request.urlopen", side_effect=self._http_error(code)):
                self.assertFalse(MOD.probe_a("http://gw", "driving", 5), code)

    def test_probe_4xx_means_baseline_alive(self):
        with mock.patch("urllib.request.urlopen", side_effect=self._http_error(400)):
            self.assertTrue(MOD.probe_a("http://gw", "driving", 5))

    def test_exit3_when_gateway_502(self):
        with mock.patch("urllib.request.urlopen", side_effect=self._http_error(502)):
            with self.assertRaises(SystemExit) as cm:
                MOD.main(["--a", "http://gw", "--b", "http://127.0.0.1:9", "--profile", "driving"])
        self.assertEqual(cm.exception.code, 3)


if __name__ == "__main__":
    unittest.main()
