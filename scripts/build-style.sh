#!/usr/bin/env bash
# style/base.json + style/layers/*.json → style/style.json 조립 (반복 실행 가능한 빌드 도구)
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
python3 "$ROOT/scripts/build_style.py"
