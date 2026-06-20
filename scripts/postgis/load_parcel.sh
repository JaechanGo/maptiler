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

[ "$FRESH" = 1 ] && { echo "→ TRUNCATE parcel (전체 파티션)"; psql -v ON_ERROR_STOP=1 -q -c "TRUNCATE parcel;" >/dev/null; }

STG="_stg_parcel"
echo "연속지적 적재 → parcel (${#SHPS[@]}개 SHP)"
i=0
for shp in "${SHPS[@]}"; do
  case "$shp" in *"(1)"*) echo "  (중복 제외) $(basename "$shp")"; continue;; esac
  lyr="$(basename "$shp" .shp)"; i=$((i+1))
  SHAPE_ENCODING="$ENC" ogr2ogr -f PostgreSQL "$PG_OGR" "$shp" \
    -nln "$STG" -overwrite -lco GEOMETRY_NAME=geom -nlt PROMOTE_TO_MULTI \
    -s_srs "$SRS" -t_srs EPSG:4326 -skipfailures \
    -dialect SQLITE -sql "SELECT \"$PNUF\" AS pnu, \"$JIBUNF\" AS jibun, GEOMETRY FROM \"$lyr\"" 2>/dev/null
  cnt=$(psql_q -c "
    INSERT INTO parcel(pnu, jibun, sido_cd, sgg_cd, emd_cd, geom)
    SELECT pnu, jibun, left(pnu,2), left(pnu,5), left(pnu,8),
           ST_Multi(ST_CollectionExtract(ST_MakeValid(geom),3))
    FROM ${STG}
    WHERE geom IS NOT NULL AND pnu IS NOT NULL AND length(pnu) >= 2
    ON CONFLICT (sido_cd, pnu) DO NOTHING;
    SELECT count(*) FROM ${STG} WHERE geom IS NOT NULL AND pnu IS NOT NULL;")
  echo "  [$i/${#SHPS[@]}] $lyr → ${cnt}건"
done
psql -v ON_ERROR_STOP=1 -q -c "DROP TABLE IF EXISTS ${STG}; ANALYZE parcel;" >/dev/null
echo "── 시도별 적재 결과 ──"
psql -P pager=off -c "SELECT sido_cd, count(*) FROM parcel GROUP BY sido_cd ORDER BY sido_cd;"
echo "OK: parcel 적재 완료"
