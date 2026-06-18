#!/bin/bash
# GIS건물통합정보(시도별 SHP, EPSG:5186) → 3D 건물 벡터타일 buildings.mbtiles
# 디스크 절약형: 시도별로 변환→타일→중간파일 즉시 삭제, 마지막에 tile-join 병합.
# 컬럼(값 샘플 확정): A16=높이(m, 결측多), A26=지상층수. render_height=A16>0?A16:A26*3.3 (둘다0→6m).
set -euo pipefail
SRC="${1:-$HOME/Downloads/gis}"
OUTDIR="$HOME/geocode-build/tiles"; PARTS="$OUTDIR/_parts"; mkdir -p "$PARTS"
TMPJL="$HOME/geocode-build/_one.geojsonl"
MBT="$OUTDIR/buildings.mbtiles"
rm -f "$PARTS"/*.mbtiles 2>/dev/null || true

i=0
while IFS= read -r shp; do
  case "$shp" in *"(1)"*) echo "  (중복 제외) $shp"; continue;; esac
  lyr=$(basename "$shp" .shp); i=$((i+1))
  ogr2ogr -f GeoJSONSeq "$TMPJL" "$shp" \
    -s_srs EPSG:5186 -t_srs EPSG:4326 -skipfailures \
    -dialect SQLITE -sql "SELECT GEOMETRY,
        CASE WHEN CAST(A16 AS REAL) > 0 THEN ROUND(CAST(A16 AS REAL),1)
             WHEN CAST(A26 AS REAL) > 0 THEN ROUND(CAST(A26 AS REAL)*3.3,1)
             ELSE 6 END AS render_height,
        CAST(A26 AS INTEGER) AS levels
      FROM \"$lyr\" WHERE GEOMETRY IS NOT NULL" 2>/dev/null
  tippecanoe -o "$PARTS/$lyr.mbtiles" -l building -Z13 -z16 \
    --drop-densest-as-needed --simplification=4 --no-tile-size-limit \
    -y render_height -y levels --quiet --force "$TMPJL"
  echo "  [$i] $lyr → $(du -h "$PARTS/$lyr.mbtiles"|cut -f1)  (여유 $(df -h /System/Volumes/Data|awk 'NR==2{print $4}'))"
  rm -f "$TMPJL"
done < <(find "$SRC" -iname '*.shp')

echo "[병합] tile-join → $MBT"
tile-join -o "$MBT" --no-tile-size-limit --force "$PARTS"/*.mbtiles
rm -rf "$PARTS"
echo "OK: $MBT ($(du -h "$MBT"|cut -f1))"
