#!/usr/bin/env bash
# 시군구 사전(lawd_sigungu) 구축 — 내비게이션DB match_jibun_<시도>.txt 에서 추출.
# 용도: 지번 검색의 동명중복 좁힘(시도 → 시군구 단위). geocode-api-pg.py 의 lawd_sigungu 참조.
# 포맷: 법정동코드(10)|시도명|시군구명|읍면동|리|산|본번|부번|... (CP949)
# 시도별 스트리밍 추출(7z 단일파일)로 디스크 피크 ~200MB. 결과 254행(전국 시군구).
#   사용: scripts/postgis/build_sigungu_dict.sh [--staged <navi추출본디렉토리>] [--7z <navi.7z>]
#         (하위호환: 7z 경로를 위치인자 $2 로도 받음 — 옵션 우선·위치인자 폴백)
#
# R4: 입력 소스는 navi 권위표기(match_jibun_<시도>.txt, substr(법정동코드,1,5)|시군구명)를 유지한다.
#     address.bcode(left,5) 파생경로(B안)는 불채택 — navi 권위표기 보존 vs address 파생표기 도입에
#     따른 표기 drift 및 geocode-api-pg.py 의 sigungu_nm LIKE 매칭 회귀를 회피하기 위함(plan §2.2,
#     spec C2 §단계4: "address.bcode(left,5) 대체경로는 sigungu_nm 포맷 충돌로 금지").
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"; source "$HERE/_pg-env.sh" 2>/dev/null || true
: "${PGHOST:=localhost}"; : "${PGPORT:=5433}"; : "${PGUSER:=cuvia}"; : "${PGDATABASE:=cuvia}"; : "${PGPASSWORD:=cuvia}"; export PGPASSWORD
PSQL="psql -h $PGHOST -p $PGPORT -U $PGUSER -d $PGDATABASE -v ON_ERROR_STOP=1 -tA"

# ── 입력 소스 해소: --staged/--7z 옵션 우선, 미지정 시 위치인자($2)·환경변수 폴백(하위호환) ──
SEVENZ_OPT=""; STAGED_OPT=""; POS=()
while [ $# -gt 0 ]; do
  case "$1" in
    --7z)     [ $# -ge 2 ] || { echo "✗ --7z 값 누락(경로 필요)" >&2; exit 2; }; SEVENZ_OPT="$2"; shift 2;;
    --staged) [ $# -ge 2 ] || { echo "✗ --staged 값 누락(경로 필요)" >&2; exit 2; }; STAGED_OPT="$2"; shift 2;;
    *)        POS+=("$1"); shift;;
  esac
done
# 기존 호출규약 보존: 7z 경로가 위치인자 $2(=POS[1]) 였음 → 옵션 미지정 시 위치인자 폴백.
# 기본 경로는 BUILD_HOME 기준(빌드 스튜디오 계약). $HOME 은 systemd(User= 미지정) 환경에 없을 수 있어
# set -u 아래서 "HOME: unbound variable" 로 죽는다 — [실측 2026-09-03 .244] lawd_sigungu 갱신이 통째로 빠짐.
_BH="${BUILD_HOME:-${HOME:-/root}/geocode-build}"
F="${SEVENZ_OPT:-${POS[1]:-$_BH/sources/juso_navi/202605_내비게이션용DB_전체분.7z}}"
STAGED="${STAGED_OPT:-${NAVI_STAGED:-$_BH/staged/navi}}"

# ── 소스 부재 시 skip 가드(비치명) ──
# "소스 없음"은 정상 skip(exit 0 — load-all 에서 fail 누적 안 함),
# "소스 있는데 적재 실패"는 아래 $PSQL 의 ON_ERROR_STOP=1 로 비-0 종료(구분 유지).
if [ ! -d "$STAGED" ] && [ ! -f "$F" ]; then
  echo "  (건너뜀) navi 소스 없음(staged·7z 모두 부재) — lawd_sigungu 재생성 skip(기존 보존): $STAGED / $F"
  exit 0
fi

TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
CSV="$TMP/sigungu.csv"; : > "$CSV"

SIDOS="seoul busan daegu incheon gwangju daejeon ulsan sejong gyunggi gangwon chungbuk chungnam jeonbuk jeonnam gyeongbuk gyeongnam jeju"
for s in $SIDOS; do
  fn="match_jibun_${s}.txt"
  src=""
  if [ -f "$STAGED/$fn" ]; then
    src="$STAGED/$fn"            # staged 추출본 우선(7z 추출 생략 → 디스크 피크 회피, build-studio staged/navi 와 일관)
  elif [ -f "$F" ]; then
    7z e -y -o"$TMP" "$F" "$fn" >/dev/null 2>&1 || true
    [ -f "$TMP/$fn" ] && src="$TMP/$fn"   # 7z 폴백(현행 로직 보존)
  fi
  if [ -n "$src" ]; then
    iconv -f CP949 -t UTF-8//IGNORE "$src" 2>/dev/null \
      | awk -F'|' 'length($1)>=5 && $3!="" {print substr($1,1,5)"|"$3}' | sort -u >> "$CSV"
    [ "$src" = "$TMP/$fn" ] && rm -f "$TMP/$fn"
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
