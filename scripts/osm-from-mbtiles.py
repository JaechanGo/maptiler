#!/usr/bin/env python3
"""korea.mbtiles → OSM 지오코딩 소스(osm.sqlite) 재추출 (무의존 MVT 디코드).
scripts/07-gen-geocode.py 의 extract_mbtiles 로직 재현 — iCloud evict된 geocode.sqlite 대체용.
출력 places(id,name,type,subtype,lon,lat) 를 09-gen-geocode.py 의 --osm 입력으로 사용.
"""
import gzip, math, os, sqlite3, sys, re, unicodedata

MB = "/Users/jaechango_cudo/Library/Mobile Documents/com~apple~CloudDocs/maptiler/tiles/korea.mbtiles"
OUT = os.path.expanduser("~/geocode-build/osm.sqlite")
EXTENT = 4096; Z = 14

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
    p = 0
    cmd, p = rv(geo, p)
    if (cmd & 7) != 1: return None
    x, p = rv(geo, p); y, p = rv(geo, p)
    return ((x >> 1) ^ -(x & 1), (y >> 1) ^ -(y & 1))

def tile_lonlat(tx, ty, px, py):
    n = 1 << Z
    lon = (tx + px / EXTENT) / n * 360.0 - 180.0
    yn = (ty + py / EXTENT) / n
    lat = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * yn))))
    return lon, lat

def norm(s): return re.sub(r'\s+', ' ', unicodedata.normalize('NFC', s)).strip()
def station_display(name, is_station):
    name = norm(name)
    return name + '역' if (is_station and not name.endswith('역')) else name

def extract():
    db = sqlite3.connect(f"file:{MB}?mode=ro", uri=True)
    WANT = {'poi', 'place', 'transportation_name'}
    total = db.execute("SELECT count(*) FROM tiles WHERE zoom_level=?", (Z,)).fetchone()[0]
    done = 0
    for tc, tr, blob in db.execute("SELECT tile_column,tile_row,tile_data FROM tiles WHERE zoom_level=?", (Z,)):
        done += 1
        if done % 20000 == 0: print(f"  ...{done:,}/{total:,}", file=sys.stderr)
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
            def val(i, _vals=vals, _vc=vcache):
                if i not in _vc:
                    r = None
                    for a, b2, c in fields(_vals[i]):
                        if a == 1: r = c.decode('utf-8', 'replace')
                    _vc[i] = r
                return _vc[i]
            for fb in feats:
                tags = {}; geo = None
                for ff, ww, vv in fields(fb):
                    if ff == 2:
                        arr = []; p = 0
                        while p < len(vv): gi, p = rv(vv, p); arr.append(gi)
                        for i in range(0, len(arr) - 1, 2):
                            k = keys[arr[i]]
                            if k in ('name', 'name:ko', 'class', 'subclass'): tags[k] = val(arr[i + 1])
                    elif ff == 4: geo = vv
                nm = tags.get('name:ko') or tags.get('name')
                if not nm or geo is None: continue
                pt = first_point(geo)
                if not pt or not (0 <= pt[0] <= EXTENT - 1 and 0 <= pt[1] <= EXTENT - 1): continue
                lon, lat = tile_lonlat(tc, ty, pt[0], pt[1])
                cls = tags.get('class', '')
                is_station = (lname == 'poi' and cls == 'railway')
                typ = {'poi': 'poi', 'place': 'place', 'transportation_name': 'road'}[lname]
                if is_station: typ = 'station'
                yield (station_display(nm, is_station), typ, cls, lon, lat)

def main():
    tmp = OUT + ".tmp"
    if os.path.exists(tmp): os.remove(tmp)
    db = sqlite3.connect(tmp)
    db.executescript("PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF;"
                     "CREATE TABLE places(id INTEGER PRIMARY KEY, name TEXT, type TEXT, subtype TEXT, lon REAL, lat REAL);")
    seen = set(); pid = 0; batch = []
    for name, typ, sub, lon, lat in extract():
        if not (124 <= lon <= 132 and 33 <= lat <= 39): continue
        key = (typ, name, round(lon, 5), round(lat, 5))
        if key in seen: continue
        seen.add(key); pid += 1
        batch.append((pid, name, typ, sub, round(lon, 6), round(lat, 6)))
        if len(batch) >= 50000:
            db.executemany("INSERT INTO places VALUES(?,?,?,?,?,?)", batch); batch.clear()
    if batch: db.executemany("INSERT INTO places VALUES(?,?,?,?,?,?)", batch)
    db.commit(); db.close(); os.replace(tmp, OUT)
    print(f"OK: {OUT}  OSM {pid:,}건")
    c = sqlite3.connect(OUT)
    print("  type:", c.execute("SELECT type,count(*) FROM places GROUP BY type ORDER BY 2 DESC").fetchall())

if __name__ == "__main__":
    main()
