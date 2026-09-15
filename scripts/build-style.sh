#!/usr/bin/env bash
# style/base.json + style/layers/*.json → style/style.json 조립 (반복 실행 가능한 빌드 도구)
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# 동적타일 캐시 무효화용 빌드 식별자(날짜+git短해시). 외부에서 BUILD_ID 주면 그대로 사용.
# git 이 없는 빌드 호스트(/home/maptiler 는 rsync 배포본)에선 'nogit' 고정값이 되어 같은 날 재빌드끼리 캐시 네임스페이스가 겹친다
# ([실측 2026-09-04] package 산출 v20260904-nogit). refresh_tile_cache.sh 와 같은 규칙으로 시각(HHMMSS)을 쓴다.
: "${BUILD_ID:=$(date +%Y%m%d)-$(git -C "$ROOT" rev-parse --short HEAD 2>/dev/null || date +%H%M%S)}"
export BUILD_ID
python3 "$ROOT/scripts/build_style.py"
