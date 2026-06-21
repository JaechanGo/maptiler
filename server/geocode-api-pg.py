#!/usr/bin/env python3
"""PostGIS 백엔드 지오코딩/역지오코딩 API (Phase 5 — 5b shadow / 5c 전환).

server/geocode-api.py(SQLite FTS5+R-tree)의 **엔드포인트 계약·응답 형태·스코어링을 그대로 유지**하고
질의층만 PostGIS(pg_trgm·btree·GiST·ST_Contains)로 교체. 프론트(demo/js/search.js) 무변경.

데이터: load_geocode.py 가 geocode.sqlite→PostGIS address(+poi) 로 옮긴 것(좌표·dedup·파싱 그대로 재사용).
검색 인덱스: scripts/postgis/schema/11-address-search.sql.
연결: DATABASE_URL 또는 PG* 환경변수. GEOCODE_PORT(기본 8082).

전환 단계:
  5b(shadow): 별도 포트(8092)로 띄워 SQLite판(8082)과 병행, scripts/13d-geocode-parity.py 로 질의 parity 측정.
  5c(전환):  게이트웨이 /geocode·/reverse upstream 을 이 서비스로 교체.
"""
import json, math, os, re, sys, unicodedata
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

PORT = int(os.environ.get("GEOCODE_PORT", "8082"))
DSN = os.environ.get("DATABASE_URL") or (
    f"host={os.environ.get('PGHOST','localhost')} port={os.environ.get('PGPORT','5433')} "
    f"user={os.environ.get('PGUSER','cuvia')} dbname={os.environ.get('PGDATABASE','cuvia')} "
    f"password={os.environ.get('PGPASSWORD','cuvia')}")
ADDR_CAP = 400
POOL = ConnectionPool(DSN, min_size=1, max_size=8, kwargs={"row_factory": dict_row}, open=False)

TOKEN_RE = re.compile(r"[^\w가-힣]+", re.UNICODE)


# ── 표시/응답 헬퍼 (geocode-api.py 와 동일 형태) ─────────────────────
def _g(r, k): return r.get(k)
def norm(s): return re.sub(r"\s+", " ", unicodedata.normalize("NFC", s or "")).strip()
def rnorm(s): return re.sub(r"[.\s]", "", unicodedata.normalize("NFC", s or ""))


def addr_str(r):
    s = f'{r["sido"]} {r["sigungu"]} {r["emd"]} {r["road"]} {r["main_no"]}'
    if r.get("sub_no"): s += f'-{r["sub_no"]}'
    if r.get("bld"): s += f' ({r["bld"]})'
    return s


def road_str(r):
    s = f'{r["sido"]} {r["sigungu"]} {r["road"]} {r["main_no"]}'
    if r.get("sub_no"): s += f'-{r["sub_no"]}'
    if r.get("bld"): s += f' ({r["bld"]})'
    return s


def parcel_str(r):
    jb = _g(r, "jibun")
    return (f'{r["sido"]} {r["sigungu"]} {jb}' if jb else f'{r["sido"]} {r["sigungu"]} {r["emd"]}').strip()


def addr_obj(r):
    return {
        "road": road_str(r), "parcel": parcel_str(r),
        "zipcode": _g(r, "postal") or "", "bld": _g(r, "bld") or "",
        "structure": {
            "sido": r["sido"], "sigungu": r["sigungu"], "emd": r["emd"],
            "haeng_dong": _g(r, "haeng_dong"),
            "road_name": _g(r, "road"), "main_no": _g(r, "main_no"), "sub_no": _g(r, "sub_no"),
            "b_code": _g(r, "bcode"), "h_code": _g(r, "hcode"),
        },
    }


def category_of(r):
    c1 = _g(r, "cat1"); c2 = _g(r, "cat2"); sub = _g(r, "subtype")
    if c1:
        cat = {"primary": c1, "label": " > ".join(x for x in (c1, c2) if x)}
        if c2: cat["sub"] = c2
        return cat
    return {"label": sub} if sub else None


def parse(q):
    q = re.sub(r"(?<=\d)\.(?=\d)", "", norm(q))
    house = road = None; terms = []
    for t in re.split(r"[\s,]+", q):
        if not t: continue
        m = re.fullmatch(r"(\d+)(?:-(\d+))?", t)
        if m: house = (int(m.group(1)), int(m.group(2) or 0)); continue
        if re.search(r"(로|길)$", t): road = rnorm(t); continue
        ct = re.sub(r"[^\w가-힣]", "", t)
        if ct: terms.append(ct)
    return {"road": road, "house": house, "terms": terms}


# ── 좌표 → 최근접 도로명주소(역지오코딩/주소부착) ──────────────────
def addr_at(cur, lon, lat):
    cur.execute(
        """SELECT * FROM address
           WHERE kind='addr' AND geom IS NOT NULL
             AND ST_DWithin(geom::geography, ST_SetSRID(ST_MakePoint(%s,%s),4326)::geography, 2500)
           ORDER BY geom <-> ST_SetSRID(ST_MakePoint(%s,%s),4326) LIMIT 1""",
        (lon, lat, lon, lat))
    r = cur.fetchone()
    return addr_obj(r) if r else None


def geocode(cur, q, limit):
    p = parse(q); results = []

    # ---- 주소 경로 ----
    if p["road"]:
        rn = p["road"]; terms = p["terms"]; h = p["house"]
        def rb(r):
            return 12 * sum(1 for t in terms
                            if t in (r["sido"], r["sigungu"], r["emd"], r.get("haeng_dong") or "")
                            or t in (r["sigungu"] or ""))
        def fetch(extra, nums):
            cur.execute(f"SELECT *, ST_X(geom) AS lon, ST_Y(geom) AS lat FROM address "
                        f"WHERE kind='addr' AND road_norm=%s{extra} LIMIT {ADDR_CAP}", (rn, *nums))
            return cur.fetchall()
        cand = []
        if h:
            r1 = fetch(" AND main_no=%s AND sub_no=%s", (h[0], h[1]))
            if r1:
                cand = [(200 + rb(r), r) for r in r1]
            else:
                r2 = fetch(" AND main_no=%s", (h[0],))
                if r2:
                    cand = [(150 + rb(r), r) for r in r2]
        if not cand:
            cand = [(110 + rb(r), r) for r in fetch("", ())]
        for s, r in cand:
            results.append((s, {"name": addr_str(r), "kind": "addr",
                                "lon": r["lon"], "lat": r["lat"], "address": addr_obj(r)}))

    # ---- 이름 경로 (역/지명/POI/건물명) ----
    if p["terms"] and (not p["road"] or not results):
        base = {"station": 175, "place": 165, "dong": 160, "poi": 140, "biz": 135, "road": 120, "addr": 130}
        nq = norm(q); multi = len(p["terms"]) >= 2
        # 각 토큰: name 부분일치(단일=prefix, 다중=지역도 허용)
        conds = []; args = []
        for i, t in enumerate(p["terms"]):
            if multi:   # search_text(이름+도로명+지번, trgm 인덱스) + 지역 토큰
                conds.append("(search_text ILIKE %s OR bld ILIKE %s OR sido ILIKE %s OR sigungu ILIKE %s OR emd ILIKE %s)")
                args += [f"%{t}%", f"%{t}%", f"%{t}%", f"%{t}%", f"%{t}%"]
            else:       # 단일 토큰 = search_text/건물명 prefix
                conds.append("(search_text ILIKE %s OR bld ILIKE %s)")
                args += [f"{t}%", f"{t}%"]
        sql = ("SELECT *, ST_X(geom) AS lon, ST_Y(geom) AS lat FROM address "
               "WHERE kind <> 'addr' AND geom IS NOT NULL AND " + " AND ".join(conds) +
               f" LIMIT {ADDR_CAP}")
        cur.execute(sql, args)
        for r in cur.fetchall():
            disp = addr_str(r) if r["kind"] == "addr" else r["name"]
            s = base.get(r["kind"], 100) + (30 if (r.get("name") or "") == nq else 0)
            item = {"name": disp, "kind": r["kind"], "subtype": _g(r, "subtype"),
                    "lon": r["lon"], "lat": r["lat"]}
            cat = category_of(r)
            if cat: item["category"] = cat
            if _g(r, "phone"): item["phone"] = r["phone"]
            if _g(r, "source"): item["source"] = r["source"]
            results.append((s, item))

    # ---- 병합·정렬·중복 제거 ----
    results.sort(key=lambda x: -x[0])
    out = []; seen = set()
    for s, item in results:
        if item["lon"] is None or item["lat"] is None:
            continue
        k = (item["name"], round(item["lon"], 5), round(item["lat"], 5))
        if k in seen: continue
        seen.add(k); out.append(item)
        if len(out) >= limit: break
    for it in out:
        if it["kind"] != "addr":
            it["address"] = addr_at(cur, it["lon"], it["lat"])
    return out


def reverse(cur, lon, lat, limit):
    address = addr_at(cur, lon, lat)
    pt = "ST_SetSRID(ST_MakePoint(%s,%s),4326)"
    cur.execute(
        f"""SELECT *, ST_X(geom) AS lon, ST_Y(geom) AS lat,
                   ST_Distance(geom::geography, {pt}::geography) AS d FROM address
            WHERE geom IS NOT NULL
              AND ST_DWithin(geom::geography, {pt}::geography, 20000)
            ORDER BY geom <-> {pt} LIMIT %s""",
        (lon, lat, lon, lat, lon, lat, limit))
    nearest = []
    for r in cur.fetchall():
        nm = addr_str(r) if r["kind"] == "addr" else r["name"]
        item = {"name": nm, "kind": r["kind"], "subtype": _g(r, "subtype"),
                "lon": r["lon"], "lat": r["lat"], "dist_m": round(r["d"], 1)}
        cat = category_of(r)
        if cat: item["category"] = cat
        nearest.append(item)
    cur.execute(
        f"""SELECT name, level AS type, code FROM admin_boundary
            WHERE ST_Contains(geom, {pt})""", (lon, lat))
    areas = [{"name": a["name"], "type": a["type"], "code": a["code"]} for a in cur.fetchall()]
    return {"address": address, "nearest": nearest, "areas": areas}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def _send(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path); qs = parse_qs(u.query)
        try:
            with POOL.connection() as con, con.cursor() as cur:
                if u.path == "/health":
                    cur.execute("SELECT count(*) c FROM address"); pc = cur.fetchone()["c"]
                    cur.execute("SELECT count(*) c FROM admin_boundary"); ac = cur.fetchone()["c"]
                    return self._send({"ok": True, "places": pc, "areas": ac})
                if u.path == "/geocode":
                    q = (qs.get("q") or [""])[0]
                    limit = min(int((qs.get("limit") or ["8"])[0]), 50)
                    return self._send({"query": q, "results": geocode(cur, q, limit)})
                if u.path == "/reverse":
                    try:
                        lon = float((qs.get("lon") or [""])[0]); lat = float((qs.get("lat") or [""])[0])
                    except ValueError:
                        return self._send({"error": "lon/lat 필요"}, 400)
                    limit = min(int((qs.get("limit") or ["6"])[0]), 50)
                    return self._send({"lon": lon, "lat": lat, **reverse(cur, lon, lat, limit)})
                return self._send({"error": "not found",
                                   "endpoints": ["/geocode?q=", "/reverse?lon=&lat=", "/health"]}, 404)
        except psycopg.OperationalError as e:
            return self._send({"error": f"PostGIS 연결 실패: {str(e)[:120]}"}, 503)


def _selftest():
    POOL.open()
    with POOL.connection() as con, con.cursor() as cur:
        for q in sys.argv[2:] or ["화성시 만세구 3.1만세로 5-3", "강남구 테헤란로 152", "강남역"]:
            print(f"\nQ: {q}")
            for r in geocode(cur, q, 4):
                print(f"   [{r['kind']}] {r['name']} → {r['lon']},{r['lat']}")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "selftest":
        _selftest(); sys.exit(0)
    POOL.open()
    print(f"geocode-api-pg: DSN set, PORT={PORT}", file=sys.stderr)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
