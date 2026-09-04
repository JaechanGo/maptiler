#!/usr/bin/env python3
"""T046 §7 — 측지선 거리(Vincenty 역해 + haversine 폴백) 단위 테스트.

stdlib unittest 만 사용(pytest 미설치). 실행:
    /usr/bin/python3 -m unittest discover -s tests/t046 -t . -v

기지값의 출처는 **PostGIS `ST_Distance(::geography)`** 다(GeographicLib/Karney 독립 구현).
우리 구현과 계보가 다르므로 순환이 아니다. 산출 SQL 은 각 상수 옆에 적어 둔다.
계획 §4.1: Vincenty 오차 특성은 타원체 상 0.5 mm 이내여야 한다.
"""
import math
import unittest

import _ctx  # noqa: F401  (sys.path 주입)

from geodist import (  # noqa: E402
    distance_lonlat,
    distance_m,
    haversine_m,
    vincenty_m,
)

# ── PostGIS 실측 기지값 ────────────────────────────────────────────────
# docker exec server-postgis-1 psql -U cuvia -d cuvia -tAF'|' -c "
#   SELECT ST_Distance('SRID=4326;POINT(<lon1> <lat1>)'::geography,
#                      'SRID=4326;POINT(<lon2> <lat2>)'::geography)"
EQ_1DEG = 111319.49079327          # (0,0) → (1,0). 계획 §7 의 111,319.4908
SEOUL_BUSAN = 324915.29745313      # 서울시청(126.9780,37.5665) → 부산시청(129.0756,35.1796)
POLAR_89 = 157954.968632           # (0,89) → (90,89)
MM4 = 0.00397778                   # §1.6 실측 4 mm 쌍
MERIDIAN_38 = 4207498.01915441     # (127.5,0) → (127.5,38). §4.1 lat_0 누락 시 어긋나는 양

SEOUL = (37.5665, 126.9780)        # (lat, lon)
BUSAN = (35.1796, 129.0756)

# §1.6 실측: 경도는 소수 17 자리까지 동일, 위도만 8 번째 자리가 다르다.
OURS_SACHEON = (36.64784810411072, 126.61326952208216)
VWORLD_SACHEON = (36.64784813995594, 126.61326952208216)


class TestVincenty(unittest.TestCase):
    """Vincenty 역해의 정밀도·계약."""

    def test_identical_point_is_zero(self):
        """동일점은 정확히 0.0 이다(NaN·미수렴이 아니다)."""
        self.assertEqual(distance_m(37.5665, 126.9780, 37.5665, 126.9780), 0.0)
        self.assertEqual(distance_m(0.0, 0.0, 0.0, 0.0), 0.0)

    def test_equator_one_degree(self):
        """적도 1° = 111,319.4908 m. 계획 §7 이 요구한 허용오차 ±0.5 mm."""
        d = distance_m(0.0, 0.0, 0.0, 1.0)
        self.assertAlmostEqual(d, EQ_1DEG, delta=5e-4)

    def test_seoul_busan(self):
        """중거리(325 km) 대조. 타원체 상 0.5 mm 이내."""
        d = distance_m(SEOUL[0], SEOUL[1], BUSAN[0], BUSAN[1])
        self.assertAlmostEqual(d, SEOUL_BUSAN, delta=5e-4)

    def test_high_latitude(self):
        """극단 위도(89°N)에서도 수렴한다."""
        d = distance_m(89.0, 0.0, 89.0, 90.0)
        self.assertAlmostEqual(d, POLAR_89, delta=1e-3)

    def test_four_millimeter_pair(self):
        """§1.6 실측 쌍이 0.004 m 근처로 나온다 — 미세거리에서 자릿수가 무너지지 않는다."""
        d = distance_m(
            OURS_SACHEON[0], OURS_SACHEON[1], VWORLD_SACHEON[0], VWORLD_SACHEON[1]
        )
        self.assertAlmostEqual(d, MM4, delta=1e-5)
        self.assertLess(d, 0.01)
        self.assertGreater(d, 0.001)

    def test_meridian_arc_to_38n(self):
        """자오선 호장 0→38°N = 4,207,498.0 m(§4.1 lat_0=38 누락 시 어긋나는 양)."""
        d = distance_m(0.0, 127.5, 38.0, 127.5)
        self.assertAlmostEqual(d, MERIDIAN_38, delta=1e-3)

    def test_symmetry(self):
        """d(a,b) == d(b,a). 부동소수 오차 없이 완전히 같아야 한다."""
        ab = distance_m(SEOUL[0], SEOUL[1], BUSAN[0], BUSAN[1])
        ba = distance_m(BUSAN[0], BUSAN[1], SEOUL[0], SEOUL[1])
        self.assertEqual(ab, ba)

    def test_vincenty_returns_none_on_antipode(self):
        """대척점은 Vincenty 가 수렴하지 않는다 — None 을 돌려 폴백을 알린다."""
        self.assertIsNone(vincenty_m(0.0, 0.0, 0.0, 180.0))


class TestHaversineFallback(unittest.TestCase):
    """폴백 경로. 계획 §4.1 — 한반도 위도대에서 최대 약 0.3 %."""

    def test_fallback_within_0_3_percent(self):
        """한반도 내 대표 구간에서 haversine 이 Vincenty 와 0.3 % 이내."""
        pairs = [
            (SEOUL[0], SEOUL[1], BUSAN[0], BUSAN[1]),
            (37.5665, 126.9780, 33.4996, 126.5312),   # 서울 → 제주
            (35.1796, 129.0756, 37.4563, 126.7052),   # 부산 → 인천
            (36.6478, 126.6133, 36.6500, 126.6200),   # 근거리 600 m 급
        ]
        for lat1, lon1, lat2, lon2 in pairs:
            v = vincenty_m(lat1, lon1, lat2, lon2)
            h = haversine_m(lat1, lon1, lat2, lon2)
            self.assertIsNotNone(v)
            self.assertLessEqual(abs(h - v) / v, 0.003, msg=(lat1, lon1, lat2, lon2))

    def test_haversine_radius_is_mean_earth_radius(self):
        """R = 6,371,008.8 m 고정(§4.1). 적도 1° 로 역산해 확인한다."""
        h = haversine_m(0.0, 0.0, 0.0, 1.0)
        self.assertAlmostEqual(h, 6371008.8 * math.radians(1.0), delta=1e-6)

    def test_distance_m_falls_back_and_counts(self):
        """미수렴 시 distance_m 은 haversine 으로 폴백하고 그 사실을 계수한다.

        계획 §4.1: 한반도 내에서 폴백은 0 건이어야 하며 0 이 아니면 버그 신호다.
        따라서 폴백 발동 건수를 조회할 수 있어야 한다.
        """
        import geodist

        before = geodist.fallback_count()
        d = distance_m(0.0, 0.0, 0.0, 180.0)
        after = geodist.fallback_count()
        self.assertEqual(after, before + 1)
        self.assertGreater(d, 20000000.0)

        # 정상 구간은 카운터를 올리지 않는다.
        distance_m(SEOUL[0], SEOUL[1], BUSAN[0], BUSAN[1])
        self.assertEqual(geodist.fallback_count(), after)


class TestArgumentOrder(unittest.TestCase):
    """인자 순서 고정 — 뒤집으면 전 건이 조용히 오판된다(§4.1)."""

    def test_distance_m_is_lat_lon(self):
        """distance_m 은 (lat, lon) 순이다.

        위경도를 뒤집으면 서울–부산이 전혀 다른 값이 된다는 사실로 순서를 고정한다.
        """
        right = distance_m(SEOUL[0], SEOUL[1], BUSAN[0], BUSAN[1])
        swapped = distance_m(SEOUL[1], SEOUL[0], BUSAN[1], BUSAN[0])
        self.assertAlmostEqual(right, SEOUL_BUSAN, delta=5e-4)
        self.assertGreater(abs(swapped - right), 1000.0)

    def test_distance_lonlat_takes_lon_lat_tuples(self):
        """distance_lonlat 은 (lon, lat) 튜플 2 개를 받는다 — utmk_to_wgs84 출력과 직결."""
        d = distance_lonlat((SEOUL[1], SEOUL[0]), (BUSAN[1], BUSAN[0]))
        self.assertAlmostEqual(d, SEOUL_BUSAN, delta=5e-4)


if __name__ == "__main__":
    unittest.main(verbosity=2)
