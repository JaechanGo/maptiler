#!/usr/bin/env python3
"""T046 §7 — EPSG:5179(UTM-K) → WGS84 역투영. **재사용 함수**의 계약 고정.

계획 §4.1·C6: 로직을 새로 쓰지 않는다. `scripts/09-gen-geocode.py` 의 `utmk_to_wgs84`
를 그대로 재사용한다. 우리 DB 좌표와 대조하는 것은 **순환**이므로(그 좌표가 바로 이
함수의 출력이다) 대조 대상은 **PostGIS `ST_Transform`**(독립 PROJ 구현)이다.

실행: /usr/bin/python3 -m unittest discover -s tests/t046 -t . -v
PostGIS 컨테이너(server-postgis-1)가 떠 있어야 한다.
"""
import hashlib
import math
import os
import unittest

import _ctx  # noqa: F401  (sys.path 주입)

import utmk  # noqa: E402
from pgprobe import run_sql  # noqa: E402

# 대조점(EPSG:5179). 원천 레코드가 아니라 한반도 범위 안의 합성 격자점이다.
GRID = [
    (1000000.0, 2000000.0),   # 투영 원점 — 정확히 (127.5, 38.0)
    (958000.0, 1946000.0),    # §4.1 이 교차검증 실측치를 남긴 점
    (1080000.0, 1750000.0),
    (890000.0, 1490000.0),
    (1140000.0, 2080000.0),
]

# 자오선 호장 0 → 38°N. lat_0 를 빠뜨리면 위도가 이만큼 어긋난다(§4.1).
MERIDIAN_ARC_38N_M = 4207498.0


def st_transform(points):
    """PostGIS 로 5179 → 4326 변환. [(lon, lat), …] 를 입력 순서대로 돌려준다."""
    values = ",".join(
        "(%d,%.6f,%.6f)" % (i, e, n) for i, (e, n) in enumerate(points)
    )
    sql = (
        "WITH p(i,e,n) AS (VALUES %s) "
        "SELECT i, ST_X(g), ST_Y(g) FROM ("
        "  SELECT i, ST_Transform(ST_SetSRID(ST_MakePoint(e,n),5179),4326) AS g FROM p"
        ") t ORDER BY i" % values
    )
    out = {}
    for row in run_sql(sql):
        out[int(row[0])] = (float(row[1]), float(row[2]))
    return [out[i] for i in range(len(points))]


class TestReuse(unittest.TestCase):
    """'로직 신규 작성 없음'을 기계로 고정한다(§3.1)."""

    def test_source_path_is_the_builder(self):
        self.assertTrue(utmk.SOURCE_PATH.endswith("scripts/09-gen-geocode.py"))
        self.assertTrue(os.path.exists(utmk.SOURCE_PATH))

    def test_reused_source_is_verbatim_substring_of_builder(self):
        """추출한 소스가 빌더 원문에 **문자 그대로** 들어 있다.

        한 글자라도 손대면 실패한다. 손으로 옮겨 적는 사고를 막는 장치다.
        """
        with open(utmk.SOURCE_PATH, "r", encoding="utf-8") as fh:
            original = fh.read()
        for chunk in utmk.SOURCE_CHUNKS:
            self.assertIn(chunk, original)

    def test_source_digest_is_recorded(self):
        """재사용한 소스의 SHA-256 을 노출한다 — 원본이 바뀌면 리포트에서 드러난다."""
        joined = "\n".join(utmk.SOURCE_CHUNKS)
        self.assertEqual(
            utmk.SOURCE_SHA256, hashlib.sha256(joined.encode("utf-8")).hexdigest()
        )


class TestContract(unittest.TestCase):
    """반환 순서와 반올림 — 뒤집거나 놓치면 전 건이 조용히 오판된다(§4.1)."""

    def test_projection_origin_is_exact(self):
        """(1,000,000, 2,000,000) → 정확히 (127.5, 38.0)."""
        self.assertEqual(utmk.utmk_to_wgs84(1000000.0, 2000000.0), (127.5, 38.0))

    def test_returns_lon_lat_not_lat_lon(self):
        """반환은 (lon, lat) 순서다. 한반도에서 lon > lat 이므로 뒤집힘이 드러난다."""
        lon, lat = utmk.utmk_to_wgs84(958000.0, 1946000.0)
        self.assertGreater(lon, 120.0)
        self.assertLess(lon, 132.0)
        self.assertGreater(lat, 33.0)
        self.assertLess(lat, 39.0)

    def test_rounds_to_six_decimals(self):
        """소수 6 자리 반올림 특성을 고정한다(≈0.11 m, §4.1)."""
        for e, n in GRID:
            lon, lat = utmk.utmk_to_wgs84(e, n)
            self.assertEqual(lon, round(lon, 6), msg=(e, n))
            self.assertEqual(lat, round(lat, 6), msg=(e, n))

    def test_accepts_int_and_str_free(self):
        """E/N 은 float 로 넘긴다. int 입력도 같은 값을 낸다."""
        self.assertEqual(
            utmk.utmk_to_wgs84(1000000, 2000000), utmk.utmk_to_wgs84(1000000.0, 2000000.0)
        )


class TestAgainstPostGIS(unittest.TestCase):
    """독립 PROJ 구현과의 대조. 우리 DB 좌표를 쓰지 않는다(C6)."""

    def test_matches_st_transform_on_grid(self):
        """5 개 대조점에서 PostGIS 값을 6 자리로 반올림한 것과 **완전히** 같다.

        계획 §7 은 '차이 ≤ 1e-4 m' 라고 적었으나 재사용 함수가 소수 6 자리로
        반올림하므로(≈0.11 m) 그 형태로는 성립할 수 없다. 반올림 격자에 스냅된
        값끼리의 **완전 일치**는 그보다 강한 조건이며, 이것이 두 구현의 실질
        동등성을 그대로 검정한다.
        """
        expected = st_transform(GRID)
        for (e, n), (plon, plat) in zip(GRID, expected):
            lon, lat = utmk.utmk_to_wgs84(e, n)
            self.assertEqual(lon, round(plon, 6), msg="E=%r N=%r lon" % (e, n))
            self.assertEqual(lat, round(plat, 6), msg="E=%r N=%r lat" % (e, n))

    def test_raw_difference_is_sub_millimeter(self):
        """반올림을 걷어내고 본 차이는 밀리미터 미만이다.

        위도 1e-6° ≈ 0.111 m 이므로 반올림 자체가 만드는 최대 오차는 0.056 m 다.
        그 절반(5e-7°)을 상한으로 두면 반올림 외의 계통 오차가 없음을 확인할 수 있다.
        """
        expected = st_transform(GRID)
        for (e, n), (plon, plat) in zip(GRID, expected):
            lon, lat = utmk.utmk_to_wgs84(e, n)
            self.assertLessEqual(abs(lon - plon), 5.0e-7, msg="E=%r N=%r lon" % (e, n))
            self.assertLessEqual(abs(lat - plat), 5.0e-7, msg="E=%r N=%r lat" % (e, n))


class TestNegativeLat0(unittest.TestCase):
    """`lat_0=38` 누락을 재현하는 음성 케이스(§4.1)."""

    def test_missing_lat0_shifts_by_meridian_arc(self):
        """`_lat0` 를 0 으로 바꾼 변형본은 위도가 약 4,207 km 어긋난다.

        재사용 소스를 문자열 치환해 변형본을 만든다 — 우리 모듈은 손대지 않는다.
        """
        broken = utmk.compile_variant(
            {"_lat0=math.radians(38.0)": "_lat0=math.radians(0.0)"}
        )
        good_lon, good_lat = utmk.utmk_to_wgs84(1000000.0, 2000000.0)
        bad_lon, bad_lat = broken(1000000.0, 2000000.0)

        self.assertAlmostEqual(good_lon, bad_lon, places=6)  # 경도는 영향 없음
        shift_m = abs(good_lat - bad_lat) * MERIDIAN_ARC_38N_M / 38.0
        self.assertGreater(shift_m, 4.0e6)
        self.assertLess(shift_m, 4.4e6)


if __name__ == "__main__":
    unittest.main(verbosity=2)
