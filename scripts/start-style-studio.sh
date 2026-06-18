#!/usr/bin/env bash
# [폐쇄망/쇼룸] Style Studio를 호스트에서 기동 — 지도 스택(compose) 옆에서 스타일을 현장 지정.
# 호스트 실행이라 docker로 tileserver 재시작이 가능(스타일 저장 시 자동 reload).
#   STUDIO_TOKEN=secret ./scripts/start-style-studio.sh      # 토큰 보호(권장, LAN 노출 시)
#   COMPOSE_FILE=/path/docker-compose.yml ./scripts/start-style-studio.sh
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export COMPOSE_FILE="${COMPOSE_FILE:-$ROOT/server/docker-compose.yml}"   # tileserver 재시작 대상
export HOST="${HOST:-0.0.0.0}"          # 운영 서버에서 접속하려면 0.0.0.0. 로컬 전용은 127.0.0.1
export PORT="${PORT:-8091}"
export TILE_PORT="${TILE_PORT:-8080}"

if [ ! -f "$COMPOSE_FILE" ]; then
  echo "경고: COMPOSE_FILE 없음($COMPOSE_FILE) — 스타일 저장 시 tileserver 재시작이 스킵됩니다." >&2
fi
if [ -z "${STUDIO_TOKEN:-}" ] && [ "$HOST" = "0.0.0.0" ]; then
  echo "⚠ STUDIO_TOKEN 미설정 + 0.0.0.0 바인드 — 신뢰망에서만 쓰거나 STUDIO_TOKEN 설정 권장." >&2
  echo "  예) STUDIO_TOKEN=\$(openssl rand -hex 12) ./scripts/start-style-studio.sh" >&2
fi
echo "CUVIA Style Studio → http://<서버IP>:$PORT  (?token=… 로 접속, COMPOSE_FILE=$COMPOSE_FILE)"
exec python3 "$ROOT/scripts/style-studio.py"
