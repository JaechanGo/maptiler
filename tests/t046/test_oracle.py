#!/usr/bin/env python3
"""T046 §7 — 존재 오라클(§4.3)·본번 근사·심판 조회. PNU 조립과 SQL 형태를 고정한다.

두 층으로 나눠 검정한다.
  · **진리표**는 가짜 runner 를 주입해 결정적으로 확인한다(DB 무관).
  · **SQL 형태**는 문자열로 검사한다 — `substr(pnu,1,8)` 로 파티션 키를 가리면
    1,000 키 조회가 69,430 ms → 263.9 ms 의 **263 배** 차이로 무너진다(§1.12).
  · **실 DB 스모크**는 PostGIS 를 실제로 부른다. 2 차 검토 조건 1(시도코드 12)의
    회귀는 실제 데이터가 없다는 사실 자체가 쟁점이라 실 DB 로만 검정할 수 있다.

실행: /usr/bin/python3 -m unittest discover -s tests/t046 -t . -v
"""
import re
import time
import unittest

import _ctx  # noqa: F401  (sys.path 주입)

import oracle  # noqa: E402
from oracle import (  # noqa: E402
    Oracle,
    build_pnu,
    pnu_from_bm25,
    resolve_pnu,
    sido_relax_candidates,
    split_pnu,
)
from pgprobe import run_sql  # noqa: E402


class FakeRunner:
    """SQL 안의 `/* t046:<태그> */` 주석을 보고 미리 정한 행을 돌려준다."""

    def __init__(self, table):
        self.table = table          # {태그: [[셀, …], …]}
        self.calls = []             # 실행된 SQL 전문

    def __call__(self, sql, **kw):
        self.calls.append(sql)
        m = re.search(r"/\* t046:([a-z_0-9]+) \*/", sql)
        if not m:
            raise AssertionError("SQL 에 t046 태그 주석이 없다:\n%s" % sql)
        return self.table.get(m.group(1), [])


class TestBuildPnu(unittest.TestCase):
    """§4.3-a — PNU 19 자리 = 법정동코드(10) + 대지구분(1) + 본번(4) + 부번(4)."""

    def test_length_and_layout(self):
        pnu = build_pnu("4481025026", 0, 200, 1)
        self.assertEqual(len(pnu), 19)
        self.assertEqual(pnu[:10], "4481025026")
        self.assertEqual(pnu[10], "1")
        self.assertEqual(pnu[11:15], "0200")
        self.assertEqual(pnu[15:19], "0001")

    def test_san_flag_mapping(self):
        """원천 `[05]` 산 여부 0→'1'(일반), 1→'2'(산). 뒤집으면 전 건이 어긋난다."""
        self.assertEqual(build_pnu("4481025026", 0, 200, 1)[10], "1")
        self.assertEqual(build_pnu("4481025026", 1, 200, 1)[10], "2")
        self.assertEqual(build_pnu("4481025026", True, 102, 0)[10], "2")
        self.assertEqual(build_pnu("4481025026", False, 102, 0)[10], "1")

    def test_zero_padding(self):
        pnu = build_pnu("4481025026", 0, 7, 0)
        self.assertEqual(pnu[11:15], "0007")
        self.assertEqual(pnu[15:19], "0000")

    def test_rejects_bad_bcode(self):
        for bad in ("448102502", "44810250267", "", None):
            with self.assertRaises(ValueError, msg=repr(bad)):
                build_pnu(bad, 0, 200, 1)

    def test_rejects_overflow(self):
        """본번·부번은 4 자리를 넘을 수 없다 — 잘라내면 조용히 오답이 된다."""
        with self.assertRaises(ValueError):
            build_pnu("4481025026", 0, 12345, 0)


class TestPnuFromBm25(unittest.TestCase):
    """§4.3-a — 건물관리번호 25 자리의 앞 19 자리가 곧 PNU 다."""

    def test_slices_first_19(self):
        bm25 = "4481025026" + "1" + "0200" + "0001" + "100001"
        self.assertEqual(len(bm25), 25)
        self.assertEqual(pnu_from_bm25(bm25), bm25[:19])

    def test_rejects_wrong_length(self):
        for bad in ("", None, "44810250261020000011"):
            with self.assertRaises(ValueError, msg=repr(bad)):
                pnu_from_bm25(bad)

    def test_two_paths_agree_on_wellformed_record(self):
        """조립본과 BM25 절단본이 일치하는 것이 정상이다."""
        bm25 = "4481025026" + "1" + "0200" + "0001" + "100001"
        self.assertEqual(build_pnu("4481025026", 0, 200, 1), pnu_from_bm25(bm25))


class TestResolvePnu(unittest.TestCase):
    """2 차 검토 조건 5(Minor) — 이중 경로 불일치 시 `BM25[:19]` 를 채택하고 계수한다."""

    def test_agreement_reports_no_mismatch(self):
        bm25 = "4481025026" + "1" + "0200" + "0001" + "100001"
        pnu, mismatch = resolve_pnu("4481025026", 0, 200, 1, bm25)
        self.assertEqual(pnu, bm25[:19])
        self.assertFalse(mismatch)

    def test_disagreement_prefers_bm25(self):
        """건물관리번호가 우선이다 — 부번이 다른 경우를 만들어 확인한다."""
        bm25 = "4481025026" + "1" + "0200" + "0009" + "100001"
        pnu, mismatch = resolve_pnu("4481025026", 0, 200, 1, bm25)
        self.assertEqual(pnu, bm25[:19])
        self.assertTrue(mismatch)

    def test_missing_bm25_falls_back_to_assembly(self):
        pnu, mismatch = resolve_pnu("4481025026", 0, 200, 1, None)
        self.assertEqual(pnu, build_pnu("4481025026", 0, 200, 1))
        self.assertFalse(mismatch)


class TestSplitPnu(unittest.TestCase):
    """§1.12 — 파이썬이 미리 쪼갠다. SQL 에서 자르면 파티션 pruning 이 죽는다."""

    def test_fields(self):
        pnu = build_pnu("4481025026", 1, 102, 3)
        p = split_pnu(pnu)
        self.assertEqual(p["sido_cd"], "44")
        self.assertEqual(p["emd_cd"], "44810250")
        self.assertEqual(p["san"], 1)
        self.assertEqual(p["ji_main"], 102)
        self.assertEqual(p["ji_sub"], 3)

    def test_san_is_smallint_zero_or_one(self):
        """`parcel.san` 은 0/1 이다. PNU 의 '1'/'2' 를 그대로 넣으면 안 된다."""
        self.assertEqual(split_pnu(build_pnu("4481025026", 0, 1, 0))["san"], 0)
        self.assertEqual(split_pnu(build_pnu("4481025026", 1, 1, 0))["san"], 1)

    def test_emd_cd_is_eight_digits(self):
        """`parcel.emd_cd` 는 char(8) 이다 — 10 자리를 넣으면 전건 미적중이다."""
        self.assertEqual(len(split_pnu(build_pnu("4481025026", 0, 1, 0))["emd_cd"]), 8)


class TestSidoRelax(unittest.TestCase):
    """2 차 검토 조건 1(Critical) — 접두 12 를 46 → 29 순으로 재조회한다."""

    def test_twelve_expands_to_46_then_29(self):
        pnu = build_pnu("1211012345", 0, 200, 1)
        cands = sido_relax_candidates(pnu)
        self.assertEqual(len(cands), 2)
        self.assertEqual(cands[0][:2], "46")
        self.assertEqual(cands[1][:2], "29")
        for c in cands:
            self.assertEqual(c[2:], pnu[2:])   # 나머지 17 자리는 그대로

    def test_other_sido_has_no_candidates(self):
        """대조군 — 전북 52 는 정규화 대상이 아니다."""
        pnu = build_pnu("5211012345", 0, 200, 1)
        self.assertEqual(sido_relax_candidates(pnu), [])


class TestSqlShape(unittest.TestCase):
    """§1.12 — SQL 형태가 파티션 pruning 을 살려 두는지 문자열로 검사한다."""

    FORBIDDEN = re.compile(r"substr\s*\(\s*(k\.)?pnu", re.I)

    def _assert_no_substr_on_pnu(self, sql):
        self.assertIsNone(
            self.FORBIDDEN.search(sql),
            msg="파티션 키를 표현식으로 가렸다(263 배 회귀):\n%s" % sql,
        )

    def test_apx_binds_parsed_columns(self):
        """본번 근사는 사전 파싱한 4 개 컬럼을 각각 대조한다(부번 무관)."""
        sql = oracle.sql_apx_batch(1)
        self._assert_no_substr_on_pnu(sql)
        for col in ("sido_cd", "emd_cd", "san", "ji_main"):
            self.assertIn(col, sql, msg=col)
        self.assertNotIn("ji_sub", sql)      # 근사는 부번을 보지 않는다

    def test_parcel_lookup_binds_parsed_columns(self):
        sql = oracle.sql_parcel_batch(1)
        self._assert_no_substr_on_pnu(sql)
        self.assertIn("sido_cd", sql)

    def test_referee_uses_point_on_surface_not_geom_pt(self):
        """`parcel.geom_pt` 는 전량 NULL 이다(§1.8) — 대표점은 ST_PointOnSurface."""
        sql = oracle.sql_referee_parcel_batch(1)
        self.assertIn("ST_Contains", sql)
        self.assertNotIn("geom_pt", sql)

    def test_address_lookup_uses_synth_pnu_index_expression(self):
        """`address` 에는 emd_cd·ji_main 이 없다(§1.8).

        전용 색인 `(bcode || substr(bd_mgt_sn,11,9)) WHERE kind='addr'` 의
        **정확히 같은 표현식**을 써야 색인을 탄다. 여기서의 substr 은 파티션 키가
        아니라 색인 정의의 일부이므로 금지 대상이 아니다.
        """
        sql = oracle.sql_addr_batch(1)
        self.assertIn("bd_mgt_sn", sql)
        self.assertIn("kind", sql)
        self.assertIn("'addr'", sql)

    def test_all_sql_carry_tag_comment(self):
        """진단 로그에서 어느 조회인지 식별할 수 있어야 한다."""
        for fn in (
            oracle.sql_addr_batch,
            oracle.sql_parcel_batch,
            oracle.sql_apx_batch,
            oracle.sql_referee_parcel_batch,
            oracle.sql_road_bm25_batch,
        ):
            self.assertRegex(fn(1), r"/\* t046:[a-z_0-9]+ \*/", msg=fn.__name__)


class TestJibunTruthTable(unittest.TestCase):
    """§4.3-b — 지번 3 분기. 가짜 runner 로 결정적으로 검정한다."""

    PNU = "44810250261" + "0200" + "0001"

    def _oracle(self, addr_rows, parcel_rows):
        runner = FakeRunner({"addr": addr_rows, "parcel": parcel_rows})
        return Oracle(runner=runner), runner

    def test_branch_A(self):
        """address 에 kind='addr' 로 존재 → O=A."""
        orc, _ = self._oracle([["0"]], [])
        self.assertEqual(orc.jibun_batch({0: self.PNU}), {0: "A"})

    def test_branch_P(self):
        """address 미적중, parcel 적중 → O=P."""
        orc, _ = self._oracle([], [["0"]])
        self.assertEqual(orc.jibun_batch({0: self.PNU}), {0: "P"})

    def test_branch_N(self):
        """둘 다 미적중 → O=N."""
        orc, _ = self._oracle([], [])
        self.assertEqual(orc.jibun_batch({0: self.PNU}), {0: "N"})

    def test_A_takes_precedence_over_P(self):
        """양쪽 적중이면 A 다 — 분기는 배타적이어야 한다."""
        orc, _ = self._oracle([["0"]], [["0"]])
        self.assertEqual(orc.jibun_batch({0: self.PNU}), {0: "A"})

    def test_batches_do_not_lose_keys(self):
        """미적중 키도 결과에 N 으로 남는다 — 조용히 사라지면 분모가 틀어진다."""
        keys = {i: self.PNU for i in range(5)}
        orc, _ = self._oracle([["2"]], [["4"]])
        out = orc.jibun_batch(keys)
        self.assertEqual(len(out), 5)
        self.assertEqual(out[2], "A")
        self.assertEqual(out[4], "P")
        self.assertEqual(out[0], "N")


class TestRoadTruthTable(unittest.TestCase):
    """§4.3-c — 도로명 4 분기(A25 / A19 / P / N)."""

    PNU = "44810250261" + "0200" + "0001"
    BM25 = PNU + "100001"

    def _oracle(self, bm25_rows, addr_rows, parcel_rows):
        runner = FakeRunner(
            {"road_bm25": bm25_rows, "addr": addr_rows, "parcel": parcel_rows}
        )
        return Oracle(runner=runner)

    def test_branch_A25(self):
        """건물관리번호 25 자리 완전 일치."""
        orc = self._oracle([["0"]], [["0"]], [["0"]])
        self.assertEqual(orc.road_batch({0: (self.BM25, self.PNU)}), {0: "A25"})

    def test_branch_A19(self):
        """같은 PNU 의 다른 건물은 존재한다."""
        orc = self._oracle([], [["0"]], [["0"]])
        self.assertEqual(orc.road_batch({0: (self.BM25, self.PNU)}), {0: "A19"})

    def test_branch_P(self):
        orc = self._oracle([], [], [["0"]])
        self.assertEqual(orc.road_batch({0: (self.BM25, self.PNU)}), {0: "P"})

    def test_branch_N(self):
        orc = self._oracle([], [], [])
        self.assertEqual(orc.road_batch({0: (self.BM25, self.PNU)}), {0: "N"})


class TestApxAndReferee(unittest.TestCase):
    """`O_apx`(본번 근사)와 심판(§4.3-d)."""

    PNU = "44810250261" + "0200" + "0001"

    def test_apx_true_false(self):
        orc = Oracle(runner=FakeRunner({"apx": [["0"]]}))
        self.assertEqual(orc.apx_batch({0: self.PNU, 1: self.PNU}), {0: True, 1: False})

    def test_referee_absent_is_none_not_false(self):
        """심판 자료 부재는 False 가 아니라 None 이다 — 분류 11 의 근거다.

        None 을 False 로 뭉개면 분류 7/10 으로 잘못 흘러간다(조건 4).
        """
        orc = Oracle(runner=FakeRunner({"referee": []}))
        out = orc.referee_parcel_batch({0: (self.PNU, 127.0, 37.0)})
        self.assertIsNone(out[0])

    def test_referee_true_false(self):
        orc = Oracle(
            runner=FakeRunner({"referee": [["0", "t"], ["1", "f"]]})
        )
        out = orc.referee_parcel_batch(
            {0: (self.PNU, 127.0, 37.0), 1: (self.PNU, 127.0, 37.0)}
        )
        self.assertIs(out[0], True)
        self.assertIs(out[1], False)


class TestSido12AgainstLiveDb(unittest.TestCase):
    """2 차 검토 조건 1 회귀 — 실 PostGIS. 원천 값을 하드코딩하지 않고 DB 에서 뽑는다."""

    @classmethod
    def setUpClass(cls):
        cls.orc = Oracle()
        cls.jeonnam = cls._sample_pnu("46")
        cls.jeonbuk = cls._sample_pnu("52")

    @staticmethod
    def _sample_pnu(sido_cd):
        rows = run_sql(
            "SELECT pnu FROM parcel WHERE sido_cd='%s' LIMIT 1" % sido_cd
        )
        return rows[0][0] if rows else None

    def test_prefix_12_hits_after_relaxation(self):
        """접두를 12 로 바꾼 PNU 는 엄격 조회로는 못 맞히고, 완화하면 맞힌다."""
        self.assertIsNotNone(self.jeonnam, "전남(46) 표본을 뽑지 못했다")
        fake12 = "12" + self.jeonnam[2:]

        strict = self.orc.jibun_batch({0: fake12}, relax12=False)
        self.assertEqual(strict[0], "N")

        relaxed = self.orc.jibun_batch({0: fake12}, relax12=True)
        self.assertIn(relaxed[0], ("A", "P"))
        self.assertEqual(self.orc.relax12_hits, 1)

    def test_control_group_jeonbuk_hits_without_relaxation(self):
        """대조군 — 전북 52 는 완화 없이 적중하고 완화 카운터를 올리지 않는다."""
        self.assertIsNotNone(self.jeonbuk, "전북(52) 표본을 뽑지 못했다")
        orc = Oracle()
        out = orc.jibun_batch({0: self.jeonbuk}, relax12=True)
        self.assertIn(out[0], ("A", "P"))
        self.assertEqual(orc.relax12_hits, 0)

    def test_relaxation_never_fires_for_non_12_prefix(self):
        orc = Oracle()
        orc.jibun_batch({0: self.jeonbuk}, relax12=True)
        self.assertEqual(orc.relax12_attempts, 0)


class TestBatchPerformance(unittest.TestCase):
    """§1.12 실측 회귀 — 1,000 키 semi-join. 263 배 회귀를 잡는 느슨한 상한."""

    def test_thousand_key_batch_is_fast(self):
        rows = run_sql("SELECT pnu FROM parcel WHERE sido_cd='44' LIMIT 1000")
        pnus = {i: r[0] for i, r in enumerate(rows)}
        self.assertEqual(len(pnus), 1000, "표본 1,000 키를 확보하지 못했다")

        orc = Oracle()
        t0 = time.time()
        out = orc.jibun_batch(pnus)
        elapsed = time.time() - t0

        self.assertEqual(len(out), 1000)
        self.assertLess(
            elapsed, 10.0, "1,000 키 배치가 %.1f s — 파티션 pruning 을 확인하라" % elapsed
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
