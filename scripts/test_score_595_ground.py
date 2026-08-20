#!/usr/bin/env python3
"""G 축(실측 정확도) 확장의 TDD 테스트 — Task 034 (I3·I4·I5·I6·I8).

대상: `scripts/15-score-595.py` 에 신설되는 G 축 자산.
  · check-independence  (I3, A003 §6.6)
  · measure-ground      (I4)
  · AXES/RESCORE_AXES · AxisScore · render_report (I5, A003 §6.7)

원칙
  - **서버를 실제로 호출하지 않는다.** 순방향/역방향 호출부는 `get` 인자로 주입한다.
    CLI 표면 검증만 subprocess 를 쓰며, 그때도 네트워크에 닿기 전에 종료하는
    경로(=`resolve_server` 가드)만 밟는다. 운영 서버 주소는 문자열로만 쓴다.
  - 기존 테스트 5파일과 채점 자산 4종의 동작은 건드리지 않는다. §4.11 이 그 회귀 가드다.

stdlib unittest 만 쓴다(pytest 미설치). 실행:
    python3 -m unittest discover -s scripts -t scripts -p 'test_*.py'
"""
import argparse
import contextlib
import hashlib
import importlib.util
import io
import json
import math
import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "15-score-595.py")


def _load():
    """숫자로 시작하는 파일명이라 일반 import 가 안 된다. 468행이 __main__ 가드라 부작용은 없다."""
    spec = importlib.util.spec_from_file_location("score595_ground_uut", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


M = _load()

# 서울 시청 근방 — 한반도 범위 검사(§2.1.2 ②)를 통과하는 기준 좌표
BASE_LAT, BASE_LON = 37.5759, 126.9768
M_PER_DEG_LAT = 111194.93          # R=6371008.8 기준 위도 1도

# 통과 경로 픽스처의 거리. **두 임계 사이**여야 한다 —
#   0.5 m(GROUND_SELF_REF_M) 초과   → 자기참조가 아니다
#   50 m(GROUND_SYSTEMATIC_M) 미만  → 데이텀 오선언·계통 고장도 아니다
# 이 밴드를 벗어난 값(예: 100 m)을 쓰면 B-1 규칙이 정상적으로 발동해
# "통과"가 아니라 "판정 불가"가 나온다. 그건 버그가 아니라 설계다.
PASS_D_M = 5.0


# ------------------------------------------------------------------ 픽스처 도구
def _tmp(suffix):
    fd, path = tempfile.mkstemp(suffix=suffix)
    os.close(fd)
    return path


def write_sample(rows, header=None, path=None):
    """G 표본 CSV 를 만든다. rows 는 열 순서대로의 문자열 튜플."""
    header = header or ["no", "jibun", "lat", "lon", "grade", "source", "crs"]
    path = path or _tmp(".csv")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(",".join(header) + "\n")
        for r in rows:
            fh.write(",".join(str(x) for x in r) + "\n")
    return path


def row(no=1, jibun="서울특별시 중구 태평로1가 31", lat=BASE_LAT, lon=BASE_LON,
        grade="L4", source="국가기준점", crs="EPSG:4326"):
    return (no, jibun, lat, lon, grade, source, crs)


def make_get(fwd=None, rev=None, calls=None):
    """서버 호출 주입기. fwd/rev 는 응답 dict 또는 callable."""
    def get(path, **params):
        if calls is not None:
            calls.append((path, params))
        if path == "/geocode":
            return fwd(params["q"]) if callable(fwd) else (fwd if fwd is not None else {})
        if path == "/reverse":
            return (rev(params["lat"], params["lon"]) if callable(rev)
                    else (rev if rev is not None else {}))
        raise AssertionError(f"예상 밖 경로: {path}")
    return get


def fwd_at(dlat_m=0.0, dlon_m=0.0):
    """기준 좌표에서 북쪽으로 dlat_m 만큼 떨어진 순방향 응답을 낸다."""
    lat = BASE_LAT + dlat_m / M_PER_DEG_LAT
    lon = BASE_LON + dlon_m / (M_PER_DEG_LAT * math.cos(math.radians(BASE_LAT)))
    return {"results": [{"lat": lat, "lon": lon}]}


def indep_args(sample, out=None, server="http://127.0.0.1:8092", allow_production=False):
    return argparse.Namespace(sample=sample, server=server,
                              allow_production=allow_production,
                              out=out or _tmp(".json"), rules=None, timeout=10.0)


def ground_args(sample, report, out=None, server="http://127.0.0.1:8092"):
    return argparse.Namespace(sample=sample, independence_report=report,
                              server=server, allow_production=False,
                              out=out, rules=None, timeout=10.0)


def read_json(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def write_json(path, obj):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(obj, fh)


def read_bytes(path):
    with open(path, "rb") as fh:
        return fh.read()


def read_text(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def run_cli(*argv):
    return subprocess.run([sys.executable, SCRIPT, *argv],
                          capture_output=True, text=True, timeout=60)


class QuietCase(unittest.TestCase):
    """이 하네스의 명령들은 보고서를 표준출력에 찍는다. 테스트 로그를 가리지 않게 삼킨다.

    실패 정보는 러너가 stderr 로 내보내므로 삼켜지지 않는다.
    """

    def run(self, result=None):
        with contextlib.redirect_stdout(io.StringIO()):
            return super().run(result)


# ================================================================== §4.1
class TestSelfRefThreshold(QuietCase):
    """GROUND_SELF_REF_M = 0.5 · 엄격 부등호 `<` (R-7)."""

    def test_049m_counts(self):
        self.assertTrue(M.is_self_ref(0.49))

    def test_050m_does_not_count(self):
        self.assertFalse(M.is_self_ref(0.50), "0.50 m 는 세지 않는다 (엄격 부등호)")

    def test_051m_does_not_count(self):
        self.assertFalse(M.is_self_ref(0.51))

    def test_zero_counts(self):
        self.assertTrue(M.is_self_ref(0.0), "T019 D-4 가 관측한 0.0 m 는 자기참조다")


# ================================================================== §4.2
class TestIsContaminated(QuietCase):
    """오염 임계 5% — 정수산술 (R-6)."""

    def test_49_of_1000_is_clean(self):
        self.assertFalse(M.is_contaminated(49, 1000), "4.90% 는 임계 미만")

    def test_19_of_400_is_clean(self):
        # 400 분모의 인접 정수쌍(19 vs 20)은 눈금을 정확히 5.00% 에 박는다.
        self.assertFalse(M.is_contaminated(19, 400))

    def test_20_of_400_is_contaminated(self):
        self.assertTrue(M.is_contaminated(20, 400), "5.00% 는 경계 포함")

    def test_21_of_400_is_contaminated(self):
        self.assertTrue(M.is_contaminated(21, 400))

    def test_zero_hits_is_clean(self):
        self.assertFalse(M.is_contaminated(0, 400))

    def test_1_of_20_is_contaminated(self):
        self.assertTrue(M.is_contaminated(1, 20))

    def test_float_unstable_combo_is_stable(self):
        self.assertTrue(M.is_contaminated(1, 3), "1/3 = 33% — 정수산술로 흔들리지 않는다")


# ================================================================== §4.3
class TestJudgeIndependence(QuietCase):
    """n_effective 하한과 3값 판정 (R2)."""

    def test_all_no_result_is_inconclusive(self):
        self.assertEqual(M.judge_independence(hits=0, n=400, n_effective=0, d_m_min=None),
                         "inconclusive", "전건 무응답은 pass 가 아니다")

    def test_effective_199_of_400_is_inconclusive(self):
        self.assertEqual(M.judge_independence(0, 400, 199, 12.0), "inconclusive")

    def test_effective_200_of_400_is_pass(self):
        self.assertEqual(M.judge_independence(0, 400, 200, 12.0), "pass", "50.00% 경계 포함")

    def test_effective_201_of_400_is_pass(self):
        self.assertEqual(M.judge_independence(0, 400, 201, 12.0), "pass")

    def test_contamination_outranks_low_effective(self):
        """B-3 교체 케이스 — hits <= n_effective 를 지키면서 오염 우선순위를 본다."""
        self.assertEqual(M.judge_independence(hits=20, n=400, n_effective=20, d_m_min=0.0),
                         "fail", "오염(fail)을 유효건수(inconclusive)보다 먼저 본다")

    def test_empty_sample_exits(self):
        with self.assertRaises(SystemExit):
            M.judge_independence(0, 0, 0, None)

    def test_has_enough_effective_odd_n(self):
        self.assertTrue(M.has_enough_effective(200, 399))
        self.assertFalse(M.has_enough_effective(199, 399))

    def test_exit_code_pass_is_zero(self):
        s = write_sample([row(i, lat=BASE_LAT, lon=BASE_LON) for i in range(1, 5)])
        a = indep_args(s)
        self.assertEqual(M.cmd_check_independence(a, get=make_get(fwd=lambda q: fwd_at(PASS_D_M))), 0)

    def test_exit_code_fail_is_3(self):
        s = write_sample([row(i) for i in range(1, 5)])
        a = indep_args(s)
        with self.assertRaises(SystemExit) as cm:
            M.cmd_check_independence(a, get=make_get(fwd=lambda q: fwd_at(0.0)))
        self.assertEqual(cm.exception.code, 3)

    def test_exit_code_inconclusive_is_4(self):
        s = write_sample([row(i) for i in range(1, 5)])
        a = indep_args(s)
        with self.assertRaises(SystemExit) as cm:
            M.cmd_check_independence(a, get=make_get(fwd={}))
        self.assertEqual(cm.exception.code, 4)

    def test_report_written_before_exit(self):
        s = write_sample([row(i) for i in range(1, 5)])
        out = _tmp(".json")
        a = indep_args(s, out=out)
        with self.assertRaises(SystemExit):
            M.cmd_check_independence(a, get=make_get(fwd=lambda q: fwd_at(0.0)))
        rep = read_json(out)
        self.assertEqual(rep["verdict"], "fail", "실패했을 때가 증거가 가장 필요한 때다")


# ================================================================== §4.4
class TestAxisScore(QuietCase):
    """비합산의 타입 수준 강제 (P1) 와 등급 필수 (P2)."""

    def setUp(self):
        self.s = M.AxisScore("S", "reverse", 10, 20)
        self.g = M.AxisScore("G", "ground", 10, 20, grade="L4")

    def test_s_plus_g_raises(self):
        with self.assertRaises(TypeError):
            self.s + self.g

    def test_s_plus_s_raises(self):
        with self.assertRaises(TypeError):
            self.s + M.AxisScore("S", "roundtrip", 1, 2)

    def test_sum_raises(self):
        with self.assertRaises(TypeError):
            sum([self.s, self.g])

    def test_iadd_raises(self):
        total = 0
        with self.assertRaises(TypeError):
            total += self.s

    def test_g_without_grade_raises(self):
        with self.assertRaises(ValueError):
            M.AxisScore("G", "ground", 10, 20)

    def test_g_with_unknown_grade_raises(self):
        with self.assertRaises(ValueError):
            M.AxisScore("G", "ground", 10, 20, grade="L3")

    def test_unknown_family_raises(self):
        with self.assertRaises(ValueError):
            M.AxisScore("X", "reverse", 1, 2)

    def test_rate_uses_own_denominator(self):
        self.assertEqual(self.s.rate(), 50.0, "fmt() 의 기본 분모 595 를 상속하지 않는다")

    def test_rate_with_zero_n(self):
        self.assertEqual(M.AxisScore("S", "reverse", 0, 0).rate(), 0.0)


# ================================================================== §4.5
class TestRenderReport(QuietCase):
    """A003 §6.7 보고 양식 — family 검증과 합산 칸 부재."""

    def setUp(self):
        self.s = M.AxisScore("S", "roundtrip", 516, 595)
        self.g = M.AxisScore("G", "ground", 10, 20, grade="L4")
        self.indep = {"verdict": "pass", "ratio_pct": 0.67, "n_effective": 288, "n": 300}

    def test_g_score_in_s_section_raises(self):
        with self.assertRaises(ValueError):
            M.render_report([self.g], [])

    def test_s_score_in_g_section_raises(self):
        with self.assertRaises(ValueError):
            M.render_report([], [self.s])

    def test_no_total_column(self):
        out = M.render_report([self.s], [self.g], self.indep)
        for banned in ("합계", "총점", "평균", "total"):
            self.assertNotIn(banned, out.lower() if banned == "total" else out,
                             f"보고 양식에 '{banned}' 칸이 있으면 S/G 합산이 열린다")

    def test_g_section_carries_grade_and_independence(self):
        out = M.render_report([self.s], [self.g], self.indep)
        self.assertIn("L4", out)
        self.assertIn("pass", out)
        self.assertIn("288", out)
        self.assertIn("0.67", out)

    def test_zero_n_renders_na_not_zero_pct(self):
        """B-5 — n=0 은 '정확도 0%' 가 아니라 '측정 없음' 이다."""
        empty = M.AxisScore("G", "ground", 0, 0, grade="L2")
        out = M.render_report([], [empty], self.indep)
        self.assertIn("n/a", out)
        self.assertNotIn("0.0%", out)

    def test_s_and_g_sections_are_separate(self):
        out = M.render_report([self.s], [self.g], self.indep)
        self.assertIn("[S]", out)
        self.assertIn("[G]", out)
        self.assertLess(out.index("[S]"), out.index("[G]"))


# ================================================================== §4.6
class TestMeasureGroundGate(QuietCase):
    """게이트는 끌 수 없다 (§2.2.1)."""

    def setUp(self):
        self.sample = write_sample([row(i) for i in range(1, 5)])
        self.fp = hashlib.sha256(read_bytes(self.sample)).hexdigest()

    def _report(self, **over):
        base = {"schema": "ground-independence/1", "sample_fingerprint": self.fp,
                "crs": "EPSG:4326", "verdict": "pass", "n": 4, "n_effective": 4,
                "hits": 0, "ratio_pct": 0.0}
        base.update(over)
        p = _tmp(".json")
        write_json(p, base)
        return p

    def test_fingerprint_mismatch_exits(self):
        rep = self._report(sample_fingerprint="0" * 64)
        with self.assertRaises(SystemExit):
            M.cmd_measure_ground(ground_args(self.sample, rep), get=make_get())

    def test_verdict_fail_exits(self):
        rep = self._report(verdict="fail")
        with self.assertRaises(SystemExit):
            M.cmd_measure_ground(ground_args(self.sample, rep), get=make_get())

    def test_verdict_inconclusive_exits(self):
        rep = self._report(verdict="inconclusive")
        with self.assertRaises(SystemExit):
            M.cmd_measure_ground(ground_args(self.sample, rep), get=make_get())

    def test_verdict_missing_or_unknown_exits(self):
        for rep in (self._report(verdict="어쩌다생긴새값"), self._report()):
            if "verdict" in read_json(rep) and rep.endswith(".json"):
                pass
        rep = self._report()
        d = read_json(rep)
        d.pop("verdict")
        write_json(rep, d)
        with self.assertRaises(SystemExit):
            M.cmd_measure_ground(ground_args(self.sample, rep), get=make_get())
        with self.assertRaises(SystemExit):
            M.cmd_measure_ground(ground_args(self.sample, self._report(verdict="unknown")),
                                 get=make_get())

    def test_report_crs_mismatch_exits(self):
        rep = self._report(crs="EPSG:5186")
        with self.assertRaises(SystemExit):
            M.cmd_measure_ground(ground_args(self.sample, rep), get=make_get())

    def test_no_server_call_when_gate_fails(self):
        calls = []
        rep = self._report(verdict="fail")
        with self.assertRaises(SystemExit):
            M.cmd_measure_ground(ground_args(self.sample, rep), get=make_get(calls=calls))
        self.assertEqual(calls, [], "게이트 실패 뒤에는 네트워크가 나가면 안 된다")

    def test_no_bypass_flag_in_parser(self):
        out = run_cli("measure-ground", "--help")
        for flag in ("--force", "--skip-independence", "--no-gate", "--ignore-independence"):
            self.assertNotIn(flag, out.stdout, "게이트를 끌 수 있으면 게이트가 아니다")


# ================================================================== §4.7
class TestIndependenceReportRoundtrip(QuietCase):
    """생산자(check-independence) ↔ 소비자(measure-ground) 왕복 (R5)."""

    def test_roundtrip_with_lowercase_crs_sample(self):
        """B-4 — 소문자 `epsg:4326` 표본이 게이트에서 거부되지 않는다."""
        sample = write_sample([row(i, crs="epsg: 4326") for i in range(1, 5)])
        out = _tmp(".json")
        rc = M.cmd_check_independence(indep_args(sample, out=out),
                                      get=make_get(fwd=lambda q: fwd_at(PASS_D_M)))
        self.assertEqual(rc, 0)
        rc2 = M.cmd_measure_ground(
            ground_args(sample, out),
            get=make_get(rev=lambda la, lo: {"address": {"parcel": "서울특별시 중구 태평로1가 31"}}))
        self.assertEqual(rc2, 0, "생산자가 낸 보고서를 소비자가 그대로 먹어야 한다")

    def test_report_has_all_required_keys(self):
        sample = write_sample([row(i) for i in range(1, 5)])
        out = _tmp(".json")
        M.cmd_check_independence(indep_args(sample, out=out),
                                 get=make_get(fwd=lambda q: fwd_at(PASS_D_M)))
        rep = read_json(out)
        for key in ("schema", "sample_fingerprint", "crs", "verdict",
                    "n", "n_effective", "hits", "ratio_pct"):
            self.assertIn(key, rep)

    def test_fingerprint_matches_file_sha256(self):
        sample = write_sample([row(i) for i in range(1, 5)])
        out = _tmp(".json")
        M.cmd_check_independence(indep_args(sample, out=out),
                                 get=make_get(fwd=lambda q: fwd_at(PASS_D_M)))
        rep = read_json(out)
        self.assertEqual(rep["sample_fingerprint"],
                         hashlib.sha256(read_bytes(sample)).hexdigest())


# ================================================================== §4.8
class TestLoadGroundSample(QuietCase):
    """CRS 선언 + 범위 관측 2중 검사 (R1)."""

    def test_missing_crs_column_exits(self):
        p = write_sample([(1, "서울 중구 1", BASE_LAT, BASE_LON, "L4", "국가기준점")],
                         header=["no", "jibun", "lat", "lon", "grade", "source"])
        with self.assertRaises(SystemExit):
            M.load_ground_sample(p)

    def test_wrong_crs_exits(self):
        p = write_sample([row(crs="EPSG:5186")])
        with self.assertRaises(SystemExit):
            M.load_ground_sample(p)

    def test_lowercase_crs_passes(self):
        p = write_sample([row(crs=" epsg:4326 ")])
        rows = M.load_ground_sample(p)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["crs"], M.GROUND_CRS, "보고서에는 정규화된 정본을 싣는다")

    def test_tm_plane_values_exit(self):
        """선언은 EPSG:4326 인데 값은 TM 평면 — 선언을 믿지 않는다."""
        p = write_sample([row(lat=452000, lon=198000)])
        with self.assertRaises(SystemExit):
            M.load_ground_sample(p)

    def test_swapped_lat_lon_exits(self):
        p = write_sample([row(lat=127.05, lon=37.55)])
        with self.assertRaises(SystemExit):
            M.load_ground_sample(p)

    def test_lat_out_of_range_exits(self):
        with self.assertRaises(SystemExit):
            M.load_ground_sample(write_sample([row(lat=32.9)]))

    def test_lon_out_of_range_exits(self):
        with self.assertRaises(SystemExit):
            M.load_ground_sample(write_sample([row(lon=132.1)]))

    def test_bad_grade_exits(self):
        with self.assertRaises(SystemExit):
            M.load_ground_sample(write_sample([row(grade="L3")]))

    def test_extra_column_ignored(self):
        p = write_sample([(1, "서울 중구 1", BASE_LAT, BASE_LON, "L4", "국가기준점",
                           "EPSG:4326", "메모")],
                         header=["no", "jibun", "lat", "lon", "grade", "source", "crs", "note"])
        self.assertEqual(len(M.load_ground_sample(p)), 1)

    def test_one_bad_row_aborts_whole_sample(self):
        p = write_sample([row(1), row(2, lat=452000), row(3)])
        with self.assertRaises(SystemExit) as cm:
            M.load_ground_sample(p)
        self.assertIn("3", str(cm.exception), "오류 메시지에 CSV 행번호가 들어가야 한다")

    def test_report_records_crs(self):
        sample = write_sample([row(i) for i in range(1, 5)])
        out = _tmp(".json")
        M.cmd_check_independence(indep_args(sample, out=out),
                                 get=make_get(fwd=lambda q: fwd_at(PASS_D_M)))
        self.assertEqual(read_json(out)["crs"], "EPSG:4326")


# ================================================================== §4.9
class TestCliSurface(QuietCase):
    """기존 CLI 표면 보존 (§2.3.1)."""

    def test_measure_rejects_axis(self):
        r = run_cli("measure", "--xlsx", "x.xlsx", "--server", "http://127.0.0.1:8092",
                    "--axis", "roundtrip")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("--axis", r.stderr)

    def test_rescore_requires_axis(self):
        r = run_cli("rescore", "--dump", "d.json")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("--axis", r.stderr)

    def test_rescore_rejects_xlsx(self):
        r = run_cli("rescore", "--axis", "roundtrip", "--dump", "d.json", "--xlsx", "x.xlsx")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("--xlsx", r.stderr)

    def test_rescore_rejects_server(self):
        r = run_cli("rescore", "--axis", "roundtrip", "--dump", "d.json",
                    "--server", "http://127.0.0.1:8092")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("--server", r.stderr)

    def test_rescore_rejects_ground_axis(self):
        r = run_cli("rescore", "--axis", "ground", "--dump", "d.json")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("ground", r.stderr)

    def test_rescore_help_choices_unchanged(self):
        r = run_cli("rescore", "--help")
        self.assertIn("roundtrip", r.stdout)
        self.assertIn("reverse", r.stdout)
        self.assertNotIn("ground", r.stdout)


# ================================================================== §4.10
class TestServerGuardInheritance(QuietCase):
    """`--server` 무기본값 · PRODUCTION_HOSTS 를 G 축이 상속한다."""

    def test_check_independence_without_server_exits(self):
        s = write_sample([row()])
        r = run_cli("check-independence", "--sample", s, "--out", _tmp(".json"))
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("--server", r.stderr + r.stdout)

    def test_measure_ground_without_server_exits(self):
        s = write_sample([row()])
        r = run_cli("measure-ground", "--sample", s, "--independence-report", _tmp(".json"))
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("--server", r.stderr + r.stdout)

    def test_check_independence_rejects_production(self):
        s = write_sample([row()])
        r = run_cli("check-independence", "--sample", s, "--out", _tmp(".json"),
                    "--server", "http://" + M.PRODUCTION_HOSTS[0] + ":8092")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("--allow-production", r.stderr + r.stdout)

    def test_measure_ground_rejects_production(self):
        s = write_sample([row()])
        r = run_cli("measure-ground", "--sample", s, "--independence-report", _tmp(".json"),
                    "--server", "http://" + M.PRODUCTION_HOSTS[0] + ":8092")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("--allow-production", r.stderr + r.stdout)

    def test_allow_production_returns_value(self):
        """호출은 하지 않는다 — resolve_server 반환값만 본다."""
        url = "http://" + M.PRODUCTION_HOSTS[0] + ":8092"
        self.assertEqual(M.resolve_server(url, True), url)

    def test_production_hosts_constant_unchanged(self):
        self.assertEqual(M.PRODUCTION_HOSTS, ("192.168.102.245",))


# ================================================================== §4.11
class TestLegacyContract(QuietCase):
    """기존 계약 회귀 가드 (양보 불가 제약)."""

    def test_axes_still_contains_original_two(self):
        self.assertIn("roundtrip", M.AXES)
        self.assertIn("reverse", M.AXES)
        self.assertIn("ground", M.AXES)

    def test_rescore_axes_is_subset(self):
        self.assertTrue(set(M.RESCORE_AXES) <= set(M.AXES))
        self.assertEqual(M.RESCORE_AXES, ("roundtrip", "reverse"))

    def test_load_expected_signature_unchanged(self):
        import inspect
        self.assertEqual(list(inspect.signature(M.load_expected).parameters), ["xlsx"])

    def test_load_dump_still_rejects_reverse_axis(self):
        p = _tmp(".json")
        write_json(p, {"metrics": {"rt_ok": 1}, "rt_bad": []})
        self.assertEqual(M.load_dump(p, "roundtrip"), (1, []))
        with self.assertRaises(SystemExit):
            M.load_dump(p, "reverse")

    def test_cmd_measure_source_unchanged(self):
        """계약 불일치(항상 0/595)를 알면서도 고치지 않는다 — 문서에만 적는다."""
        import inspect
        src = inspect.getsource(M.cmd_measure)
        self.assertIn('fwd = get("/geocode", q=expected)', src)
        self.assertIn('lat, lon = fwd["lat"], fwd["lon"]', src)
        self.assertIn('.get("jibun", "")', src)

    def test_no_ours_env_var_in_this_harness(self):
        """검사 범위는 이 하네스 한 파일 (R7)."""
        self.assertNotIn("OURS", read_text(SCRIPT))


# ================================================================== §4.12
class TestHaversine(QuietCase):
    def test_same_point_is_zero(self):
        self.assertEqual(M.haversine_m(BASE_LAT, BASE_LON, BASE_LAT, BASE_LON), 0.0)

    def test_one_degree_latitude(self):
        d = M.haversine_m(37.0, 127.0, 38.0, 127.0)
        self.assertAlmostEqual(d, 111194.9, delta=111.2)

    def test_known_pair(self):
        # 적도에서 경도 1도 = 지구 평균반경 * 1도(rad)
        d = M.haversine_m(0.0, 0.0, 0.0, 1.0)
        self.assertAlmostEqual(d, M.EARTH_R_M * math.radians(1.0), delta=1.0)

    def test_sub_metre_resolution_matches_threshold(self):
        near = M.haversine_m(BASE_LAT, BASE_LON, BASE_LAT + 0.49 / M_PER_DEG_LAT, BASE_LON)
        far = M.haversine_m(BASE_LAT, BASE_LON, BASE_LAT + 0.51 / M_PER_DEG_LAT, BASE_LON)
        self.assertTrue(M.is_self_ref(near))
        self.assertFalse(M.is_self_ref(far))


# ================================================================== B-1
class TestDatumMisdeclaration(QuietCase):
    """데이텀 수준 오선언은 범위 검사를 통과한다 — d_i 분포로 닫힌 실패를 낸다."""

    def test_report_carries_distance_distribution(self):
        sample = write_sample([row(i) for i in range(1, 5)])
        out = _tmp(".json")
        M.cmd_check_independence(indep_args(sample, out=out),
                                 get=make_get(fwd=lambda q: fwd_at(PASS_D_M)))
        rep = read_json(out)
        for key in ("d_m_min", "d_m_median", "d_m_p90"):
            self.assertIn(key, rep)

    def test_datum_shifted_sample_is_inconclusive_not_pass(self):
        """구 측지계(동경측지계) 상당 평행이동: 위도 +0.0032° / 경도 −0.0028°."""
        sample = write_sample([row(i) for i in range(1, 5)])
        shifted = {"results": [{"lat": BASE_LAT + 0.0032, "lon": BASE_LON - 0.0028}]}
        out = _tmp(".json")
        with self.assertRaises(SystemExit) as cm:
            M.cmd_check_independence(indep_args(sample, out=out),
                                     get=make_get(fwd=shifted))
        self.assertEqual(cm.exception.code, 4)
        rep = read_json(out)
        self.assertEqual(rep["verdict"], "inconclusive")
        self.assertEqual(rep["hits"], 0)
        self.assertGreater(rep["d_m_min"], M.GROUND_SYSTEMATIC_M)

    def test_normal_spread_still_passes(self):
        """전건이 계통적으로 어긋난 것이 아니면 hits=0 은 정상 통과다."""
        self.assertEqual(M.judge_independence(0, 400, 400, 12.0), "pass")

    def test_systematic_threshold_constant(self):
        self.assertEqual(M.GROUND_SYSTEMATIC_M, 50.0)


# ================================================================== B-7
class TestValidationSurvivesOptimizedMode(QuietCase):
    """`python3 -O` 는 assert 를 통째로 지운다 — 별 프로세스로 확인한다."""

    def test_value_errors_survive_dash_O(self):
        probe = _tmp(".py")
        with open(probe, "w", encoding="utf-8") as fh:
            fh.write(
                "import importlib.util, sys\n"
                f"spec = importlib.util.spec_from_file_location('u', {SCRIPT!r})\n"
                "m = importlib.util.module_from_spec(spec)\n"
                "spec.loader.exec_module(m)\n"
                "assert False, 'this assert must be stripped by -O'\n"
                "ok = 0\n"
                "try:\n"
                "    m.AxisScore('G', 'ground', 1, 2)\n"
                "except ValueError:\n"
                "    ok += 1\n"
                "try:\n"
                "    m.render_report([], [m.AxisScore('S', 'reverse', 1, 2)])\n"
                "except ValueError:\n"
                "    ok += 1\n"
                "print('OK' if ok == 2 else f'NG {ok}')\n")
        r = subprocess.run([sys.executable, "-O", probe],
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("OK", r.stdout, "-O 에서 검증이 사라졌다 — assert 를 썼다는 뜻이다")


if __name__ == "__main__":
    unittest.main(verbosity=2)
