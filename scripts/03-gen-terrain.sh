#!/usr/bin/env bash
# [온라인 단계] SRTM 30m (AWS elevation-tiles-prod, 인증 불필요)
#   → GDAL 병합/웹메르카토르 투영 → rio-rgbify Terrain-RGB mbtiles
# 한국 범위: 위도 N33~N38, 경도 E124~E131 (48타일, ~1GB)
# 추후 국토지리정보원 DEM 으로 교체 시: data/dem/hgt 대신 GeoTIFF 를
# gdalbuildvrt 입력으로 주면 이후 단계는 동일하다(출처 독립 설계).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
HGT="$ROOT/data/dem/hgt"
mkdir -p "$HGT"

echo "[1/4] SRTM HGT 다운로드"
for lat in 33 34 35 36 37 38; do
  for lon in 124 125 126 127 128 129 130 131; do
    f="N${lat}E${lon}.hgt"
    [ -f "$HGT/$f" ] && continue
    if curl -sfL "https://s3.amazonaws.com/elevation-tiles-prod/skadi/N${lat}/N${lat}E${lon}.hgt.gz" \
         -o "$HGT/$f.gz.tmp"; then
      gunzip -c "$HGT/$f.gz.tmp" > "$HGT/$f.tmp" && mv "$HGT/$f.tmp" "$HGT/$f"
      rm -f "$HGT/$f.gz.tmp"
      echo "  ✓ $f"
    else
      rm -f "$HGT/$f.gz.tmp"
      echo "  - $f (해당 구역 데이터 없음 — 바다 구역은 정상)"
    fi
  done
done

echo "[2/4] 병합(VRT) 및 EPSG:3857 투영"
gdalbuildvrt -overwrite "$ROOT/data/dem/korea.vrt" "$HGT"/*.hgt
gdalwarp -overwrite -t_srs EPSG:3857 -r bilinear -multi \
  -co COMPRESS=DEFLATE -co BIGTIFF=YES \
  "$ROOT/data/dem/korea.vrt" "$ROOT/data/dem/korea-3857.tif"

echo "[3/4] Terrain-RGB 인코딩 (z5~z12)"
# WORKAROUND: rio-rgbify 0.4.0 + rasterio 1.5 + GDAL 3.13 에서 EPSG:3857 입력 시
#   transform_bounds(densify_pts=0) 버그 발생 → EPSG:4326 VRT 를 직접 입력.
#   gdalwarp 결과(korea-3857.tif)는 타 용도 참조용으로 보존.
rm -f "$ROOT/tiles/terrain.mbtiles"
rio rgbify -b -10000 -i 0.1 --min-z 5 --max-z 12 \
  -j "$(sysctl -n hw.ncpu 2>/dev/null || nproc)" --format png \
  "$ROOT/data/dem/korea.vrt" "$ROOT/tiles/terrain.mbtiles"

echo "[4/4] mbtiles 메타데이터 보강 (TileServer-GL 서빙용)"
sqlite3 "$ROOT/tiles/terrain.mbtiles" <<'SQL'
INSERT OR REPLACE INTO metadata VALUES('name','terrain');
INSERT OR REPLACE INTO metadata VALUES('format','png');
INSERT OR REPLACE INTO metadata VALUES('minzoom','5');
INSERT OR REPLACE INTO metadata VALUES('maxzoom','12');
INSERT OR REPLACE INTO metadata VALUES('bounds','124.0,33.0,132.0,39.0');
SQL
echo "지형 타일 생성 완료: tiles/terrain.mbtiles"
