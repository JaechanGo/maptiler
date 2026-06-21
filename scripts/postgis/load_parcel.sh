#!/usr/bin/env bash
# 연속지적도 SHP(VWorld dsId=30563) → PostGIS parcel (시도 LIST 파티션) (Phase 2)
# PNU(19) 앞 10 = 법정동코드(시도2+시군구3+읍면동3+리2). sido_cd=left(pnu,2) → 파티션 자동 라우팅.
# 전국 ≈39.6M 필지. 디스크/시간 큼. ogr2ogr 5186→4326 + CP949.
#
# 예) 전국 폴더(시군구 ZIP 압축해제본) 한 번에:
#   scripts/postgis/load_parcel.sh --shp ~/geocode-build/staged/parcel --fresh
#   특정 시도만 갱신(파티션 TRUNCATE 후):  psql -c 'TRUNCATE parcel_11;'  뒤 해당 시도 폴더로 실행
# 필드명 다르면 --pnu-field/--jibun-field 로 지정. ogrinfo -so 로 확인.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"; source "$HERE/_pg-env.sh"
pg_need ogr2ogr psql

# 무손실 적재 가속 — 결과(좌표·정밀도·유효성·dedup) 불변, 속도만 단축.
#  · synchronous_commit=off: 적재 세션 fsync 대기 제거(실패 시 재적재로 끝나는 벌크라 안전). ogr2ogr/psql 공통.
#  · MWM/MPW: --fresh 재적재 후 인덱스 일괄 재생성 시 사용. 호스트 RAM 에 맞춰 env 로 조정.
export PGOPTIONS="${PGOPTIONS:+$PGOPTIONS }-c synchronous_commit=off"
MWM="${PARCEL_MAINT_MEM:-2GB}"            # 인덱스 재생성 maintenance_work_mem (워커당 소비; RAM 넉넉하면 8GB 까지 ↑)
MPW="${PARCEL_MAINT_WORKERS:-4}"          # 병렬 인덱스 빌드 워커(btree pnu 한정; GiST 는 무시됨)
_cores=$(nproc 2>/dev/null || getconf _NPROCESSORS_ONLN 2>/dev/null || echo 4)
JOBS="${PARCEL_JOBS:-$(( _cores < 8 ? _cores : 8 ))}"   # 시군구 병렬 적재 워커수(코어 자동감지, 기본 상한 8)

SHP="" SRS="EPSG:5186" PNUF="PNU" JIBUNF="JIBUN" ENC="CP949" FRESH=0
while [ $# -gt 0 ]; do case "$1" in
  --shp) SHP="$2"; shift 2;;
  --srs) SRS="$2"; shift 2;;
  --pnu-field) PNUF="$2"; shift 2;;
  --jibun-field) JIBUNF="$2"; shift 2;;
  --encoding) ENC="$2"; shift 2;;
  --fresh) FRESH=1; shift;;
  *) echo "알 수 없는 인자: $1" >&2; exit 2;;
esac; done
[ -n "$SHP" ] || { echo "필수: --shp <파일|폴더>" >&2; exit 2; }

mapfile -t SHPS < <(if [ -d "$SHP" ]; then find "$SHP" -iname '*.shp' | sort; else echo "$SHP"; fi)
[ "${#SHPS[@]}" -gt 0 ] || { echo "✗ SHP 없음: $SHP" >&2; exit 1; }

# --fresh(전국 재적재)일 때만 geom GiST·pnu 인덱스를 내렸다가 적재 후 일괄 재생성.
# 살아있는 GiST 의 행단위 갱신이 최대 병목 — 일괄 빌드가 훨씬 빠르고 인덱스도 덜 부푼다.
# UNIQUE(sido_cd,pnu) 인덱스는 ON CONFLICT 의 arbiter 라 반드시 유지(중복제거 정확성 직결).
# 증분 적재(--fresh 없이 시도별 TRUNCATE 후)는 전국 인덱스 재빌드가 낭비라 인덱스를 그대로 둔다.
DROPPED_IDX=0
if [ "$FRESH" = 1 ]; then
  echo "→ TRUNCATE parcel (전체 파티션)"
  psql -v ON_ERROR_STOP=1 -q -c "TRUNCATE parcel;" >/dev/null
  echo "→ 적재용 인덱스 DROP: parcel_geom_gix·parcel_pnu_idx (UNIQUE(sido_cd,pnu)는 유지)"
  psql -v ON_ERROR_STOP=1 -q -c "DROP INDEX IF EXISTS parcel_geom_gix; DROP INDEX IF EXISTS parcel_pnu_idx;" >/dev/null
  DROPPED_IDX=1
fi

STG_BASE="_stg_parcel"
N=${#SHPS[@]}
echo "연속지적 적재 → parcel (${N}개 SHP, 병렬 ${JOBS} 워커)"

# 단일 SHP 적재 워커 — 워커별 스테이징 테이블(${STG_BASE}_<idx>)로 이름 충돌 회피.
# parcel 파티션으로의 동시 INSERT 는 PG 행수준 잠금이라 안전(ON CONFLICT DO NOTHING → 교착 없음).
# --fresh 로 GiST 를 내린 상태면 인덱스 경합도 없어 병렬 효율 최대.
load_one() {
  local shp="$1" idx="$2" stg="${STG_BASE}_$2"
  local lyr; lyr="$(basename "$shp" .shp)"
  PG_USE_COPY=YES SHAPE_ENCODING="$ENC" ogr2ogr -f PostgreSQL "$PG_OGR" "$shp" \
    -nln "$stg" -overwrite -lco GEOMETRY_NAME=geom -lco UNLOGGED=YES -nlt PROMOTE_TO_MULTI \
    -s_srs "$SRS" -t_srs EPSG:4326 -skipfailures \
    -dialect SQLITE -sql "SELECT \"$PNUF\" AS pnu, \"$JIBUNF\" AS jibun, GEOMETRY FROM \"$lyr\"" 2>/dev/null
  local cnt
  cnt=$(psql_q -c "
    INSERT INTO parcel(pnu, jibun, sido_cd, sgg_cd, emd_cd, geom)
    SELECT pnu, jibun, left(pnu,2), left(pnu,5), left(pnu,8),
           ST_Multi(ST_CollectionExtract(ST_MakeValid(geom),3))
    FROM ${stg}
    WHERE geom IS NOT NULL AND pnu IS NOT NULL AND length(pnu) >= 2
    ON CONFLICT (sido_cd, pnu) DO NOTHING;
    SELECT count(*) FROM ${stg} WHERE geom IS NOT NULL AND pnu IS NOT NULL;")
  psql -v ON_ERROR_STOP=1 -q -c "DROP TABLE IF EXISTS ${stg};" >/dev/null
  echo "  [$idx/${N}] $lyr → ${cnt}건"
}

# 워커 풀: 최대 JOBS 개 동시 실행, 하나 끝나면 다음 투입(wait -n).
# 진행로그 [i/N] 는 완료 순서라 순번이 뒤섞여 보일 수 있음(정상). 워커 SQL 오류 시 즉시 중단(fail-fast).
i=0
for shp in "${SHPS[@]}"; do
  case "$shp" in *"(1)"*) echo "  (중복 제외) $(basename "$shp")"; continue;; esac
  i=$((i+1))
  load_one "$shp" "$i" &
  while [ "$(jobs -rp | wc -l)" -ge "$JOBS" ]; do wait -n; done
done
wait
# 워커가 각자 스테이징을 정리하지만, 비정상 종료 잔여분(_stg_parcel_*)까지 일괄 정리.
psql -v ON_ERROR_STOP=1 -q -At -c \
  "SELECT 'DROP TABLE IF EXISTS '||quote_ident(tablename)||';' FROM pg_tables WHERE tablename LIKE '_stg_parcel%'" \
  | psql -v ON_ERROR_STOP=1 -q -f - >/dev/null
if [ "$DROPPED_IDX" = 1 ]; then
  echo "→ 인덱스 일괄 재생성 (maintenance_work_mem=${MWM}, workers=${MPW})"
  psql -v ON_ERROR_STOP=1 -q -c "
    SET maintenance_work_mem='${MWM}';
    SET max_parallel_maintenance_workers=${MPW};
    CREATE INDEX IF NOT EXISTS parcel_geom_gix ON parcel USING gist (geom);
    CREATE INDEX IF NOT EXISTS parcel_pnu_idx  ON parcel (pnu);" >/dev/null
fi
psql -v ON_ERROR_STOP=1 -q -c "ANALYZE parcel;" >/dev/null
echo "── 시도별 적재 결과 ──"
psql -P pager=off -c "SELECT sido_cd, count(*) FROM parcel GROUP BY sido_cd ORDER BY sido_cd;"
echo "OK: parcel 적재 완료"
