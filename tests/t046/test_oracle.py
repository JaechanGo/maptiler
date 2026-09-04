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


class LegacyRunner:
    """구 시도코드는 없고 **현행 코드만 적재된** DB 를 흉내낸다.

    실측(§조건 1-b)이 그렇다 — `parcel_42`·`parcel_45` 파티션은 존재하지 않고
    `parcel_51`·`parcel_52` 만 있다. 그래서 `hit_prefix` 로 시작하는 키를 SQL 이
    실제로 물어봤을 때에만 행을 돌려준다. 무보정 조회는 반드시 빈손이어야 한다.
    """

    def __init__(self, hit_prefixes=("51", "52"), tags=("addr", "parcel", "apx"),
                 referee_value="t"):
        self.hit_prefixes = tuple(hit_prefixes)
        self.tags = tuple(tags)
        self.referee_value = referee_value
        self.calls = []

    def __call__(self, sql, **kw):
        self.calls.append(sql)
        m = re.search(r"/\* t046:([a-z_0-9]+) \*/", sql)
        if not m:
            raise AssertionError("SQL 에 t046 태그 주석이 없다:\n%s" % sql)
        tag = m.group(1)
        if tag not in self.tags and tag != "referee":
            return []
        # 어느 키가 현행 코드로 조회됐는지 SQL 리터럴에서 읽는다.
        hit = []
        for key, sido in re.findall(r"\('(\d+)'(?:::text)?,'(\d{2})'", sql):
            if sido in self.hit_prefixes:
                hit.append(key)
        for key, pnu in re.findall(r"\('(\d+)'(?:::text)?,'(\d{19})'", sql):
            if pnu[:2] in self.hit_prefixes:
                hit.append(key)
        if tag == "referee":
            return [[k, self.referee_value] for k in hit]
        return [[k] for k in hit]


class TestSidoLegacyCorrection(unittest.TestCase):
    """조건 1-b — 구 시도코드(42 강원 · 45 전북) → 현행(51 · 52) 보정.

    `relax12`(12 → 46·29)와 **방향이 반대**이고 별도 계수기여야 한다.
    합산해 버리면 §7.6 이 보고한 1,560/1,546 의 의미가 무너진다.
    """

    GANGWON_OLD = "42" + "11010100" + "1" + "0200" + "0001"   # 42… 구 강원
    JEONBUK_OLD = "45" + "11010100" + "1" + "0200" + "0001"   # 45… 구 전북
    CONTROL = "44" + "81025026" + "1" + "0200" + "0001"       # 44 충남 — 보정 대상 아님

    def test_gangwon_42_is_corrected_to_51(self):
        orc = Oracle(runner=LegacyRunner())
        out = orc.jibun_batch({0: self.GANGWON_OLD}, relax12=False, legacy=True)
        self.assertEqual(out[0], "A", "42→51 보정이 적중하지 않았다")
        fixed = "51" + self.GANGWON_OLD[2:]
        self.assertTrue(
            any(fixed in s for s in orc._run.calls),
            "보정된 시도코드 51 로 재조회한 흔적이 없다",
        )

    def test_jeonbuk_45_is_corrected_to_52(self):
        orc = Oracle(runner=LegacyRunner())
        out = orc.jibun_batch({0: self.JEONBUK_OLD}, relax12=False, legacy=True)
        self.assertEqual(out[0], "A", "45→52 보정이 적중하지 않았다")

    def test_control_group_hits_without_correction(self):
        """대조군 — 보정 없이 적중해야 한다. 보정이 켜져도 계수기는 움직이지 않는다."""
        orc = Oracle(runner=LegacyRunner(hit_prefixes=("44",)))
        out = orc.jibun_batch({0: self.CONTROL}, relax12=False, legacy=True)
        self.assertEqual(out[0], "A")
        self.assertEqual(orc.legacy_attempts, 0, "대조군에 보정을 시도했다")
        self.assertEqual(orc.legacy_hits, 0)

    def test_legacy_counters_are_separate_from_relax12(self):
        orc = Oracle(runner=LegacyRunner())
        orc.jibun_batch({0: self.GANGWON_OLD}, relax12=True, legacy=True)
        self.assertEqual(orc.legacy_attempts, 1)
        self.assertEqual(orc.legacy_hits, 1)
        self.assertIn(0, orc.legacy_keys)
        self.assertEqual(orc.relax12_attempts, 0, "relax12 계수기가 오염됐다")
        self.assertEqual(orc.relax12_hits, 0)
        self.assertEqual(orc.relax12_keys, set())

    def test_road_legacy_substitutes_bm25_prefix(self):
        """BM25 25 자리의 앞 19 자리가 PNU 이므로 접두 2 자리 치환이 성립한다."""
        bm25 = self.GANGWON_OLD + "300001"             # 19 + 6 = 25 자리
        self.assertEqual(len(bm25), 25)
        orc = Oracle(runner=LegacyRunner(tags=("road_bm25", "addr", "parcel")))
        out = orc.road_batch({0: (bm25, self.GANGWON_OLD)}, legacy=True)
        self.assertEqual(out[0], "A25", "보정된 BM25 로 A25 를 회수하지 못했다")
        corrected = "51" + bm25[2:]
        self.assertTrue(
            any(corrected in s for s in orc._run.calls),
            "치환된 25 자리 BM25 로 조회한 흔적이 없다",
        )

    # -- F2: 이번에 확장한 심판 · O_apx 축 ---------------------------
    def test_apx_applies_legacy_correction(self):
        orc = Oracle(runner=LegacyRunner())
        out = orc.apx_batch({0: self.GANGWON_OLD})
        self.assertIs(out[0], True, "O_apx 축에 구 시도코드 보정이 없다")
        self.assertEqual(orc.legacy_apx_attempts, 1)
        self.assertEqual(orc.legacy_apx_hits, 1)

    def test_apx_control_group_needs_no_correction(self):
        orc = Oracle(runner=LegacyRunner(hit_prefixes=("44",)))
        out = orc.apx_batch({0: self.CONTROL})
        self.assertIs(out[0], True)
        self.assertEqual(orc.legacy_apx_attempts, 0)

    def test_referee_applies_legacy_correction(self):
        """보정 전에는 자료 부재(None → 분류 11)로 잘못 흘러갔다."""
        orc = Oracle(runner=LegacyRunner())
        out = orc.referee_parcel_batch({0: (self.GANGWON_OLD, 127.0, 37.0)})
        self.assertIs(out[0], True, "심판 축에 구 시도코드 보정이 없다")
        self.assertEqual(orc.legacy_referee_attempts, 1)
        self.assertEqual(orc.legacy_referee_hits, 1)

    def test_referee_legacy_preserves_false_verdict(self):
        """보정으로 자료를 찾았는데 점이 밖이면 False 다. True 로 부풀리지 마라."""
        orc = Oracle(runner=LegacyRunner(referee_value="f"))
        out = orc.referee_parcel_batch({0: (self.JEONBUK_OLD, 127.0, 37.0)})
        self.assertIs(out[0], False)
        self.assertEqual(orc.legacy_referee_hits, 1, "자료를 찾은 것은 적중이다")

    def test_referee_counters_separate_from_jibun_legacy(self):
        orc = Oracle(runner=LegacyRunner())
        orc.referee_parcel_batch({0: (self.GANGWON_OLD, 127.0, 37.0)})
        orc.apx_batch({1: self.GANGWON_OLD})
        self.assertEqual(orc.legacy_attempts, 0, "지번 축 계수기가 오염됐다")
        self.assertEqual(orc.legacy_hits, 0)


class ParcelDBRunner:
    """리(법정동코드 9·10 번째 자리)까지 갖춘 가짜 `parcel`.

    `parcel_sido_cd_pnu_key` 가 `(sido_cd, pnu)` **UNIQUE** 이므로 실 DB 에서
    PNU 는 한 행이다. 따라서 `(sido_cd, emd_cd8, san, ji_main, ji_sub)` 조인이
    두 행 이상을 무는 경우, 남은 자유 자리는 **리 2 자리뿐이므로** 그 행들은
    반드시 서로 다른 법정동이다. 이 가짜는 그 상황을 재현한다.

    답은 **SQL 이 실제로 건네준 가장 구체적인 식별자**로 정한다:

        19 자리 리터럴 → 정확 PNU 조인   (옳다)
        10 자리 리터럴 → 법정동코드 조인 (부번을 무시하는 `O_apx` 용으로 옳다)
        그 외(8 자리)  → 절단 조인       (이웃 리를 함께 문다)

    즉 SQL 이 리를 버리면 이 가짜도 리를 구별하지 못한다. 그것이 검정 대상이다.
    """

    def __init__(self, parcels, tags=("parcel", "apx", "referee")):
        # `parcels` = {pnu19: 점이 그 필지 안에 있는가}
        self.parcels = dict(parcels)
        self.tags = tuple(tags)
        self.calls = []

    @staticmethod
    def _rows(sql):
        """`VALUES` 절의 행별 문자열 리터럴 목록.

        `%s::char(2)` 같은 타입 캐스트에도 괄호가 들어가므로 정규식으로 행을
        가르면 안 된다. 깊이를 세서 **최상위 괄호쌍**만 행으로 본다.
        """
        head = sql.split("(VALUES ", 1)[1].split("\n", 1)[0]
        rows, depth, start = [], 0, None
        for n, ch in enumerate(head):
            if ch == "(":
                if depth == 0:
                    start = n + 1
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0:
                    rows.append(head[start:n])
                elif depth < 0:              # VALUES 절이 닫혔다
                    break
        return [re.findall(r"'([^']*)'", row) for row in rows]

    def _match(self, lits):
        """행 리터럴 → 걸리는 PNU 집합. `lits[0]` 은 키라 건너뛴다."""
        for width, sel in ((19, lambda p, v: p == v),
                           (10, lambda p, v: p[:10] == v),
                           (8, lambda p, v: p[:8] == v)):
            for v in lits[1:]:
                if re.fullmatch(r"\d{%d}" % width, v):
                    return {p for p in self.parcels if sel(p, v)}
        return set()

    def __call__(self, sql, **kw):
        self.calls.append(sql)
        m = re.search(r"/\* t046:([a-z_0-9]+) \*/", sql)
        if not m:
            raise AssertionError("SQL 에 t046 태그 주석이 없다:\n%s" % sql)
        if m.group(1) not in self.tags:
            return []
        out = []
        for lits in self._rows(sql):
            hit = self._match(lits)
            if not hit:
                continue
            if m.group(1) == "referee":
                out.append([lits[0], "t" if any(self.parcels[p] for p in hit)
                            else "f"])
            else:
                out.append([lits[0]])
        return out


class TestRiDiscrimination(unittest.TestCase):
    """리 절단 — `split_pnu` 의 `emd_cd = pnu[:8]` 이 법정동코드 10 자리에서
    **리 2 자리를 버린다.** `parcel.emd_cd` 도 char(8) 이라 조인이 이웃 리의
    동일번지 필지를 함께 문다.

    실측(본 표본 지번 D0 = 5,946 중 절단 키로 필지를 찾은 5,159 건 기준):

        심판 축 모호(행 2 개 이상)          1,623 (31.5%)
        그중 심판이 **실제 판정한** 건      139 / 144 (96.5%)
        `O_apx` 축 모호                     4,837 (93.8%)
        정확 PNU 는 부재인데 판정된 건         56

    `bool_or(ST_Contains(...))` 는 "같은 읍면동 안 **아무** 동일번지 필지"에
    점이 들어가도 `True` 를 낸다. 그것은 심판이 답해야 할 질문이 아니다.
    """

    #        법정동코드 10   산 본번  부번
    NEIGHBOUR = "5173033021" "1" "0182" "0000"   # 이웃 리 — 점은 **여기** 있다
    QUERIED = "5173033028" "1" "0182" "0000"     # 질의 지번 — 점은 여기 없다
    SAME_RI_OTHER_SUB = "5173033028" "1" "0182" "0003"   # 같은 리, 부번만 다름

    def setUp(self):
        for pnu in (self.NEIGHBOUR, self.QUERIED, self.SAME_RI_OTHER_SUB):
            self.assertEqual(len(pnu), 19, "고정값 PNU 가 19 자리가 아니다")
        self.assertEqual(self.NEIGHBOUR[:8], self.QUERIED[:8],
                         "두 필지가 같은 8 자리 읍면동이어야 검정이 성립한다")
        self.assertNotEqual(self.NEIGHBOUR[:10], self.QUERIED[:10],
                            "두 필지의 리가 달라야 한다")

    # -- 심판 축 ------------------------------------------------------
    def test_referee_does_not_borrow_neighbouring_ri(self):
        """점이 **이웃 리**의 동일번지 필지 안에 있을 때 `True` 를 주면 안 된다."""
        orc = Oracle(runner=ParcelDBRunner({self.NEIGHBOUR: True,
                                            self.QUERIED: False}))
        out = orc.referee_parcel_batch({0: (self.QUERIED, 127.0, 37.0)},
                                       legacy=False)
        self.assertIs(out[0], False,
                      "이웃 리 필지의 포함 여부를 질의 지번의 판정으로 썼다")

    def test_referee_absent_when_exact_parcel_missing(self):
        """질의 PNU 가 없으면 **자료 부재(None)** 다 — 이웃 리로 대신 답하지 마라."""
        orc = Oracle(runner=ParcelDBRunner({self.NEIGHBOUR: True}))
        out = orc.referee_parcel_batch({0: (self.QUERIED, 127.0, 37.0)},
                                       legacy=False)
        self.assertIsNone(out[0],
                          "정확 PNU 가 부재인데 이웃 리로 판정을 만들어 냈다")

    def test_referee_still_answers_for_the_exact_parcel(self):
        """회귀 방지 — 정확 PNU 가 있으면 그 필지의 판정을 그대로 낸다."""
        orc = Oracle(runner=ParcelDBRunner({self.NEIGHBOUR: False,
                                            self.QUERIED: True}))
        out = orc.referee_parcel_batch({0: (self.QUERIED, 127.0, 37.0)},
                                       legacy=False)
        self.assertIs(out[0], True)

    # -- 존재(O=P) 축 -------------------------------------------------
    def test_parcel_existence_requires_same_bcode(self):
        """`O=P` 는 **질의 법정동**의 필지여야 한다. 이웃 리는 존재 근거가 아니다."""
        orc = Oracle(runner=ParcelDBRunner({self.NEIGHBOUR: True}))
        out = orc.jibun_batch({0: self.QUERIED}, relax12=False, legacy=False)
        self.assertEqual(out[0], "N",
                         "이웃 리의 동일번지를 질의 지번의 존재로 셌다")

    def test_parcel_existence_hits_on_exact_pnu(self):
        """회귀 방지 — 정확 PNU 가 있으면 `P` 다."""
        orc = Oracle(runner=ParcelDBRunner({self.QUERIED: True}))
        out = orc.jibun_batch({0: self.QUERIED}, relax12=False, legacy=False)
        self.assertEqual(out[0], "P")

    # -- 근사(O_apx) 축 -----------------------------------------------
    def test_apx_requires_same_bcode(self):
        """`O_apx` 도 법정동 안에서 근사해야 한다 — 무시하는 것은 부번뿐이다."""
        orc = Oracle(runner=ParcelDBRunner({self.NEIGHBOUR: True}))
        out = orc.apx_batch({0: self.QUERIED}, legacy=False)
        self.assertIs(out[0], False,
                      "이웃 리의 같은 본번을 근사 적중으로 셌다")

    def test_apx_still_ignores_ji_sub_within_same_bcode(self):
        """회귀 방지 — 같은 리면 부번이 달라도 근사 적중이다.

        이게 깨지면 `O_apx` 가 `O=P` 와 같아져 근사의 존재 이유가 사라진다.
        """
        orc = Oracle(runner=ParcelDBRunner({self.SAME_RI_OTHER_SUB: True}))
        out = orc.apx_batch({0: self.QUERIED}, legacy=False)
        self.assertIs(out[0], True, "같은 리의 다른 부번을 근사에서 놓쳤다")

    # -- SQL 형태 -----------------------------------------------------
    def test_apx_sql_still_prunes_by_partition_key(self):
        """리를 되살리면서 **파티션 pruning 을 잃으면 안 된다**(§1.12 263 배).

        `sido_cd` 는 리터럴로 묶여 있어야 하고, 색인
        `parcel_jibun_lookup (emd_cd, ji_main, ji_sub)` 를 타도록 `emd_cd`·
        `ji_main` 도 남아 있어야 한다. 리는 **추가 필터**로 건다.
        """
        sql = oracle.sql_apx_batch(1)
        self.assertIn("p.sido_cd = k.sido_cd", sql)
        self.assertIn("p.emd_cd = k.emd_cd", sql)
        self.assertIn("p.ji_main = k.ji_main", sql)
        self.assertIn("substr(p.pnu, 1, 10) = k.bcode", sql)

    def test_referee_and_parcel_sql_bind_exact_pnu(self):
        """심판·존재 SQL 이 정확 PNU 로 묶는지 형태로도 고정한다.

        `parcel_sido_cd_pnu_key (sido_cd, pnu)` 가 UNIQUE 라 이 조인은 절단
        조인보다 **더 옳으면서 더 싸다**.
        """
        for sql in (oracle.sql_referee_parcel_batch(1),
                    oracle.sql_parcel_batch(1)):
            self.assertIn("p.pnu = k.pnu", sql)
            self.assertIn("p.sido_cd = k.sido_cd", sql)

    def test_split_pnu_exposes_bcode_and_pnu(self):
        """`split_pnu` 가 **10 자리 법정동코드**와 원본 PNU 를 함께 내놓는가."""
        got = split_pnu(self.QUERIED)
        self.assertEqual(got["bcode"], self.QUERIED[:10])
        self.assertEqual(got["pnu"], self.QUERIED)
        self.assertEqual(got["emd_cd"], self.QUERIED[:8],
                         "기존 8 자리 키도 색인용으로 남아 있어야 한다")


class ReprPointRunner:
    """`ST_PointOnSurface(parcel.geom)` 조회를 흉내낸다.

    **psql 은 모든 열을 문자열로 돌려준다**(실측). 과거 이 사실을 놓쳐 `sid` 가
    int/str 로 어긋나면서 PIP 미적중이 '자료 부재'로 위장된 적이 있다. 그래서
    이 가짜도 좌표를 **문자열로** 돌려준다 — 형변환을 빠뜨리면 여기서 깨진다.
    """

    def __init__(self, points, hit_prefixes=("51", "52", "44", "26")):
        self.points = dict(points)                 # {pnu19: (lon, lat)}
        self.hit_prefixes = tuple(hit_prefixes)
        self.calls = []

    def __call__(self, sql, **kw):
        self.calls.append(sql)
        m = re.search(r"/\* t046:([a-z_0-9]+) \*/", sql)
        if not m:
            raise AssertionError("SQL 에 t046 태그 주석이 없다:\n%s" % sql)
        if m.group(1) != "reprpt":
            return []
        out = []
        for key, _sido, pnu in re.findall(
                r"\('([^']+)'(?:::text)?,'(\d{2})'(?:::char\(2\))?,"
                r"'(\d{19})'", sql):
            if pnu in self.points and pnu[:2] in self.hit_prefixes:
                lon, lat = self.points[pnu]
                out.append([key, "%.7f" % lon, "%.7f" % lat])
        return out


class TestReprPointBatch(unittest.TestCase):
    """F1 — 역방향 재측정의 **질의 좌표 기준**(지번 층).

    원본 판정에 쓴 VWorld 순방향 좌표는 보존되지 않았고 순방향 재호출은
    금지다. 태스크가 허용한 "표본에서 재유도"를 택하되, 이 조회도 **F2 와 같은
    구 시도코드 보정을 통과해야 한다.** 보정 없는 제 3 의 축을 새로 만드는 것이
    바로 F2 가 지적한 결함이다.
    """

    GANGWON_OLD = "42" + "11010100" + "1" + "0200" + "0001"
    GANGWON_NEW = "51" + "11010100" + "1" + "0200" + "0001"
    CONTROL = "44" + "81025026" + "1" + "0200" + "0001"

    def test_정확_PNU_로_조인하고_대표점을_쓴다(self):
        sql = oracle.sql_repr_point_batch(1)
        self.assertIn("/* t046:reprpt */", sql)
        self.assertIn("p.pnu = k.pnu", sql)
        self.assertIn("p.sido_cd = k.sido_cd", sql)
        # `geom_pt` 는 전량 NULL 이다(§1.8 실측) — 쓰면 전건이 사라진다.
        self.assertIn("ST_PointOnSurface", sql)
        self.assertNotIn("geom_pt", sql)
        self.assertNotIn("substr(p.pnu", sql)

    def test_좌표는_문자열로_와도_float_로_돌려준다(self):
        run = ReprPointRunner({self.CONTROL: (127.1234567, 35.7654321)})
        got = oracle.Oracle(run).repr_point_batch({"k1": self.CONTROL})
        self.assertEqual(sorted(got), ["k1"])
        lon, lat = got["k1"]
        self.assertIsInstance(lon, float)
        self.assertIsInstance(lat, float)
        self.assertAlmostEqual(lon, 127.1234567, places=6)
        self.assertAlmostEqual(lat, 35.7654321, places=6)

    def test_필지가_없으면_키가_아예_빠진다(self):
        """부재를 `(0, 0)` 으로 채우면 좌표가 아프리카 앞바다로 간다."""
        run = ReprPointRunner({})
        got = oracle.Oracle(run).repr_point_batch({"k1": self.CONTROL})
        self.assertEqual(got, {})

    def test_구_시도코드는_현행으로_보정해_다시_찾는다(self):
        run = ReprPointRunner({self.GANGWON_NEW: (128.0, 37.5)})
        orc = oracle.Oracle(run)
        got = orc.repr_point_batch({"k1": self.GANGWON_OLD})
        self.assertIn("k1", got)
        self.assertEqual(orc.legacy_reprpt_hits, 1)
        self.assertEqual(orc.legacy_reprpt_attempts, 1)
        self.assertIn("k1", orc.legacy_reprpt_keys)

    def test_보정을_끄면_찾지_못한다(self):
        run = ReprPointRunner({self.GANGWON_NEW: (128.0, 37.5)})
        orc = oracle.Oracle(run)
        self.assertEqual(orc.repr_point_batch({"k1": self.GANGWON_OLD},
                                              legacy=False), {})
        self.assertEqual(orc.legacy_reprpt_attempts, 0)

    def test_대조군은_보정_없이_찾는다(self):
        run = ReprPointRunner({self.CONTROL: (127.0, 35.0)})
        orc = oracle.Oracle(run)
        self.assertIn("k1", orc.repr_point_batch({"k1": self.CONTROL}))
        self.assertEqual(orc.legacy_reprpt_attempts, 0)

    def test_계수기가_지번_심판_근사_축과_섞이지_않는다(self):
        run = ReprPointRunner({self.GANGWON_NEW: (128.0, 37.5)})
        orc = oracle.Oracle(run)
        orc.repr_point_batch({"k1": self.GANGWON_OLD})
        self.assertEqual(orc.legacy_hits, 0)
        self.assertEqual(orc.legacy_apx_hits, 0)
        self.assertEqual(orc.legacy_referee_hits, 0)
        self.assertEqual(orc.legacy_reprpt_hits, 1)

    def test_빈_입력은_질의하지_않는다(self):
        run = ReprPointRunner({})
        orc = oracle.Oracle(run)
        self.assertEqual(orc.repr_point_batch({}), {})
        self.assertEqual(run.calls, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
