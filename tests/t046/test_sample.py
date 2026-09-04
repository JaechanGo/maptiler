#!/usr/bin/env python3
"""T046 §7 — 층화 표본추출. 시드 재현성·층 판정·제외 조건·교란(표본 B).

실행: /usr/bin/python3 -m unittest discover -s tests/t046 -t . -v

이 파일은 VWorld 도 PostGIS 도 부르지 않는다. 원천 파일은 **한 개(sejong,
32,999 행)** 만 실제로 읽어 1 패스 계수 항등식을 검정한다 — 전수 19M 행 스캔은
본 실행의 몫이다.

**필드 인덱스는 계획 §1.4 가 적지 않은 것을 실측으로 확정했다.**
`match_jibun[12]` = 지번일련번호(대표지번 = 0). 근거: 세종 32,999 행 중 27,945 행
(84.7 %)이 0 이고, 0 이 아닌 행들은 같은 건물관리번호를 공유하는 관련지번이다
(어진동 573·583·585·591·592 → 전부 `3611011000101200005000001`). 경기 40 만 행에서도
0 이 338,590 행으로 같은 구조다. §2.4 의 "대표지번만 뽑아 중복 필지 질의를 막는다"가
가리키는 필드가 이것이다.
`match_build[03]` = 법정읍면동명(도농 판정 입력), `[01]` 시도명, `[02]` 시군구명.
"""
import hashlib
import os
import unittest

import _ctx  # noqa: F401  (sys.path 주입)

import sample  # noqa: E402

SEJONG_JIBUN = os.path.expanduser(
    "~/geocode-build/staged/navi/match_jibun_sejong.txt"
)

# §1.4 실측 표의 합계. 1 패스가 한 행도 흘리지 않았는지 검정하는 기준값이다.
TOTAL_JIBUN = 8192209
TOTAL_BUILD = 10722641

# 세종 파일 실측(`wc -l` 및 §1.4 표).
SEJONG_ROWS = 32999
SEJONG_REPRESENTATIVE = 27945


def jline(**kw):
    """match_jibun 20 필드 합성 행. 지정하지 않은 필드는 빈 문자열."""
    f = [""] * 20
    f[0] = kw.get("bcode", "3611035026")
    f[1] = kw.get("sido", "세종특별자치시")
    f[2] = kw.get("sigungu", "")
    f[3] = kw.get("emd", "장군면")
    f[4] = kw.get("ri", "하봉리")
    f[5] = kw.get("san", "0")
    f[6] = kw.get("ji_main", "177")
    f[7] = kw.get("ji_sub", "4")
    f[8] = kw.get("road_cd", "361101000015")
    f[12] = kw.get("seq", "0")
    f[18] = kw.get("bm25", "3611035026101770004000001")
    f[19] = kw.get("bcode_emd", "3611035000")
    return "|".join(f)


def bline(**kw):
    """match_build 33 필드 합성 행."""
    f = [""] * 33
    f[0] = kw.get("bcode", "3611010100")
    f[1] = kw.get("sido", "세종특별자치시")
    f[2] = kw.get("sigungu", "")
    f[3] = kw.get("emd", "반곡동")
    f[4] = kw.get("road_cd", "361102000002")
    f[5] = kw.get("road", "한누리대로")
    f[6] = kw.get("basement", "0")
    f[7] = kw.get("bld_main", "1811")
    f[8] = kw.get("bld_sub", "0")
    f[9] = kw.get("zip", "30145")
    f[10] = kw.get("bm25", "3611010100100470001000001")
    f[13] = kw.get("hcode", "3611055600")
    f[23] = kw.get("cx", "983304.853471")
    f[24] = kw.get("cy", "1833329.756414")
    f[25] = kw.get("ex", "983296.172464")
    f[26] = kw.get("ey", "1833330.968984")
    return "|".join(f)


class TestSeeds(unittest.TestCase):
    """시드 체계 — §2.4. 층마다 독립이어야 한 층 재추출이 다른 층을 흔들지 않는다."""

    def test_master_seed_is_pinned(self):
        self.assertEqual(sample.MASTER_SEED, 20460821)

    def test_stratum_seed_matches_documented_formula(self):
        """계획이 못박은 식 그대로여야 한다 — 식이 바뀌면 과거 표본을 재현할 수 없다."""
        for sido, urban, atype in [
            ("sejong", "urban", "jibun"),
            ("gyunggi", "rural", "road"),
            ("jeonnamgwangju", "urban", "road"),
        ]:
            raw = "%d:%s:%s:%s" % (sample.MASTER_SEED, sido, urban, atype)
            want = int(hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16], 16)
            self.assertEqual(sample.stratum_seed(sido, urban, atype), want)

    def test_stratum_seeds_are_pairwise_distinct(self):
        """64 개 조합 전부 서로 다른 시드. 충돌하면 두 층이 같은 난수열을 쓴다."""
        seeds = {}
        for sido in sample.SIDO_KEYS:
            for urban in ("urban", "rural"):
                for atype in ("jibun", "road"):
                    s = sample.stratum_seed(sido, urban, atype)
                    self.assertNotIn(s, seeds, msg=(sido, urban, atype, seeds.get(s)))
                    seeds[s] = (sido, urban, atype)
        self.assertEqual(len(seeds), 64)

    def test_sido_keys_are_the_sixteen(self):
        """16 개다. 17 이 아니다 — 광주는 전남과 합쳐져 있다(§1.4)."""
        self.assertEqual(len(sample.SIDO_KEYS), 16)
        self.assertIn("jeonnamgwangju", sample.SIDO_KEYS)
        self.assertNotIn("gwangju", sample.SIDO_KEYS)

    def test_sido_key_from_filename(self):
        for name, want in [
            ("match_jibun_sejong.txt", "sejong"),
            ("match_build_jeonnamgwangju.txt", "jeonnamgwangju"),
            ("match_jibun_gyunggi.txt", "gyunggi"),
        ]:
            self.assertEqual(sample.sido_key_from_filename(name), want)

    def test_sido_key_rejects_unknown_file(self):
        with self.assertRaises(ValueError):
            sample.sido_key_from_filename("match_jibun_atlantis.txt")


class TestReservoir(unittest.TestCase):
    """1-pass reservoir(k=200) — §2.4. 19M 행을 상수메모리로 훑는다."""

    def test_same_seed_same_sample(self):
        """같은 시드·같은 입력 순서 → 완전히 같은 표본(2 회 실행 동일성)."""
        data = list(range(5000))
        a = sample.Reservoir(200, 12345)
        b = sample.Reservoir(200, 12345)
        for x in data:
            a.offer(x)
            b.offer(x)
        self.assertEqual(a.items(), b.items())
        self.assertEqual(len(a.items()), 200)

    def test_different_seed_different_sample(self):
        data = list(range(5000))
        a = sample.Reservoir(200, 12345)
        b = sample.Reservoir(200, 54321)
        for x in data:
            a.offer(x)
            b.offer(x)
        self.assertNotEqual(a.items(), b.items())

    def test_counts_everything_it_saw(self):
        """`seen` 은 제안받은 전량이다 — `N_h` 가 여기서 나온다."""
        r = sample.Reservoir(10, 7)
        for x in range(1234):
            r.offer(x)
        self.assertEqual(r.seen, 1234)
        self.assertEqual(len(r.items()), 10)

    def test_fewer_than_k_keeps_all_in_order(self):
        """모집단이 k 보다 작으면 전량 보존하고 순서도 입력 순서 그대로다."""
        r = sample.Reservoir(200, 7)
        for x in range(37):
            r.offer(x)
        self.assertEqual(r.items(), list(range(37)))

    def test_empty_stratum_yields_empty(self):
        """공집합 층(서울·대전 농촌)은 빈 표본이며 예외를 던지지 않는다(§1.5)."""
        r = sample.Reservoir(200, 7)
        self.assertEqual(r.items(), [])
        self.assertEqual(r.seen, 0)

    def test_uniformity_chi_square(self):
        """균등성 — 10 버킷 카이제곱. 편향된 구현(예: 앞쪽 k 개 고정)은 여기서 죽는다.

        1,000 행을 100 행씩 10 버킷으로 나누고, 서로 다른 시드로 100 회 뽑는다.
        회당 k=50 이므로 총 5,000 선택, 버킷당 기대 500.
        임계값 27.877 = χ²(df=9, p=0.001). 시드가 고정이라 이 테스트는 결정적이다.
        """
        buckets = [0] * 10
        trials, k, n = 100, 50, 1000
        for t in range(trials):
            r = sample.Reservoir(k, sample.MASTER_SEED + t)
            for x in range(n):
                r.offer(x)
            for x in r.items():
                buckets[x // 100] += 1

        total = trials * k
        self.assertEqual(sum(buckets), total)
        expected = total / 10.0
        chi2 = sum((b - expected) ** 2 / expected for b in buckets)
        self.assertLess(chi2, 27.877, msg="buckets=%r chi2=%.3f" % (buckets, chi2))


class TestUrbanRural(unittest.TestCase):
    """도농 기계 판정 — §1.5. 접미 ∈ {동,가,로} → 도시, ∈ {읍,면} → 농촌."""

    def test_urban_suffixes(self):
        for emd in ("반곡동", "종로1가", "여의대방로", "나성동"):
            self.assertEqual(sample.urban_rural(emd), "urban", msg=emd)

    def test_rural_suffixes(self):
        for emd in ("장군면", "덕산면", "조치원읍", "덕산읍"):
            self.assertEqual(sample.urban_rural(emd), "rural", msg=emd)

    def test_unknown_suffix_raises(self):
        """다섯 접미 밖의 값은 조용히 한쪽에 몰아넣지 않는다 — 층 왜곡의 씨앗이다."""
        for emd in ("무슨리", "", "세종특별자치시"):
            with self.assertRaises(ValueError, msg=emd):
                sample.urban_rural(emd)


class TestExclusions(unittest.TestCase):
    """추출 제외 조건 — §2.4. 제외 사유를 계수할 수 있어야 리포트에 실린다."""

    def test_representative_jibun_passes(self):
        self.assertIsNone(sample.jibun_exclusion(jline(seq="0").split("|")))

    def test_non_representative_jibun_excluded(self):
        """지번일련번호 ≠ 0 → 제외. 중복 필지 질의를 막는다."""
        for seq in ("1", "2", "20331"):
            self.assertEqual(
                sample.jibun_exclusion(jline(seq=seq).split("|")),
                "not_representative",
                msg=seq,
            )

    def test_basement_build_excluded(self):
        """지하여부 ≠ 0 → 제외. '지하' 접두는 양쪽 파서 거동이 갈리는 별개 문제다."""
        self.assertIsNone(sample.build_exclusion(bline(basement="0").split("|")))
        for b in ("1", "2"):
            self.assertEqual(
                sample.build_exclusion(bline(basement=b).split("|")),
                "basement",
                msg=b,
            )

    def test_missing_sigungu_is_not_excluded(self):
        """시군구 결측(세종)은 **제외하지 않는다** — 제외하면 sejong 층이 왜곡된다."""
        self.assertIsNone(sample.jibun_exclusion(jline(sigungu="").split("|")))
        self.assertIsNone(sample.build_exclusion(bline(sigungu="").split("|")))

    def test_exclusion_reasons_are_enumerable(self):
        """리포트가 '제외 조건별 제외 건수'를 요구한다(§2.4) — 사유 집합을 노출한다."""
        self.assertIn("not_representative", sample.EXCLUSION_REASONS)
        self.assertIn("basement", sample.EXCLUSION_REASONS)


class TestQueryAssembly(unittest.TestCase):
    """질의 문자열 조립 — §2.4. 이 문자열이 양쪽 지오코더에 그대로 간다."""

    def test_jibun_with_ri_and_sub(self):
        rec = sample.jibun_record(jline().split("|"), "sejong")
        self.assertEqual(rec["query"], "세종특별자치시 장군면 하봉리 177-4")

    def test_jibun_drops_zero_sub(self):
        """부번 0 이면 `-0` 을 붙이지 않는다."""
        rec = sample.jibun_record(jline(ji_sub="0").split("|"), "sejong")
        self.assertEqual(rec["query"], "세종특별자치시 장군면 하봉리 177")

    def test_jibun_omits_missing_sigungu_without_double_space(self):
        """시군구 결측은 **생략**해 조립한다. 빈칸 두 개가 남으면 안 된다."""
        rec = sample.jibun_record(jline(sigungu="").split("|"), "sejong")
        self.assertNotIn("  ", rec["query"])
        self.assertTrue(rec["query"].startswith("세종특별자치시 장군면"))

    def test_jibun_with_sigungu(self):
        line = jline(sido="충청남도", sigungu="예산군", emd="덕산면", ri="사천리",
                     ji_main="200", ji_sub="1")
        rec = sample.jibun_record(line.split("|"), "chungnam")
        self.assertEqual(rec["query"], "충청남도 예산군 덕산면 사천리 200-1")

    def test_jibun_without_ri(self):
        rec = sample.jibun_record(jline(emd="나성동", ri="", ji_main="723",
                                        ji_sub="0").split("|"), "sejong")
        self.assertEqual(rec["query"], "세종특별자치시 나성동 723")

    def test_jibun_san_prefix(self):
        """산번지는 `산 ` 접두가 붙는다."""
        rec = sample.jibun_record(jline(san="1", ji_main="30", ji_sub="2").split("|"),
                                  "sejong")
        self.assertEqual(rec["query"], "세종특별자치시 장군면 하봉리 산 30-2")

    def test_road_query(self):
        rec = sample.build_record(bline().split("|"), "sejong")
        self.assertEqual(rec["query"], "세종특별자치시 반곡동 한누리대로 1811")

    def test_road_drops_zero_sub(self):
        rec = sample.build_record(bline(bld_sub="0").split("|"), "sejong")
        self.assertNotIn("-", rec["query"])

    def test_road_keeps_nonzero_sub(self):
        rec = sample.build_record(bline(bld_sub="7").split("|"), "sejong")
        self.assertTrue(rec["query"].endswith("1811-7"))

    def test_road_query_uses_sigungu_not_emd_when_present(self):
        """도로명 조립은 `{시도} {시군구} {도로명} {본번}` 이다(§2.4).

        시군구가 있으면 읍면동은 넣지 않는다 — 도로명주소에 법정동은 들어가지 않는다.
        """
        line = bline(sido="서울특별시", sigungu="종로구", emd="세종로",
                     road="세종대로", bld_main="175", bld_sub="0")
        rec = sample.build_record(line.split("|"), "seoul")
        self.assertEqual(rec["query"], "서울특별시 종로구 세종대로 175")


class TestOracleKeysOnRecord(unittest.TestCase):
    """표본 레코드가 오라클 키를 함께 싣는다 — §2.4. 본 측정 때 재파싱하지 않는다."""

    def test_jibun_record_carries_pnu_and_parts(self):
        rec = sample.jibun_record(jline().split("|"), "sejong")
        self.assertEqual(rec["pnu"], "3611035026101770004")
        self.assertEqual(rec["bcode"], "3611035026")
        self.assertEqual(rec["san"], 0)
        self.assertEqual(rec["ji_main"], 177)
        self.assertEqual(rec["ji_sub"], 4)
        self.assertEqual(rec["layer"], "jibun")
        self.assertEqual(rec["sido"], "sejong")
        self.assertEqual(rec["urban"], "rural")

    def test_pnu_dual_path_mismatch_is_flagged_and_bm25_wins(self):
        """조립본 ≠ `BM25[:19]` 이면 **BM25 를 채택**하고 표시한다(조건 5).

        실물이 있다: 세종 나성동 776 번지 행의 건물관리번호는 504-0 을 가리킨다.
        관련지번이 대표지번의 건물관리번호를 물고 있기 때문이다.
        """
        line = jline(bcode="3611010700", emd="나성동", ri="", ji_main="776",
                     ji_sub="0", bm25="3611010700105040000000001")
        rec = sample.jibun_record(line.split("|"), "sejong")
        self.assertTrue(rec["pnu_mismatch"])
        self.assertEqual(rec["pnu"], "3611010700105040000")

    def test_pnu_match_is_not_flagged(self):
        rec = sample.jibun_record(jline().split("|"), "sejong")
        self.assertFalse(rec["pnu_mismatch"])

    def test_build_record_carries_bm25_and_both_coord_pairs(self):
        """도로명 층은 심판용으로 출입구·건물중심 **둘 다** 싣는다(§4.3)."""
        rec = sample.build_record(bline().split("|"), "sejong")
        self.assertEqual(rec["bm25"], "3611010100100470001000001")
        self.assertEqual(rec["pnu"], "3611010100100470001")
        self.assertEqual(rec["entrance_5179"], (983296.172464, 1833330.968984))
        self.assertEqual(rec["center_5179"], (983304.853471, 1833329.756414))
        self.assertEqual(rec["layer"], "road")

    def test_record_key_is_stable_and_layer_scoped(self):
        """A·B 중복 판정에 쓰는 키. 같은 PNU 라도 층이 다르면 다른 레코드다."""
        j = sample.jibun_record(jline().split("|"), "sejong")
        b = sample.build_record(
            bline(bcode="3611035026", bm25="3611035026101770004000001").split("|"),
            "sejong",
        )
        self.assertEqual(sample.record_key(j), sample.record_key(j))
        self.assertNotEqual(sample.record_key(j), sample.record_key(b))


class TestOnePassAccounting(unittest.TestCase):
    """1 패스 계수 항등식 — §2.4 부산물. 한 행도 흘리지 않았음을 검정한다.

    계획 §7 은 "`N_h` 합계 = §1.4 전체 행수"라고 적었으나, §2.4 가 제외 조건을
    두었으므로 `N_h`(추출 가능 모집단)와 전체 행수는 원리상 같을 수 없다.
    검정해야 하는 것은 **`Σ N_h + Σ 제외 = 전체 행수`** 라는 항등식이다.
    이쪽이 "빠짐없이 계상했는가"를 정확히 재며, 계획의 의도도 이것이다.
    """

    def test_totals_pinned_to_plan_table(self):
        self.assertEqual(sample.TOTAL_ROWS_JIBUN, TOTAL_JIBUN)
        self.assertEqual(sample.TOTAL_ROWS_BUILD, TOTAL_BUILD)
        self.assertEqual(
            sample.TOTAL_ROWS_JIBUN + sample.TOTAL_ROWS_BUILD, 18914850
        )

    def test_scan_file_accounts_for_every_row(self):
        """실 원천 1 개(sejong, 32,999 행)로 항등식을 검정한다."""
        self.assertTrue(os.path.exists(SEJONG_JIBUN), SEJONG_JIBUN)
        stats = sample.scan_file(SEJONG_JIBUN)
        self.assertEqual(stats.rows, SEJONG_ROWS)
        self.assertEqual(stats.kept + sum(stats.excluded.values()), stats.rows)
        self.assertEqual(stats.kept, SEJONG_REPRESENTATIVE)
        self.assertEqual(stats.excluded["not_representative"],
                         SEJONG_ROWS - SEJONG_REPRESENTATIVE)

    def test_scan_file_populates_stratum_counts(self):
        """층별 `N_h` 가 나온다. 세종은 도시·농촌 둘 다 비어 있지 않다(§1.5)."""
        stats = sample.scan_file(SEJONG_JIBUN)
        self.assertEqual(sum(stats.n_h.values()), stats.kept)
        self.assertIn(("sejong", "urban", "jibun"), stats.n_h)
        self.assertIn(("sejong", "rural", "jibun"), stats.n_h)
        self.assertGreater(stats.n_h[("sejong", "rural", "jibun")],
                           stats.n_h[("sejong", "urban", "jibun")])

    def test_source_files_are_thirty_two_sorted(self):
        """읽기 순서 규칙: 파일명 사전순 오름차순(§2.4 M11)."""
        files = sample.source_files()
        self.assertEqual(len(files), 32)
        self.assertEqual(files, sorted(files))
        self.assertEqual(len([f for f in files if "match_jibun" in f]), 16)
        self.assertEqual(len([f for f in files if "match_build" in f]), 16)

    def test_manifest_records_path_size_and_digest(self):
        """재현성 정보 3 종을 파일마다 기록한다(M11). 여기서는 1 개만 확인한다."""
        entry = sample.manifest_entry(SEJONG_JIBUN)
        self.assertTrue(os.path.isabs(entry["path"]))
        self.assertEqual(entry["bytes"], os.path.getsize(SEJONG_JIBUN))
        self.assertEqual(len(entry["sha256"]), 64)
        self.assertEqual(entry["sha256"], sample.sha256_file(SEJONG_JIBUN))

    def test_encoding_conventions_are_pinned(self):
        self.assertEqual(sample.ENCODING, "cp949")
        self.assertEqual(sample.DELIMITER, "|")


class TestSampleSizes(unittest.TestCase):
    """표본 규모 — §2.1·§2.3."""

    def test_per_stratum_sizes(self):
        self.assertEqual(sample.SAMPLE_A_PER_STRATUM, 200)
        self.assertEqual(sample.SAMPLE_B_PER_STRATUM, 40)

    def test_perturb_limit(self):
        self.assertEqual(sample.PERTURB_LIMIT, 10)


class TestDisjointAB(unittest.TestCase):
    """M12 — 표본 B 는 A 의 여집합에서 뽑는다. 겹치면 두 측정이 독립이 아니다."""

    def test_b_pool_excludes_a(self):
        recs = [
            sample.jibun_record(
                jline(ji_main=str(i), ji_sub="0",
                      bm25="36110350261%04d0000000001" % i).split("|"),
                "sejong",
            )
            for i in range(100)
        ]
        a_keys = {sample.record_key(r) for r in recs[:30]}
        pool = sample.exclude_keys(recs, a_keys)
        self.assertEqual(len(pool), 70)
        self.assertEqual(
            {sample.record_key(r) for r in pool} & a_keys, set()
        )


class FakeParcel:
    """`parcel` 존재 조회의 대역. 주입한 PNU 집합만 '실존'이다."""

    def __init__(self, existing):
        self.existing = set(existing)
        self.queried = []

    def __call__(self, pnus):
        pnus = list(pnus)
        self.queried.append(list(pnus))
        return {p for p in pnus if p in self.existing}


class TestPerturb(unittest.TestCase):
    """표본 B 교란 — §2.3. C4 회귀 장치: 결과가 **실존하면 안 된다**."""

    def _rec(self, ji_main=200, ji_sub=0):
        line = jline(bcode="4481036031", sido="충청남도", sigungu="예산군",
                     emd="덕산면", ri="사천리", ji_main=str(ji_main),
                     ji_sub=str(ji_sub),
                     bm25="448103603110%03d%04d000001" % (ji_main, ji_sub))
        rec = sample.jibun_record(line.split("|"), "chungnam")
        rec["pnu_mismatch"] = False
        rec["pnu"] = sample.pnu_of(rec)
        return rec

    def test_candidate_order_is_pinned(self):
        """후보 순서: 부번 1..10(원본 자신 제외), 그 뒤 본번 단독.

        §2.3 은 규칙 2(부번 -1,-2,…)에 상한 10 회를 걸고, 규칙 4(본번 단독)를
        뒤에 두었다. 순서를 이렇게 고정해야 계획의 두 규칙이 모두 살아 있고,
        같은 시드에서 같은 교란 결과가 재현된다.
        """
        rec = self._rec(ji_main=200, ji_sub=3)
        subs = [c["ji_sub"] for c in sample.perturb_candidates(rec)]
        self.assertEqual(subs[:9], [1, 2, 4, 5, 6, 7, 8, 9, 10])
        self.assertEqual(subs[-1], 0)

    def test_candidate_order_when_original_sub_is_zero(self):
        """원본이 본번 단독이면 본번 단독은 후보가 아니다(자기 자신)."""
        rec = self._rec(ji_main=200, ji_sub=0)
        subs = [c["ji_sub"] for c in sample.perturb_candidates(rec)]
        self.assertEqual(subs, [1, 2, 3, 4, 5, 6, 7, 8, 9, 10])
        self.assertNotIn(0, subs)

    def test_perturb_picks_first_absent_candidate(self):
        """`-1` 이 실존하면 건너뛰고 `-2` 를 잡는다 — C4 가 지적한 바로 그 경로다."""
        rec = self._rec(ji_main=200, ji_sub=0)
        probe = FakeParcel({"4481036031102000001"})
        out = sample.perturb(rec, probe)
        self.assertIsNotNone(out)
        self.assertEqual(out["ji_sub"], 2)
        self.assertEqual(out["pnu"], "4481036031102000002")
        self.assertEqual(out["query"], "충청남도 예산군 덕산면 사천리 200-2")

    def test_perturb_result_is_absent_from_parcel(self):
        """C4 회귀 — 채택된 교란 결과의 PNU 는 `parcel` 에 **없어야** 한다."""
        existing = {"4481036031102000%03d" % i for i in range(1, 6)}
        rec = self._rec(ji_main=200, ji_sub=0)
        probe = FakeParcel(existing)
        out = sample.perturb(rec, probe)
        self.assertIsNotNone(out)
        self.assertNotIn(out["pnu"], probe.existing)

    def test_perturb_discards_after_limit(self):
        """10 회 안에 부존재를 못 찾으면 폐기(None). 폐기 건수는 층별로 집계된다."""
        rec = self._rec(ji_main=200, ji_sub=0)
        probe = FakeParcel({"4481036031102000%03d" % i for i in range(0, 12)})
        self.assertIsNone(sample.perturb(rec, probe))

    def test_perturb_probes_at_most_limit_plus_fallback(self):
        """조회 횟수가 상한을 넘지 않는다 — 배치 1 회로 후보 전량을 확인한다."""
        rec = self._rec(ji_main=200, ji_sub=3)
        probe = FakeParcel(set())
        sample.perturb(rec, probe)
        self.assertEqual(len(probe.queried), 1)
        self.assertLessEqual(len(probe.queried[0]), sample.PERTURB_LIMIT + 1)

    def test_perturb_keeps_bcode_san_and_main(self):
        """`(법정동코드, 산, 본번)` 은 고정이다(§2.3 규칙 1)."""
        rec = self._rec(ji_main=200, ji_sub=0)
        out = sample.perturb(rec, FakeParcel(set()))
        self.assertEqual(out["bcode"], rec["bcode"])
        self.assertEqual(out["san"], rec["san"])
        self.assertEqual(out["ji_main"], rec["ji_main"])
        self.assertNotEqual(out["ji_sub"], rec["ji_sub"])

    def test_perturb_marks_record_as_synthetic(self):
        """교란 레코드는 실존 표본과 섞이면 안 된다 — 표시를 남긴다."""
        out = sample.perturb(self._rec(), FakeParcel(set()))
        self.assertTrue(out["perturbed"])
        self.assertEqual(out["origin_pnu"], "4481036031102000000")


if __name__ == "__main__":
    unittest.main(verbosity=2)
