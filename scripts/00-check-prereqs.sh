#!/usr/bin/env bash
# 빌드 머신(인터넷 가능한 Mac)에 필요한 도구가 모두 있는지 점검한다.
set -uo pipefail
ok=1
need() {
  if command -v "$1" >/dev/null 2>&1; then
    echo "✓ $1"
  else
    echo "✗ $1 없음 — 설치: $2"; ok=0
  fi
}
need java    "brew install openjdk@21 (Planetiler는 Java 21+ 필요)"
need docker  "Docker Desktop 설치"
need python3 "brew install python"
need gdalwarp "brew install gdal"
need gdalbuildvrt "brew install gdal"
need sqlite3 "macOS 기본 포함"
need jq      "brew install jq"
need curl    "macOS 기본 포함"
need rio     "pipx install rio-rgbify (또는 pip3 install rio-rgbify)"
need git     "xcode-select --install"
echo "--- java 버전(21 이상이어야 함) ---"
java -version 2>&1 | head -1
if [ "$ok" -eq 1 ]; then echo "모든 도구 준비 완료"; else echo "누락 도구를 설치한 뒤 다시 실행하세요"; exit 1; fi
