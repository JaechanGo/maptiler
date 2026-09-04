#!/usr/bin/env python3
"""T046 F1·F4 — 역방향 재측정 경로 단위 테스트. 네트워크를 타지 않는다.

검수 F4 는 "B 단계가 `measure.py` +119 행을 고치며 테스트를 0 개 추가했다"를
지적했다. 역방향 경로가 HTTP 와 얽혀 있어 시험할 수 없던 것이 원인이므로,
호출부(`CallResult`)와 좌표 유도부를 가짜로 갈아끼워 전부 시험한다.

실행: /usr/bin/python3 -m unittest discover -s tests/t046 -t tests/t046 -v
"""
import json
import unittest

import _ctx  # noqa: F401  (sys.path 주입)

import measure  # noqa: E402


def _res(entries, status="OK"):
    return {"response": {"status": status, "result": list(entries)}}


PARCEL = {
    "type": "parcel", "text": "부산광역시 기장군 기장읍 만화리 210-1",
    "structure": {"level1": "부산광역시", "level2": "기장군", "level3": "",
                  "level4L": "기장읍", "level4LC": "2671025030",
                  "level5": "210-1"},
}
ROAD = {
    "type": "road", "text": "부산광역시 기장군 기장읍 동서길 100-2",
    "structure": {"level3": "기장읍", "level4L": "동서길",
                  "level4LC": "4220076", "level5": "100-2"},
}
OURS_BODY = {
    "address": {"structure": {
        "sido": "부산광역시", "sigungu": "기장군", "emd": "기장읍", "ri": None,
        "san": False, "road_name": "동서길", "main_no": 100, "sub_no": 2,
        "ji_main": 210, "ji_sub": 1, "b_code": "2671025030"}},
    "address_source": "pip_key",
    "nearest": [{"dist_m": 4.25}],
}


def ok(body):
    return measure.CallResult(True, "OK", 1, None, body)


def fail(status="NOT_FOUND", body=None):
    """`status != "OK"` 는 HTTP 200 이어도 실패다(태스크 §6)."""
    return measure.CallResult(False, status, 1, None, body)


class REC(dict):
    pass


def rec(sid=1, layer="jibun", **kw):
    r = REC({"sid": sid, "layer": layer, "stratum": "busan|rural|" + layer,
             "urban": "rural", "query": "질의"})
    r.update(kw)
    return r


# ── URL ──────────────────────────────────────────────────────────────
class TestReverseUrl(unittest.TestCase):
    def test_기본이_type_both_이다(self):
        """A 단계는 `type=PARCEL` 고정이라 도로명 축을 받지 못했다."""
        url = measure.vworld_reverse_url("KEY", 129.2, 35.2)
        self.assertIn("type=both", url)
        self.assertIn("request=getAddress", url)
        self.assertIn("crs=EPSG%3A4326", url)

    def test_좌표는_경도_위도_순이다(self):
        url = measure.vworld_reverse_url("KEY", 129.2, 35.2)
        self.assertIn("point=129.2000000%2C35.2000000", url)

    def test_로그에_키가_드러나지_않는다(self):
        url = measure.vworld_reverse_url("SECRET", 129.2, 35.2)
        self.assertNotIn("SECRET", measure.mask_key(url))
        self.assertIn("key=***", measure.mask_key(url))


# ── 항목 선택 ─────────────────────────────────────────────────────────
class TestGateE1Direction(unittest.TestCase):
    """F8 — E1 은 **순방향** 게이트다. 역방향 실패를 더하면 안 된다."""

    def test_역방향_실패는_E1_이_아니다(self):
        c = measure.Counters()
        c.note_status("vworld", "fwd", "NOT_FOUND")
        c.note_status("vworld", "rev", "NOT_FOUND")
        c.note_status("vworld", "fwd", "OK")
        c.note_status("ours", "fwd", "HTTP_503")
        self.assertEqual(c.gate_e1(), 1)

    def test_순방향_실패만_모아_센다(self):
        c = measure.Counters()
        for st in ("NOT_FOUND", "TIMEOUT", "BAD_BODY"):
            c.note_status("vworld", "fwd", st)
        self.assertEqual(c.gate_e1(), 3)


class TestParseReverse(unittest.TestCase):
    def test_도로명이_먼저_와도_지번을_고른다(self):
        lc, lv5, txt = measure.parse_vworld_reverse(_res([ROAD, PARCEL]))
        self.assertEqual(lc, "2671025030")      # 도로명코드 4220076 이 아니다
        self.assertEqual(lv5, "210-1")
        self.assertIn("만화리", txt)

    def test_도로명만_오면_지번_축은_비운다(self):
        self.assertEqual(measure.parse_vworld_reverse(_res([ROAD])),
                         (None, None, None))

    def test_빈_응답도_죽지_않는다(self):
        for body in (None, {}, _res([], status="NOT_FOUND")):
            self.assertEqual(measure.parse_vworld_reverse(body),
                             (None, None, None))


# ── 질의 좌표 기준 ────────────────────────────────────────────────────
class FakeOracle:
    def __init__(self, points):
        self.points = dict(points)
        self.asked = []

    def repr_point_batch(self, keys, legacy=True):
        self.asked.append(dict(keys))
        return {k: self.points[v] for k, v in keys.items() if v in self.points}


class FakePool:
    @staticmethod
    def map(fn, items):
        return [fn(i) for i in items]


class TestChunkPoints(unittest.TestCase):
    """좌표를 못 만든 건을 **조용히 빼지 않는다.** 사유와 함께 남긴다."""

    PNU = "2671025030" "1" "0210" "0001"

    def test_지번은_필지_대표점을_쓴다(self):
        orc = FakeOracle({self.PNU: (129.2, 35.2)})
        pts, skip = measure.chunk_points(
            [rec(1, "jibun", pnu=self.PNU)], "src", orc, None, FakePool)
        self.assertEqual(pts, {1: (129.2, 35.2)})
        self.assertEqual(skip, {})

    def test_필지가_없으면_사유를_남긴다(self):
        orc = FakeOracle({})
        pts, skip = measure.chunk_points(
            [rec(1, "jibun", pnu=self.PNU)], "src", orc, None, FakePool)
        self.assertEqual(pts, {})
        self.assertIn("대표점 부재", skip[1])

    def test_표본에_PNU_가_없으면_사유가_다르다(self):
        orc = FakeOracle({})
        _pts, skip = measure.chunk_points([rec(1, "jibun")], "src", orc,
                                          None, FakePool)
        self.assertIn("PNU 없음", skip[1])

    def test_도로명은_원천_좌표를_쓴다(self):
        pts, skip = measure.chunk_points(
            [rec(2, "road", anchors=[(129.1, 35.1), (129.3, 35.3)])],
            "src", FakeOracle({}), None, FakePool)
        self.assertEqual(pts, {2: (129.1, 35.1)})   # 출입구 우선
        self.assertEqual(skip, {})

    def test_원천_좌표가_없으면_사유를_남긴다(self):
        _pts, skip = measure.chunk_points([rec(2, "road", anchors=[])],
                                          "src", FakeOracle({}), None, FakePool)
        self.assertIn("원천 좌표 없음", skip[2])

    def test_fwd_기준은_우리_순방향_top1_이다(self):
        class F:
            @staticmethod
            def forward_point(r):
                return (129.9, 35.9) if r["sid"] == 1 else None

        pts, skip = measure.chunk_points([rec(1), rec(2)], "fwd",
                                         FakeOracle({}), F, FakePool)
        self.assertEqual(pts, {1: (129.9, 35.9)})
        self.assertIn("주소 후보를 내지 않음", skip[2])

    def test_fwd_기준은_오라클을_부르지_않는다(self):
        orc = FakeOracle({})

        class F:
            @staticmethod
            def forward_point(r):
                return (1.0, 2.0)

        measure.chunk_points([rec(1, "jibun", pnu=self.PNU)], "fwd", orc,
                             F, FakePool)
        self.assertEqual(orc.asked, [])


# ── 판정 행 ──────────────────────────────────────────────────────────
class TestRev2Row(unittest.TestCase):
    def test_필드_집합이_고정이다(self):
        row = measure.rev2_row(rec(1, "road"), "src",
                               ok(_res([PARCEL, ROAD])), ok(OURS_BODY))
        self.assertEqual(sorted(row), sorted(measure.REV2_FIELDS))

    def test_좌표도_주소_문자열도_남기지_않는다(self):
        """§3.3 — 판정 레코드에서 좌표와 원문은 떨어져 나간다."""
        row = measure.rev2_row(rec(1, "road"), "src",
                               ok(_res([PARCEL, ROAD])), ok(OURS_BODY))
        blob = json.dumps(row, ensure_ascii=False)
        for leak in ("만화리", "동서길", "기장읍", "129.", "35.2", "210-1"):
            self.assertNotIn(leak, blob, leak)

    def test_양쪽이_같으면_축이_참이다(self):
        row = measure.rev2_row(rec(1, "road"), "src",
                               ok(_res([PARCEL, ROAD])), ok(OURS_BODY))
        for axis in ("sido", "sgg", "emd", "ri_code", "jibun_main",
                     "jibun_sub", "san", "road_name", "bld_main", "bld_sub"):
            self.assertIs(row["axes"][axis]["eq"], True, axis)
        self.assertTrue(row["vw_has_parcel"])
        self.assertTrue(row["vw_has_road"])
        self.assertEqual(row["address_source"], "pip_key")
        self.assertEqual(row["rev_dist_m"], 4.25)

    def test_도로명_항목이_없으면_도로명_축은_판정불가다(self):
        row = measure.rev2_row(rec(1, "jibun"), "src",
                               ok(_res([PARCEL])), ok(OURS_BODY))
        self.assertFalse(row["vw_has_road"])
        self.assertIsNone(row["axes"]["road_name"]["eq"])
        self.assertIsNone(row["axes"]["bld_main"]["eq"])
        self.assertIs(row["axes"]["jibun_main"]["eq"], True)

    def test_status_가_OK_가_아니면_전_축이_판정불가다(self):
        """부재·오류를 성공으로 계산하지 않는다."""
        row = measure.rev2_row(rec(1), "src",
                               fail("NOT_FOUND", _res([], "NOT_FOUND")),
                               ok(OURS_BODY))
        self.assertEqual(row["rev_v_status"], "NOT_FOUND")
        self.assertFalse(row["vw_has_parcel"])
        for axis in measure.REV2_FIELDS and row["axes"]:
            self.assertIsNone(row["axes"][axis]["eq"], axis)

    def test_우리_서버_실패도_성공으로_세지_않는다(self):
        row = measure.rev2_row(rec(1), "src", ok(_res([PARCEL])),
                               fail("HTTP_502"))
        self.assertFalse(row["ours_ok"])
        self.assertIsNone(row["address_source"])
        self.assertIsNone(row["rev_dist_m"])
        self.assertIsNone(row["axes"]["jibun_main"]["eq"])

    def test_기준_좌표가_없으면_호출_없이_사유만_남는다(self):
        row = measure.rev2_row(rec(1), "src", None, None, skip="원천 좌표 없음")
        self.assertFalse(row["basis_ok"])
        self.assertEqual(row["skip"], "원천 좌표 없음")
        self.assertIsNone(row["rev_v_status"])
        self.assertFalse(row["ours_ok"])

    def test_리_칸을_함께_낸다(self):
        row = measure.rev2_row(rec(1), "src", ok(_res([PARCEL])), ok(OURS_BODY))
        self.assertIs(row["ri"]["ri_code_vw"], True)
        self.assertIs(row["ri"]["ri_fill_vw"], True)
        self.assertIs(row["ri"]["ri_fill_ours"], False)
        self.assertIsNone(row["ri"]["ri_name_eq"])

    def test_기준_이름을_그대로_싣는다(self):
        for basis in measure.REV2_BASES:
            row = measure.rev2_row(rec(1), basis, ok(_res([PARCEL])),
                                   ok(OURS_BODY))
            self.assertEqual(row["basis"], basis)


if __name__ == "__main__":
    unittest.main(verbosity=2)
