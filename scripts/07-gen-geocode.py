#!/usr/bin/env python3
"""지오코딩/역지오코딩 인덱스 빌드 → geocode/geocode.sqlite (무의존: SQLite 내장 FTS5+R-tree만).

forward(지오코딩)  : 이름/주소 텍스트 → 좌표   (FTS5 unicode61 + prefix, 한글 별칭)
reverse(역지오코딩): 좌표 → 최근접 장소(R-tree) + 포함 영역 point-in-polygon(행정동/지번 등)

입력 소스(있는 것만 사용):
  - data/dong/dong-labels.geojson      : 아파트 동(점)            [04/05 생성물]
  - tiles/korea.mbtiles                : 역·지명·도로명·POI(점/선) [OSM]
  - (옵션) --poi-csv  상호,업종,경도,위도 헤더 CSV  : 국가 상가정보(15083033)
  - (옵션) --areas-geojson  Polygon/MultiPolygon(name 속성) : 행정경계/연속지적 등(역지오코딩 영역)

국가데이터(SHP)는 먼저 ogr2ogr 로 EPSG:4326 GeoJSON/CSV 로 변환 후 위 인자로 투입.
폐쇄망 2단계: 온라인에서 이 인덱스를 빌드 → 반입 → server/geocode-api.py 가 서빙.
"""
import argparse, csv, gzip, json, math, pathlib, re, sqlite3, sys, unicodedata

ROOT = pathlib.Path(__file__).resolve().parents[1]
MB = ROOT / "tiles/korea.mbtiles"
DONG = ROOT / "data/dong/dong-labels.geojson"
OUT = ROOT / "geocode/geocode.sqlite"
EXTENT = 4096
# korea.mbtiles poi/place/transportation_name 추출 줌
Z = 14
# 역지오코딩 최근접 검색 윈도(도). 약 ±1.1km부터 시작해 없으면 확장.
NEAR_WIN = [0.01, 0.05, 0.2]

# ---- MVT 디코드(무의존) ----------------------------------------------------
def rv(b, p):
    s = 0; r = 0
    while True:
        x = b[p]; p += 1; r |= (x & 0x7f) << s
        if not x & 0x80: return r, p
        s += 7

def fields(b):
    p = 0; n = len(b)
    while p < n:
        t, p = rv(b, p); f = t >> 3; w = t & 7
        if w == 0: v, p = rv(b, p); yield f, w, v
        elif w == 2: ln, p = rv(b, p); yield f, w, b[p:p+ln]; p += ln
        elif w == 1: yield f, w, b[p:p+8]; p += 8
        elif w == 5: yield f, w, b[p:p+4]; p += 4

def first_point(geo):
    # geometry command stream 의 첫 MoveTo 좌표(점/선 공통 앵커)
    p = 0
    cmd, p = rv(geo, p)
    if (cmd & 7) != 1: return None      # MoveTo 아님
    x, p = rv(geo, p); y, p = rv(geo, p)
    return ((x >> 1) ^ -(x & 1), (y >> 1) ^ -(y & 1))

def tile_lonlat(tx, ty, px, py):
    n = 1 << Z
    lon = (tx + px / EXTENT) / n * 360.0 - 180.0
    yn = (ty + py / EXTENT) / n
    lat = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * yn))))
    return lon, lat

# ---- 정규화/별칭 -----------------------------------------------------------
def norm(s):
    return re.sub(r'\s+', ' ', unicodedata.normalize('NFC', s)).strip()

def search_text(name, is_station):
    # FTS 검색용 텍스트: 표시명 + 공백제거형 + (역 접미사 양쪽)
    name = norm(name)
    variants = {name, name.replace(' ', '')}
    if is_station:
        base = name[:-1] if name.endswith('역') else name
        variants.add(base); variants.add(base + '역')
        variants.add(base.replace(' ', '')); variants.add((base + '역').replace(' ', ''))
    return ' '.join(v for v in variants if v)

def station_display(name, is_station):
    name = norm(name)
    if is_station and not name.endswith('역'):
        return name + '역'
    return name

# ---- 추출 ------------------------------------------------------------------
def extract_mbtiles():
    if not MB.exists():
        print(f"  (건너뜀) {MB} 없음", file=sys.stderr); return
    WANT = {'poi', 'place', 'transportation_name'}
    db = sqlite3.connect(MB)
    total = db.execute("SELECT count(*) FROM tiles WHERE zoom_level=?", (Z,)).fetchone()[0]
    print(f"  korea.mbtiles z{Z} {total:,}타일 디코드...", file=sys.stderr)
    done = 0
    for tc, tr, blob in db.execute("SELECT tile_column,tile_row,tile_data FROM tiles WHERE zoom_level=?", (Z,)):
        done += 1
        if done % 10000 == 0:
            print(f"    ...{done:,}/{total:,}", file=sys.stderr)
        ty = (1 << Z) - 1 - tr
        try: tile = gzip.decompress(blob)
        except Exception: tile = blob
        for f, w, lb in fields(tile):
            if f != 3: continue
            lname = None; keys = []; vals = []; feats = []
            for ff, ww, vv in fields(lb):
                if ff == 1: lname = vv.decode('utf-8', 'replace')
                elif ff == 3: keys.append(vv.decode('utf-8', 'replace'))
                elif ff == 4: vals.append(vv)
                elif ff == 2: feats.append(vv)
            if lname not in WANT: continue
            vcache = {}
            def val(i):
                if i not in vcache:
                    r = None
                    for a, b2, c in fields(vals[i]):
                        if a == 1: r = c.decode('utf-8', 'replace')
                    vcache[i] = r
                return vcache[i]
            for fb in feats:
                tags = {}; geo = None
                for ff, ww, vv in fields(fb):
                    if ff == 2:
                        arr = []; p = 0
                        while p < len(vv): gi, p = rv(vv, p); arr.append(gi)
                        for i in range(0, len(arr) - 1, 2):
                            k = keys[arr[i]]
                            if k in ('name', 'name:ko', 'class', 'subclass'):
                                tags[k] = val(arr[i + 1])
                    elif ff == 4: geo = vv
                nm = tags.get('name:ko') or tags.get('name')
                if not nm or geo is None: continue
                pt = first_point(geo)
                if not pt or not (0 <= pt[0] <= EXTENT - 1 and 0 <= pt[1] <= EXTENT - 1):
                    continue        # 버퍼 복사본 제외(전국 중복 방지)
                lon, lat = tile_lonlat(tc, ty, pt[0], pt[1])
                cls = tags.get('class', '')
                is_station = (lname == 'poi' and cls == 'railway')
                typ = {'poi': 'poi', 'place': 'place', 'transportation_name': 'road'}[lname]
                if is_station: typ = 'station'
                yield (station_display(nm, is_station), typ, cls, lon, lat, is_station)
    db.close()

def extract_dong():
    if not DONG.exists():
        print(f"  (건너뜀) {DONG} 없음", file=sys.stderr); return
    feats = json.loads(DONG.read_text(encoding='utf-8'))['features']
    print(f"  동 라벨 {len(feats):,}건", file=sys.stderr)
    for ft in feats:
        lon, lat = ft['geometry']['coordinates']
        yield (ft['properties']['dong'], 'dong', 'apartments', lon, lat, False)

def extract_poi_csv(path):
    # 국가 상가정보 CSV: 헤더에서 상호명/경도/위도/상권업종 자동 매핑
    print(f"  상가 CSV: {path}", file=sys.stderr)
    with open(path, encoding='utf-8-sig', newline='') as f:
        rd = csv.DictReader(f)
        cols = {c: c for c in rd.fieldnames or []}
        def find(*cands):
            for c in (rd.fieldnames or []):
                if any(k in c for k in cands): return c
            return None
        c_name = find('상호명', '상호'); c_lon = find('경도', '경도(', 'lon', 'x'); c_lat = find('위도', 'lat', 'y')
        c_cat = find('상권업종소분류명', '업종', '분류명')
        if not (c_name and c_lon and c_lat):
            print(f"    ⚠ 헤더에서 상호/경도/위도 컬럼을 못 찾음: {rd.fieldnames}", file=sys.stderr); return
        n = 0
        for row in rd:
            try:
                lon = float(row[c_lon]); lat = float(row[c_lat])
            except (TypeError, ValueError):
                continue
            nm = (row.get(c_name) or '').strip()
            if not nm: continue
            n += 1
            yield (nm, 'biz', (row.get(c_cat) or '').strip() if c_cat else '', lon, lat, False)
        print(f"    상가 {n:,}건", file=sys.stderr)

def load_areas(path):
    # 행정경계/지번 등 Polygon/MultiPolygon (name 속성) → (name, type, outer_rings[])
    gj = json.loads(pathlib.Path(path).read_text(encoding='utf-8'))
    out = []
    for ft in gj.get('features', []):
        props = ft.get('properties', {})
        name = props.get('name') or props.get('NAME') or props.get('adm_nm') or props.get('EMD_KOR_NM') \
            or props.get('A2') or props.get('JIBUN') or ''
        g = ft.get('geometry') or {}
        gt = g.get('type'); coords = g.get('coordinates')
        rings = []
        if gt == 'Polygon': rings = [coords[0]]
        elif gt == 'MultiPolygon': rings = [poly[0] for poly in coords]
        if name and rings:
            out.append((norm(str(name)), props.get('type', 'area'), rings))
    return out

# ---- 빌드 ------------------------------------------------------------------
def build(rows, areas):
    OUT.parent.mkdir(parents=True, exist_ok=True)
    tmp = OUT.with_suffix('.sqlite.tmp'); tmp.unlink(missing_ok=True)
    db = sqlite3.connect(tmp)
    db.executescript("""
      PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF;
      CREATE TABLE places(id INTEGER PRIMARY KEY, name TEXT, type TEXT, subtype TEXT, lon REAL, lat REAL);
      CREATE VIRTUAL TABLE places_fts USING fts5(txt, tokenize='unicode61', prefix='2 3 4');
      CREATE VIRTUAL TABLE place_rtree USING rtree(id, minlon, maxlon, minlat, maxlat);
      CREATE TABLE areas(id INTEGER PRIMARY KEY, name TEXT, type TEXT, rings TEXT);
      CREATE VIRTUAL TABLE area_rtree USING rtree(id, minlon, maxlon, minlat, maxlat);
    """)
    seen = set(); pid = 0
    type_rank = {'station': 0, 'place': 1, 'dong': 2, 'road': 3, 'poi': 4, 'biz': 5}
    for name, typ, sub, lon, lat, is_station in rows:
        if not (124 <= lon <= 132 and 33 <= lat <= 39): continue    # 한국 범위 가드
        key = (typ, name, round(lon, 5), round(lat, 5))
        if key in seen: continue
        seen.add(key); pid += 1
        db.execute("INSERT INTO places VALUES(?,?,?,?,?,?)", (pid, name, typ, sub, round(lon, 6), round(lat, 6)))
        db.execute("INSERT INTO places_fts(rowid,txt) VALUES(?,?)", (pid, search_text(name, is_station)))
        db.execute("INSERT INTO place_rtree VALUES(?,?,?,?,?)", (pid, lon, lon, lat, lat))
    aid = 0
    for name, typ, rings in areas:
        xs = [p[0] for r in rings for p in r]; ys = [p[1] for r in rings for p in r]
        if not xs: continue
        aid += 1
        db.execute("INSERT INTO areas VALUES(?,?,?,?)", (aid, name, typ, json.dumps(rings)))
        db.execute("INSERT INTO area_rtree VALUES(?,?,?,?,?)", (aid, min(xs), max(xs), min(ys), max(ys)))
    db.execute("CREATE TABLE meta(k TEXT, v TEXT)")
    db.executemany("INSERT INTO meta VALUES(?,?)", [('places', str(pid)), ('areas', str(aid))])
    db.commit(); db.close()
    tmp.replace(OUT)
    return pid, aid

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--poi-csv', help='국가 상가정보 CSV')
    ap.add_argument('--areas-geojson', help='행정경계/지번 Polygon GeoJSON(역지오코딩)')
    args = ap.parse_args()

    print("[지오코드 인덱스 빌드]", file=sys.stderr)
    def all_rows():
        yield from extract_dong()
        yield from extract_mbtiles()
        if args.poi_csv: yield from extract_poi_csv(args.poi_csv)
    areas = load_areas(args.areas_geojson) if args.areas_geojson else []
    if areas: print(f"  영역(폴리곤) {len(areas):,}건", file=sys.stderr)

    pid, aid = build(all_rows(), areas)
    sz = OUT.stat().st_size / 1048576
    print("=" * 56)
    print(f"OK: {OUT.relative_to(ROOT)}  장소 {pid:,}건 · 영역 {aid:,}건 · {sz:.1f}MB")

if __name__ == '__main__':
    main()
