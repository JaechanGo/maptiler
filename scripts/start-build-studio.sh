#!/usr/bin/env bash
# build-studio(빌드 콘솔, :18081)를 백그라운드 데몬으로 (재)기동 — 터미널 종료에도 생존.
# 재실행하면 기존 인스턴스를 끄고 새로 띄운다(코드/HTML 변경 반영 시 그냥 다시 실행).
#   ./scripts/start-build-studio.sh                 # http://localhost:18081
#   PORT=18081 BUILD_HOME=~/geocode-build ./scripts/start-build-studio.sh
# 중지:  lsof -ti tcp:18081 | xargs kill     (또는 PORT 지정)
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export BUILD_HOME="${BUILD_HOME:-$HOME/geocode-build}"
export PORT="${PORT:-18081}"
export TILE_PORT="${TILE_PORT:-8080}"               # 미리보기 tileserver 포트
export STYLE_STUDIO_PORT="${STYLE_STUDIO_PORT:-18082}"  # '스타일 디자인' 링크 대상
export HOST="${HOST:-0.0.0.0}"                      # 기본 외부(LAN) 노출 — 무인증 콘솔이므로 신뢰망에서만. 로컬 전용은 HOST=127.0.0.1
export COMPOSE_FILE="${COMPOSE_FILE:-$ROOT/server/docker-compose.yml}"
mkdir -p "$BUILD_HOME"
LOG="$BUILD_HOME/build-studio.log"

lsof -ti tcp:"$PORT" 2>/dev/null | xargs kill 2>/dev/null || true   # 기존 인스턴스 종료(재시작)

# 세션과 분리된 데몬으로 기동(2회 fork + setsid) — 터미널을 닫아도 살아있음
python3 - "$ROOT/scripts/build-studio.py" "$LOG" <<'PY'
import os, sys, runpy
script, log = sys.argv[1], sys.argv[2]
if os.fork() > 0: os._exit(0)          # 1차 fork
os.setsid()                            # 새 세션 리더(프로세스그룹 분리)
if os.fork() > 0: os._exit(0)          # 2차 fork → 세션리더 아님(터미널 재획득 방지)
f = open(log, "a"); os.dup2(f.fileno(), 1); os.dup2(f.fileno(), 2)
dn = open(os.devnull); os.dup2(dn.fileno(), 0)   # 참조 유지(익명 open은 GC로 fd 닫힘→EBADF)
sys.argv = ["build-studio.py"]
runpy.run_path(script, run_name="__main__")
PY

sleep 2
if lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
  echo "✅ build-studio 기동 → http://localhost:$PORT  (PID $(pgrep -f scripts/build-studio.py | head -1)) · 로그: $LOG"
else
  echo "❌ 기동 실패 — $LOG 확인"; tail -5 "$LOG" 2>/dev/null || true; exit 1
fi
