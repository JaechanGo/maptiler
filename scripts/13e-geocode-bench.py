#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""지오코더 성능 벤치 — /geocode·/reverse 의 질의 지연(latency)을 질의클래스별로 측정.

배경: 13d-geocode-parity.py 는 두 백엔드의 '정확성 parity'만 본다(결과가 같은가). 이 스크립트는
  보완재로 '얼마나 빠른가'를 측정한다 — Phase 5c(PostGIS 전환) 의 성능 회귀/개선을 수치로 판정.
  지번 검색경로(geocode-api-pg.py: search_text ILIKE '%동%' + 번지 ILIKE + region ILIKE) 신규
  추가에 따른 지연 영향이 핵심 관심사라, 질의를 '클래스'(jibun/road/name/poi/admin/reverse)로
  나눠 클래스별 p50/p95/p99 를 따로 낸다(전체 평균은 지번 회귀를 숨길 수 있음).

측정법: 클래스별로 같은 질의를 --repeat 회 반복(워밍업 --warmup 회는 통계 제외), 벽시계 지연 수집.
  단일 클라이언트 순차 호출(동시성 없음) — 절대 처리량(QPS)이 아닌 '질의당 응답시간' 기준선용.
  부하/동시성 측정이 필요하면 --concurrency 로 스레드 N개 병렬(처리량·꼬리지연 동시 관측).

사용:
  python3 13e-geocode-bench.py                                  # 기본 :8082, 내장 질의셋, repeat=20
  python3 13e-geocode-bench.py --base http://localhost:8092     # PostGIS 백엔드
  python3 13e-geocode-bench.py --queries q.csv                  # CSV: 'q'(검색어) + 선택 'class','lon','lat'
  python3 13e-geocode-bench.py --a :8082 --b :8092              # A·B 동시측정 + Δ(전환 회귀 게이트)
  python3 13e-geocode-bench.py --slo-p95 200 --slo-class jibun  # 지번 p95 ≤ 200ms 면 exit 0
  python3 13e-geocode-bench.py --selftest                       # 네트워크 없이 백분위/통계 검증

판정: --slo-p95 지정 시 (해당 클래스의 p95 ≤ SLO) AND 오류 0 → exit 0, 아니면 1. A·B 모드는
  대상(B)의 p95 가 기준(A) 대비 --max-regress 배 이내여야 통과(기본 1.5 = 50% 회귀까지 허용).
의존: 표준 라이브러리만(urllib·threading) — 폐쇄망/무의존 일관(13d 와 동일 정책).
"""
import argparse, csv, json, statistics, sys, threading, time
import urllib.parse, urllib.request
from collections import defaultdict

# 내장 질의셋 — 클래스별 골고루. 지번(jibun)을 두텁게(본번/부번/산/법정동중복) 깔아 신규 경로를 집중 관측.
DEFAULT_QUERIES = [
    # (class, query)
    ("jibun", "역삼동 736"),            # 본번만
    ("jibun", "역삼동 736-12"),         # 본번-부번
    ("jibun", "삼성동 159"),
    ("jibun", "중앙동 1"),              # 법정동 전국중복(좁히는 지역토큰 없음 — worst case)
    ("jibun", "강남구 역삼동 736-12"),  # 지역토큰으로 좁힘(best case)
    ("jibun", "안성시 산120"),          # 산(임야) 번지
    ("jibun", "종로1가 1"),             # 'N가' 토큰
    ("road",  "서울특별시 종로구 세종대로 175"),
    ("road",  "경기도 수원시 영통구 광교로 145"),
    ("road",  "대전광역시 유성구 대학로 99"),
    ("road",  "테헤란로 152"),          # 도로명+본번(지역 미지정)
    ("name",  "강남역"), ("name", "서울역"), ("name", "부산역"),
    ("poi",   "경복궁"), ("poi", "롯데월드타워"), ("poi", "인천국제공항"),
    ("admin", "제주특별자치도 제주시"), ("admin", "광주광역시 북구"),
]


def http_get(base, path, params, timeout=15):
    """API GET → (dict, latency_ms, err)."""
    url = base.rstrip("/") + path + "?" + urllib.parse.urlencode(params)
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            body = json.loads(r.read().decode("utf-8"))
        return body, (time.perf_counter() - t0) * 1000.0, None
    except Exception as e:  # noqa: BLE001 — 네트워크/JSON 오류 전부 실패로 집계
        return None, (time.perf_counter() - t0) * 1000.0, f"{type(e).__name__}: {e}"


def pct(xs, p):
    """정렬 표본의 p 백분위(0~100) — 선형보간(numpy 미의존, 빈 표본 0.0)."""
    if not xs:
        return 0.0
    s = sorted(xs)
    if len(s) == 1:
        return s[0]
    rank = (p / 100.0) * (len(s) - 1)
    lo = int(rank); hi = min(lo + 1, len(s) - 1)
    frac = rank - lo
    return s[lo] + (s[hi] - s[lo]) * frac


def summarize(samples):
    """지연 표본 리스트 → 통계 dict."""
    return {
        "n": len(samples),
        "min": min(samples) if samples else 0.0,
        "p50": pct(samples, 50), "p95": pct(samples, 95), "p99": pct(samples, 99),
        "max": max(samples) if samples else 0.0,
        "mean": statistics.fmean(samples) if samples else 0.0,
    }


def _norm_base(b):
    """':8082' / 'host:8082' 같은 약식도 허용 → 완전 URL."""
    if not b:
        return b
    if b.startswith(("http://", "https://")):
        return b
    if b.startswith(":"):
        return "http://localhost" + b
    return "http://" + b


def run_one(base, cls, q, lon, lat, limit, repeat, warmup, results, errors, lock):
    """한 질의를 warmup+repeat 회 호출 → 통계 제외분 빼고 results[cls] 에 지연 누적."""
    is_rev = (cls == "reverse")
    path = "/reverse" if is_rev else "/geocode"
    params = ({"lon": lon, "lat": lat, "limit": limit} if is_rev
              else {"q": q, "limit": limit})
    local = []
    for i in range(warmup + repeat):
        _, ms, err = http_get(base, path, params)
        if err:
            with lock:
                errors.append((cls, q if not is_rev else f"{lon},{lat}", err))
            continue
        if i >= warmup:
            local.append(ms)
    with lock:
        results[cls].extend(local)


def bench(base, queries, limit, repeat, warmup, concurrency):
    """질의셋 전체를 측정 → {class: [latency_ms,...]}, [errors]."""
    results = defaultdict(list); errors = []; lock = threading.Lock()
    tasks = [(cls, q, lon, lat) for (cls, q, lon, lat) in queries]
    if concurrency <= 1:
        for cls, q, lon, lat in tasks:
            run_one(base, cls, q, lon, lat, limit, repeat, warmup, results, errors, lock)
    else:
        sem = threading.Semaphore(concurrency); threads = []
        def worker(args):
            with sem:
                run_one(base, *args, limit, repeat, warmup, results, errors, lock)
        for t in tasks:
            th = threading.Thread(target=worker, args=(t,)); th.start(); threads.append(th)
        for th in threads:
            th.join()
    return results, errors


def load_queries(path):
    """CSV('q' + 선택 'class','lon','lat') → [(class,q,lon,lat)]. lon/lat 있으면 reverse 로도 추가."""
    out = []
    for row in csv.DictReader(open(path, encoding="utf-8-sig")):
        q = (row.get("q") or row.get("query") or "").strip()
        cls = (row.get("class") or "").strip() or _guess_class(q)
        lon, lat = row.get("lon"), row.get("lat")
        if q:
            out.append((cls, q, None, None))
        if lon and lat:
            try:
                out.append(("reverse", q, float(lon), float(lat)))
            except ValueError:
                pass
    return out


def _guess_class(q):
    """클래스 미지정 질의의 단순 추정(라벨 없을 때만)."""
    import re
    if re.search(r"(로|길)\s*\d", q):
        return "road"
    if re.search(r"(동|리)\s*\d", q) or re.search(r"\d가\s*\d", q):
        return "jibun"
    if re.search(r"(시|군|구)$", q.strip()):
        return "admin"
    return "name"


def print_table(title, stats_by_class):
    print(f"\n[{title}]")
    print(f"  {'class':<9} {'n':>5} {'p50':>8} {'p95':>8} {'p99':>8} {'max':>8} {'mean':>8}  (ms)")
    order = ["jibun", "road", "name", "poi", "admin", "reverse"]
    keys = [k for k in order if k in stats_by_class] + [k for k in stats_by_class if k not in order]
    alls = []
    for k in keys:
        s = stats_by_class[k]; alls_src = s.pop("_raw", None)
        print(f"  {k:<9} {s['n']:>5} {s['p50']:>8.1f} {s['p95']:>8.1f} {s['p99']:>8.1f} "
              f"{s['max']:>8.1f} {s['mean']:>8.1f}")


def stats_with_raw(results):
    out = {}
    for cls, samples in results.items():
        s = summarize(samples); s["_raw"] = samples; out[cls] = s
    return out


def selftest():
    # 백분위 검산: 1..100 → p50≈50.5, p95≈95.05, p99≈99.01, min/max 경계
    xs = list(range(1, 101))
    assert abs(pct(xs, 50) - 50.5) < 1e-6, pct(xs, 50)
    assert abs(pct(xs, 95) - 95.05) < 1e-6, pct(xs, 95)
    assert abs(pct(xs, 99) - 99.01) < 1e-6, pct(xs, 99)
    assert pct(xs, 0) == 1 and pct(xs, 100) == 100
    assert pct([], 95) == 0.0 and pct([7.0], 95) == 7.0
    s = summarize([10.0, 20.0, 30.0])
    assert s["min"] == 10.0 and s["max"] == 30.0 and abs(s["mean"] - 20.0) < 1e-9
    assert _norm_base(":8082") == "http://localhost:8082"
    assert _norm_base("h:9") == "http://h:9" and _norm_base("http://x") == "http://x"
    assert _guess_class("세종대로 175") == "road"
    assert _guess_class("역삼동 736") == "jibun"
    assert _guess_class("강남역") == "name"
    print("BENCH SELFTEST: PASS ✓")
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://localhost:8082", help="단일 백엔드 base URL(':8082' 약식 허용)")
    ap.add_argument("--a", help="A·B 비교: 기준 백엔드(예 :8082 SQLite)")
    ap.add_argument("--b", help="A·B 비교: 대상 백엔드(예 :8092 PostGIS)")
    ap.add_argument("--queries", help="질의 CSV('q' + 선택 'class','lon','lat')")
    ap.add_argument("--limit", type=int, default=8, help="top-N(실제 서빙 기본값과 맞춤)")
    ap.add_argument("--repeat", type=int, default=20, help="질의당 측정 반복수")
    ap.add_argument("--warmup", type=int, default=3, help="통계 제외 워밍업 반복수(캐시 적재)")
    ap.add_argument("--concurrency", type=int, default=1, help="동시 클라이언트 수(>1 = 부하/꼬리지연)")
    ap.add_argument("--slo-p95", type=float, help="통과선: 지정 클래스 p95(ms) 이하")
    ap.add_argument("--slo-class", default="jibun", help="--slo-p95 적용 클래스(기본 jibun)")
    ap.add_argument("--max-regress", type=float, default=1.5, help="A·B 모드: B p95 ≤ A p95 × 이 배수")
    ap.add_argument("--json", action="store_true", help="결과를 JSON 으로도 stdout 끝에 출력")
    ap.add_argument("--selftest", action="store_true", help="네트워크 없이 통계 검증")
    args = ap.parse_args()

    if args.selftest:
        return selftest()

    queries = load_queries(args.queries) if args.queries else [
        (cls, q, None, None) for cls, q in DEFAULT_QUERIES]
    nq = len(queries)
    print(f"질의 {nq}건 · repeat={args.repeat} warmup={args.warmup} concurrency={args.concurrency} limit={args.limit}")

    payload = {}
    if args.a and args.b:
        a, b = _norm_base(args.a), _norm_base(args.b)
        print(f"A(기준) = {a}   B(대상) = {b}")
        ra, ea = bench(a, queries, args.limit, args.repeat, args.warmup, args.concurrency)
        rb, eb = bench(b, queries, args.limit, args.repeat, args.warmup, args.concurrency)
        sa, sb = stats_with_raw(ra), stats_with_raw(rb)
        print_table(f"A {a}", {k: dict(v) for k, v in sa.items()})
        print_table(f"B {b}", {k: dict(v) for k, v in sb.items()})
        # 회귀 게이트: 클래스별 B.p95 / A.p95
        print(f"\n[Δ 회귀] B.p95 / A.p95  (게이트 ≤ {args.max_regress:g}배)")
        worst = 1.0; fail_cls = None
        for k in sorted(set(sa) | set(sb)):
            pa = sa.get(k, {}).get("p95", 0.0); pb = sb.get(k, {}).get("p95", 0.0)
            ratio = (pb / pa) if pa else float("inf") if pb else 1.0
            flag = "" if ratio <= args.max_regress else "  ✗회귀"
            print(f"  {k:<9} A {pa:>7.1f} → B {pb:>7.1f}  ×{ratio:>4.2f}{flag}")
            if ratio > worst:
                worst, fail_cls = ratio, k
        errs = len(ea) + len(eb)
        ok = (worst <= args.max_regress) and errs == 0
        for tag, errlist in (("A", ea), ("B", eb)):
            if errlist:
                print(f"\n  [{tag} 오류 {len(errlist)}건]")
                for cls, q, e in errlist[:5]:
                    print(f"    {cls} {q!r}: {e}")
        print(f"\n[판정] 최대 회귀 ×{worst:.2f}"
              + (f" (클래스 {fail_cls})" if fail_cls else "")
              + f" · 오류 {errs} → " + ("✅ 통과" if ok else "⛔ 미달"))
        payload = {"a": {k: {kk: vv for kk, vv in v.items() if kk != '_raw'} for k, v in sa.items()},
                   "b": {k: {kk: vv for kk, vv in v.items() if kk != '_raw'} for k, v in sb.items()},
                   "max_regress": worst, "errors": errs, "pass": ok}
        rc = 0 if ok else 1
    else:
        base = _norm_base(args.base)
        print(f"base = {base}")
        r, e = bench(base, queries, args.limit, args.repeat, args.warmup, args.concurrency)
        s = stats_with_raw(r)
        print_table(base, {k: dict(v) for k, v in s.items()})
        if e:
            print(f"\n  [오류 {len(e)}건]")
            for cls, q, err in e[:8]:
                print(f"    {cls} {q!r}: {err}")
        ok = True; rc = 0
        if args.slo_p95 is not None:
            cs = s.get(args.slo_class)
            p95 = cs["p95"] if cs else 0.0
            meets = (cs is not None) and (p95 <= args.slo_p95) and not e
            print(f"\n[판정] {args.slo_class} p95 {p95:.1f}ms ≤ SLO {args.slo_p95:g}ms · 오류 {len(e)} → "
                  + ("✅ 통과" if meets else "⛔ 미달"))
            ok = meets; rc = 0 if meets else 1
        payload = {"base": base,
                   "stats": {k: {kk: vv for kk, vv in v.items() if kk != '_raw'} for k, v in s.items()},
                   "errors": len(e), "pass": ok}

    if args.json:
        print("\n" + json.dumps(payload, ensure_ascii=False))
    return rc


if __name__ == "__main__":
    sys.exit(main())
