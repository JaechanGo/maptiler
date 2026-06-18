#!/bin/bash
# geocode.sqlite의 시설(kind=biz) → poi.mbtiles (지도 라벨용, 줌별 밀도 자동조절)
set -e
export PATH="/opt/homebrew/bin:$PATH"
DB="$HOME/geocode-build/geocode.sqlite"
JL="$HOME/geocode-build/poi.geojsonl"
OUT="$HOME/geocode-build/tiles/poi.mbtiles"

echo "[1/2] biz → GeoJSONSeq"
python3 - "$DB" "$JL" <<'PY'
import sqlite3, json, sys
db = sqlite3.connect(sys.argv[1]); n = 0
with open(sys.argv[2], "w", encoding="utf-8") as f:
    for name, sub, cat1, lon, lat in db.execute(
            "SELECT name,subtype,cat1,lon,lat FROM places WHERE kind='biz'"):
        if lon is None or lat is None: continue
        f.write(json.dumps({"type":"Feature",
            "properties":{"name":name,"cat":sub or "","cat1":cat1 or ""},
            "geometry":{"type":"Point","coordinates":[round(lon,6),round(lat,6)]}},
            ensure_ascii=False) + "\n"); n += 1
print(f"  {n:,} features", file=sys.stderr)
PY

echo "[2/2] tippecanoe → $OUT (z11–16, 줌별 솎기+클러스터)"
tippecanoe -o "$OUT" -l poi -Z11 -z16 \
  --drop-densest-as-needed --cluster-distance=6 \
  -y name -y cat -y cat1 --maximum-tile-bytes=700000 --quiet --force "$JL"
rm -f "$JL"
echo "OK: $OUT ($(du -h "$OUT"|cut -f1))"
