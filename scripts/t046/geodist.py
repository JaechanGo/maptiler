#!/usr/bin/env python3
"""T046 §4.1 — 측지선 거리. Vincenty 역해 + haversine 폴백.

순수 함수 모듈이다. 외부 호출·파일 입출력·전역 상태 변경이 없다(폴백 계수기 제외).

## 타원체 선택

기지값의 출처가 PostGIS `ST_Distance(::geography)` 이고 그쪽이 **WGS84** 를 쓰므로
여기서도 WGS84(`a=6378137.0`, `1/f=298.257223563`)를 쓴다.

`utmk.py` 가 쓰는 EPSG:5179 정의는 GRS80(`1/f=298.257222101`)이다. 편평률이
소수 9 자리에서 갈리지만 한반도 규모 거리로 환산하면 나노미터 급이라 §7 이 요구한
0.5 mm 허용오차에 영향이 없다. **다른 목적의 다른 상수**이므로 통일하지 않는다.

## 폴백

Vincenty 역해는 대척점 근처에서 수렴하지 않는다. 한반도 내 좌표쌍은 최대 이격이
1,000 km 미만이라 폴백이 발동할 수 없다 — 그러므로 `fallback_count()` 가 0 이
아니면 좌표 파싱이 깨졌다는 신호다(§4.1). 리포트에 이 값을 싣는다.
"""
import math
import threading

__all__ = [
    "distance_m",
    "distance_lonlat",
    "vincenty_m",
    "haversine_m",
    "fallback_count",
    "reset_fallback_count",
    "A_AXIS",
    "F_FLAT",
    "B_AXIS",
    "R_MEAN",
    "MAX_ITER",
    "TOL",
]

# ── WGS84 타원체 ──────────────────────────────────────────────────────
A_AXIS = 6378137.0
F_FLAT = 1.0 / 298.257223563
B_AXIS = (1.0 - F_FLAT) * A_AXIS

# haversine 폴백용 평균 반경(IUGG). §4.1 이 못박은 값.
R_MEAN = 6371008.8

# §4.1 — 상대오차 1e-12, 최대 200 회.
TOL = 1e-12
MAX_ITER = 200

_fallbacks = 0
_lock = threading.Lock()


def fallback_count():
    """haversine 폴백이 발동한 누적 건수."""
    with _lock:
        return _fallbacks


def reset_fallback_count():
    """측정 창 시작 시 0 으로 되돌린다."""
    global _fallbacks
    with _lock:
        _fallbacks = 0


def _bump_fallback():
    global _fallbacks
    with _lock:
        _fallbacks += 1


def _ordered(lat1, lon1, lat2, lon2):
    """두 점을 사전순으로 고정한다.

    거리는 대칭 함수지만 Vincenty 의 부동소수 연산 경로는 인자 순서에 따라
    달라져 최하위 비트가 갈릴 수 있다. 입력을 정규화해 `d(a,b) == d(b,a)` 를
    **비트 단위로** 보장한다. 의미론적으로도 옳은 정규화다.
    """
    if (lat1, lon1) > (lat2, lon2):
        return lat2, lon2, lat1, lon1
    return lat1, lon1, lat2, lon2


def vincenty_m(lat1, lon1, lat2, lon2):
    """Vincenty 역해. 수렴하면 미터, 미수렴이면 `None`.

    인자는 **(위도, 경도)** 순이다. 뒤집으면 전 건이 조용히 오판된다.
    """
    lat1, lon1, lat2, lon2 = _ordered(lat1, lon1, lat2, lon2)

    L = math.radians(lon2 - lon1)
    U1 = math.atan((1.0 - F_FLAT) * math.tan(math.radians(lat1)))
    U2 = math.atan((1.0 - F_FLAT) * math.tan(math.radians(lat2)))
    sinU1, cosU1 = math.sin(U1), math.cos(U1)
    sinU2, cosU2 = math.sin(U2), math.cos(U2)

    lam = L
    sinSigma = cosSigma = sigma = cos2Alpha = cos2SigmaM = 0.0
    converged = False

    for _ in range(MAX_ITER):
        sinLam, cosLam = math.sin(lam), math.cos(lam)
        sinSigma = math.hypot(cosU2 * sinLam,
                              cosU1 * sinU2 - sinU1 * cosU2 * cosLam)
        cosSigma = sinU1 * sinU2 + cosU1 * cosU2 * cosLam
        if sinSigma == 0.0:
            # 동일점(cosσ>0)과 정확한 대척점(cosσ<0)이 여기서 갈린다.
            return 0.0 if cosSigma > 0.0 else None
        sigma = math.atan2(sinSigma, cosSigma)
        sinAlpha = cosU1 * cosU2 * sinLam / sinSigma
        cos2Alpha = 1.0 - sinAlpha * sinAlpha
        if cos2Alpha == 0.0:
            cos2SigmaM = 0.0            # 적도선 — 2σm 이 정의되지 않는다
        else:
            cos2SigmaM = cosSigma - 2.0 * sinU1 * sinU2 / cos2Alpha
        C = F_FLAT / 16.0 * cos2Alpha * (4.0 + F_FLAT * (4.0 - 3.0 * cos2Alpha))
        lam_prev = lam
        lam = L + (1.0 - C) * F_FLAT * sinAlpha * (
            sigma + C * sinSigma * (
                cos2SigmaM + C * cosSigma * (-1.0 + 2.0 * cos2SigmaM ** 2)))
        if abs(lam - lam_prev) < TOL:
            converged = True
            break

    if not converged:
        return None

    u2 = cos2Alpha * (A_AXIS ** 2 - B_AXIS ** 2) / (B_AXIS ** 2)
    A = 1.0 + u2 / 16384.0 * (
        4096.0 + u2 * (-768.0 + u2 * (320.0 - 175.0 * u2)))
    B = u2 / 1024.0 * (256.0 + u2 * (-128.0 + u2 * (74.0 - 47.0 * u2)))
    d_sigma = B * sinSigma * (
        cos2SigmaM + B / 4.0 * (
            cosSigma * (-1.0 + 2.0 * cos2SigmaM ** 2)
            - B / 6.0 * cos2SigmaM * (-3.0 + 4.0 * sinSigma ** 2)
            * (-3.0 + 4.0 * cos2SigmaM ** 2)))

    return B_AXIS * A * (sigma - d_sigma)


def haversine_m(lat1, lon1, lat2, lon2):
    """구면 근사. 한반도 위도대에서 Vincenty 대비 최대 약 0.3 %(§4.1)."""
    lat1, lon1, lat2, lon2 = _ordered(lat1, lon1, lat2, lon2)
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2.0) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2.0) ** 2
    return R_MEAN * 2.0 * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))


def distance_m(lat1, lon1, lat2, lon2):
    """측지선 거리(m). 인자는 **(위도, 경도)** 순.

    Vincenty 가 미수렴하면 haversine 으로 폴백하고 `fallback_count()` 를 올린다.
    """
    d = vincenty_m(lat1, lon1, lat2, lon2)
    if d is None:
        _bump_fallback()
        return haversine_m(lat1, lon1, lat2, lon2)
    return d


def distance_lonlat(p1, p2):
    """`(lon, lat)` 튜플 2 개를 받는다 — `utmk_to_wgs84` 출력 순서와 직결(§4.1)."""
    return distance_m(p1[1], p1[0], p2[1], p2[0])
