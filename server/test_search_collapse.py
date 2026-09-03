"""검색 결과 '같은 자리·같은 이름' 접기(collapse_dups) 회귀 테스트.

[실측 2026-09-03 .244] dedup_er 의 대표(is_primary)는 타일 표시층에만 쓰이고 검색은 안 봤다.
  · '이마트24R춘의역점' / '이마트24 R춘의역점' — localdata 2행, 0m → 둘 다 노출
  · OSM 역(station) + 정류장/출입구(poi) 같은 이름 10~25m → '대전역 ×8'
규칙: 정규화 이름 동일 + 30m 이내 → 뒤 항목 버림. 양방향 정류장(50~100m)은 유지(서로 다른 객체).
"""
import importlib.util
import os
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_MOD_PATH = os.environ.get("GEOCODE_MODULE", os.path.join(_HERE, "geocode-api-pg.py"))


def _load_module(path=_MOD_PATH):
    spec = importlib.util.spec_from_file_location("geocode_api_pg", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


M = _load_module()


def _it(name, lon, lat, kind="biz", prim=None, source="localdata"):
    d = {"name": name, "kind": kind, "lon": lon, "lat": lat, "source": source}
    if prim is not None:
        d["_prim"] = prim
    return d


class TestCollapseDups(unittest.TestCase):
    def test_same_spot_variant_spelling_collapses_to_primary(self):
        # 이름 표기만 다른 같은 점포(0m) — 대표(is_primary=1)가 남아야 한다
        rows = [(135, _it("이마트24 R춘의역점", 126.785869, 37.504535, prim=0)),
                (135, _it("이마트24R춘의역점", 126.785869, 37.504535, prim=1))]
        rows.sort(key=lambda x: (-x[0], 1 if x[1].get("_prim") == 0 else 0))
        out = M.collapse_dups(rows, 10)
        self.assertEqual([o["name"] for o in out], ["이마트24R춘의역점"])

    def test_station_and_stop_within_radius_collapse(self):
        # OSM 역 노드 + 13m 떨어진 동명 poi → 1건(앞선 station 유지)
        rows = [(150, _it("상동역", 126.753173, 37.505836, kind="station", source="osm")),
                (140, _it("상동역", 126.753300, 37.505900, kind="poi", source="osm"))]
        out = M.collapse_dups(rows, 10)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["kind"], "station")

    def test_bidirectional_stops_beyond_radius_kept(self):
        # 양방향 정류장 ~80m — 서로 다른 객체, 둘 다 유지
        rows = [(140, _it("상동역", 126.7530, 37.5058, kind="poi", source="osm")),
                (140, _it("상동역", 126.7539, 37.5058, kind="poi", source="osm"))]
        out = M.collapse_dups(rows, 10)
        self.assertEqual(len(out), 2)

    def test_different_names_same_spot_kept(self):
        rows = [(135, _it("스타벅스 부천점", 126.78, 37.50)),
                (135, _it("투썸플레이스 부천점", 126.78, 37.50))]
        self.assertEqual(len(M.collapse_dups(rows, 10)), 2)

    def test_limit_and_none_coords(self):
        rows = [(150, _it("A", None, None)), (140, _it("B", 126.1, 37.1)),
                (130, _it("C", 126.2, 37.2)), (120, _it("D", 126.3, 37.3))]
        out = M.collapse_dups(rows, 2)
        self.assertEqual([o["name"] for o in out], ["B", "C"])

    def test_exact_key_rule_preserved(self):
        # 종전 규칙(이름 완전일치 + 좌표 5자리)도 그대로 동작
        rows = [(135, _it("같은집", 126.7800001, 37.5000001)), (135, _it("같은집", 126.7800002, 37.5000002))]
        self.assertEqual(len(M.collapse_dups(rows, 10)), 1)

    def test_dup_key_normalization(self):
        self.assertEqual(M._dup_key("이마트24 R춘의역점"), M._dup_key("이마트24R춘의역점"))
        self.assertEqual(M._dup_key("Starbucks (부천)"), "starbucks부천")
        self.assertEqual(M._dup_key(None), "")



class TestRefineSigungu(unittest.TestCase):
    def test_prefix_upgrade(self):
        own = {"sido": "경기도", "sigungu": "부천시"}
        self.assertEqual(M.refine_sigungu(own, {"sigungu": "부천시 원미구"})["sigungu"], "부천시 원미구")

    def test_no_change_when_not_prefix_or_missing(self):
        own = {"sigungu": "부천시"}
        self.assertEqual(M.refine_sigungu(own, {"sigungu": "인천광역시 부평구"})["sigungu"], "부천시")
        self.assertEqual(M.refine_sigungu(own, {})["sigungu"], "부천시")
        self.assertEqual(M.refine_sigungu({}, {"sigungu": "부천시 원미구"}), {})
        self.assertEqual(M.refine_sigungu(own, {"sigungu": "부천시"})["sigungu"], "부천시")


class TestNamePathSql(unittest.TestCase):
    def test_short_prefix_is_materialized(self):
        sql = M._name_path_sql(["(search_text LIKE %s OR bld LIKE %s)"], True)
        self.assertTrue(sql.startswith("SELECT * FROM (("))
        self.assertIn("search_text LIKE %s LIMIT", sql); self.assertIn("bld LIKE %s LIMIT", sql)
        self.assertEqual(sql.count("%s"), 2)
        self.assertTrue(sql.rstrip().endswith(f"LIMIT {M.SHORT_PREFIX_CAP}"))
        # 인식 못 하는 형태(다중 cond)는 CTE 폴백
        sql2 = M._name_path_sql(["a", "b"], True)
        self.assertTrue(sql2.startswith("WITH c AS MATERIALIZED ("))

    def test_long_or_multi_unchanged(self):
        sql = M._name_path_sql(["(search_text ILIKE %s OR bld ILIKE %s)"], False)
        self.assertTrue(sql.startswith("SELECT *, ST_X(geom) AS lon"))
        self.assertNotIn("MATERIALIZED", sql)
        self.assertTrue(sql.rstrip().endswith(f"LIMIT {M.ADDR_CAP}"))


class TestNarrowByRegion(unittest.TestCase):
    """지역 좁힘 — 인천 개편(신 코드 DB) 실측 회귀 (2026-09-03)."""
    CDS = ["28155103", "11140101", "27110101", "26110101"]   # 인천 영종구 운서동(신), 서울 중구, 대구 중구, 부산 중구 동

    def test_old_gu_name_with_sido_falls_back_to_sido(self):
        # '인천 중구 운서동': 2단이 타도시 중구로 좁혀 비면 → 시도(28)만으로 폴백
        out = M.narrow_by_region(self.CDS, set(), {"11140", "27110", "26110"}, {"28"})
        self.assertEqual(out, ["28155103"])

    def test_remap_bidirectional_without_sido(self):
        # '중구 운서동': 대응표(old 28110147 ∪ new 28155103) 로 인천 동을 특정
        out = M.narrow_by_region(self.CDS, {"28110147", "28155103"}, {"11140", "27110", "26110"}, set())
        self.assertEqual(out, ["28155103"])

    def test_other_city_gu_not_hijacked_by_incheon_remap(self):
        # '대구 중구 동인동': 대응표(인천)에 걸려도 시도 27 과 교차해 비면 → 2단 시군구∧시도 → 대구 중구
        out = M.narrow_by_region(self.CDS, {"28110147", "28155103"}, {"11140", "27110", "26110"}, {"27"})
        self.assertEqual(out, ["27110101"])

    def test_no_sido_gu_ambiguous_uses_sgg(self):
        # '중구 동인동'(시도 없음): 1단(인천) 비면 → 2단 시군구 전부
        cds = ["11140101", "27110101"]
        out = M.narrow_by_region(cds, {"28110147"}, {"11140", "27110"}, set())
        self.assertEqual(out, ["11140101", "27110101"])

    def test_no_dictionary_hit_keeps_all(self):
        self.assertEqual(M.narrow_by_region(self.CDS, set(), set(), set()), self.CDS)

    def test_all_empty_when_region_hit_but_no_match(self):
        # 지정 시도에 그 동이 없음 → [] (타지역 혼입 차단)
        self.assertEqual(M.narrow_by_region(["11140101"], set(), set(), {"28"}), [])


class TestNormalizeOwn(unittest.TestCase):
    def test_legacy_sido_is_remapped_for_display(self):
        M._HAS_SIDO_REMAP = False    # 치환표 0행(통합 코드 DB) 이어도 표기 정규화는 동작해야 한다
        own = M.normalize_own({"sido": "광주광역시", "sigungu": "서구"}, {"sido": "전남광주통합특별시", "sigungu": "서구"})
        self.assertEqual(own["sido"], "전남광주통합특별시")
        own = M.normalize_own({"sido": "전라남도", "sigungu": "순천시"}, {})
        self.assertEqual(own["sido"], "전남광주통합특별시")

    def test_current_sido_untouched_and_sigungu_refined(self):
        own = M.normalize_own({"sido": "경기도", "sigungu": "부천시"}, {"sido": "경기도", "sigungu": "부천시 원미구"})
        self.assertEqual((own["sido"], own["sigungu"]), ("경기도", "부천시 원미구"))
        self.assertEqual(M.normalize_own({}, {"sido": "경기도"}), {})


if __name__ == "__main__":
    unittest.main()
