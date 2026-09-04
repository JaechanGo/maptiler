#!/usr/bin/env python3
"""역지오코딩 정답 QC (빌드 자산: scripts/qc) — PostGIS address(주소점, 도로명+지번+행정구역 보유)에서 뽑은 표본 좌표로 /reverse 를 호출해
동일 도로명·지번·행정구역이 나오는지, 그리고 그 도로명을 /geocode 로 되찾으면 표본 좌표로 돌아오는지 본다.
사용: reverse_truth_qc.py http://host:18080 sample.csv"""
import csv, json, math, sys, time, urllib.parse, urllib.request, urllib.error

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://localhost:18080").rstrip("/")
CSV = sys.argv[2] if len(sys.argv) > 2 else None
N = int(sys.argv[3]) if len(sys.argv) > 3 else 60


def sample_from_postgis(n):
    """표본 CSV 가 없으면 빌드 호스트의 postgis 컨테이너에서 무작위 주소점 n 개를 뽑는다(TABLESAMPLE 0.5% 중 random)."""
    import subprocess, tempfile, os
    pgc = os.environ.get("PG_CONTAINER", "server-postgis-1")
    sql = ("COPY (SELECT road, main_no, sub_no, jibun, bld, sido, sigungu, emd, ri, ST_X(geom) lon, ST_Y(geom) lat "
           "FROM address TABLESAMPLE SYSTEM (0.5) WHERE kind='addr' AND road IS NOT NULL AND main_no IS NOT NULL AND geom IS NOT NULL "
           f"ORDER BY random() LIMIT {n}) TO STDOUT WITH (FORMAT csv, HEADER);")
    r = subprocess.run(["docker", "exec", "-i", pgc, "psql", "-U", os.environ.get("PGUSER", "cuvia"), "-d", os.environ.get("PGDATABASE", "cuvia"),
                        "-q", "-c", sql], capture_output=True, text=True, timeout=300)
    if r.returncode != 0 or not r.stdout.strip():
        sys.exit(f"표본 추출 실패(postgis 컨테이너 {pgc}): {r.stderr[:200]}")
    fd, path = tempfile.mkstemp(suffix=".csv"); os.close(fd)
    open(path, "w", encoding="utf-8").write(r.stdout)
    return path


if not CSV:
    CSV = sample_from_postgis(N)

def get(path, params):
    url = f"{BASE}{path}?{urllib.parse.urlencode(params)}"
    t = time.time()
    try:
        with urllib.request.urlopen(url, timeout=25) as r:
            return r.status, json.loads(r.read().decode()), time.time() - t
    except urllib.error.HTTPError as e:
        return e.code, None, time.time() - t
    except Exception:
        return -1, None, time.time() - t

def dist_m(a, b):
    dx = (a[0]-b[0]) * 111320 * math.cos(math.radians((a[1]+b[1])/2)); dy = (a[1]-b[1]) * 110540
    return math.hypot(dx, dy)

rows = list(csv.DictReader(open(CSV, encoding="utf-8")))
bad = []; t_rev = []; t_fwd = []; n_ok_road = n_ok_parcel = n_ok_region = n_ok_fwd = 0
for r in rows:
    lon, lat = float(r["lon"]), float(r["lat"])
    want_road = f"{r['road']} {r['main_no']}" + (f"-{r['sub_no']}" if r["sub_no"] not in ("", "0") else "")
    want_jibun = r["jibun"]
    c, j, dt = get("/reverse", {"lon": lon, "lat": lat}); t_rev.append(dt)
    if c != 200 or not j:
        bad.append(("reverse-HTTP", want_road, c)); continue
    a = j.get("address") or {}; st = a.get("structure") or {}
    road = a.get("road") or ""; parcel = a.get("parcel") or ""
    ok_road = want_road in road
    ok_parcel = (want_jibun.split()[-1] in parcel) if want_jibun else True
    ok_region = (st.get("sido") == r["sido"] and (st.get("sigungu") or "") == r["sigungu"] and (st.get("emd") or "") == r["emd"]
                 and ((st.get("ri") or "") == (r["ri"] or "")))
    n_ok_road += ok_road; n_ok_parcel += ok_parcel; n_ok_region += ok_region
    if not (ok_road and ok_parcel and ok_region):
        bad.append(("reverse", f"{r['sido']} {r['sigungu']} {r['emd']} {r['ri']} {want_road} / {want_jibun}",
                    f"→ {st.get('sido')} {st.get('sigungu')} {st.get('emd')} {st.get('ri') or ''} | {road[:40]} / {parcel[:30]}"))
    # 정방향 되찾기
    q = f"{r['sido']} {r['sigungu']} {want_road}"
    c2, g, dt2 = get("/geocode", {"q": q, "limit": 1}); t_fwd.append(dt2)
    top = ((g or {}).get("results") or [None])[0]
    if c2 == 200 and top:
        d = dist_m((top["lon"], top["lat"]), (lon, lat))
        if d <= 50: n_ok_fwd += 1
        else: bad.append(("forward", q, f"{d:.0f}m 떨어진 {top.get('name')}"))
    else:
        bad.append(("forward", q, f"HTTP{c2} 결과없음"))

n = len(rows)
print(f"표본 {n}건 (시도 {len(set(r['sido'] for r in rows))}종)")
print(f"  역방향 도로명 일치 {n_ok_road}/{n} · 지번 일치 {n_ok_parcel}/{n} · 시도·시군구·읍면동·리 일치 {n_ok_region}/{n}")
print(f"  정방향 되찾기(≤50m) {n_ok_fwd}/{n}")
for lst, name in ((t_rev, "역방향"), (t_fwd, "정방향")):
    lst = sorted(lst); print(f"  {name} 지연 p50 {lst[len(lst)//2]*1000:.0f}ms p95 {lst[int(len(lst)*.95)-1]*1000:.0f}ms max {lst[-1]*1000:.0f}ms")
print(f"  불일치 {len(bad)}건")
for b in bad[:20]:
    print("   ✗", " | ".join(str(x) for x in b))

# 게이트: 도로명·행정구역 일치 ≥ 90%, 정방향 되찾기 ≥ 95% (지번은 필지 PIP 우선 설계라 참고치)
thr_ok = (n_ok_road >= 0.9 * n) and (n_ok_region >= 0.9 * n) and (n_ok_fwd >= 0.95 * n)
print("GATE:", "PASS" if thr_ok else "FAIL")
sys.exit(0 if thr_ok else 1)
