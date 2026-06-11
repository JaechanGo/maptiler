#!/usr/bin/env bash
# [폐쇄망 단계] 번들 압축 해제 디렉토리에서 실행:
#   tar xzf cuvia-map-bundle.tgz && ./scripts/deploy.sh /path/to/images.tar
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
IMAGES_TAR="${1:-$ROOT/images.tar}"

if [ -f "$IMAGES_TAR" ]; then
  echo "Docker 이미지 적재: $IMAGES_TAR"
  docker load -i "$IMAGES_TAR"
else
  echo "⚠ images.tar 경로를 인자로 주세요 (이미 load 했다면 무시)"
fi

cd "$ROOT/server" && docker compose up -d
echo "기동 완료:"
echo "  스타일  http://<이서버IP>:8080/styles/cuvia/style.json"
echo "  데모    http://<이서버IP>:8081/demo/"
