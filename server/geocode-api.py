#!/usr/bin/env python3
"""무의존 지오코딩/역지오코딩 API (Python 표준 라이브러리만) — 통합본.

  GET /geocode?q=화성시 만세구 3.1만세로 5-3   → {"results":[{name,kind,lon,lat,...}]}
  GET /geocode?q=강남역                        → 역/지명/POI 이름 검색
  GET /reverse?lon=126.86&lat=37.55            → {"nearest":[...], "areas":[...]}
  GET /health                                  → {"ok":true,"places":N,"areas":M}

데이터: 09-gen-geocode.py 가 만든 통합 인덱스(도로명주소 내비DB + OSM 역/POI/지명).
  places(id,kind,name,subtype, sido,sigungu,emd,road,road_norm,main_no,sub_no,bld,postal,haeng_dong,bd_mgt_sn, lon,lat)
  places_fts(name, region, road, bld)  ← addr→region/road/bld, OSM→name

검색 전략:
  · 주소(도로명+건물번호) : road 컬럼 매칭 + 본번/부번 INTEGER 정밀, 없으면 부번→본번→도로 단계 폴백,
                            행정구역 토큰은 region 가산.  ← '전부 AND'로 0건 나는 문제 방지
  · 이름(역/지명/POI/건물명): name+bld 컬럼 prefix 검색(도로명 질의엔 잡음 억제).
환경변수 GEOCODE_DB(기본 ~/geocode-build/geocode.sqlite), GEOCODE_PORT(기본 8082).
"""
import json, math, os, pathlib, re, sqlite3, sys, unicodedata
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

DB_PATH = os.environ.get('GEOCODE_DB', os.path.expanduser('~/geocode-build/geocode.sqlite'))
PORT = int(os.environ.get('GEOCODE_PORT', '8082'))
NEAR_WIN = (0.01, 0.05, 0.2)        # 역지오코딩 최근접 검색 윈도(도)
TOKEN_RE = re.compile(r'[^\w가-힣]+', re.UNICODE)
ADDR_CAP = 400                      # 도로명 후보 상한(흔한 도로명 가드)


def connect():
    if not pathlib.Path(DB_PATH).exists():
        raise FileNotFoundError(DB_PATH)
    con = sqlite3.connect(f'file:{DB_PATH}?mode=ro', uri=True, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.create_function('hav', 4, _hav, deterministic=True)
    return con


def _hav(lat1, lon1, lat2, lon2):
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return r * 2 * math.asin(math.sqrt(a))


def point_in_ring(lon, lat, ring):
    inside = False; n = len(ring); j = n - 1
    for i in range(n):
        xi, yi = ring[i]; xj, yj = ring[j]
        if ((yi > lat) != (yj > lat)) and (lon < (xj - xi) * (lat - yi) / (yj - yi + 1e-300) + xi):
            inside = not inside
        j = i
    return inside


def norm(s): return re.sub(r'\s+', ' ', unicodedata.normalize('NFC', s or '')).strip()
def rnorm(s): return re.sub(r'[.\s]', '', unicodedata.normalize('NFC', s or ''))


def addr_str(r):
    s = f'{r["sido"]} {r["sigungu"]} {r["emd"]} {r["road"]} {r["main_no"]}'
    if r["sub_no"]: s += f'-{r["sub_no"]}'
    if r["bld"]: s += f' ({r["bld"]})'
    return s


def parse(q):
    q = re.sub(r'(?<=\d)\.(?=\d)', '', norm(q))
    house = road = None; terms = []
    for t in re.split(r'[\s,]+', q):          # 공백 분리 — '5-3'의 하이픈 보존(본번-부번)
        if not t: continue
        m = re.fullmatch(r'(\d+)(?:-(\d+))?', t)
        if m: house = (int(m.group(1)), int(m.group(2) or 0)); continue
        if re.search(r'(로|길)$', t): road = rnorm(t); continue
        ct = re.sub(r'[^\w가-힣]', '', t)      # FTS 안전 토큰
        if ct: terms.append(ct)
    return {'road': road, 'house': house, 'terms': terms}


def _fts(con, match, where='', args=()):
    sql = ("SELECT p.* FROM places_fts f JOIN places p ON p.id=f.rowid WHERE places_fts MATCH ?"
           + (f" AND {where}" if where else "") + f" LIMIT {ADDR_CAP}")
    try:
        return con.execute(sql, (match, *args)).fetchall()
    except sqlite3.OperationalError:
        return []


def geocode(con, q, limit):
    p = parse(q); results = []

    # ---- 주소 경로: 도로명 토큰이 있을 때 ----
    # road_norm·본번·부번을 SQL로 내려 ADDR_CAP/prefix오염('사직로N번길' 등)에 정확매칭이 잘리지 않게 한다.
    if p['road']:
        rn = p['road']; terms = p['terms']; h = p['house']
        reg = ' '.join([f'road:"{rn}"*'] + [f'region:"{t}"*' for t in terms])
        bare = f'road:"{rn}"*'
        def rb(r):
            return 12 * sum(1 for t in terms
                            if t in (r['sido'], r['sigungu'], r['emd'], r['haeng_dong'] or '')
                            or t in (r['sigungu'] or ''))
        def fetch(extra, nums):
            w = "p.kind='addr' AND p.road_norm=?" + extra
            rows = _fts(con, reg, w, (rn, *nums))
            if not rows and terms:                                   # region 불일치 → 도로명만으로 회수
                rows = _fts(con, bare, w, (rn, *nums))
            return rows
        cand = []
        if h:
            r1 = fetch(" AND p.main_no=? AND p.sub_no=?", (h[0], h[1]))
            if r1:
                cand = [(200 + rb(r), r) for r in r1]                # 본번+부번 정확
            else:
                r2 = fetch(" AND p.main_no=?", (h[0],))
                if r2:
                    cand = [(150 + rb(r), r) for r in r2]            # 본번만(부번 폴백)
        if not cand:
            cand = [(110 + rb(r), r) for r in fetch("", ())]         # 도로명만
        for s, r in cand:
            results.append((s, {'name': addr_str(r), 'kind': 'addr', 'lon': r['lon'], 'lat': r['lat'],
                                'building': f"{r['main_no']}" + (f"-{r['sub_no']}" if r['sub_no'] else ""),
                                'postal': r['postal']}))

    # ---- 이름 경로: 역/지명/POI/건물명 (도로명 질의엔 잡음 억제) ----
    if p['terms'] and (not p['road'] or not results):
        # 2+토큰이면 지역(region)도 검색 → '스타벅스 강남' 같은 상호+지역 조합. 단일 토큰은 name/bld만(주소 오염 방지)
        cols = '{name region bld}' if len(p['terms']) >= 2 else '{name bld}'
        m = ' '.join(f'{cols}:"{t}"*' for t in p['terms'])
        base = {'station': 175, 'place': 165, 'dong': 160, 'poi': 140, 'biz': 135, 'road': 120, 'addr': 130}
        nq = norm(q)
        for r in _fts(con, m):
            disp = addr_str(r) if r['kind'] == 'addr' else r['name']
            s = base.get(r['kind'], 100) + (30 if (r['name'] or '') == nq else 0)
            results.append((s, {'name': disp, 'kind': r['kind'], 'subtype': r['subtype'],
                                'lon': r['lon'], 'lat': r['lat']}))

    # ---- 병합·정렬·중복 제거 ----
    results.sort(key=lambda x: -x[0])
    out = []; seen = set()
    for s, item in results:
        k = (item['name'], round(item['lon'], 5), round(item['lat'], 5))
        if k in seen:
            continue
        seen.add(k); out.append(item)
        if len(out) >= limit:
            break
    return out


def reverse(con, lon, lat, limit):
    nearest = []
    for w in NEAR_WIN:
        rows = con.execute(
            """SELECT p.*, hav(?,?,p.lat,p.lon) d FROM place_rtree r JOIN places p ON p.id=r.id
               WHERE r.minlon>=? AND r.maxlon<=? AND r.minlat>=? AND r.maxlat<=?
               ORDER BY d LIMIT ?""",
            (lat, lon, lon - w, lon + w, lat - w, lat + w, limit)).fetchall()
        if rows:
            nearest = [{'name': addr_str(r) if r['kind'] == 'addr' else r['name'], 'kind': r['kind'],
                        'lon': r['lon'], 'lat': r['lat'], 'dist_m': round(r['d'], 1)} for r in rows]
            break
    areas = []
    for a in con.execute(
            """SELECT a.name,a.type,a.rings FROM area_rtree r JOIN areas a ON a.id=r.id
               WHERE r.minlon<=? AND r.maxlon>=? AND r.minlat<=? AND r.maxlat>=?""",
            (lon, lon, lat, lat)):
        for ring in json.loads(a['rings']):
            if point_in_ring(lon, lat, ring):
                areas.append({'name': a['name'], 'type': a['type']}); break
    return {'nearest': nearest, 'areas': areas}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path); qs = parse_qs(u.query)
        try:
            con = connect()
        except FileNotFoundError:
            return self._send({'error': f'geocode DB 없음: {DB_PATH}'}, 503)
        try:
            if u.path == '/health':
                p = con.execute("SELECT v FROM meta WHERE k='places'").fetchone()
                a = con.execute("SELECT count(*) c FROM areas").fetchone()
                return self._send({'ok': True, 'places': int(p['v']) if p else 0, 'areas': a['c']})
            if u.path == '/geocode':
                q = (qs.get('q') or [''])[0]
                limit = min(int((qs.get('limit') or ['8'])[0]), 50)
                return self._send({'query': q, 'results': geocode(con, q, limit)})
            if u.path == '/reverse':
                try:
                    lon = float((qs.get('lon') or [''])[0]); lat = float((qs.get('lat') or [''])[0])
                except ValueError:
                    return self._send({'error': 'lon/lat 필요'}, 400)
                limit = min(int((qs.get('limit') or ['6'])[0]), 50)
                return self._send({'lon': lon, 'lat': lat, **reverse(con, lon, lat, limit)})
            return self._send({'error': 'not found', 'endpoints': ['/geocode?q=', '/reverse?lon=&lat=', '/health']}, 404)
        finally:
            con.close()


def _selftest():
    con = connect()
    for q in sys.argv[2:] or ["화성시 만세구 3.1만세로 5-3", "강남구 테헤란로 152", "강남역"]:
        print(f"\nQ: {q}")
        for r in geocode(con, q, 4):
            print(f"   [{r['kind']}] {r['name']} → {r['lon']},{r['lat']}")


if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == 'selftest':
        _selftest(); sys.exit(0)
    print(f"geocode-api: DB={DB_PATH} PORT={PORT}", file=sys.stderr)
    ThreadingHTTPServer(('0.0.0.0', PORT), Handler).serve_forever()
