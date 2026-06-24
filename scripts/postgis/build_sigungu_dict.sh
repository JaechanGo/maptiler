#!/usr/bin/env bash
# 시군구 사전(lawd_sigungu) 구축 — 내비게이션DB match_jibun_<시도>.txt 에서 추출.
# 용도: 지번 검색의 동명중복 좁힘(시도 → 시군구 단위). geocode-api-pg.py 의 lawd_sigungu 참조.
# 포맷: 법정동코드(10)|시도명|시군구명|읍면동|리|산|본번|부번|... (CP949)
# 시도별 스트리밍 추출(7z 단일파일)로 디스크 피크 ~200MB. 결과 254행(전국 시군구).
#   사용: scripts/postgis/build_sigungu_dict.sh [--7z <navi.7z>]
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"; source "$HERE/_pg-env.sh" 2>/dev/null || true
: "${PGHOST:=localhost}"; : "${PGPORT:=5433}"; : "${PGUSER:=cuvia}"; : "${PGDATABASE:=cuvia}"; : "${PGPASSWORD:=cuvia}"; export PGPASSWORD
PSQL="psql -h $PGHOST -p $PGPORT -U $PGUSER -d $PGDATABASE -v ON_ERROR_STOP=1 -tA"

F="${2:-$HOME/geocode-build/sources/juso_navi/202605_내비게이션용DB_전체분.7z}"
[ -f "$F" ] || { echo "✗ 내비DB 7z 없음: $F (--7z 로 지정)"; exit 1; }
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
CSV="$TMP/sigungu.csv"; : > "$CSV"

SIDOS="seoul busan daegu incheon gwangju daejeon ulsan sejong gyunggi gangwon chungbuk chungnam jeonbuk jeonnam gyeongbuk gyeongnam jeju"
for s in $SIDOS; do
  fn="match_jibun_${s}.txt"
  7z e -y -o"$TMP" "$F" "$fn" >/dev/null 2>&1 || true
  if [ -f "$TMP/$fn" ]; then
    iconv -f CP949 -t UTF-8//IGNORE "$TMP/$fn" 2>/dev/null \
      | awk -F'|' 'length($1)>=5 && $3!="" {print substr($1,1,5)"|"$3}' | sort -u >> "$CSV"
    rm -f "$TMP/$fn"
  fi
  echo "  $s: 누적 $(sort -u "$CSV" | wc -l | tr -d ' ')"
done
sort -u "$CSV" -o "$CSV"
echo "시군구 distinct: $(wc -l < "$CSV")"

$PSQL -c "DROP TABLE IF EXISTS lawd_sigungu;
          CREATE TABLE lawd_sigungu(sigungu_cd char(5) PRIMARY KEY, sigungu_nm text);" >/dev/null
$PSQL -c "\copy lawd_sigungu FROM '$CSV' WITH (FORMAT csv, DELIMITER '|')"
$PSQL -c "CREATE INDEX IF NOT EXISTS lawd_sigungu_nm_idx ON lawd_sigungu(sigungu_nm); ANALYZE lawd_sigungu;" >/dev/null
echo "OK: lawd_sigungu $($PSQL -c 'select count(*) from lawd_sigungu') 건"
