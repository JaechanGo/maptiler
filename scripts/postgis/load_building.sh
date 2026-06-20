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

SHP="" SRS="EPSG:5186" HF="A16" LF="A26" ENC="CP949" FRESH=0 SIDO=""
while [ $# -gt 0 ]; do case "$1" in
  --shp) SHP="$2"; shift 2;;
  --srs) SRS="$2"; shift 2;;
  --height-field) HF="$2"; shift 2;;
  --levels-field) LF="$2"; shift 2;;
  --encoding) ENC="$2"; shift 2;;
  --sido) SIDO="$2"; shift 2;;            # 파일명에서 자동추출 실패 시 강제 지정
  --fresh) FRESH=1; shift;;
  *) echo "알 수 없는 인자: $1" >&2; exit 2;;
esac; done
[ -n "$SHP" ] || { echo "필수: --shp <파일|폴더>" >&2; exit 2; }

mapfile -t SHPS < <(if [ -d "$SHP" ]; then find "$SHP" -iname '*.shp' | sort; else echo "$SHP"; fi)
[ "${#SHPS[@]}" -gt 0 ] || { echo "✗ SHP 없음: $SHP" >&2; exit 1; }

[ "$FRESH" = 1 ] && { echo "→ TRUNCATE building (전체 파티션)"; psql -v ON_ERROR_STOP=1 -q -c "TRUNCATE building;" >/dev/null; }

STG="_stg_building"
echo "건물 적재 → building (${#SHPS[@]}개 SHP)"
i=0
for shp in "${SHPS[@]}"; do
  case "$shp" in *"(1)"*) echo "  (중복 제외) $(basename "$shp")"; continue;; esac
  lyr="$(basename "$shp" .shp)"; i=$((i+1))
  # 시도코드: --sido 우선, 없으면 파일명 [AC]_D010_<NN>_ 에서 추출
  sido="$SIDO"
  if [ -z "$sido" ]; then sido="$(printf '%s' "$lyr" | sed -nE 's/.*_D010_([0-9]{2})_.*/\1/p')"; fi
  if [ -z "$sido" ]; then echo "  ✗ 시도코드 추출 실패: $lyr (--sido 로 지정)"; continue; fi

  SHAPE_ENCODING="$ENC" ogr2ogr -f PostgreSQL "$PG_OGR" "$shp" \
    -nln "$STG" -overwrite -lco GEOMETRY_NAME=geom -nlt PROMOTE_TO_MULTI \
    -s_srs "$SRS" -t_srs EPSG:4326 -skipfailures \
    -dialect SQLITE -sql "SELECT
        CASE WHEN CAST(\"$HF\" AS REAL) > 0 THEN ROUND(CAST(\"$HF\" AS REAL),1)
             WHEN CAST(\"$LF\" AS REAL) > 0 THEN ROUND(CAST(\"$LF\" AS REAL)*3.3,1)
             ELSE 6 END AS render_height,
        CAST(\"$LF\" AS INTEGER) AS levels,
        GEOMETRY
      FROM \"$lyr\" WHERE GEOMETRY IS NOT NULL" 2>/dev/null
  cnt=$(psql_q -c "
    INSERT INTO building(sido_cd, render_height, levels, geom)
    SELECT '${sido}', render_height, levels,
           ST_Multi(ST_CollectionExtract(ST_MakeValid(geom),3))
    FROM ${STG} WHERE geom IS NOT NULL;
    SELECT count(*) FROM ${STG} WHERE geom IS NOT NULL;")
  echo "  [$i/${#SHPS[@]}] $lyr (시도 $sido) → ${cnt}건"
done
psql -v ON_ERROR_STOP=1 -q -c "DROP TABLE IF EXISTS ${STG}; ANALYZE building;" >/dev/null
echo "── 시도별 적재 결과 ──"
psql -P pager=off -c "SELECT sido_cd, count(*) FROM building GROUP BY sido_cd ORDER BY sido_cd;"
echo "OK: building 적재 완료"
