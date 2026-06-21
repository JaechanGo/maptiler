#!/usr/bin/env bash
# 폐쇄망 반입 자산 버전 핀(에어갭 재현성) — 01-download-data.sh·package.sh 공용 단일출처.
# latest 리다이렉트 금지. MAPLIBRE 변경 시: 01 이 vendor/maplibre 재다운로드 + demo/스타일 동기 확인.
# 각 값은 env override 가능(이미 설정돼 있으면 그 값 유지).
export MAPLIBRE_VERSION="${MAPLIBRE_VERSION:-5.16.0}"     # vendor/maplibre/maplibre-gl.{js,css} (unpkg)
export PLANETILER_VERSION="${PLANETILER_VERSION:-v0.10.2}" # planetiler.jar (GitHub release)
export FONTS_VERSION="${FONTS_VERSION:-v2.0}"             # openmaptiles/fonts noto-open-sans.zip
