#!/usr/bin/env python3
"""길찾기(OSRM) 회귀 QC — 프로필 3종·진입로 정제 안전성·옵션·엣지케이스·성능. FEAT-007/ADR-009.

13-qc-check.py 와 같은 계약: FAIL 하나라도 있으면 비0 종료(파이프라인 차단), WARN 만이면 0.
데모(demo/js/routing.js)가 쓰는 것과 같은 게이트웨이 엔드포인트만 두드리므로,
그래프 교체·프로필 계수 변경·컨테이너 재시작 뒤 이 스크립트만 돌리면 회귀를 잡을 수 있다.

사용:
  python3 13i-route-qc.py --api http://192.168.102.244:18080
  python3 13i-route-qc.py --api http://localhost:8080 --quick   # 성능·장거리 생략
"""
import argparse, json, math, statistics, sys, time, urllib.error, urllib.parse, urllib.request

R = []
ICON = {"PASS": "✓", "WARN": "△", "FAIL": "✗"}
def rec(sev, name, detail=""):
    R.append((sev, name, detail))
    print(f"  {ICON[sev]} [{sev}] {name}" + (f" — {detail}" if detail else ""), flush=True)
    return sev

API = ""
TIMEOUT = 90

# ── 골든 지점(WGS84) — geocode 응답에서 확보한 실좌표. 재현성을 위해 하드코딩한다.
#    지오코더가 바뀌어도 라우팅 회귀만 보게 하려는 의도(지오코딩 회귀는 13d/13f 담당).
P = {
    "춘의역":        (126.787199, 37.503730),
    "상동역":        (126.753173, 37.505836),
    "부천시청역":    (126.763998, 37.504679),
    "서울시청":      (126.980501, 37.566049),
    "부산시청":      (129.075505, 35.178421),
    "대구반월당역":  (128.593410, 35.865509),
    "대구중앙로역":  (128.594226, 35.869957),
    "제주시청":      (126.531252, 33.499780),
    "제주버스터미널": (126.514007, 33.499416),
    "밀양상동역":    (128.760404, 35.555718),
    "상동로196":     (126.755165, 37.514653),
    "상인초등학교":  (126.756456, 37.512641),
    "한솔영어교습소": (126.754451, 37.495806),
    # 도로에서 먼 지점(진입로 정제 대상) — 공원 안 공중화장실. 차량 스냅 50.8m
    "공원화장실":    (126.751158, 37.496268),
}

PROFILES = ["driving", "walking", "cycling"]   # --profiles 로 좁힐 수 있다(로컬 단일 그래프 검증용)
# (출발, 도착) — 근거리 도심·중거리·타 지역. 프로필 3종 공통.
PAIRS = [
    ("춘의역", "상동역"),
    ("부천시청역", "상동역"),
    ("상동로196", "상인초등학교"),
    ("대구반월당역", "대구중앙로역"),
    ("제주시청", "제주버스터미널"),
]


def get(path, timeout=None):
    url = API.rstrip("/") + path
    with urllib.request.urlopen(url, timeout=timeout or TIMEOUT) as r:
        return json.load(r)


def coords(*names):
    return ";".join(f"{P[n][0]},{P[n][1]}" for n in names)


def route(profile, cs, extra=""):
    return get(f"/route/v1/{profile}/{cs}?overview=false{extra}")


def haversine_m(a, b):
    r = 6371000.0
    dlat = math.radians(b[1] - a[1]); dlon = math.radians(b[0] - a[0])
    x = math.sin(dlat / 2) ** 2 + math.cos(math.radians(a[1])) * math.cos(math.radians(b[1])) * math.sin(dlon / 2) ** 2
    return 2 * r * math.asin(math.sqrt(x))


# ── A. 프로필 3종 기본 동작 ────────────────────────────────────────
def check_profiles():
    print("\nA. 프로필 3종 기본 경로")
    for prof in PROFILES:
        ok = fail = 0
        for s, e in PAIRS:
            try:
                d = route(prof, coords(s, e))
                r = (d.get("routes") or [None])[0]
                if d.get("code") != "Ok" or not r:
                    rec("FAIL", f"{prof} {s}→{e}", f"code={d.get('code')}"); fail += 1; continue
                straight = haversine_m(P[s], P[e])
                # 우회율 — 직선 대비 경로거리. 5배 넘으면 그래프 연결 이상 신호.
                ratio = r["distance"] / straight if straight > 0 else 0
                if r["distance"] <= 0 or r["duration"] <= 0:
                    rec("FAIL", f"{prof} {s}→{e}", "거리/시간 0"); fail += 1
                elif ratio > 5:
                    rec("FAIL", f"{prof} {s}→{e}", f"우회율 {ratio:.1f}배(직선 {straight:.0f}m → 경로 {r['distance']:.0f}m)"); fail += 1
                else:
                    ok += 1
            except Exception as ex:
                rec("FAIL", f"{prof} {s}→{e}", f"{type(ex).__name__}: {ex}"); fail += 1
        if not fail:
            rec("PASS", f"{prof} 경로 {ok}/{len(PAIRS)}건", "code=Ok·우회율 정상")


# ── B. 프로필 간 소요시간 정합 ────────────────────────────────────
def check_profile_order():
    print("\nB. 프로필 간 정합 (차량 ≤ 자전거 ≤ 도보)")
    if len(PROFILES) < 3:
        rec("WARN", "프로필 정합 생략", f"검사 대상 {PROFILES} — 3종 모두 필요")
        return
    bad = []
    for s, e in PAIRS:
        try:
            t = {}
            for prof in PROFILES:
                d = route(prof, coords(s, e))
                t[prof] = (d["routes"][0]["duration"], d["routes"][0]["distance"])
            dv, cy, wk = t["driving"][0], t["cycling"][0], t["walking"][0]
            if not (dv <= cy <= wk):
                bad.append(f"{s}→{e} 차량{dv/60:.1f}·자전거{cy/60:.1f}·도보{wk/60:.1f}분")
        except Exception as ex:
            bad.append(f"{s}→{e} {type(ex).__name__}")
    if bad:
        # 근거리에선 도보가 지름길을 써 자전거보다 빠를 수 있어 WARN(설계상 허용).
        rec("WARN", "프로필 시간 역전", "; ".join(bad))
    else:
        rec("PASS", "차량 ≤ 자전거 ≤ 도보", f"{len(PAIRS)}쌍")


# ── C. 진입로 정제 안전성 (핵심) ──────────────────────────────────
#    OSRM 기본 스냅은 '직선거리 최단'이라 막다른 길에 붙으면 실주행이 돌아간다.
#    데모는 총 이동시간(주행 + 스냅지점→목적지 도보환산)이 최소인 진입점을 고른다.
#    여기서 같은 계산을 재현해 '정제가 기본보다 나빠지는 케이스가 없음'을 확인한다.
SNAP_REFINE_M = 25
WALK_M_PER_MIN = 75


def refine(profile, target, origin):
    """데모 betterSnap 재현 → (선택 후보, 후보목록) 또는 (None, 후보목록)"""
    nr = get(f"/nearest/v1/{profile}/{target[0]},{target[1]}?number=8")
    seen, cands = set(), []
    for w in nr.get("waypoints", []):
        k = tuple(w["location"])
        if k in seen: continue
        seen.add(k); cands.append(w)
    if len(cands) < 2 or cands[0]["distance"] < SNAP_REFINE_M:
        return None, cands
    cs = ";".join([f"{origin[0]},{origin[1]}"] + [f"{c['location'][0]},{c['location'][1]}" for c in cands])
    dests = ";".join(str(i + 1) for i in range(len(cands)))
    tb = get(f"/table/v1/{profile}/{cs}?sources=0&destinations={dests}")
    durs = (tb.get("durations") or [[]])[0]
    best, bestcost = None, float("inf")
    for c, sec in zip(cands, durs):
        if sec is None: continue
        cost = sec / 60 + c["distance"] / WALK_M_PER_MIN
        if cost < bestcost: bestcost, best = cost, c
    return best, cands


def check_snap_refine():
    print("\nC. 진입로 정제 안전성")
    cases = [("한솔영어교습소", "공원화장실"), ("상동역", "공원화장실"), ("부천시청역", "공원화장실")]
    fired = 0
    for s, e in cases:
        origin, target = P[s], P[e]
        for prof in PROFILES:
            try:
                best, cands = refine(prof, target, origin)
                if best is None:
                    continue   # 미발동(도로 가까움) — 정상
                fired += 1
                base = cands[0]
                base_r = route(prof, f"{origin[0]},{origin[1]};{base['location'][0]},{base['location'][1]}")["routes"][0]
                best_r = route(prof, f"{origin[0]},{origin[1]};{best['location'][0]},{best['location'][1]}")["routes"][0]
                base_cost = base_r["duration"] / 60 + base["distance"] / WALK_M_PER_MIN
                best_cost = best_r["duration"] / 60 + best["distance"] / WALK_M_PER_MIN
                if best_cost > base_cost + 1e-6:
                    rec("FAIL", f"정제 악화 {prof} {s}→{e}",
                        f"기본 {base_cost:.2f}분 → 정제 {best_cost:.2f}분")
                else:
                    gain = base_cost - best_cost
                    rec("PASS", f"정제 {prof} {s}→{e}",
                        f"{base_r['distance']:.0f}m/{base_cost:.2f}분 → {best_r['distance']:.0f}m/{best_cost:.2f}분 (-{gain:.2f}분)")
            except Exception as ex:
                rec("FAIL", f"정제 {prof} {s}→{e}", f"{type(ex).__name__}: {ex}")
    if fired == 0:
        rec("WARN", "정제 발동 0건", "골든 케이스가 더 이상 임계(25m)를 넘지 않음 — 표본 갱신 필요")


# ── D. 일반 지점은 정제 미발동(추가 질의 없음) ───────────────────
def check_no_refine_for_normal():
    print("\nD. 일반 지점 정제 미발동")
    bad = []
    for n in ("춘의역", "상동역", "부천시청역", "상동로196", "서울시청"):
        try:
            d = get(f"/nearest/v1/driving/{P[n][0]},{P[n][1]}?number=1")
            dist = d["waypoints"][0]["distance"]
            if dist >= SNAP_REFINE_M:
                bad.append(f"{n} {dist:.1f}m")
        except Exception as ex:
            bad.append(f"{n} {type(ex).__name__}")
    if bad:
        rec("WARN", "일반 지점이 정제 임계 초과", "; ".join(bad) + " — 질의 수 증가(동작은 정상)")
    else:
        rec("PASS", "일반 지점 도로 근접", "정제 미발동 → 질의 1회 유지")


# ── E. 대안 경로·회피 옵션 ────────────────────────────────────────
def check_options():
    print("\nE. 대안 경로·회피 옵션")
    try:
        d = route("driving", coords("춘의역", "상동역"), "&alternatives=true")
        n = len(d.get("routes") or [])
        rec("PASS" if n >= 1 else "FAIL", "대안 경로", f"{n}개 반환")
    except Exception as ex:
        rec("FAIL", "대안 경로", f"{type(ex).__name__}: {ex}")

    for prof, cls, pair in (("driving", "toll", ("서울시청", "부산시청")),
                            ("driving", "motorway", ("서울시청", "부산시청")),
                            ("walking", "steps", ("춘의역", "상동역"))):
        if prof not in PROFILES:
            continue
        try:
            d = route(prof, coords(*pair), f"&exclude={cls}")
            r = (d.get("routes") or [None])[0]
            if d.get("code") == "Ok" and r:
                rec("PASS", f"exclude={cls} ({prof})", f"{r['duration']/60:.0f}분 {r['distance']/1000:.1f}km")
            else:
                rec("FAIL", f"exclude={cls} ({prof})", f"code={d.get('code')}")
        except Exception as ex:
            rec("FAIL", f"exclude={cls} ({prof})", f"{type(ex).__name__}: {ex}")

    # 조합은 프로필 excludable 이 단일 집합만 선언 → 400 이 '정상'. UI 는 배타 선택으로 막는다.
    try:
        route("driving", coords("서울시청", "부산시청"), "&exclude=toll,motorway")
        rec("WARN", "exclude 조합 허용됨", "UI 배타 선택 제약을 완화할 수 있음(excludable 확장됨)")
    except urllib.error.HTTPError as e:
        rec("PASS" if e.code == 400 else "WARN", "exclude 조합 차단", f"HTTP {e.code}(기대 400)")
    except Exception as ex:
        rec("WARN", "exclude 조합", f"{type(ex).__name__}")


# ── F. 경유지 ─────────────────────────────────────────────────────
def check_waypoints():
    print("\nF. 경유지")
    try:
        cs = coords("춘의역", "부천시청역", "상동역")
        d = route("driving", cs)
        r = d["routes"][0]
        legs = len(r["legs"])
        direct = route("driving", coords("춘의역", "상동역"))["routes"][0]
        # 경유지가 직행 경로선 위에 거의 놓여 있으면 두 거리가 사실상 같다(부천시청역이 그런 위치).
        # 미세한 역전은 경로 분할에 따른 계산 차이일 뿐이므로 직행의 1%(최소 50m)까지 허용한다.
        tol = max(50.0, direct["distance"] * 0.01)
        if legs != 2:
            rec("FAIL", "경유지 leg 수", f"{legs}개(기대 2)")
        elif r["distance"] < direct["distance"] - tol:
            rec("FAIL", "경유 경로가 직행보다 짧음", f"{r['distance']:.0f}m < {direct['distance']:.0f}m (허용 {tol:.0f}m)")
        else:
            rec("PASS", "경유지 2구간", f"경유 {r['distance']:.0f}m · 직행 {direct['distance']:.0f}m")
    except Exception as ex:
        rec("FAIL", "경유지", f"{type(ex).__name__}: {ex}")


# ── G. 엣지 케이스 ────────────────────────────────────────────────
def check_edges():
    print("\nG. 엣지 케이스")
    # 동일 지점
    try:
        d = route("driving", coords("상동역", "상동역"))
        r = (d.get("routes") or [None])[0]
        ok = d.get("code") == "Ok" and r and r["distance"] < 50
        rec("PASS" if ok else "WARN", "동일 지점", f"code={d.get('code')} dist={r['distance'] if r else '-'}")
    except Exception as ex:
        rec("WARN", "동일 지점", f"{type(ex).__name__}")
    # 해상 좌표 — 데모는 radiuses(2km)를 붙여 질의하므로 거부되는 게 정상.
    # radiuses 없이 부르면 OSRM 이 12~29km 떨어진 섬 도로로 스냅한다(실측: 가거도길·태하길).
    try:
        d = get("/route/v1/driving/125.000000,34.000000;126.000000,34.500000?overview=false&radiuses=2000;2000")
        rec("WARN", "해상 좌표(radiuses 2km)", f"code={d.get('code')} — 거부 기대")
    except urllib.error.HTTPError as e:
        body = ""
        try: body = json.loads(e.read().decode()).get("code", "")
        except Exception: pass
        rec("PASS" if e.code == 400 else "WARN", "해상 좌표 거부", f"HTTP {e.code} {body}")
    except Exception as ex:
        rec("WARN", "해상 좌표", f"{type(ex).__name__}")
    # radiuses 가 정상 지점을 막지 않는지(공원 안 50m 스냅)
    try:
        o, t = P["한솔영어교습소"], P["공원화장실"]
        d = get(f"/route/v1/driving/{o[0]},{o[1]};{t[0]},{t[1]}?overview=false&radiuses=2000;2000")
        ok = d.get("code") == "Ok"
        rec("PASS" if ok else "FAIL", "radiuses 정상지점 통과", f"code={d.get('code')}")
    except Exception as ex:
        rec("FAIL", "radiuses 정상지점", f"{type(ex).__name__}: {ex}")
    # 동명이역(밀양 상동역) — 부천과 혼동 없이 장거리로 나와야
    try:
        d = route("driving", coords("춘의역", "밀양상동역"))
        r = d["routes"][0]
        ok = r["distance"] > 200000
        rec("PASS" if ok else "FAIL", "동명이역 구분", f"{r['distance']/1000:.0f}km(기대 >200km)")
    except Exception as ex:
        rec("FAIL", "동명이역", f"{type(ex).__name__}")


# ── H. 성능 ───────────────────────────────────────────────────────
def check_perf():
    print("\nH. 응답 성능")
    lat = []
    for _ in range(3):
        for s, e in PAIRS:
            t0 = time.time()
            try:
                route("driving", coords(s, e))
                lat.append((time.time() - t0) * 1000)
            except Exception:
                pass
    if not lat:
        rec("FAIL", "성능 측정", "표본 0")
        return
    p50 = statistics.median(lat); p95 = sorted(lat)[int(len(lat) * 0.95) - 1]
    sev = "PASS" if p95 < 1000 else ("WARN" if p95 < 3000 else "FAIL")
    rec(sev, "근거리 응답", f"p50 {p50:.0f}ms · p95 {p95:.0f}ms (n={len(lat)})")
    # 장거리 1회
    t0 = time.time()
    try:
        r = route("driving", coords("서울시청", "부산시청"))["routes"][0]
        ms = (time.time() - t0) * 1000
        sev = "PASS" if ms < 5000 else "WARN"
        rec(sev, "장거리(서울→부산)", f"{ms:.0f}ms · {r['duration']/3600:.2f}시간 {r['distance']/1000:.0f}km")
    except Exception as ex:
        rec("FAIL", "장거리", f"{type(ex).__name__}")


def main():
    global API, TIMEOUT
    ap = argparse.ArgumentParser()
    ap.add_argument("--api", required=True, help="게이트웨이 베이스 URL (예: http://192.168.102.244:18080)")
    ap.add_argument("--timeout", type=int, default=90)
    ap.add_argument("--quick", action="store_true", help="성능·장거리 생략")
    ap.add_argument("--profiles", default="", help="검사할 프로필(쉼표) — 로컬 단일 그래프 검증 시 예: driving")
    a = ap.parse_args()
    API, TIMEOUT = a.api, a.timeout
    if a.profiles:
        # osrm-routed 는 URL 의 프로필 문자열을 무시하고 자기 그래프로 답한다 → 단일 그래프를
        # 직접 띄워 검증할 때는 해당 프로필 하나만 돌려야 결과가 의미를 갖는다.
        PROFILES[:] = [p.strip() for p in a.profiles.split(",") if p.strip()]

    print(f"길찾기 QC — {API}")
    check_profiles()
    check_profile_order()
    check_snap_refine()
    check_no_refine_for_normal()
    check_options()
    check_waypoints()
    check_edges()
    if not a.quick:
        check_perf()

    n = {"PASS": 0, "WARN": 0, "FAIL": 0}
    for sev, _, _ in R: n[sev] += 1
    print(f"\n결과: PASS {n['PASS']} · WARN {n['WARN']} · FAIL {n['FAIL']}")
    if n["FAIL"]:
        print("FAIL 항목:")
        for sev, name, detail in R:
            if sev == "FAIL": print(f"  ✗ {name} — {detail}")
    sys.exit(1 if n["FAIL"] else 0)


if __name__ == "__main__":
    main()
