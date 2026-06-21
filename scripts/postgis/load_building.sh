#!/usr/bin/env bash
# GIS건물통합정보 SHP(VWorld, 시도별) → PostGIS building (시도 LIST 파티션) (Phase 2)
# 10-gen-buildings.sh 와 동일 컬럼/휴리스틱: A16=높이(m,결측多), A26=지상층수.
#   render_height = A16>0?A16 : (A26>0?A26*3.3 : 6).  기존 buildings.mbtiles 대체.
# sido_cd 는 파일명 AL_D010_<시도2>_<YYYYMMDD>.shp(전체분) / CH_D010_*(변동분) 에서 추출.
#
# 예) 전국 폴더:  scripts/postgis/load_building.sh --shp ~/geocode-build/staged/gis --fresh
#     단일 시도:  scripts/postgis/load_building.sh --shp AL_D010_11_20260610.shp --sido 11
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"; source "$HERE/_pg-env.sh"
pg_need ogr2ogr psql

# 무손실 적재 가속(load_parcel 과 동일 전략) — 결과(좌표·높이·유효성) 불변, 속도만 단축.
#  · synchronous_commit=off: 적재 세션 fsync 대기 제거(실패 시 재적재로 끝나는 벌크라 안전). ogr2ogr/psql 공통.
#  · MWM/MPW: --fresh 재적재 후 인덱스 일괄 재생성 시 사용. 호스트 RAM 에 맞춰 env 로 조정.
export PGOPTIONS="${PGOPTIONS:+$PGOPTIONS }-c synchronous_commit=off"
MWM="${BUILDING_MAINT_MEM:-2GB}"          # 인덱스 재생성 maintenance_work_mem (워커당 소비; RAM 넉넉하면 8GB 까지 ↑)
MPW="${BUILDING_MAINT_WORKERS:-4}"        # 병렬 인덱스 빌드 워커(GiST 는 무시됨; btree pnu 한정)
_cores=$(nproc 2>/dev/null || getconf _NPROCESSORS_ONLN 2>/dev/null || echo 4)
JOBS="${BUILDING_JOBS:-$(( _cores < 8 ? _cores : 8 ))}"   # 시도 병렬 적재 워커수(코어 자동감지, 기본 상한 8; 1M파일 다수면 RAM 보고 ↓)

SHP="" SRS="EPSG:5186" HF="A16" LF="A26" ENC="CP949" FRESH=0 SIDO="" MGTF=""
while [ $# -gt 0 ]; do case "$1" in
  --shp) SHP="$2"; shift 2;;
  --srs) SRS="$2"; shift 2;;
  --height-field) HF="$2"; shift 2;;
  --levels-field) LF="$2"; shift 2;;
  --encoding) ENC="$2"; shift 2;;
  --sido) SIDO="$2"; shift 2;;            # 파일명에서 자동추출 실패 시 강제 지정
  --mgt-field) MGTF="$2"; shift 2;;       # 건물관리번호 필드(opt-in 중복방어). ogrinfo -so 로 확인 후 지정
  --fresh) FRESH=1; shift;;
  *) echo "알 수 없는 인자: $1" >&2; exit 2;;
esac; done
[ -n "$SHP" ] || { echo "필수: --shp <파일|폴더>" >&2; exit 2; }

mapfile -t SHPS < <(if [ -d "$SHP" ]; then find "$SHP" -iname '*.shp' | sort; else echo "$SHP"; fi)
[ "${#SHPS[@]}" -gt 0 ] || { echo "✗ SHP 없음: $SHP" >&2; exit 1; }

# 중복방어(opt-in) — --mgt-field 지정 시 건물관리번호(bld_mgt_no)를 적재하고 ON CONFLICT 로 중복 SHP/행을 무시.
# 미지정(기본)이면 현행대로 단순 append. arbiter=부분 UNIQUE building_mgt_uix(스키마 21-building.sql, bld_mgt_no NOT NULL).
MGT_OGR_SEL="" MGT_INS_COL="" MGT_INS_SEL="" MGT_CONFLICT=""
if [ -n "$MGTF" ]; then
  MGT_OGR_SEL="\"$MGTF\" AS bld_mgt_no, "
  MGT_INS_COL="bld_mgt_no, "
  MGT_INS_SEL="bld_mgt_no, "
  MGT_CONFLICT="ON CONFLICT (sido_cd, bld_mgt_no) WHERE bld_mgt_no IS NOT NULL DO NOTHING"
  echo "→ 중복방어 ON: bld_mgt_no ← 필드 '$MGTF' (ON CONFLICT sido_cd,bld_mgt_no)"
fi

# --fresh(전국 재적재)일 때만 geom GiST·pnu 인덱스를 내렸다가 적재 후 일괄 재생성.
# 살아있는 GiST 의 행단위 갱신이 최대 병목 — 일괄 빌드가 훨씬 빠르고 인덱스도 덜 부푼다.
# 부분 UNIQUE building_mgt_uix(arbiter)는 유지 — --mgt-field 시 ON CONFLICT 동작, 미사용 시 NULL 이라 빈 인덱스(무비용).
# 증분 적재(--fresh 없이)는 전국 인덱스 재빌드가 낭비라 인덱스를 그대로 둔다.
DROPPED_IDX=0
if [ "$FRESH" = 1 ]; then
  echo "→ TRUNCATE building (전체 파티션)"
  psql -v ON_ERROR_STOP=1 -q -c "TRUNCATE building;" >/dev/null
  echo "→ 적재용 인덱스 DROP: building_geom_gix·building_pnu_idx"
  psql -v ON_ERROR_STOP=1 -q -c "DROP INDEX IF EXISTS building_geom_gix; DROP INDEX IF EXISTS building_pnu_idx;" >/dev/null
  DROPPED_IDX=1
fi

STG_BASE="_stg_building"
N=${#SHPS[@]}
echo "건물 적재 → building (${N}개 SHP, 병렬 ${JOBS} 워커)"

# 단일 SHP 적재 워커 — 워커별 스테이징 테이블(${STG_BASE}_<idx>)로 이름 충돌 회피.
# render_height/levels 는 ogr2ogr SQLite 방언에서 A16/A26 으로 산정해 스테이징에 적재.
# building 파티션으로의 동시 INSERT 는 PG 가 동시삽입 안전(ON CONFLICT 없음=중복 방어 없음, 단순 append).
# --fresh 로 인덱스를 내린 상태면 인덱스 경합도 없어 병렬 효율 최대.
load_one() {
  local shp="$1" idx="$2" sido="$3" stg="${STG_BASE}_$2"
  local lyr; lyr="$(basename "$shp" .shp)"
  PG_USE_COPY=YES SHAPE_ENCODING="$ENC" ogr2ogr -f PostgreSQL "$PG_OGR" "$shp" \
    -nln "$stg" -overwrite -lco GEOMETRY_NAME=geom -lco UNLOGGED=YES -nlt PROMOTE_TO_MULTI \
    -s_srs "$SRS" -t_srs EPSG:4326 -skipfailures \
    -dialect SQLITE -sql "SELECT ${MGT_OGR_SEL}
        CASE WHEN CAST(\"$HF\" AS REAL) > 0 THEN ROUND(CAST(\"$HF\" AS REAL),1)
             WHEN CAST(\"$LF\" AS REAL) > 0 THEN ROUND(CAST(\"$LF\" AS REAL)*3.3,1)
             ELSE 6 END AS render_height,
        CAST(\"$LF\" AS INTEGER) AS levels,
        GEOMETRY
      FROM \"$lyr\" WHERE GEOMETRY IS NOT NULL" 2>/dev/null
  local cnt
  cnt=$(psql_q -c "
    INSERT INTO building(sido_cd, ${MGT_INS_COL}render_height, levels, geom)
    SELECT '${sido}', ${MGT_INS_SEL}render_height, levels,
           ST_Multi(ST_CollectionExtract(ST_MakeValid(geom),3))
    FROM ${stg} WHERE geom IS NOT NULL
    ${MGT_CONFLICT};
    SELECT count(*) FROM ${stg} WHERE geom IS NOT NULL;")
  psql -v ON_ERROR_STOP=1 -q -c "DROP TABLE IF EXISTS ${stg};" >/dev/null
  echo "  [$idx/${N}] $lyr (시도 $sido) → ${cnt}건"
}

# 워커 풀: 최대 JOBS 개 동시 실행, 하나 끝나면 다음 투입(wait -n).
# 진행로그 [i/N] 는 완료 순서라 순번이 뒤섞여 보일 수 있음(정상). 워커 SQL 오류 시 즉시 중단(fail-fast).
# 시도코드: --sido 우선, 없으면 파일명 [AC]_D010_<NN>_ 에서 추출. (1) 접미는 재다운로드 중복으로 보고 스킵.
i=0
for shp in "${SHPS[@]}"; do
  case "$shp" in *"(1)"*) echo "  (중복 제외) $(basename "$shp")"; continue;; esac
  lyr="$(basename "$shp" .shp)"
  sido="$SIDO"
  [ -n "$sido" ] || sido="$(printf '%s' "$lyr" | sed -nE 's/.*_D010_([0-9]{2})_.*/\1/p')"
  if [ -z "$sido" ]; then echo "  ✗ 시도코드 추출 실패: $lyr (--sido 로 지정)"; continue; fi
  i=$((i+1))
  load_one "$shp" "$i" "$sido" &
  while [ "$(jobs -rp | wc -l)" -ge "$JOBS" ]; do wait -n; done
done
wait
# 워커가 각자 스테이징을 정리하지만, 비정상 종료 잔여분(_stg_building_*)까지 일괄 정리.
psql -v ON_ERROR_STOP=1 -q -At -c \
  "SELECT 'DROP TABLE IF EXISTS '||quote_ident(tablename)||';' FROM pg_tables WHERE tablename LIKE '_stg_building%'" \
  | psql -v ON_ERROR_STOP=1 -q -f - >/dev/null
if [ "$DROPPED_IDX" = 1 ]; then
  echo "→ 인덱스 일괄 재생성 (maintenance_work_mem=${MWM}, workers=${MPW})"
  psql -v ON_ERROR_STOP=1 -q -c "
    SET maintenance_work_mem='${MWM}';
    SET max_parallel_maintenance_workers=${MPW};
    CREATE INDEX IF NOT EXISTS building_geom_gix ON building USING gist (geom);
    CREATE INDEX IF NOT EXISTS building_pnu_idx  ON building (pnu);" >/dev/null
fi
psql -v ON_ERROR_STOP=1 -q -c "ANALYZE building;" >/dev/null
echo "── 시도별 적재 결과 ──"
psql -P pager=off -c "SELECT sido_cd, count(*) FROM building GROUP BY sido_cd ORDER BY sido_cd;"
echo "OK: building 적재 완료"
