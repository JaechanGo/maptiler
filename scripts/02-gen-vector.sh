#!/usr/bin/env bash
# [온라인 단계] OSM 추출본 → OpenMapTiles 스키마 벡터타일(.mbtiles)
# --download: Natural Earth/수역 폴리곤 등 보조 데이터 자동 다운로드(최초 1회, ~1GB)
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
# -Xmx: 한국 규모는 12g면 충분. 더 큰 지역(대륙/행성)은 머신 RAM에 맞춰 상향할 것.
java -Xmx12g -jar planetiler/planetiler.jar \
  --osm_path="data/osm/south-korea.osm.pbf" \
  --output="tiles/korea.mbtiles" \
  --download --force
echo "벡터 타일 생성 완료: tiles/korea.mbtiles"
