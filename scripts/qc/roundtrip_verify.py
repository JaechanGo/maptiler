#!/usr/bin/env python3
"""역지오코딩→지오코딩 왕복 정합 — 임의 좌표의 /reverse 주소를 /geocode 로 되찾아 거리·행정구역 일치를 본다.
사용: roundtrip_verify.py http://host:18080 [n]"""
import json, math, random, sys, time, urllib.parse, urllib.request, urllib.error

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://192.168.102.244:18080").rstrip("/")
N = int(sys.argv[2]) if len(sys.argv) > 2 else 24
random.seed(20260903)
# 도심·교외·농촌 혼합 표본 중심점(lon, lat, 반경 km)
CENTERS = [(126.766, 37.503, 3, "부천"), (126.978, 37.566, 4, "서울 도심"), (127.028, 37.498, 3, "강남"),
           (129.075, 35.180, 4, "부산"), (127.385, 36.350, 4, "대전"), (126.853, 35.160, 4, "광주"),
           (126.531, 33.499, 3, "제주"), (127.730, 37.881, 3, "춘천"), (128.601, 35.871, 3, "대구"),
           (126.705, 37.456, 3, "인천"), (127.147, 35.824, 3, "전주"), (127.286, 36.480, 4, "세종"),
           (127.489, 36.635, 3, "청주"), (128.678, 35.228, 3, "창원"), (127.113, 37.323, 3, "용인 농촌")]

def get(path, params):
    url = f"{BASE}{path}?{urllib.parse.urlencode(params)}"
    t = time.time()
    try:
        with urllib.request.urlopen(url, timeout=25) as r:
            return r.status, json.loads(r.read().decode()), time.time() - t
    except urllib.error.HTTPError as e:
        return e.code, None, time.time() - t
    except Exception as e:
        return -1, None, time.time() - t

def dist_m(a, b):
    dx = (a[0]-b[0]) * 111320 * math.cos(math.radians((a[1]+b[1])/2)); dy = (a[1]-b[1]) * 110540
    return math.hypot(dx, dy)

rows = []
for i in range(N):
    lon0, lat0, rk, label = CENTERS[i % len(CENTERS)]
    lon = lon0 + random.uniform(-rk, rk) / 88.8; lat = lat0 + random.uniform(-rk, rk) / 111.0
    c1, rv, t1 = get("/reverse", {"lon": lon, "lat": lat})
    if c1 != 200 or not rv:
        rows.append((label, "reverse-HTTP", c1, None, None, t1)); continue
    addr = rv.get("address") or {}
    road = addr.get("road"); parcel = addr.get("parcel")
    q = road or parcel
    if not q:
        rows.append((label, "reverse-빈주소", 200, None, None, t1)); continue
    # 역지오 결과의 대표 좌표(있으면) — 없으면 질의점 기준
    rlon, rlat = (rv.get("lon") or lon), (rv.get("lat") or lat)
    c2, gj, t2 = get("/geocode", {"q": q, "limit": 1})
    if c2 != 200 or not gj or not gj.get("results"):
        rows.append((label, "geocode-실패", c2, q, None, t2)); continue
    g = gj["results"][0]
    d = dist_m((g["lon"], g["lat"]), (lon, lat))
    gs = ((g.get("address") or {}).get("structure") or {})
    rs = (addr.get("structure") or {})
    same_sgg = (gs.get("sigungu") == rs.get("sigungu")) if (gs.get("sigungu") and rs.get("sigungu")) else None
    rows.append((label, "ok", 200, q, d, t1 + t2, g.get("kind"), same_sgg))

ok = [r for r in rows if r[1] == "ok"]
print(f"표본 {N} · 왕복 성공 {len(ok)} · 실패 {N-len(ok)}")
for r in rows:
    if r[1] != "ok":
        print(f"  ✗ {r[0]:8s} {r[1]} http={r[2]} q={r[3]}")
ds = sorted(r[4] for r in ok)
if ds:
    print(f"  왕복 거리: 중앙값 {ds[len(ds)//2]:.0f}m · p90 {ds[int(len(ds)*0.9)-1]:.0f}m · max {ds[-1]:.0f}m")
far = [r for r in ok if r[4] > 300]
for r in far:
    print(f"  ⚠ {r[0]:8s} {r[4]:.0f}m  q={r[3]}  kind={r[6]} sgg일치={r[7]}")
mis = [r for r in ok if r[7] is False]
print(f"  시군구 불일치 {len(mis)}건" + ("" if not mis else ": " + "; ".join(f"{r[0]}:{r[3]}" for r in mis[:5])))
ts = sorted(r[5] for r in ok)
if ts:
    print(f"  왕복 소요: 중앙값 {ts[len(ts)//2]*1000:.0f}ms · max {ts[-1]*1000:.0f}ms")

_gate = (len(ok) >= 0.95 * N) and (len(mis) == 0)
print("GATE:", "PASS" if _gate else "FAIL")
sys.exit(0 if _gate else 1)
