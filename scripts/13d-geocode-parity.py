#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""지오코더 parity — SQLite 백엔드(:8082) vs PostGIS 백엔드(:8092) 질의 결과 비교.

배경: geocode-api.py(FTS5+R-tree)와 geocode-api-pg.py(pg_trgm+GiST+ST_Contains)는
  엔드포인트 계약·응답 형태·스코어링을 동일하게 유지한다(geocode-api-pg.py 헤더). 데이터도
  load_geocode.py 가 geocode.sqlite 를 무손실 복사해 적재 → '같은 질의에 같은 답'이어야 한다.
  이 스크립트는 그 '질의 parity'를 수치로 측정해 Phase 5c(게이트웨이 upstream 전환) 가능 여부를 판정한다.

비교 대상(두 API 공통 응답):
  GET /geocode?q=…          → {"results":[{name,kind,lon,lat,...}]}
  GET /reverse?lon=&lat=    → {"nearest":[{name,kind,lon,lat,dist_m}], "areas":[{name,type,code}]}
  GET /health               → {"ok":true,"places":N,"areas":M}

사용:
  python3 13d-geocode-parity.py                          # 기본 :8082(A) vs :8092(B), 내장 질의셋
  python3 13d-geocode-parity.py --queries q.csv          # CSV: 'q' 컬럼(검색어) + 선택 'lon','lat'(역지오)
  python3 13d-geocode-parity.py --a http://h:8082 --b http://h:8092 --limit 5
  python3 13d-geocode-parity.py --selftest               # 네트워크 없이 헬퍼 검증

판정: geocode top-1 좌표 일치율 ≥ --min-top1 AND reverse 행정동 일치율 ≥ --min-area → exit 0, 아니면 exit 1.
의존: 표준 라이브러리만(urllib) — 폐쇄망/무의존 일관.
"""
import argparse, csv, json, math, sys, urllib.parse, urllib.request

# 내장 질의셋 — addr/station/place/admin 골고루(질의 CSV 미지정 시 사용)
DEFAULT_QUERIES = [
    "서울특별시 종로구 세종대로 175",
    "부산광역시 해운대구 우동",
    "경기도 수원시 영통구 광교로 145",
    "대전광역시 유성구 대학로 99",
    "강남역", "서울역", "부산역", "수원역",
    "경복궁", "해운대해수욕장", "인천국제공항", "롯데월드타워",
    "제주특별자치도 제주시", "광주광역시 북구", "강원특별자치도 춘천시",
]


def http_json(base, path, params, timeout=10):
    """API GET → (dict, None) | (None, err)."""
    url = base.rstrip("/") + path + "?" + urllib.parse.urlencode(params)
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8")), None
    except Exception as e:  # noqa: BLE001 — 네트워크/JSON 오류 전부 불일치로 집계
        return None, f"{type(e).__name__}: {e}"


def haversine(lon1, lat1, lon2, lat2):
    """두 점 거리(m)."""
    R = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(min(1.0, math.sqrt(a)))


def rkey(item):
    """결과 식별키(이름 + 5자리 반올림 좌표) — top-N 겹침 계산용."""
    return (item.get("name"), round(item.get("lon") or 0, 5), round(item.get("lat") or 0, 5))


def _jaccard(sa, sb):
    if not sa and not sb:
        return 1.0
    u = sa | sb
    return len(sa & sb) / len(u) if u else 1.0


def compare_geocode(a, b, queries, limit, coord_tol, show):
    """geocode 질의별 A·B 비교 → 지표 dict + 불일치 샘플."""
    n = len(queries)
    both_empty = presence_mismatch = errors = 0
    coord_same = name_same = both_have = 0
    jac_sum = 0.0
    samples = []
    for q in queries:
        da, ea = http_json(a, "/geocode", {"q": q, "limit": limit})
        db, eb = http_json(b, "/geocode", {"q": q, "limit": limit})
        if ea or eb:
            errors += 1
            if len(samples) < show:
                samples.append(f"  [err] q={q!r}  A:{ea or 'ok'}  B:{eb or 'ok'}")
            continue
        ra = da.get("results") or []
        rb = db.get("results") or []
        ka = {rkey(x) for x in ra}
        kb = {rkey(x) for x in rb}
        jac_sum += _jaccard(ka, kb)
        if not ra and not rb:
            both_empty += 1
            continue
        if bool(ra) != bool(rb):
            presence_mismatch += 1
            if len(samples) < show:
                samples.append(f"  [presence] q={q!r}  A={len(ra)}건 B={len(rb)}건")
            continue
        both_have += 1
        ta, tb = ra[0], rb[0]
        d = haversine(ta.get("lon", 0), ta.get("lat", 0), tb.get("lon", 0), tb.get("lat", 0))
        cs = d <= coord_tol
        ns = ta.get("name") == tb.get("name")
        coord_same += cs
        name_same += ns
        if (not cs or not ns) and len(samples) < show:
            samples.append(
                f"  [top1] q={q!r}\n      A: {ta.get('name')} ({ta.get('lon')},{ta.get('lat')})"
                f"\n      B: {tb.get('name')} ({tb.get('lon')},{tb.get('lat')})  Δ={d:.1f}m"
            )
    return {
        "n": n, "errors": errors, "both_empty": both_empty,
        "presence_mismatch": presence_mismatch, "both_have": both_have,
        "coord_same": coord_same, "name_same": name_same,
        "top1_coord_rate": (coord_same / both_have) if both_have else 1.0,
        "top1_name_rate": (name_same / both_have) if both_have else 1.0,
        "mean_jaccard": (jac_sum / (n - errors)) if (n - errors) else 1.0,
    }, samples


def compare_reverse(a, b, points, limit, rev_tol, show):
    """reverse 좌표별 A·B 비교 → 지표 dict + 불일치 샘플."""
    n = len(points)
    errors = nearest_within = nearest_have = area_same = 0
    samples = []
    for lon, lat in points:
        da, ea = http_json(a, "/reverse", {"lon": lon, "lat": lat, "limit": limit})
        db, eb = http_json(b, "/reverse", {"lon": lon, "lat": lat, "limit": limit})
        if ea or eb:
            errors += 1
            if len(samples) < show:
                samples.append(f"  [err] ({lon},{lat})  A:{ea or 'ok'}  B:{eb or 'ok'}")
            continue
        na = da.get("nearest") or []
        nb = db.get("nearest") or []
        if na and nb:
            nearest_have += 1
            d = haversine(na[0].get("lon", 0), na[0].get("lat", 0),
                          nb[0].get("lon", 0), nb[0].get("lat", 0))
            if d <= rev_tol:
                nearest_within += 1
            elif len(samples) < show:
                samples.append(
                    f"  [nearest] ({lon},{lat})  A:{na[0].get('name')}  B:{nb[0].get('name')}  Δ={d:.1f}m"
                )
        ca = {x.get("code") for x in (da.get("areas") or [])}
        cb = {x.get("code") for x in (db.get("areas") or [])}
        if ca == cb:
            area_same += 1
        elif len(samples) < show:
            samples.append(f"  [areas] ({lon},{lat})  A={sorted(ca)}  B={sorted(cb)}")
    ok = n - errors
    return {
        "n": n, "errors": errors, "nearest_have": nearest_have,
        "nearest_within": nearest_within, "area_same": area_same,
        "nearest_rate": (nearest_within / nearest_have) if nearest_have else 1.0,
        "area_rate": (area_same / ok) if ok else 1.0,
    }, samples


def derive_reverse_points(a, queries, limit):
    """역지오 좌표 미지정 시: A(기준)의 geocode top-1 좌표를 역지오 입력으로 파생(한국 내 유효점)."""
    pts = []
    for q in queries:
        da, ea = http_json(a, "/geocode", {"q": q, "limit": limit})
        if ea:
            continue
        res = da.get("results") or []
        if res and res[0].get("lon") is not None and res[0].get("lat") is not None:
            pts.append((res[0]["lon"], res[0]["lat"]))
    return pts


def selftest():
    assert abs(haversine(127.0, 37.0, 127.0, 37.0)) < 1e-6
    # 위도 37°에서 경도 0.001° ≈ 88~89m
    d = haversine(127.000, 37.0, 127.001, 37.0)
    assert 80 < d < 95, d
    assert _jaccard({1, 2}, {1, 2}) == 1.0
    assert _jaccard(set(), set()) == 1.0
    assert _jaccard({1}, {2}) == 0.0
    assert rkey({"name": "x", "lon": 1.234567, "lat": 2.0}) == ("x", 1.23457, 2.0)
    print("selftest OK")
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--a", default="http://localhost:8082", help="기준(SQLite) base URL")
    ap.add_argument("--b", default="http://localhost:8092", help="대상(PostGIS) base URL")
    ap.add_argument("--queries", help="질의 CSV('q' 컬럼 + 선택 'lon','lat')")
    ap.add_argument("--limit", type=int, default=5, help="top-N")
    ap.add_argument("--coord-tol", type=float, default=1.0, help="geocode top-1 좌표 동일 판정 임계(m)")
    ap.add_argument("--rev-tol", type=float, default=1.0, help="reverse nearest 동일 판정 임계(m)")
    ap.add_argument("--min-top1", type=float, default=0.99, help="geocode top-1 좌표 일치율 통과선")
    ap.add_argument("--min-area", type=float, default=0.99, help="reverse 행정동 일치율 통과선")
    ap.add_argument("--show", type=int, default=20, help="카테고리별 불일치 샘플 출력 개수")
    ap.add_argument("--selftest", action="store_true", help="네트워크 없이 헬퍼만 검증")
    args = ap.parse_args()

    if args.selftest:
        return selftest()

    # 질의셋
    geo_qs, rev_pts = [], []
    if args.queries:
        for row in csv.DictReader(open(args.queries, encoding="utf-8-sig")):
            q = (row.get("q") or row.get("query") or "").strip()
            if q:
                geo_qs.append(q)
            lon, lat = row.get("lon"), row.get("lat")
            if lon and lat:
                try:
                    rev_pts.append((float(lon), float(lat)))
                except ValueError:
                    pass
        if not geo_qs:
            geo_qs = list(DEFAULT_QUERIES)
    else:
        geo_qs = list(DEFAULT_QUERIES)

    print(f"A(기준) = {args.a}   B(대상) = {args.b}")

    # health
    ha, ea = http_json(args.a, "/health", {})
    hb, eb = http_json(args.b, "/health", {})
    print("\n[health]")
    print(f"  A: {ea or ha}")
    print(f"  B: {eb or hb}")
    if ea or eb:
        print("\n✗ 한쪽 health 실패 — 두 백엔드가 모두 떠 있는지 확인(포트/컨테이너).")
        return 1

    # geocode
    gm, gs = compare_geocode(args.a, args.b, geo_qs, args.limit, args.coord_tol, args.show)
    print(f"\n[geocode] 질의 {gm['n']}건  (오류 {gm['errors']} · 양쪽빈값 {gm['both_empty']} · 유무불일치 {gm['presence_mismatch']})")
    print(f"  top-1 좌표 일치율 : {gm['top1_coord_rate']:.3f}  ({gm['coord_same']}/{gm['both_have']}, ≤{args.coord_tol:g}m)")
    print(f"  top-1 이름 일치율 : {gm['top1_name_rate']:.3f}  ({gm['name_same']}/{gm['both_have']})")
    print(f"  top-N 겹침(Jaccard 평균) : {gm['mean_jaccard']:.3f}")
    if gs:
        print("  ── 불일치 샘플 ──")
        print("\n".join(gs))

    # reverse — 좌표 미지정 시 A geocode top-1 에서 파생
    if not rev_pts:
        rev_pts = derive_reverse_points(args.a, geo_qs, args.limit)
        src = "A geocode top-1 파생"
    else:
        src = "질의 CSV lon/lat"
    rm, rs = compare_reverse(args.a, args.b, rev_pts, args.limit, args.rev_tol, args.show)
    print(f"\n[reverse] 좌표 {rm['n']}건 ({src})  (오류 {rm['errors']})")
    print(f"  nearest top-1 거리 일치율 : {rm['nearest_rate']:.3f}  ({rm['nearest_within']}/{rm['nearest_have']}, ≤{args.rev_tol:g}m)")
    print(f"  행정동(areas) 일치율      : {rm['area_rate']:.3f}  ({rm['area_same']}/{rm['n'] - rm['errors']})")
    if rs:
        print("  ── 불일치 샘플 ──")
        print("\n".join(rs))

    # 판정
    pass_top1 = gm["top1_coord_rate"] >= args.min_top1
    pass_area = rm["area_rate"] >= args.min_area
    ok = pass_top1 and pass_area and gm["errors"] == 0 and rm["errors"] == 0
    print(f"\n[판정] geocode top-1 ≥{args.min_top1:g}: {'OK' if pass_top1 else 'FAIL'}"
          f" · reverse 행정동 ≥{args.min_area:g}: {'OK' if pass_area else 'FAIL'}")
    print("✅ PARITY 통과 — Phase 5c(게이트웨이 전환) 진행 가능" if ok
          else "⛔ PARITY 미달 — 불일치 원인 분석 후 재측정(전환 보류)")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
