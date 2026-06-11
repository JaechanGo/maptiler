#!/usr/bin/env bash
# Usage: ./scripts/00-check-prereqs.sh
# 빌드 머신(인터넷 가능한 Mac)에 필요한 도구가 모두 있는지 점검한다.
# -e 는 의도적으로 생략: need() 가 누락 도구를 모두 수집한 뒤 마지막에 한 번 실패 처리한다.
set -uo pipefail
ok=1
need() {
  if command -v "$1" >/dev/null 2>&1; then
    echo "✓ $1"
  else
    echo "✗ $1 없음 — 설치: $2"
    ok=0
  fi
}
need java    "brew install openjdk@21 (Planetiler는 Java 21+ 필요)"
need docker  "Docker Desktop 설치"
need python3 "brew install python"
# GDAL 바이너리 2종 (둘 다 brew install gdal 로 설치됨)
need gdalwarp     "brew install gdal"
need gdalbuildvrt "brew install gdal"
need sqlite3 "macOS 기본 포함"
need jq      "brew install jq"
need curl    "macOS 기본 포함"
need rio     "pipx install --include-deps rio-rgbify (rio 명령은 rasterio 가 제공)"
need git     "xcode-select --install"
if command -v java >/dev/null 2>&1; then
  echo "--- java 버전(21 이상이어야 함) ---"
  java -version 2>&1 | head -1
fi
if [ "$ok" -eq 1 ]; then
  echo "모든 도구 준비 완료"
else
  echo "누락 도구를 설치한 뒤 다시 실행하세요"
  exit 1
fi
