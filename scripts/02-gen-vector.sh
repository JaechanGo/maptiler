#!/usr/bin/env bash
# [온라인 단계] OSM 추출본 → OpenMapTiles 스키마 벡터타일(.mbtiles)
# --download: Natural Earth/수역 폴리곤 등 보조 데이터 자동 다운로드(최초 1회, ~1GB)
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
mkdir -p tiles data/osm   # 클린 clone 호스트 대비(출력 디렉토리)
# planetiler.jar 는 .gitignore 제외 벤더 바이너리 → clone 호스트엔 없음. JVM 의 모호한 메시지 대신 명확히 안내.
if [ ! -s planetiler/planetiler.jar ]; then
  echo "[오류] planetiler/planetiler.jar 없음 — 'scripts/setup-build-host.sh' 실행(자동 다운로드) 후 재시도" >&2
  exit 1
fi
# -Xmx: 한국 규모는 12g면 충분. 더 큰 지역(대륙/행성)은 머신 RAM에 맞춰 상향할 것.
java -Xmx12g -jar planetiler/planetiler.jar \
  --osm_path="data/osm/south-korea.osm.pbf" \
  --output="tiles/korea.mbtiles" \
  --download --force
echo "벡터 타일 생성 완료: tiles/korea.mbtiles"
