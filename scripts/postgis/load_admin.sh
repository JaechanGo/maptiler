#!/usr/bin/env bash
# 행정구역 경계 SHP → PostGIS admin_boundary (Phase 1)
# 06-gen-areas.py 와 동일한 소스/필드 관례. 단 여기는 '전체 폴리곤'을 그대로 적재(martin 타일 서빙용).
# level 별로 기존분을 멱등 교체. ogr2ogr 로 5186/5179→4326 + CP949 안전.
#
# 예) 법정동(읍면동) — VWorld LSMD_ADM_SECT_UMD_* (시도별 폴더):
#   scripts/postgis/load_admin.sh --shp ~/geocode-build/sources/boundary/legal \
#       --level emd --srs EPSG:5186 --name-field EMD_NM --code-field EMD_CD
#   행정동 — BND_ADM_DONG_PG(단일):
#   scripts/postgis/load_admin.sh --shp .../BND_ADM_DONG_PG.shp \
#       --level adm_dong --srs EPSG:5186 --name-field ADM_NM --code-field ADM_CD
#   시도/시군구 — 무인증 대체(SGIS/gisdeveloper) SHP 도 동일(필드명만 ogrinfo 로 확인).
# 필드명은 소스마다 다르니 `ogrinfo -so <shp>` 로 확인할 것.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"; source "$HERE/_pg-env.sh"
pg_need ogr2ogr psql

SHP="" LEVEL="" NAMEF="" CODEF="" SRS="EPSG:5186" ENC="CP949" SIMPLIFY=""
while [ $# -gt 0 ]; do case "$1" in
  --shp) SHP="$2"; shift 2;;
  --level) LEVEL="$2"; shift 2;;          # sido|sigungu|emd|adm_dong
  --name-field) NAMEF="$2"; shift 2;;
  --code-field) CODEF="$2"; shift 2;;
  --srs) SRS="$2"; shift 2;;
  --encoding) ENC="$2"; shift 2;;
  --simplify) SIMPLIFY="$2"; shift 2;;    # ogr2ogr -simplify(도). 미지정=원본
  *) echo "알 수 없는 인자: $1" >&2; exit 2;;
esac; done
[ -n "$SHP" ] && [ -n "$LEVEL" ] && [ -n "$NAMEF" ] || { echo "필수: --shp --level --name-field" >&2; exit 2; }

# 소스 목록(파일 또는 폴더 내 *.shp)
mapfile -t SHPS < <(if [ -d "$SHP" ]; then find "$SHP" -iname '*.shp' | sort; else echo "$SHP"; fi)
[ "${#SHPS[@]}" -gt 0 ] || { echo "✗ SHP 없음: $SHP" >&2; exit 1; }

STG="_stg_admin"
echo "행정구역 적재 → admin_boundary (level=$LEVEL, ${#SHPS[@]}개 SHP)"
# 멱등: 이 level 기존분 제거
psql -v ON_ERROR_STOP=1 -q -c "DELETE FROM admin_boundary WHERE level = '${LEVEL}';" >/dev/null

n=0
for shp in "${SHPS[@]}"; do
  case "$shp" in *"(1)"*) echo "  (중복 제외) $(basename "$shp")"; continue;; esac
  lyr="$(basename "$shp" .shp)"
  code_sel="NULL"; [ -n "$CODEF" ] && code_sel="\"$CODEF\""
  # ★ 확장은 반드시 ${simp[@]+"${simp[@]}"} 로 — bash 4.4 미만은 `set -u` 아래서 **빈 배열**의
  #   "${arr[@]}" 를 unbound variable 로 취급해 스크립트가 죽는다(4.4+ 에서 고쳐진 동작이라
  #   최신 개발기에선 재현되지 않는다). [실측 2026-09-02 .244/bash 4.2.46]
  #   "load_admin.sh: line 47: simp[@]: unbound variable" 로 법정동·행정동 적재가 **둘 다 실패**했고,
  #   load-all 은 앞서 DELETE 로 기존 level 을 지운 뒤였기에 admin_boundary 가 0 으로 남았다.
  simp=(); [ -n "$SIMPLIFY" ] && simp=(-simplify "$SIMPLIFY")
  # SHP → PG 임시 staging(name/code/geom). PROMOTE_TO_MULTI 로 MultiPolygon 통일.
  SHAPE_ENCODING="$ENC" ogr2ogr -f PostgreSQL "$PG_OGR" "$shp" \
    -nln "$STG" -overwrite -lco GEOMETRY_NAME=geom -nlt PROMOTE_TO_MULTI \
    -s_srs "$SRS" -t_srs EPSG:4326 -skipfailures ${simp[@]+"${simp[@]}"} \
    -dialect SQLITE -sql "SELECT \"$NAMEF\" AS name, $code_sel AS code, GEOMETRY FROM \"$lyr\""
  # staging → admin_boundary (sido_cd = code 앞 2자리)
  ins=$(psql_q -c "
    INSERT INTO admin_boundary(level,code,name,sido_cd,geom)
    SELECT '${LEVEL}', code, name, left(code,2),
           ST_Multi(ST_CollectionExtract(ST_MakeValid(geom),3))
    FROM ${STG} WHERE geom IS NOT NULL AND name IS NOT NULL;
    SELECT count(*) FROM ${STG} WHERE geom IS NOT NULL AND name IS NOT NULL;")
  echo "  + $(basename "$shp"): ${ins}"
  n=$((n+1))
done
psql -v ON_ERROR_STOP=1 -q -c "DROP TABLE IF EXISTS ${STG}; ANALYZE admin_boundary;" >/dev/null
tot=$(psql_q -c "SELECT count(*) FROM admin_boundary WHERE level='${LEVEL}';")
echo "OK: admin_boundary level=$LEVEL 총 ${tot}건 ($n SHP)"
