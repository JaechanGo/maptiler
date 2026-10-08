#!/usr/bin/env python3
"""길찾기 소요시간 변화 게이트 — OSRM 이미지·프로필·데이터 교체 전후 비교(CONTRACT C3).

스파이크 검증본(.superpowers/osrm-v26/compare.py)의 OD 생성·질의·요약을 그대로
옮기고(같은 --seed 면 같은 OD 집합 → 스파이크 측정값과 비교 가능) 게이트 판정과
종료코드를 붙였다. 07-gen-route-graph.sh 교체 파이프라인이 이 스크립트로
소요시간 분포가 무너지는 교체(신호 계수 누락·프로필·데이터 이상)를 차단한다.

사용:
  python3 scripts/13k-route-drift.py --a http://127.0.0.1:18080 --b http://127.0.0.1:15001 --profile driving
  python3 scripts/13k-route-drift.py --a http://192.168.102.244:18080 --b http://127.0.0.1:15901 --n 70 --json /tmp/drift.json

종료코드: 0 PASS / 1 FAIL(게이트 위반) / 2 사용법 오류 / 3 기준(A) 사용 불가 —
골든 첫 쌍 A 질의가 연결 자체에 실패(URLError·ConnectionRefused·timeout).
HTTP 400(NoSegment 등)처럼 응답이라도 오면 연결 가능으로 본다.

게이트 기본값 근거 — v26 업그레이드 실측(.244, 2026-10-08): 신호 키 보정본 도심
합계 +1.3%·p10 1.005, 미보정본 합계 -8.1%·p10 0.776. 정상 교체는 합계 +1.3%·
p10 1.005 수준의 미세 변화만 보이므로 도심 합계비 ±5%(--max-dev 0.05)·p10 하한
0.85(--min-p10)는 정상에 넉넉하고, 계수가 빠진 교체(-8.1%·p10 0.776)는 확실히
걸린다. A성공·B실패 허용 2%(--max-bfail)는 회귀 신호의 노이즈 여유.

주의) Python 3.6 호환(.244 측 파이프라인과의 호환성 유지) — 표준 라이브러리만 쓰고
런타임 타입 주석·dataclass·walrus 는 쓰지 않는다.
"""
import argparse, json, math, random, statistics, sys, urllib.error, urllib.request
from concurrent.futures import ThreadPoolExecutor

# ── OD 표본(스파이크 compare.py 에서 그대로 이식 — 같은 seed 같은 표본) ──────
# 도시 상자(lon0, lat0, lon1, lat1) — 도심 위주. 무작위 점은 radiuses=2000 으로 도로에 스냅.
CITIES = {
    "서울":   (126.88, 37.48, 127.10, 37.60),
    "부천인천": (126.68, 37.45, 126.80, 37.53),
    "부산":   (128.98, 35.10, 129.12, 35.22),
    "대구":   (128.53, 35.83, 128.65, 35.90),
    "대전":   (127.35, 36.30, 127.45, 36.38),
    "광주":   (126.84, 35.13, 126.94, 35.19),
    "수원":   (126.97, 37.25, 127.07, 37.31),
}
CENTERS = {   # 도시간 장거리
    "서울시청": (126.980501, 37.566049), "부산시청": (129.075505, 35.178421),
    "대구시청": (128.601763, 35.871435), "대전시청": (127.384548, 36.350461),
    "광주시청": (126.851675, 35.160032), "인천시청": (126.705206, 37.456256),
    "강릉시청": (128.876057, 37.751853), "전주시청": (127.148000, 35.824200),
}
GOLDEN = [   # 13i 골든·보정 근거 경로
    ("춘의역", (126.787199, 37.503730), "상동역", (126.753173, 37.505836)),
    ("부천시청역", (126.763998, 37.504679), "상동역", (126.753173, 37.505836)),
    ("상동로196", (126.755165, 37.514653), "상인초", (126.756456, 37.512641)),
    ("대구반월당역", (128.593410, 35.865509), "대구중앙로역", (128.594226, 35.869957)),
    ("부천시청역", (126.763998, 37.504679), "서울시청", (126.980501, 37.566049)),
    ("서울시청", (126.980501, 37.566049), "부산시청", (129.075505, 35.178421)),
]


def hav(a, b):
    R = 6371000.0
    la1, la2 = math.radians(a[1]), math.radians(b[1])
    d = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin(math.radians(b[0] - a[0]) / 2) ** 2
    return 2 * R * math.asin(math.sqrt(d))


def build_ods(n_urban, seed):
    rnd = random.Random(seed)
    ods = [(f"G:{a}→{b}", pa, pb) for a, pa, b, pb in GOLDEN]
    per = max(1, n_urban // len(CITIES))
    for city, (x0, y0, x1, y1) in CITIES.items():
        k = 0
        while k < per:
            p = (round(rnd.uniform(x0, x1), 6), round(rnd.uniform(y0, y1), 6))
            q = (round(rnd.uniform(x0, x1), 6), round(rnd.uniform(y0, y1), 6))
            if 800 <= hav(p, q) <= 15000:
                ods.append((f"U:{city}#{k}", p, q)); k += 1
    names = list(CENTERS)
    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            if rnd.random() < 0.5:
                ods.append((f"L:{names[i]}→{names[j]}", CENTERS[names[i]], CENTERS[names[j]]))
    return ods


# ── 질의(compare.py 와 동일 — timeout 만 인자화) ─────────────────────────────
def route_url(base, profile, a, b):
    return (f"{base.rstrip('/')}/route/v1/{profile}/{a[0]},{a[1]};{b[0]},{b[1]}"
            "?overview=false&steps=false&radiuses=2000;2000")


def q(base, profile, a, b, timeout=60):
    url = route_url(base, profile, a, b)
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            j = json.load(r)
    except Exception as e:  # noqa: BLE001 — HTTP 400(NoSegment 등)도 실패로 집계
        return None, str(e)[:60]
    if j.get("code") != "Ok":
        return None, j.get("code")
    rt = j["routes"][0]
    return (rt["duration"], rt["distance"]), None


def pct(xs, p):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(round(p / 100 * (len(xs) - 1))))]


def probe_a(base, profile, timeout):
    """기준(A) 사용 불가 판정 — 골든 첫 쌍을 A 에 1회 질의해 '연결 자체'만 확인.

    응답이 오면(HTTP 400 NoSegment 포함) 연결 가능 → True.
    URLError·ConnectionRefused·timeout 처럼 응답 자체가 안 오면 → False.
    B 쪽 연결 실패는 SKIP 사유가 아니라 게이트 FAIL 대상이다(검증 후보니까).
    """
    a, b = GOLDEN[0][1], GOLDEN[0][3]
    try:
        with urllib.request.urlopen(route_url(base, profile, a, b), timeout=timeout) as r:
            json.load(r)
        return True
    except urllib.error.HTTPError:
        return True   # 오류 응답이라도 '응답'이 왔다 = 기준 API 가 살아 있다
    except Exception:
        return False


# ── 게이트 판정(순수 함수 — 네트워크·출력 비의존, 단위테스트 대상) ────────────
def evaluate(rows, fails_b_only, n_total, max_dev, min_p10, max_bfail):
    """도심 시간비 게이트. 반환: (ok, reasons, stats).

    rows          compare.py rows 형식(dict: name, da, db, la, lb, tr, lr).
                  도심(이름 'U:')만 게이트 대상 — 장거리('L:')·골든('G:')은 보고만.
    fails_b_only  'A 성공·B 실패' 목록(회귀 신호). 'B 성공·A 실패'는 게이트 밖
                  (기준선 쪽 시간초과 등 — 실측에서 운영 v5 가 서울→부산 도보를
                  60초 시간초과한 사례가 있었다). 양쪽 모두 실패는 무시.
    n_total       전체 OD 수.
    stats         urban_n·total_ratio·p10·median·bfail_ratio 포함.
    """
    urban = [r for r in rows if r["name"].startswith("U:")]
    stats = dict(n_total=n_total, n_rows=len(rows), urban_n=len(urban),
                 total_ratio=None, p10=None, median=None, p90=None,
                 min=None, max=None,
                 bfail_ratio=(len(fails_b_only) / n_total) if n_total else 0.0)
    if not urban:
        return False, ["도심 비교 0건"], stats

    trs = [r["tr"] for r in urban]
    sum_da = sum(r["da"] for r in urban)
    sum_db = sum(r["db"] for r in urban)
    stats["total_ratio"] = (sum_db / sum_da) if sum_da else 1.0
    stats["p10"] = pct(trs, 10)
    stats["median"] = statistics.median(trs)
    stats["p90"] = pct(trs, 90)
    stats["min"] = min(trs)
    stats["max"] = max(trs)

    reasons = []
    lo, hi = 1.0 - max_dev, 1.0 + max_dev
    if not (lo <= stats["total_ratio"] <= hi):
        reasons.append(f"도심 시간 합계비 {stats['total_ratio']:.3f} — 허용 [{lo:.3f}, {hi:.3f}] 이탈")
    if stats["p10"] < min_p10:
        reasons.append(f"도심 OD 시간비 p10 {stats['p10']:.3f} < 최소 {min_p10:.3f}")
    if stats["bfail_ratio"] > max_bfail:
        reasons.append(f"A성공·B실패 {len(fails_b_only)}건/{n_total}건({stats['bfail_ratio']:.1%}) > 최대 {max_bfail:.1%}")
    return (not reasons), reasons, stats


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="OSRM 소요시간 변화 게이트 — 교체 전후 도심 시간비 분포 비교(CONTRACT C3)")
    ap.add_argument("--a", required=True, help="기준 API 베이스 URL(운영 게이트웨이, 예 http://127.0.0.1:18080)")
    ap.add_argument("--b", required=True, help="후보 API 베이스 URL(임시 검증 컨테이너)")
    ap.add_argument("--profile", default="driving", choices=["driving", "walking", "cycling"],
                    help="A 쪽 라우팅 프로필(기본 driving)")
    ap.add_argument("--b-profile", default="", help="B 쪽 URL 프로필 문자열(기본=--profile)")
    ap.add_argument("--n", type=int, default=280, help="도심 OD 목표 건수(기본 280)")
    ap.add_argument("--seed", type=int, default=20261008, help="OD 표본 seed(기본 20261008)")
    ap.add_argument("--max-dev", type=float, default=0.05, help="도심 시간 합계비 허용 편차(기본 0.05)")
    ap.add_argument("--min-p10", type=float, default=0.85, help="도심 OD 시간비 p10 하한(기본 0.85)")
    ap.add_argument("--max-bfail", type=float, default=0.02, help="A성공·B실패 허용 비율(기본 0.02)")
    ap.add_argument("--timeout", type=int, default=60, help="1건 질의 타임아웃 초(기본 60)")
    ap.add_argument("--json", default="", help="rows·fails·stats·ok JSON 저장 경로")
    args = ap.parse_args(argv)
    bp = args.b_profile or args.profile

    # 기준(A) 사용 불가면 측정 자체가 불가 — 골든 첫 쌍 1회 질의로만 판정하고 빠진다.
    if not probe_a(args.a, args.profile, args.timeout):
        print(f"RESULT: SKIP 기준 API 연결 불가 {args.a}")
        sys.exit(3)

    ods = build_ods(args.n, args.seed)

    def one(od):
        name, p, r = od
        ra, ea = q(args.a, args.profile, p, r, args.timeout)
        rb, eb = q(args.b, bp, p, r, args.timeout)
        return name, ra, rb, ea, eb

    with ThreadPoolExecutor(8) as ex:
        res = list(ex.map(one, ods))

    rows, fails_b_only, fails_a_only = [], [], []
    for name, ra, rb, ea, eb in res:
        if ra is None and rb is None:
            continue                                   # 양쪽 모두 실패 — 무시
        if ra is not None and rb is not None:
            rows.append(dict(name=name, da=ra[0], db=rb[0], la=ra[1], lb=rb[1],
                             tr=rb[0] / ra[0] if ra[0] else 1.0,
                             lr=rb[1] / ra[1] if ra[1] else 1.0))
        elif rb is None:
            fails_b_only.append((name, ea, eb))         # A 성공·B 실패 — 회귀 신호(게이트 대상)
        else:
            fails_a_only.append((name, ea, eb))         # B 성공·A 실패 — 보고만(기준선 시간초과 등)

    ok, reasons, stats = evaluate(rows, fails_b_only, len(ods),
                                  args.max_dev, args.min_p10, args.max_bfail)

    # 요약 — compare.py 와 같은 블록. 도심은 게이트·나머지는 보고용 분포.
    def summ(sel, label):
        if not sel:
            return
        tr = [r["tr"] for r in sel]
        same = sum(1 for r in sel if abs(r["lr"] - 1) < 0.01)
        print(f"  {label:8s} n={len(sel):3d}  시간비 B/A 중앙 {statistics.median(tr):.3f}"
              f"  p10 {pct(tr,10):.3f}  p90 {pct(tr,90):.3f}  min {min(tr):.3f}  max {max(tr):.3f}"
              f"  | 합계 {sum(r['db'] for r in sel)/sum(r['da'] for r in sel):.3f}"
              f"  | 동일경로(거리±1%) {same}/{len(sel)}")

    print(f"profile={args.profile}  OD {len(ods)}건  비교 {len(rows)}"
          f"  A성공·B실패 {len(fails_b_only)}  B성공·A실패 {len(fails_a_only)}")
    summ(rows, "전체")
    summ([r for r in rows if r["name"].startswith("U:")], "도심")
    summ([r for r in rows if r["name"].startswith("L:")], "장거리")
    print("  골든:")
    for r in rows:
        if r["name"].startswith("G:"):
            print(f"    {r['name']:28s} A {r['da']/60:6.1f}분 {r['la']/1000:7.2f}km  →  B {r['db']/60:6.1f}분 {r['lb']/1000:7.2f}km  ({r['tr']:.3f})")
    out = sorted(rows, key=lambda r: abs(math.log(r["tr"])) if r["tr"] > 0 else 9, reverse=True)[:6]
    print("  편차 상위:")
    for r in out:
        print(f"    {r['name']:28s} A {r['da']/60:6.1f}분 {r['la']/1000:6.2f}km  B {r['db']/60:6.1f}분 {r['lb']/1000:6.2f}km  ({r['tr']:.3f})")
    for name, ea, eb in fails_b_only[:10]:
        print(f"  ! A성공·B실패 {name}  A={ea}  B={eb}")
    for name, ea, eb in fails_a_only[:10]:
        print(f"  ! B성공·A실패 {name}(보고만)  A={ea}  B={eb}")

    if args.json:
        fails = [dict(name=n, side="B", err_a=ea, err_b=eb) for n, ea, eb in fails_b_only]
        fails += [dict(name=n, side="A", err_a=ea, err_b=eb) for n, ea, eb in fails_a_only]
        with open(args.json, "w") as fh:
            json.dump(dict(rows=rows, fails=fails, stats=stats, ok=ok), fh, ensure_ascii=False)

    if ok:
        print("RESULT: PASS")
        sys.exit(0)
    print("RESULT: FAIL " + "; ".join(reasons))
    sys.exit(1)


if __name__ == "__main__":
    main()
