#!/usr/bin/env bash
# 라이브 검색·역지오코딩·왕복·길찾기 QC 러너 — 서빙 스택(게이트웨이)이 떠 있는 빌드 호스트에서 qc 단계가 호출한다.
# 사용: scripts/13j-search-qc.sh --api http://localhost:18080 [--skip-route]
# 게이트(하나라도 실패하면 종료코드 1):
#   · 골든셋(server/test_geocode_search_golden.py): FAIL_ERROR/FAIL_TIMEOUT 0, PASS ≥ 30/34
#   · 검색 검증(scripts/qc/search_verify.py): 비200·오류 데이터 0
#   · 역방향 정답(scripts/qc/reverse_truth_qc.py, PostGIS 표본 60): 도로명·행정구역 ≥ 90%, 되찾기 ≥ 95%
#   · 왕복 정합(scripts/qc/roundtrip_verify.py 30): 성공 ≥ 95%, 시군구 불일치 0
#   · 정답셋(scripts/qc/geo_truth_qc.py): HTTP 오류·결과없음 0 (거리 초과는 참고)
#   · 길찾기(scripts/13i-route-qc.py --quick): FAIL 0
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
API="http://localhost:18080"; SKIP_ROUTE=0
while [ $# -gt 0 ]; do case "$1" in --api) API="$2"; shift 2;; --skip-route) SKIP_ROUTE=1; shift;; *) shift;; esac; done
fail=0
run(){ echo; echo "━━ $1 ━━"; shift; "$@"; local rc=$?; [ $rc -ne 0 ] && { echo "  ✗ 실패(rc=$rc)"; fail=1; }; return 0; }
run "골든셋" bash -c "GEOCODE_API_URL='$API' python3 '$ROOT/server/test_geocode_search_golden.py' | tail -4 | tee /tmp/13j-golden.txt; grep -q 'FAIL_' /tmp/13j-golden.txt && exit 1; p=\$(grep -o 'PASS=[0-9]*' /tmp/13j-golden.txt | head -1 | cut -d= -f2); [ \"\${p:-0}\" -ge 30 ]"
run "검색 검증(중복·오류·속도)" python3 "$ROOT/scripts/qc/search_verify.py" "$API"
run "역방향 정답(PostGIS 표본 60)" python3 "$ROOT/scripts/qc/reverse_truth_qc.py" "$API" "" 60
run "왕복 정합(30)" python3 "$ROOT/scripts/qc/roundtrip_verify.py" "$API" 30
run "정답셋(관공서 12)" python3 "$ROOT/scripts/qc/geo_truth_qc.py" "$API"
[ "$SKIP_ROUTE" = 1 ] || run "길찾기 QC(quick)" bash -c "python3 '$ROOT/scripts/13i-route-qc.py' --api '$API' --quick | tail -3 | tee /tmp/13j-route.txt; ! grep -q 'FAIL [1-9]' /tmp/13j-route.txt"
echo
if [ "$fail" = 0 ]; then echo "OK: 13j 라이브 QC 전부 PASS"; else echo "✗ 13j 라이브 QC 실패 항목 있음(위 ✗)"; exit 1; fi
