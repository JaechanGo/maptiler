#!/usr/bin/env python3
"""무의존 지오코딩/역지오코딩 API (Python 표준 라이브러리만).

  GET /geocode?q=증미역&limit=8        → {"results":[{name,type,subtype,lon,lat}]}
  GET /reverse?lon=126.86&lat=37.55    → {"nearest":[{name,type,dist_m,lon,lat}], "areas":[{name,type}]}
  GET /health                          → {"ok":true,"places":N,"areas":M}

데이터: scripts/07-gen-geocode.py 가 만든 geocode/geocode.sqlite (FTS5 + R-tree + areas).
폐쇄망에서 별도 서비스(기본 :8082)로 띄우고, 소비 프론트/데모가 호출한다.
환경변수 GEOCODE_DB(기본 ../geocode/geocode.sqlite), GEOCODE_PORT(기본 8082).
"""
import json, math, os, pathlib, re, sqlite3, sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

ROOT = pathlib.Path(__file__).resolve().parents[1]
DB_PATH = os.environ.get('GEOCODE_DB', str(ROOT / 'geocode/geocode.sqlite'))
PORT = int(os.environ.get('GEOCODE_PORT', '8082'))
NEAR_WIN = (0.01, 0.05, 0.2)        # 역지오코딩 최근접 검색 윈도(도)
TOKEN_RE = re.compile(r'[^\w가-힣]+', re.UNICODE)


def connect():
    # 읽기전용 + 멀티스레드. DB 미존재 시 명확히 실패.
    if not pathlib.Path(DB_PATH).exists():
        raise FileNotFoundError(DB_PATH)
    con = sqlite3.connect(f'file:{DB_PATH}?mode=ro', uri=True, check_same_thread=False)
    con.create_function('hav', 4, _hav, deterministic=True)
    return con


def _hav(lat1, lon1, lat2, lon2):
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return r * 2 * math.asin(math.sqrt(a))


def point_in_ring(lon, lat, ring):
    # ray casting
    inside = False; n = len(ring); j = n - 1
    for i in range(n):
        xi, yi = ring[i]; xj, yj = ring[j]
        if ((yi > lat) != (yj > lat)) and (lon < (xj - xi) * (lat - yi) / (yj - yi + 1e-300) + xi):
            inside = not inside
        j = i
    return inside


def fts_query(q):
    toks = [t for t in TOKEN_RE.split(q.strip()) if t]
    if not toks:
        return None
    return ' '.join(f'"{t}"*' for t in toks)


def geocode(con, q, limit):
    m = fts_query(q)
    if not m:
        return []
    try:
        rows = con.execute(
            """SELECT p.name,p.type,p.subtype,p.lon,p.lat FROM places_fts f JOIN places p ON p.id=f.rowid
               WHERE places_fts MATCH ?
               ORDER BY (p.name=?) DESC,
                        CASE p.type WHEN 'station' THEN 0 WHEN 'place' THEN 1 WHEN 'dong' THEN 2
                                    WHEN 'road' THEN 3 WHEN 'poi' THEN 4 ELSE 5 END,
                        rank LIMIT ?""",
            (m, q.strip(), limit)).fetchall()
    except sqlite3.OperationalError:
        return []
    return [{'name': n, 'type': t, 'subtype': s, 'lon': lo, 'lat': la} for n, t, s, lo, la in rows]


def reverse(con, lon, lat, limit):
    nearest = []
    for w in NEAR_WIN:
        rows = con.execute(
            """SELECT p.name,p.type,p.subtype,p.lon,p.lat, hav(?,?,p.lat,p.lon) d
               FROM place_rtree r JOIN places p ON p.id=r.id
               WHERE r.minlon>=? AND r.maxlon<=? AND r.minlat>=? AND r.maxlat<=?
               ORDER BY d LIMIT ?""",
            (lat, lon, lon - w, lon + w, lat - w, lat + w, limit)).fetchall()
        if rows:
            nearest = [{'name': n, 'type': t, 'subtype': s, 'lon': lo, 'lat': la, 'dist_m': round(d, 1)}
                       for n, t, s, lo, la, d in rows]
            break
    # 포함 영역(행정동/지번 등) — area_rtree bbox 후보 → ray-cast
    areas = []
    for aid, name, typ, rings in con.execute(
            """SELECT a.id,a.name,a.type,a.rings FROM area_rtree r JOIN areas a ON a.id=r.id
               WHERE r.minlon<=? AND r.maxlon>=? AND r.minlat<=? AND r.maxlat>=?""",
            (lon, lon, lat, lat)):
        for ring in json.loads(rings):
            if point_in_ring(lon, lat, ring):
                areas.append({'name': name, 'type': typ})
                break
    return {'nearest': nearest, 'areas': areas}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass  # 조용히

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
                a = con.execute("SELECT v FROM meta WHERE k='areas'").fetchone()
                return self._send({'ok': True, 'places': int(p[0]) if p else 0, 'areas': int(a[0]) if a else 0})
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


if __name__ == '__main__':
    print(f"geocode-api: DB={DB_PATH} PORT={PORT}", file=sys.stderr)
    ThreadingHTTPServer(('0.0.0.0', PORT), Handler).serve_forever()
