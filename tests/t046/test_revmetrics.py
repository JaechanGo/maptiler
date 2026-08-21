#!/usr/bin/env python3
"""T046 F4 — 역방향 지표 추출·비교식 단위 테스트. 순수 함수, 외부 의존 없음.

검수 F4 가 요구한 축을 그대로 덮는다.
  · `address.structure` 파싱(우리 `/reverse` 는 `results` 키가 **없다**)
  · 리 4 지표 산출식
  · `address_source` 기록
  · `results` 키가 있어도 그쪽으로 회귀하지 않음
  · **이번에 추가한** 도로명·건물번호·본번/부번 비교식
  · **이번에 추가한** `type=both` 항목 선택(첫 항목 고정 금지)

정규화 전후를 모두 낸다는 요구 때문에 축이 3 단이다(§4.2 규칙 1~12).
  strict  원문 문자열 그대로. 공백 제거조차 하지 않는다(`.strip()` 만).
  bare    공백만 제거하고 `\\d+(-\\d+)?` 로만 읽는다. 지목 접미·산·번지는 실패.
  norm    §4.2 규칙 전체 적용(`normalize.py`).

실행: /usr/bin/python3 -m unittest discover -s tests/t046 -t tests/t046 -v
"""
import unittest

import _ctx  # noqa: F401  (sys.path 주입)

from revmetrics import (  # noqa: E402
    AXES,
    aggregate_axis,
    compare_axes,
    ours_axes,
    ri_cells,
    ri_metrics,
    source_split,
    split_reverse_entries,
    vw_parcel_axes,
    vw_road_axes,
    vw_status,
)


# ── 실측 응답 조각 ────────────────────────────────────────────────────
# 필드명은 추측이 아니라 실측이다(2026-08-21 probe, 계획 §1.3 재확인).
# 도로명 항목의 `level4LC` 는 **7 자리 도로명코드**이지 법정동코드가 아니다.
VW_PARCEL = {
    "type": "parcel",
    "text": "부산광역시 기장군 기장읍 만화리 210-1",
    "zipcode": "46052",
    "structure": {
        "level0": "대한민국", "level1": "부산광역시", "level2": "기장군",
        "level3": "", "level4L": "기장읍", "level4LC": "2671025030",
        "level4A": "기장읍", "level4AC": "2671025000",
        "level5": "210-1", "detail": "",
    },
}
VW_ROAD = {
    "type": "road",
    "text": "부산광역시 기장군 기장읍 동서길 100-2",
    "zipcode": "46052",
    "structure": {
        "level0": "대한민국", "level1": "부산광역시", "level2": "기장군",
        "level3": "기장읍", "level4L": "동서길", "level4LC": "4220076",
        "level4A": "기장읍", "level4AC": "2671025000",
        "level5": "100-2", "detail": "",
    },
}
OURS = {
    "address": {
        "bld": None, "parcel": None, "road": None, "zipcode": "46052",
        "structure": {
            "sido": "부산광역시", "sigungu": "기장군", "emd": "기장읍",
            "haeng_dong": "기장읍", "ri": None, "san": False,
            "road_name": "동서길", "main_no": 100, "sub_no": 2,
            "bld_main_no": 100, "bld_sub_no": 2,
            "ji_main": 210, "ji_sub": 1, "bld_name": None,
            "zipcode": "46052", "b_code": "2671025030", "h_code": "2671025000",
        },
    },
    "address_source": "pip_key",
    "areas": [], "contract_version": "1", "lat": 35.2, "lon": 129.2,
    "nearest": [{"dist_m": 3.4}],
}


def _body(entries, status="OK"):
    return {"response": {"status": status, "result": list(entries)}}


# ── 항목 선택 ─────────────────────────────────────────────────────────
class TestSplitReverseEntries(unittest.TestCase):
    """`type=both` 는 항목 **순서를 보장하지 않는다**. 첫 항목 고정은 결함이다."""

    def test_역방향_항목은_순서가_아니라_type_으로_고른다(self):
        got = split_reverse_entries(_body([VW_ROAD, VW_PARCEL]))
        self.assertIs(got["parcel"], VW_PARCEL)
        self.assertIs(got["road"], VW_ROAD)

    def test_정상_순서에서도_같은_결과다(self):
        got = split_reverse_entries(_body([VW_PARCEL, VW_ROAD]))
        self.assertIs(got["parcel"], VW_PARCEL)
        self.assertIs(got["road"], VW_ROAD)

    def test_도로명_항목이_없으면_None_이지_대체하지_않는다(self):
        got = split_reverse_entries(_body([VW_PARCEL]))
        self.assertIs(got["parcel"], VW_PARCEL)
        self.assertIsNone(got["road"])

    def test_지번_항목이_없으면_도로명을_지번으로_쓰지_않는다(self):
        got = split_reverse_entries(_body([VW_ROAD]))
        self.assertIsNone(got["parcel"])
        self.assertIs(got["road"], VW_ROAD)

    def test_빈_응답과_형태오류는_모두_None_이다(self):
        for body in (None, {}, {"response": {}}, _body([]),
                     {"response": {"result": {"type": "parcel"}}}):
            got = split_reverse_entries(body)
            self.assertEqual(got, {"parcel": None, "road": None}, repr(body))

    def test_type_대소문자와_공백을_흡수한다(self):
        e = dict(VW_PARCEL, type=" PARCEL ")
        self.assertIs(split_reverse_entries(_body([e]))["parcel"], e)

    def test_알_수_없는_type_은_버린다(self):
        e = dict(VW_PARCEL, type="bld")
        self.assertEqual(split_reverse_entries(_body([e])),
                         {"parcel": None, "road": None})

    def test_status_는_별도로_읽는다(self):
        self.assertEqual(vw_status(_body([VW_PARCEL])), "OK")
        self.assertEqual(vw_status(_body([], status="NOT_FOUND")), "NOT_FOUND")
        self.assertIsNone(vw_status({}))


# ── VWorld 축 추출 ────────────────────────────────────────────────────
class TestVwParcelAxes(unittest.TestCase):
    def test_법정동코드는_level4LC_10_자리다(self):
        self.assertEqual(vw_parcel_axes(VW_PARCEL)["bcode"], "2671025030")

    def test_10_자리가_아니면_버린다(self):
        e = {"type": "parcel", "structure": {"level4LC": "26710"}}
        self.assertIsNone(vw_parcel_axes(e)["bcode"])

    def test_지번_원문은_손대지_않는다(self):
        e = {"type": "parcel", "structure": {"level5": "638-1도"}}
        self.assertEqual(vw_parcel_axes(e)["jibun_raw"], "638-1도")

    def test_지목_접미는_정규화에서만_떨어진다(self):
        e = {"type": "parcel", "structure": {"level5": "638-1도"}}
        ax = vw_parcel_axes(e)
        self.assertIsNone(ax["jibun_bare"])          # bare 는 실패해야 한다
        self.assertEqual(ax["jibun"], (False, 638, 1))

    def test_산_지번은_정규화에서만_읽힌다(self):
        e = {"type": "parcel", "structure": {"level5": "산102임"}}
        ax = vw_parcel_axes(e)
        self.assertIsNone(ax["jibun_bare"])
        self.assertEqual(ax["jibun"], (True, 102, 0))

    def test_부번_없는_지번은_부번_0_이다(self):
        # bare 는 산을 표현하지 못하므로 `(본번, 부번)` 2-튜플,
        # norm 은 `(산, 본번, 부번)` 3-튜플이다. 자릿수 자체가 단을 구분한다.
        e = {"type": "parcel", "structure": {"level5": "210"}}
        self.assertEqual(vw_parcel_axes(e)["jibun_bare"], (210, 0))
        self.assertEqual(vw_parcel_axes(e)["jibun"], (False, 210, 0))

    def test_리_명칭은_text_에서만_나온다(self):
        # `structure` 에 리 필드가 없다는 것이 실측이다. `text` 파싱이 유일한 경로다.
        self.assertEqual(vw_parcel_axes(VW_PARCEL)["ri_name"], "만화리")

    def test_도시_지점은_리가_없다(self):
        e = {"type": "parcel", "text": "서울특별시 종로구 청운동 1-1",
             "structure": {"level4L": "청운동"}}
        self.assertIsNone(vw_parcel_axes(e)["ri_name"])

    def test_읍면동_명칭이_리로_오인되지_않는다(self):
        e = {"type": "parcel", "text": "경기도 양평군 양동면 210-1",
             "structure": {"level4L": "양동면"}}
        self.assertIsNone(vw_parcel_axes(e)["ri_name"])

    def test_None_항목은_전_축이_None_이다(self):
        ax = vw_parcel_axes(None)
        self.assertEqual(ax["bcode"], None)
        self.assertEqual(ax["jibun"], None)
        self.assertEqual(ax["ri_name"], None)


class TestVwRoadAxes(unittest.TestCase):
    def test_도로명은_level4L_이다(self):
        self.assertEqual(vw_road_axes(VW_ROAD)["road_name"], "동서길")

    def test_건물번호는_level5_이고_본번_부번으로_쪼갠다(self):
        ax = vw_road_axes(VW_ROAD)
        self.assertEqual(ax["bld_raw"], "100-2")
        self.assertEqual(ax["bld_no"], (100, 2))

    def test_도로명코드_7_자리를_법정동코드로_쓰지_않는다(self):
        ax = vw_road_axes(VW_ROAD)
        self.assertEqual(ax["road_cd"], "4220076")
        self.assertNotIn("bcode", ax)

    def test_부번_없는_건물번호는_부번_0_이다(self):
        e = {"type": "road", "structure": {"level4L": "세종대로", "level5": "110"}}
        self.assertEqual(vw_road_axes(e)["bld_no"], (110, 0))

    def test_지하_건물번호는_읽지_못해도_원문은_남는다(self):
        e = {"type": "road", "structure": {"level4L": "세종대로", "level5": "지하 110"}}
        ax = vw_road_axes(e)
        self.assertEqual(ax["bld_raw"], "지하 110")
        self.assertIsNone(ax["bld_no"])

    def test_None_항목은_전_축이_None_이다(self):
        ax = vw_road_axes(None)
        self.assertIsNone(ax["road_name"])
        self.assertIsNone(ax["bld_no"])


# ── 우리 축 추출 ──────────────────────────────────────────────────────
class TestOursAxes(unittest.TestCase):
    def test_address_structure_에서_읽는다(self):
        ax = ours_axes(OURS)
        self.assertEqual(ax["bcode"], "2671025030")
        self.assertEqual(ax["ji_main"], 210)
        self.assertEqual(ax["ji_sub"], 1)
        self.assertIs(ax["san"], False)
        self.assertEqual(ax["road_name"], "동서길")
        self.assertEqual(ax["bld_no"], (100, 2))

    def test_address_source_를_기록한다(self):
        self.assertEqual(ours_axes(OURS)["address_source"], "pip_key")
        self.assertIsNone(ours_axes({"address": {}})["address_source"])

    def test_results_키가_있어도_그쪽으로_회귀하지_않는다(self):
        """스키마 v1 결함의 회귀 방지 — `/reverse` 에는 `results` 가 없다."""
        poisoned = dict(OURS)
        poisoned["results"] = [{"address": {"structure": {"b_code": "9999999999",
                                                          "ji_main": 1}}}]
        ax = ours_axes(poisoned)
        self.assertEqual(ax["bcode"], "2671025030")
        self.assertEqual(ax["ji_main"], 210)

    def test_results_만_있고_address_가_없으면_부재다(self):
        ax = ours_axes({"results": [{"address": {"structure": {"b_code": "1"}}}]})
        self.assertIsNone(ax["bcode"])
        self.assertIsNone(ax["ji_main"])

    def test_리_문자열은_빈값을_None_으로_접는다(self):
        for v in (None, "", "  "):
            body = {"address": {"structure": {"ri": v}}}
            self.assertIsNone(ours_axes(body)["ri_name"])
        body = {"address": {"structure": {"ri": "만화리"}}}
        self.assertEqual(ours_axes(body)["ri_name"], "만화리")

    def test_지번_원문은_숫자에서_되짜맞춘다(self):
        self.assertEqual(ours_axes(OURS)["jibun_raw"], "210-1")
        b = {"address": {"structure": {"ji_main": 210, "ji_sub": 0, "san": True}}}
        self.assertEqual(ours_axes(b)["jibun_raw"], "산 210")

    def test_본번이_없으면_지번_전체가_부재다(self):
        b = {"address": {"structure": {"ji_main": None, "ji_sub": 3}}}
        ax = ours_axes(b)
        self.assertIsNone(ax["jibun_raw"])
        self.assertIsNone(ax["jibun"])


# ── 비교 ─────────────────────────────────────────────────────────────
class TestCompareAxes(unittest.TestCase):
    def _cmp(self, vwp=VW_PARCEL, vwr=VW_ROAD, ours=OURS):
        return compare_axes(vw_parcel_axes(vwp), vw_road_axes(vwr), ours_axes(ours))

    def test_모든_축이_이름표를_갖는다(self):
        got = self._cmp()
        self.assertEqual(sorted(got), sorted(AXES))
        for axis, cell in got.items():
            self.assertEqual(sorted(cell), ["eq", "ours", "vw"], axis)

    def test_완전_일치_사례는_전_축이_참이다(self):
        got = self._cmp()
        for axis in AXES:
            self.assertIs(got[axis]["eq"], True, axis)

    def test_법정동코드_접두는_2_5_8_10_으로_끊는다(self):
        ours = {"address": {"structure": dict(OURS["address"]["structure"],
                                              b_code="2671025099")}}
        got = compare_axes(vw_parcel_axes(VW_PARCEL), vw_road_axes(None),
                           ours_axes(ours))
        self.assertIs(got["sido"]["eq"], True)
        self.assertIs(got["sgg"]["eq"], True)
        self.assertIs(got["emd"]["eq"], True)
        self.assertIs(got["ri_code"]["eq"], False)

    def test_시도코드_12_완화는_정규화_축에서만_통한다(self):
        # 원천 202607 은 광주·전남을 `12` 로 합친다(§4.3 조건 1).
        vwp = {"type": "parcel", "structure": {"level4LC": "1234567890"}}
        ours = {"address": {"structure": {"b_code": "4634567890"}}}
        got = compare_axes(vw_parcel_axes(vwp), vw_road_axes(None), ours_axes(ours))
        self.assertIs(got["sido_strict"]["eq"], False)
        self.assertIs(got["sido"]["eq"], True)

    def test_본번_일치_부번_불일치를_따로_센다(self):
        vwp = {"type": "parcel", "structure": {"level5": "210-9"}}
        got = compare_axes(vw_parcel_axes(vwp), vw_road_axes(None), ours_axes(OURS))
        self.assertIs(got["jibun_main"]["eq"], True)
        self.assertIs(got["jibun_sub"]["eq"], False)

    def test_산여부는_별도_축이다(self):
        vwp = {"type": "parcel", "structure": {"level5": "산210-1"}}
        got = compare_axes(vw_parcel_axes(vwp), vw_road_axes(None), ours_axes(OURS))
        self.assertIs(got["jibun_main"]["eq"], True)
        self.assertIs(got["san"]["eq"], False)

    def test_지목_접미는_정규화_전후를_가른다(self):
        vwp = {"type": "parcel", "structure": {"level5": "210-1도"}}
        got = compare_axes(vw_parcel_axes(vwp), vw_road_axes(None), ours_axes(OURS))
        self.assertIs(got["jibun_raw"]["eq"], False)      # 원문 그대로면 불일치
        self.assertIsNone(got["jibun_main_bare"]["eq"])   # bare 는 파싱 실패 → 판정불가
        self.assertIs(got["jibun_main"]["eq"], True)      # 정규화하면 일치

    def test_도로명_공백은_정규화에서만_흡수된다(self):
        vwr = {"type": "road", "structure": {"level4L": "동서 길", "level5": "100-2"}}
        got = compare_axes(vw_parcel_axes(None), vw_road_axes(vwr), ours_axes(OURS))
        self.assertIs(got["road_raw"]["eq"], False)
        self.assertIs(got["road_name"]["eq"], True)

    def test_건물번호_부번_생략은_정규화에서만_흡수된다(self):
        vwr = {"type": "road", "structure": {"level4L": "동서길", "level5": "100"}}
        ours = {"address": {"structure": dict(OURS["address"]["structure"],
                                              sub_no=0, main_no=100)}}
        got = compare_axes(vw_parcel_axes(None), vw_road_axes(vwr), ours_axes(ours))
        self.assertIs(got["bld_raw"]["eq"], True)
        self.assertIs(got["bld_main"]["eq"], True)
        self.assertIs(got["bld_sub"]["eq"], True)

    def test_한쪽_부재는_일치가_아니라_판정불가다(self):
        """규칙 12 — 부재를 일치로 세지 않는다. 분모 규약은 집계층이 정한다."""
        got = compare_axes(vw_parcel_axes(VW_PARCEL), vw_road_axes(None),
                           ours_axes(OURS))
        self.assertIsNone(got["road_name"]["eq"])
        self.assertIs(got["road_name"]["vw"], False)
        self.assertIs(got["road_name"]["ours"], True)

    def test_양쪽_부재도_일치가_아니다(self):
        ours = {"address": {"structure": {"b_code": "2671025030"}}}
        got = compare_axes(vw_parcel_axes(VW_PARCEL), vw_road_axes(None),
                           ours_axes(ours))
        self.assertIsNone(got["road_name"]["eq"])
        self.assertIs(got["road_name"]["vw"], False)
        self.assertIs(got["road_name"]["ours"], False)


# ── 리 4 지표 ─────────────────────────────────────────────────────────
class TestRiMetrics(unittest.TestCase):
    """계획 §4.2 — 채움률이 아니라 정확도를 잰다. 네 지표를 모두 낸다."""

    ROWS = [
        # 코드·문자열 모두 리 있음, 문자열 일치
        {"ri_code_ours": True, "ri_code_vw": True,
         "ri_fill_ours": True, "ri_fill_vw": True, "ri_name_eq": True},
        # 코드는 리 있음인데 우리 문자열이 빈다 → 우리 쪽 상호 불일치
        {"ri_code_ours": True, "ri_code_vw": True,
         "ri_fill_ours": False, "ri_fill_vw": True, "ri_name_eq": None},
        # 코드상 리 없음, 문자열도 없음 → 상호 일관
        {"ri_code_ours": False, "ri_code_vw": False,
         "ri_fill_ours": False, "ri_fill_vw": False, "ri_name_eq": None},
        # 문자열은 다르다
        {"ri_code_ours": True, "ri_code_vw": True,
         "ri_fill_ours": True, "ri_fill_vw": True, "ri_name_eq": False},
        # 판정 불가(코드 없음)
        {"ri_code_ours": None, "ri_code_vw": None,
         "ri_fill_ours": False, "ri_fill_vw": False, "ri_name_eq": None},
    ]

    def test_칸_생성기가_문자열을_남기지_않는다(self):
        """§3.3 — 판정 레코드에 주소 원문을 싣지 않는다. 리 명칭도 원문이다."""
        cells = ri_cells(vw_parcel_axes(VW_PARCEL), ours_axes(OURS))
        self.assertEqual(sorted(cells),
                         ["ri_code_ours", "ri_code_vw", "ri_fill_ours",
                          "ri_fill_vw", "ri_name_eq"])
        for v in cells.values():
            self.assertIn(type(v), (bool, type(None)))

    def test_칸_생성기는_코드_끝_2_자리로_리_유무를_읽는다(self):
        cells = ri_cells(vw_parcel_axes(VW_PARCEL), ours_axes(OURS))
        self.assertIs(cells["ri_code_ours"], True)   # …5030 → 끝 30 ≠ 00
        self.assertIs(cells["ri_code_vw"], True)
        self.assertIs(cells["ri_fill_vw"], True)     # text 에 "만화리"
        self.assertIs(cells["ri_fill_ours"], False)  # 우리 `ri` 는 null
        self.assertIsNone(cells["ri_name_eq"])       # 한쪽이 비면 판정 불가

    def test_코드_끝_00_은_리_없음이다(self):
        vwp = vw_parcel_axes({"type": "parcel",
                              "structure": {"level4LC": "1111010100"}})
        ours = ours_axes({"address": {"structure": {"b_code": "1111010100"}}})
        cells = ri_cells(vwp, ours)
        self.assertIs(cells["ri_code_ours"], False)
        self.assertIs(cells["ri_code_vw"], False)

    def test_채움률_분모는_전체다(self):
        m = ri_metrics(self.ROWS)
        self.assertEqual(m["n"], 5)
        self.assertEqual(m["fill_ours"], 2)          # 만화리, 가리
        self.assertEqual(m["fill_vw"], 3)

    def test_코드_일치율은_양쪽_코드가_있을_때만_묻는다(self):
        m = ri_metrics(self.ROWS)
        self.assertEqual(m["code_judgeable"], 4)
        self.assertEqual(m["code_eq"], 4)

    def test_문자열_일치율은_양쪽이_채워졌을_때만_묻는다(self):
        m = ri_metrics(self.ROWS)
        self.assertEqual(m["name_judgeable"], 2)     # 만화리/만화리, 가리/나리
        self.assertEqual(m["name_eq"], 1)

    def test_둘_다_빈_것을_일치로_세지_않는다(self):
        m = ri_metrics([{"ri_code_ours": False, "ri_code_vw": False,
                         "ri_fill_ours": False, "ri_fill_vw": False,
                         "ri_name_eq": None}])
        self.assertEqual(m["name_judgeable"], 0)
        self.assertEqual(m["name_eq"], 0)

    def test_코드_문자열_상호_불일치를_양쪽_따로_센다(self):
        m = ri_metrics(self.ROWS)
        self.assertEqual(m["conflict_ours"], 1)      # 코드는 리, 문자열은 빔
        self.assertEqual(m["conflict_vw"], 0)
        self.assertEqual(m["conflict_judgeable"], 4)

    def test_빈_입력에도_0_을_돌려준다(self):
        m = ri_metrics([])
        self.assertEqual(m["n"], 0)
        self.assertEqual(m["code_eq"], 0)


# ── 집계 ─────────────────────────────────────────────────────────────
class TestAggregate(unittest.TestCase):
    ROWS = [
        {"axes": {"sido": {"eq": True}}, "address_source": "pip_key"},
        {"axes": {"sido": {"eq": False}}, "address_source": "pip_key"},
        {"axes": {"sido": {"eq": None}}, "address_source": "knn"},
        {"axes": {"sido": {"eq": True}}, "address_source": "knn"},
    ]

    def test_두_분모를_모두_낸다(self):
        a = aggregate_axis(self.ROWS, "sido")
        self.assertEqual(a["n"], 4)
        self.assertEqual(a["judgeable"], 3)
        self.assertEqual(a["eq"], 2)
        self.assertAlmostEqual(a["rate_judgeable"], 2 / 3.0)
        self.assertAlmostEqual(a["rate_all"], 2 / 4.0)   # 규칙 12 — 부재=불일치

    def test_판정가능_0_이면_비율은_None_이지_0_이_아니다(self):
        a = aggregate_axis([{"axes": {"sido": {"eq": None}}}], "sido")
        self.assertIsNone(a["rate_judgeable"])
        self.assertEqual(a["rate_all"], 0.0)

    def test_address_source_로_쪼갠다(self):
        sp = source_split(self.ROWS)
        self.assertEqual(sorted(sp), ["knn", "pip_key"])
        self.assertEqual(len(sp["knn"]), 2)
        self.assertEqual(aggregate_axis(sp["pip_key"], "sido")["eq"], 1)

    def test_source_가_없으면_None_키로_모은다(self):
        sp = source_split([{"axes": {}, "address_source": None}])
        self.assertEqual(list(sp), [None])


if __name__ == "__main__":
    unittest.main(verbosity=2)
