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

# 시도 코드(emd_cd 앞2)→명칭 변형. 동명중복(전국 교동×18 등) 시 지역토큰으로 시도 좁힘에 사용.
SIDO_NM = {
    "11": ("서울",), "26": ("부산",), "27": ("대구",), "28": ("인천",), "29": ("광주",),
    "30": ("대전",), "31": ("울산",), "36": ("세종",), "41": ("경기",),
    "43": ("충북", "충청북"), "44": ("충남", "충청남"), "46": ("전남", "전라남"),
    "47": ("경북", "경상북"), "48": ("경남", "경상남"), "50": ("제주",),
    "51": ("강원",), "52": ("전북", "전라북"),
}


# ── 표시/응답 헬퍼 (geocode-api.py 와 동일 형태) ─────────────────────
def _g(r, k): return r.get(k)
def norm(s): return re.sub(r"\s+", " ", unicodedata.normalize("NFC", s or "")).strip()
def rnorm(s): return re.sub(r"[.\s]", "", unicodedata.normalize("NFC", s or ""))
def _limit(qs, dflt, cap=50):
    # limit 파라미터 안전 파싱: 비숫자('abc'·'3.5') 입력 시 ValueError 전파로 무응답 소켓끊김(crash) 방지.
    try: return min(max(int((qs.get("limit") or [str(dflt)])[0]), 1), cap)
    except (ValueError, TypeError): return dflt


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
    house = road = dong = None; san = False; terms = []
    for t in re.split(r"[\s,]+", q):
        if not t: continue
        if t == "산": san = True; continue                  # 단독 '산'(임야) 표기
        m = re.fullmatch(r"(산)?(\d+)(?:-(\d+))?", t)        # 번지: '산12-3'·'산12'·'12-3'·'5'(산 접두 허용)
        if m:
            if m.group(1): san = True
            a, b = int(m.group(2)), int(m.group(3) or 0)
            if house is None and a <= 99999 and b <= 99999:  # 첫 유효 번지만 채택 + int4 범위 가드(오버플로 500 예방)
                house = (a, b)
            continue
        if re.search(r"(로|길)$", t): road = rnorm(t); continue
        ct = re.sub(r"[^\w가-힣]", "", t)
        if not ct: continue
        # 법정동/리/읍/면/'N가'(종로1가 등) 토큰 → 지번경로 분기 단서(terms 에도 남겨 지역가산·이름검색에 활용)
        # ※ 읍/면 누락 시 농촌(읍·면) 지번질의 전면 0건이 되므로 반드시 포함.
        if dong is None and len(ct) >= 2 and (re.search(r"(동|리|읍|면)$", ct) or re.search(r"\d가$", ct)):
            dong = ct
        terms.append(ct)
    return {"road": road, "house": house, "terms": terms, "dong": dong, "san": san}


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

    # ---- 지번 경로 (parcel 테이블 — 법정동명 + 번지 정확매칭) ----
    # 권위 지번 소스(연속지적도 39.6M). 동명을 lawd_dong 으로 emd_cd 정확등가(=) 해소 → parcel(emd_cd,ji_main,ji_sub)
    # 인덱스 정확매칭 → 대표점 ST_PointOnSurface(geom). 동명 정확등가라 2자 동명 Seq Scan 없음. san(임야) 토큰 동반시 가산.
    if not results and p["dong"] and p["house"]:
        h = p["house"]
        cur.execute("SELECT emd_cd FROM lawd_dong WHERE emd = %s", (p["dong"],))
        cds = [r["emd_cd"] for r in cur.fetchall()]
        # 동명중복 좁힘: 지역토큰(시/도)이 특정 시도를 가리키면 그 시도로 한정(엉뚱한 타시도 결과 혼입 방지).
        # 시군구 단위 좁힘은 시군구명 사전 부재로 미적용(후속) — 시도 한정만으로 광역시·도접두 질의 대부분 해소.
        region = [t for t in p["terms"] if t != p["dong"]]
        # 동명중복 좁힘 — 시군구(가장 specific) 우선, 없으면 시도. 지정 지역에 해당 동이 없으면 0건(타지역 동명 혼입 차단).
        # 시군구: lawd_sigungu(내비DB 추출 254개, '수원시 영통구' 형식). 지역토큰이 시군구명 단어와 일치하면 그 시군구(emd_cd 앞5)로 한정.
        sgg_hit = set()
        if region:
            conds = " OR ".join(["(sigungu_nm LIKE %s || '%%' OR sigungu_nm LIKE '%% ' || %s)"] * len(region))
            sa = []
            for t in region:
                sa += [t, t]
            cur.execute("SELECT sigungu_cd FROM lawd_sigungu WHERE " + conds, sa)
            sgg_hit = {r["sigungu_cd"] for r in cur.fetchall()}
        if sgg_hit:
            cds = [c for c in cds if c[:5] in sgg_hit]
        else:
            sido_hit = {code for t in region for code, names in SIDO_NM.items()
                        if any(t.startswith(n) for n in names)}
            if sido_hit:
                cds = [c for c in cds if c[:2] in sido_hit]
        if cds:
            sido_cds = list({c[:2] for c in cds})
            # ※ char 캐스팅 필수: char(2)/char(8) 컬럼에 text 배열을 그냥 ANY 하면 파티션 pruning·
            #    parcel_jibun_lookup 인덱스가 무력화돼 전 파티션 Seq Scan(전국 39.6M 시 치명적). 캐스팅하면 parcel_<sido> 1파티션 Index Scan.
            # 좌표: 매칭(보통 1행) 후 대표점. geom_pt(materialized) 있으면 사용, 없으면 즉석 ST_PointOnSurface
            # — 전국 39.6M geom_pt 일괄백필(수시간) 없이도 동작(매칭 소수행만 계산하므로 사실상 무비용).
            sql = ("SELECT jibun, emd_cd, "
                   "ST_X(COALESCE(geom_pt, ST_PointOnSurface(geom))) AS lon, "
                   "ST_Y(COALESCE(geom_pt, ST_PointOnSurface(geom))) AS lat "
                   "FROM parcel WHERE sido_cd = ANY(%s::char(2)[]) AND emd_cd = ANY(%s::char(8)[]) "
                   "AND ji_main = %s AND ji_sub = %s")
            args = [sido_cds, cds, h[0], h[1]]
            if p["san"]:
                sql += " AND san = 1"
            sql += f" ORDER BY emd_cd, ji_sub LIMIT {ADDR_CAP}"
            cur.execute(sql, args)
            for r in cur.fetchall():
                disp = f'{p["dong"]} {r["jibun"]}'.strip()
                results.append((200, {"name": disp, "kind": "addr",
                                      "lon": r["lon"], "lat": r["lat"],
                                      "address": {"parcel": disp,
                                                  "structure": {"emd": p["dong"], "b_code": r["emd_cd"]}}}))

    # ---- 지번 경로 (법정동/리 + 번지) ----
    # 도로명이 없고 동/리 토큰 + 번지가 있으면 지번주소. addr 행의 search_text 끝(= jibun '법정동 [산] 본번[-부번]')을
    # 동명 부분일치 + 번지 끝고정으로 매칭(둘 다 search_text trgm GIN 가속). 본번만이면 정확본번·부번동반 둘 다 회수.
    # 법정동명은 전국 중복(역삼동·중앙동…)이므로 동 외 지역토큰(시군구/시도)으로 좁히고 가산, 정확본번 우선 결정적 정렬.
    if not results and p["dong"] and p["house"]:
        dong = p["dong"]; h = p["house"]; sep = "산 " if p["san"] else " "
        region = [t for t in p["terms"] if t != dong]
        exact = f"%{sep}{h[0]}-{h[1]}" if h[1] else f"%{sep}{h[0]}"
        if h[1]:
            num_conds = "search_text ILIKE %s"; nums = [exact]
        else:
            num_conds = "(search_text ILIKE %s OR search_text ILIKE %s)"
            nums = [exact, f"%{sep}{h[0]}-%"]
        reg_sql = ""; reg_args = []
        for t in region:
            reg_sql += " AND (sigungu ILIKE %s OR sido ILIKE %s)"
            reg_args += [f"%{t}%", f"%{t}%"]
        cur.execute(
            "SELECT *, ST_X(geom) AS lon, ST_Y(geom) AS lat FROM address "
            f"WHERE kind='addr' AND search_text ILIKE %s AND {num_conds}{reg_sql} "
            f"ORDER BY (search_text ILIKE %s) DESC, sigungu, emd, id LIMIT {ADDR_CAP}",
            (f"%{dong}%", *nums, *reg_args, exact))
        for r in cur.fetchall():
            bonus = 12 * sum(1 for t in region if t in (r["sigungu"] or "") or t in (r["sido"] or ""))
            results.append((200 + bonus, {"name": addr_str(r), "kind": "addr",
                                          "lon": r["lon"], "lat": r["lat"], "address": addr_obj(r)}))

    # ---- 이름 경로 (역/지명/POI/건물명) ----
    if p["terms"] and not results:
        base = {"station": 175, "place": 165, "dong": 160, "poi": 140, "biz": 135, "road": 120, "addr": 130}
        nq = norm(q); multi = len(p["terms"]) >= 2
        # 각 토큰: name 부분일치(단일=prefix, 다중=지역도 허용)
        conds = []; args = []
        for t in p["terms"]:
            if multi:   # search_text(이름+도로명+지번, trgm 인덱스) + 지역 토큰
                conds.append("(search_text ILIKE %s OR bld ILIKE %s OR sido ILIKE %s OR sigungu ILIKE %s OR emd ILIKE %s)")
                args += [f"%{t}%", f"%{t}%", f"%{t}%", f"%{t}%", f"%{t}%"]
            else:       # 단일 토큰 = search_text/건물명. 3자+만 중간검색('%t%'), 2자↓는 prefix('t%')
                # — pg_trgm GIN 은 연속 3글자(trigram)가 있어야 인덱스를 탄다. 2자 infix '%서울%' 는 trigram 0개라
                #   1570만행 Seq Scan(2~11초). 2자↓는 인덱스 타는 prefix 로 유지(기존 동작, 회귀 없음).
                pat = f"%{t}%" if len(t) >= 3 else f"{t}%"
                conds.append("(search_text ILIKE %s OR bld ILIKE %s)")
                args += [pat, pat]
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
                    limit = _limit(qs, 8)
                    return self._send({"query": q, "results": geocode(cur, q, limit)})
                if u.path == "/reverse":
                    try:
                        lon = float((qs.get("lon") or [""])[0]); lat = float((qs.get("lat") or [""])[0])
                    except ValueError:
                        return self._send({"error": "lon/lat 필요"}, 400)
                    limit = _limit(qs, 6)
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
