#!/usr/bin/env bash
# [온라인 단계] 폐쇄망 반입에 필요한 모든 원본/에셋을 내려받는다.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
mkdir -p "$ROOT/planetiler" "$ROOT/data/osm" "$ROOT/data/dem/hgt" "$ROOT/tiles" \
         "$ROOT/vendor/maplibre" "$ROOT/vendor/maputnik" "$ROOT/style/glyphs"

echo "[1/5] Planetiler jar"
[ -f "$ROOT/planetiler/planetiler.jar" ] || curl -fL -o "$ROOT/planetiler/planetiler.jar" \
  https://github.com/onthegomap/planetiler/releases/latest/download/planetiler.jar

echo "[2/5] OSM 한국 추출본 (Geofabrik, ~150MB)"
[ -f "$ROOT/data/osm/south-korea.osm.pbf" ] || curl -fL -o "$ROOT/data/osm/south-korea.osm.pbf" \
  https://download.geofabrik.de/asia/south-korea-latest.osm.pbf

echo "[3/5] 글리프 폰트 (KlokanTech Noto Sans — 한글 포함)"
# klokantech/klokantech-gl-fonts 는 Git LFS 저장소로, --depth 1 clone 시 PBF가 45-byte 포인터 스텁으로
# 내려받힌다. 대신 openmaptiles/fonts noto-open-sans.zip(동일 Google Noto Sans 기반, 실제 PBF 포함)을
# 받아서 스타일에서 참조하는 디렉터리 이름("KlokanTech Noto Sans Regular/Bold")으로 배치한다.
if [ ! -d "$ROOT/style/glyphs/KlokanTech Noto Sans Regular" ]; then
  curl -fL -o /tmp/noto-open-sans.zip \
    https://github.com/openmaptiles/fonts/releases/latest/download/noto-open-sans.zip
  mkdir -p /tmp/noto_extract
  unzip -oq /tmp/noto-open-sans.zip -d /tmp/noto_extract
  mv "/tmp/noto_extract/Noto Sans Regular" "$ROOT/style/glyphs/KlokanTech Noto Sans Regular"
  mv "/tmp/noto_extract/Noto Sans Bold"    "$ROOT/style/glyphs/KlokanTech Noto Sans Bold" 2>/dev/null || true
  rm -rf /tmp/noto-open-sans.zip /tmp/noto_extract
fi

echo "[4/5] MapLibre GL JS (로컬 번들, 소비 프론트와 동일 메이저)"
[ -f "$ROOT/vendor/maplibre/maplibre-gl.js" ] || curl -fL -o "$ROOT/vendor/maplibre/maplibre-gl.js" \
  https://unpkg.com/maplibre-gl@5.16.0/dist/maplibre-gl.js
[ -f "$ROOT/vendor/maplibre/maplibre-gl.css" ] || curl -fL -o "$ROOT/vendor/maplibre/maplibre-gl.css" \
  https://unpkg.com/maplibre-gl@5.16.0/dist/maplibre-gl.css

echo "[5/5] Maputnik (오프라인 스타일 편집기)"
# v3.0.0 릴리즈에 업로드된 에셋 없음 — v2.1.1 dist.zip(정적 빌드, ~4.8 MB) 사용
if [ ! -f "$ROOT/vendor/maputnik/dist/index.html" ]; then
  if curl -fL -o /tmp/maputnik.zip https://github.com/maplibre/maputnik/releases/download/v2.1.1/dist.zip; then
    unzip -oq /tmp/maputnik.zip -d "$ROOT/vendor/maputnik" && rm /tmp/maputnik.zip
  else
    echo "⚠ maputnik.zip 자동 다운로드 실패 — https://github.com/maplibre/maputnik/releases 에서 정적 빌드 zip을 받아 vendor/maputnik/ 에 풀어주세요 (선택 항목, 빌드는 계속 진행 가능)"
  fi
fi
echo "다운로드 완료"
