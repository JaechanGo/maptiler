#!/usr/bin/env bash
# fetch_lawd_code.sh — 행정표준코드관리시스템(code.go.kr)에서 「법정동코드 전체자료」 zip 취득.
#
#   인증키·회원가입 불필요. 세션 쿠키만 있으면 된다.
#   ※ 엔드포인트 주의: /stdcode/regCodeFileDown.do 는 HTTP 400 을 낸다.
#     regCodeL.do 의 인라인 JS func_fullfileDown() 이 쓰는 /etc/codeFullDown.do 가 정답이고,
#     codeseId 에는 코드ID(00002)가 아니라 코드 **이름**('법정동코드')이 들어간다.
#
#   data.go.kr 의 StanReginCd 오픈API 는 1,000건 상한이라 쓰지 않는다(전체 53,387행).
#
# 사용:
#   scripts/postgis/fetch_lawd_code.sh [출력경로]        # 기본 ./lawd_full.zip
#
# 출력물의 SHA256 과 데이터 행수를 찍는다. T018 기준값과 다르면 원본이 갱신된 것이므로
# scripts/postgis/load_lawd_code.py 가 적재를 거부한다(--expect-rows 게이트).
set -euo pipefail

OUT="${1:-./lawd_full.zip}"
JAR="$(mktemp -t lawdcg.XXXXXX)"
trap 'rm -f "$JAR"' EXIT

echo "→ 세션 쿠키 취득 …" >&2
curl -sS -c "$JAR" 'https://www.code.go.kr/stdcode/regCodeL.do' -o /dev/null

echo "→ 전체자료 다운로드 …" >&2
curl -sS -b "$JAR" \
  -H 'Referer: https://www.code.go.kr/stdcode/regCodeL.do' \
  --data-urlencode 'codeseId=법정동코드' \
  --data 'cPage=1&pageSize=10&chkWantCnt=0&searchOk=&disuseAt=ALL&stdate=&enddate=&chkHigh=1&chkOrder=1' \
  'https://www.code.go.kr/etc/codeFullDown.do' -o "$OUT"

# zip 이 아니면 로그인 페이지·에러 HTML 을 받은 것이다. 조용히 넘어가면 안 된다.
if ! unzip -tqq "$OUT" >/dev/null 2>&1; then
  echo "✗ zip 이 아니다 — 응답 앞부분:" >&2
  head -c 300 "$OUT" >&2; echo >&2
  exit 1
fi

SZ=$(wc -c < "$OUT" | tr -d ' ')
SUM=$(shasum -a 256 "$OUT" | awk '{print $1}')
ROWS=$(python3 -c "
import sys, zipfile
z = zipfile.ZipFile(sys.argv[1])
txt = z.read(z.infolist()[0]).decode('cp949')
print(sum(1 for l in txt.split('\r\n') if l.strip()) - 1)   # 헤더 1행 제외
" "$OUT")

echo "파일   : $OUT"
echo "크기   : $SZ bytes"
echo "SHA256 : $SUM"
echo "행수   : $ROWS"
echo
echo "T018 기준값: 413346 bytes / 7b4b544a6302d26c4f4c89d2c1355beae82e958c786bad8cc8572db0d2e2eb33 / 53387행"
if [ "$ROWS" != "53387" ]; then
  echo "⚠ 행수가 T018 기준(53,387)과 다르다 — 원본이 갱신됐다." >&2
  echo "  계획서 §A 의 모든 실측 수치와 S2·S3 게이트 기대값이 무효다. 재측정 없이 진행하지 말 것." >&2
fi
