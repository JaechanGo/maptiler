#!/bin/bash
# geocode.sqlite의 시설(kind=biz) → poi.mbtiles (지도 라벨용, 줌별 밀도 자동조절)
set -e
export PATH="/opt/homebrew/bin:$PATH"
DB="$HOME/geocode-build/geocode.sqlite"
JL="$HOME/geocode-build/poi.geojsonl"
OUT="$HOME/geocode-build/tiles/poi.mbtiles"

echo "[1/2] biz → GeoJSONSeq (이름+좌표 중복 제거)"
python3 - "$DB" "$JL" <<'PY'
import sqlite3, json, sys
db = sqlite3.connect(sys.argv[1]); n = 0
SKIP_NAMES = {"업소명없음", "상호명없음", "-", "."}   # 원본 상가데이터 플레이스홀더 제외
# 같은 상호(공백 무시)+같은 좌표(소수4자리≈11m) 중복은 1건만 — 같은 가게 다중 인허가/출처중복 제거
with open(sys.argv[2], "w", encoding="utf-8") as f:
    for name, sub, cat1, lon, lat in db.execute(
            "SELECT name,subtype,cat1,lon,lat FROM places WHERE kind='biz' "
            "GROUP BY replace(replace(name,' ',''),'　',''), round(lon,4), round(lat,4)"):
        if lon is None or lat is None: continue
        if not name or name.strip() in SKIP_NAMES: continue
        f.write(json.dumps({"type":"Feature",
            "properties":{"name":name,"cat":sub or "","cat1":cat1 or ""},
            "geometry":{"type":"Point","coordinates":[round(lon,6),round(lat,6)]}},
            ensure_ascii=False) + "\n"); n += 1
print(f"  {n:,} features", file=sys.stderr)
PY

echo "[2/2] tippecanoe → $OUT (z11–17, 줌별 솎기 / 클러스터 제거 — 확대 시 라벨 표출)"
tippecanoe -o "$OUT" -l poi -Z11 -z17 \
  --drop-densest-as-needed \
  -y name -y cat -y cat1 --maximum-tile-bytes=700000 --quiet --force "$JL"
rm -f "$JL"
echo "OK: $OUT ($(du -h "$OUT"|cut -f1))"
