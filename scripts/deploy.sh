#!/usr/bin/env bash
# [폐쇄망 단계] 번들 압축 해제 디렉토리에서 실행:
#   tar xzf cuvia-map-bundle.tgz && ./scripts/deploy.sh /path/to/images.tar
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
IMAGES_TAR="${1:-$ROOT/images.tar}"

# C3: Compose v2 플러그인(docker compose) 우선, 없으면 v1 바이너리(docker-compose) 사용
if docker compose version >/dev/null 2>&1; then
  COMPOSE_CMD="docker compose"
elif command -v docker-compose >/dev/null 2>&1; then
  COMPOSE_CMD="docker-compose"
else
  echo "오류: 'docker compose' 플러그인 또는 'docker-compose' 바이너리가 필요합니다." >&2
  exit 1
fi

if [ -f "$IMAGES_TAR" ]; then
  echo "Docker 이미지 적재: $IMAGES_TAR"
  docker load -i "$IMAGES_TAR"
else
  # M1: 이미지 tar 미발견 경고는 stderr 로 출력
  echo "⚠ images.tar 경로를 인자로 주세요 (이미 load 했다면 무시)" >&2
fi

# tileserver-config.json 이 mbtiles 5종을 모두 참조하므로, 하나라도 없으면
# TileServer-GL 이 기동 자체에 실패한다(벡터 단독 degrade 없음) — 사전 검증.
for mb in korea.mbtiles terrain.mbtiles dong.mbtiles buildings.mbtiles poi.mbtiles; do
  if [ ! -s "$ROOT/tiles/$mb" ]; then   # -s: 존재+크기>0 (0바이트 evict/잘린 파일도 차단, package.sh와 통일)
    echo "오류: tiles/$mb 가 없거나 0바이트 — 번들이 불완전합니다. 압축 해제 경로를 확인하세요." >&2
    exit 1
  fi
done

# geocode 서비스용 지오코딩 인덱스 — compose 의 geocode 컨테이너가 참조한다.
if [ ! -s "$ROOT/geocode/geocode.sqlite" ]; then
  echo "오류: geocode/geocode.sqlite 가 없습니다 — 번들이 불완전합니다." >&2
  exit 1
fi

# shellcheck disable=SC2086
cd "$ROOT/server" && $COMPOSE_CMD up -d

# W1: tileserver 헬스 대기 (최대 60초, 5초 간격 × 12회)
echo "tileserver 헬스 확인 중 (최대 60초)..."
_waited=0
until curl -sf http://localhost:8080/health >/dev/null 2>&1; do
  if [ "$_waited" -ge 60 ]; then
    echo "오류: tileserver 가 60초 내에 응답하지 않습니다." >&2
    echo "  로그 확인: $COMPOSE_CMD logs tileserver" >&2
    exit 1
  fi
  sleep 5
  _waited=$((_waited + 5))
done
echo "tileserver 정상 응답 확인"

echo "기동 완료:"
echo "  스타일  http://<이서버IP>:8080/styles/cuvia/style.json"
echo "  데모    http://<이서버IP>:8081/demo/"
echo
echo "스타일 디자인(Style Studio)을 현장에서 띄우려면(선택):"
echo "  STUDIO_TOKEN=\$(openssl rand -hex 12) ./scripts/start-style-studio.sh   # → http://<이서버IP>:8091/?token=…"
echo "  (저장 시 style.json 갱신 + tileserver 자동 재시작. 색·POI·글꼴 테마 현장 지정)"
