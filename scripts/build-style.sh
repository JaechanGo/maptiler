#!/usr/bin/env bash
# style/base.json + style/layers/*.json → style/style.json 조립 (반복 실행 가능한 빌드 도구)
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# 동적타일 캐시 무효화용 빌드 식별자(날짜+git短해시). 외부에서 BUILD_ID 주면 그대로 사용.
: "${BUILD_ID:=$(date +%Y%m%d)-$(git -C "$ROOT" rev-parse --short HEAD 2>/dev/null || echo nogit)}"
export BUILD_ID
python3 "$ROOT/scripts/build_style.py"
