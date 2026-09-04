#!/usr/bin/env bash
# 정사영상(GeoTIFF) → tiles/satellite.mbtiles — 폐쇄망 위성 베이스맵 생성 + tileserver 등록.
#
# 원천: 국토지리정보원 정사영상(국토정보플랫폼, 심사·제공) GeoTIFF 일습을
#   $BUILD_HOME/sources/ortho/ 에 배치(좌표계 EPSG:5186 등 무엇이든 — 3857 로 재투영).
# 요구: GDAL(gdalbuildvrt·gdalwarp·gdal_translate·gdaladdo). 호스트에 없으면 도커
#   이미지(ghcr.io/osgeo/gdal)로 대체 — 폐쇄망 반입 번들에 이미지 포함 필요.
# 용량 감: 전국 z15 급 ≈ 4~5GB(JPEG), 도심 z17 추가 시 수십 GB — 반입 계획에 반영.
#
# 사용:  scripts/gen_satellite_mbtiles.sh [원천폴더] [출력.mbtiles]
#   기본: $BUILD_HOME/sources/ortho → $BUILD_HOME/tiles/satellite.mbtiles
# 완료 후: ① tiles/satellite.mbtiles 를 서빙 위치로 ② 아래 안내대로 tileserver-config.json
#   에 data.satellite 등록 ③ tileserver 재시작 → 데모 '위성' 버튼 자동 활성화.
# ⚠ 등록은 반드시 mbtiles 배치 '후' — tileserver-gl v5 는 파일 부재 시 크래시 루프
#   (2026-09-01 실측). 그래서 저장소 기본 config 에 satellite 를 미리 넣지 않는다.
set -euo pipefail
BUILD_HOME="${BUILD_HOME:-$HOME/geocode-build}"
SRC="${1:-$BUILD_HOME/sources/ortho}"
OUT="${2:-$BUILD_HOME/tiles/satellite.mbtiles}"
WORK="$(dirname "$OUT")/_sat_work"

command -v gdalbuildvrt >/dev/null 2>&1 && GDAL="" || GDAL="docker run --rm -v $SRC:$SRC -v $(dirname "$OUT"):$(dirname "$OUT") ghcr.io/osgeo/gdal:alpine-normal-latest"
ls "$SRC"/*.tif >/dev/null 2>&1 || ls "$SRC"/*.tiff >/dev/null 2>&1 \
  || { echo "✗ 원천 GeoTIFF 없음: $SRC (runbook (4c) — 국토지리정보원 정사영상 반입)" >&2; exit 1; }
mkdir -p "$WORK"

echo "━━ ① 모자이크 VRT (${SRC})"
$GDAL gdalbuildvrt "$WORK/mosaic.vrt" "$SRC"/*.tif*

echo "━━ ② 웹메르카토르(3857) 재투영 VRT"
$GDAL gdalwarp -of VRT -t_srs EPSG:3857 -r bilinear "$WORK/mosaic.vrt" "$WORK/mosaic_3857.vrt"

echo "━━ ③ MBTiles 생성(JPEG — 위성영상은 손실압축이 정배)"
$GDAL gdal_translate -of MBTILES -co TILE_FORMAT=JPEG -co QUALITY=85 \
      "$WORK/mosaic_3857.vrt" "$OUT"

echo "━━ ④ 오버뷰(저줌) 피라미드"
$GDAL gdaladdo -r average "$OUT" 2 4 8 16 32 64 128

rm -rf "$WORK"
echo "OK: $OUT ($(du -h "$OUT" | cut -f1))"
cat <<'EOT'
다음 단계(수동):
 1) satellite.mbtiles 를 서빙 서버 tiles/ 에 배치
 2) server/tileserver-config.json 의 "data" 에 추가:
      "satellite": { "mbtiles": "satellite.mbtiles" }
 3) docker compose restart tileserver
 → 데모 우상단 '위성' 버튼이 자동 활성화된다(/data/satellite.json 프로브).
EOT
