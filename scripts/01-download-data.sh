#!/usr/bin/env bash
# [온라인 단계] 폐쇄망 반입에 필요한 모든 원본/에셋을 내려받는다.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"

# 에어갭 재현성을 위해 버전 고정 (latest 리다이렉트 사용 금지) — 단일출처 scripts/versions.sh (package.sh 공용).
source "$ROOT/scripts/versions.sh"

# 부분 다운로드 임시 파일 정리 트랩 (bash 3.2 호환)
TMP_ZIP=""
TMP_DIR=""
trap 'rm -rf ${TMP_ZIP:+"$TMP_ZIP"} ${TMP_DIR:+"$TMP_DIR"}' EXIT

mkdir -p "$ROOT/planetiler" "$ROOT/data/osm" "$ROOT/data/dem/hgt" "$ROOT/tiles" \
         "$ROOT/vendor/maplibre" "$ROOT/vendor/maputnik" "$ROOT/style/glyphs"

echo "[1/5] Planetiler jar"
if [ ! -f "$ROOT/planetiler/planetiler.jar" ]; then
  curl -fL -o "$ROOT/planetiler/planetiler.jar.tmp" \
    "https://github.com/onthegomap/planetiler/releases/download/${PLANETILER_VERSION}/planetiler.jar" \
    && mv "$ROOT/planetiler/planetiler.jar.tmp" "$ROOT/planetiler/planetiler.jar"
fi

echo "[2/5] OSM 한국 추출본 (Geofabrik, ~150MB)"
if [ ! -f "$ROOT/data/osm/south-korea.osm.pbf" ]; then
  curl -fL -o "$ROOT/data/osm/south-korea.osm.pbf.tmp" \
    https://download.geofabrik.de/asia/south-korea-latest.osm.pbf \
    && mv "$ROOT/data/osm/south-korea.osm.pbf.tmp" "$ROOT/data/osm/south-korea.osm.pbf"
fi

echo "[3/5] 글리프 폰트 (KlokanTech Noto Sans — 한글 포함)"
# klokantech/klokantech-gl-fonts 는 Git LFS 저장소로, --depth 1 clone 시 PBF가 45-byte 포인터 스텁으로
# 내려받힌다. 대신 openmaptiles/fonts noto-open-sans.zip(동일 Google Noto Sans 기반, 실제 PBF 포함)을
# 받아서 스타일에서 참조하는 디렉터리 이름("KlokanTech Noto Sans Regular/Bold")으로 배치한다.
if [ ! -d "$ROOT/style/glyphs/KlokanTech Noto Sans Regular" ]; then
  TMP_ZIP="$(mktemp /tmp/noto-open-sans.XXXXXX.zip)"
  TMP_DIR="$(mktemp -d /tmp/noto_extract.XXXXXX)"
  curl -fL -o "$TMP_ZIP" \
    "https://github.com/openmaptiles/fonts/releases/download/${FONTS_VERSION}/noto-open-sans.zip"
  unzip -oq "$TMP_ZIP" -d "$TMP_DIR"
  mv "$TMP_DIR/Noto Sans Regular" "$ROOT/style/glyphs/KlokanTech Noto Sans Regular"
  mv "$TMP_DIR/Noto Sans Bold"    "$ROOT/style/glyphs/KlokanTech Noto Sans Bold" 2>/dev/null || true
  rm -rf "$TMP_ZIP" "$TMP_DIR"
  TMP_ZIP=""
  TMP_DIR=""
fi

echo "[4/5] MapLibre GL JS (로컬 번들, 소비 프론트와 동일 메이저)"
# [ ! -s ] (존재 & 크기>0) — 0바이트 스텁(iCloud evict·중단된 다운로드)이면 재다운로드. [ ! -f ]는 0바이트를 통과시켜 영구 잔존.
if [ ! -s "$ROOT/vendor/maplibre/maplibre-gl.js" ]; then
  curl -fL -o "$ROOT/vendor/maplibre/maplibre-gl.js.tmp" \
    "https://unpkg.com/maplibre-gl@${MAPLIBRE_VERSION}/dist/maplibre-gl.js" \
    && mv "$ROOT/vendor/maplibre/maplibre-gl.js.tmp" "$ROOT/vendor/maplibre/maplibre-gl.js"
fi
if [ ! -s "$ROOT/vendor/maplibre/maplibre-gl.css" ]; then
  curl -fL -o "$ROOT/vendor/maplibre/maplibre-gl.css.tmp" \
    "https://unpkg.com/maplibre-gl@${MAPLIBRE_VERSION}/dist/maplibre-gl.css" \
    && mv "$ROOT/vendor/maplibre/maplibre-gl.css.tmp" "$ROOT/vendor/maplibre/maplibre-gl.css"
fi

echo "[5/5] Maputnik (오프라인 스타일 편집기)"
# v3.0.0 릴리즈에 업로드된 에셋 없음 — v2.1.1 dist.zip(정적 빌드, ~4.8 MB) 사용
if [ ! -f "$ROOT/vendor/maputnik/dist/index.html" ]; then
  TMP_ZIP="$(mktemp /tmp/maputnik.XXXXXX.zip)"
  if curl -fL -o "$TMP_ZIP" https://github.com/maplibre/maputnik/releases/download/v2.1.1/dist.zip; then
    unzip -oq "$TMP_ZIP" -d "$ROOT/vendor/maputnik"
    rm -rf "$TMP_ZIP"
    TMP_ZIP=""
  else
    rm -rf "$TMP_ZIP"
    TMP_ZIP=""
    echo "⚠ maputnik.zip 자동 다운로드 실패 — https://github.com/maplibre/maputnik/releases 에서 정적 빌드 zip을 받아 vendor/maputnik/ 에 풀어주세요 (선택 항목, 빌드는 계속 진행 가능)"
  fi
fi
echo "다운로드 완료"
