#!/usr/bin/env bash
# 폐쇄망 반입 자산 버전 핀(에어갭 재현성) — 01-download-data.sh·package.sh 공용 단일출처.
# latest 리다이렉트 금지. MAPLIBRE 변경 시: 01 이 vendor/maplibre 재다운로드 + demo/스타일 동기 확인.
# 각 값은 env override 가능(이미 설정돼 있으면 그 값 유지).
export MAPLIBRE_VERSION="${MAPLIBRE_VERSION:-5.16.0}"     # vendor/maplibre/maplibre-gl.{js,css} (unpkg)
export PLANETILER_VERSION="${PLANETILER_VERSION:-v0.10.2}" # planetiler.jar (GitHub release)
export FONTS_VERSION="${FONTS_VERSION:-v2.0}"             # openmaptiles/fonts noto-open-sans.zip
export OSRM_IMAGE="${OSRM_IMAGE:-osrm/osrm-backend:v5.25.0}" # 길찾기 그래프 빌드(07)·서빙 공용 — server/docker-compose.yml 의 osrm-car/foot 태그와 반드시 일치(그래프 파일 포맷이 버전 결합)
