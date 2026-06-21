#!/usr/bin/env bash
# CUVIA PostGIS 전체 적재 오케스트레이터 (빌드호스트) — 스키마 적용 후 소스가 있는 단계만 순차 적재.
# 데이터 경로는 BUILD_HOME(기본 ~/geocode-build) 기준. 단계 선택: STEPS="schema admin parcel building geocode facility".
# 연결: libpq 환경변수(PGPASSWORD 등). compose postgis 가 떠 있어야 함:
#   cd server && docker compose --profile postgis up -d postgis martin
#
# 사용:  PGPASSWORD=... scripts/postgis/load-all.sh
#        STEPS="parcel" scripts/postgis/load-all.sh           # 특정 단계만
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"; source "$HERE/_pg-env.sh"
BUILD_HOME="${BUILD_HOME:-$HOME/geocode-build}"
STEPS="${STEPS:-schema admin parcel building geocode facility}"
has(){ case " $STEPS " in *" $1 "*) return 0;; *) return 1;; esac; }
run(){ echo; echo "━━ $* ━━"; "$@"; }
fail=0   # 적재 단계 실패 누적 — 하나라도 실패하면 종료코드 1(빌드그래프가 단계 실패로 인지). set -e 미사용이라 직접 추적.

# 0) 스키마(멱등)
if has schema; then run "$HERE/apply-schema.sh"; fi

# 1) 행정구역 — 법정동(읍면동) + 행정동. 무인증 대체 SHP 도 동일(필드명만 ogrinfo 확인).
if has admin; then
  LEGAL="$BUILD_HOME/sources/boundary/legal"
  ADMIN_SHP="$BUILD_HOME/sources/boundary/admin/BND_ADM_DONG_PG.shp"
  if [ -d "$LEGAL" ]; then
    run "$HERE/load_admin.sh" --shp "$LEGAL" --level emd --srs EPSG:5186 --name-field EMD_NM --code-field EMD_CD \
      || { echo "  ✗ 법정동 경계 적재 실패: $LEGAL" >&2; fail=1; }
  else echo "  (건너뜀) 법정동 경계 없음: $LEGAL"; fi
  if [ -f "$ADMIN_SHP" ]; then
    run "$HERE/load_admin.sh" --shp "$ADMIN_SHP" --level adm_dong --srs EPSG:5186 --name-field ADM_NM --code-field ADM_CD \
      || { echo "  ✗ 행정동 경계 적재 실패: $ADMIN_SHP" >&2; fail=1; }
  else echo "  (건너뜀) 행정동 경계 없음: $ADMIN_SHP"; fi
  # full_name 조립(코드 계층 self-join) — 시도/시군구/읍면동 적재 후
  run psql -v ON_ERROR_STOP=1 -f "$HERE/build-admin-fullname.sql" \
    || { echo "  ✗ build-admin-fullname.sql 실패" >&2; fail=1; }
fi

# 2) 연속지적도 (시도 파티션)
# ⚠ '디렉토리 없음(스킵)' 과 '적재 실패(에러)' 를 반드시 구분한다. 과거 `[ -d ] && run || echo` 는
#    load_parcel.sh 가 교착 등으로 실패해도 "(건너뜀) SHP 없음" 으로 오인 표시하고 파이프라인을 계속 진행시켰다
#    (parcel 미완·인덱스 누락인데 load-all 은 OK 로 종료). → 명시 if/else + fail 누적으로 교정.
if has parcel; then
  PARCEL="$BUILD_HOME/staged/parcel"
  if [ -d "$PARCEL" ]; then
    run "$HERE/load_parcel.sh" --shp "$PARCEL" --fresh \
      || { echo "  ✗ 연속지적 적재 실패 — parcel 미완·GiST/pnu 인덱스 누락 가능. 재실행: STEPS=parcel $0" >&2; fail=1; }
  else
    echo "  (건너뜀) 연속지적 SHP 없음: $PARCEL"
  fi
fi

# 3) 건물통합정보 (시도 파티션)
if has building; then
  GIS="$BUILD_HOME/staged/gis"
  # --mgt-field A1 = GIS건물통합식별번호(컬럼정의서+ogrinfo 실측 확정) → 중복 SHP/행 ON CONFLICT 방어.
  #   데이터 버전별 A코드 변동 시 BUILDING_MGT_FIELD 로 override(빈값이면 중복방어 OFF).
  if [ -d "$GIS" ]; then
    run "$HERE/load_building.sh" --shp "$GIS" --fresh --mgt-field "${BUILDING_MGT_FIELD:-A1}" \
      || { echo "  ✗ 건물 적재 실패 — building 미완·인덱스 누락 가능. 재실행: STEPS=building $0" >&2; fail=1; }
  else
    echo "  (건너뜀) 건물 SHP 없음: $GIS"
  fi
fi

# 4) 주소 + POI (geocode.sqlite 재사용 — 09-gen-geocode.py 산출 필요)
if has geocode; then
  GDB="$BUILD_HOME/geocode.sqlite"
  if [ -f "$GDB" ]; then
    run python3 "$HERE/load_geocode.py" --db "$GDB" \
      || { echo "  ✗ geocode 적재 실패 — address/poi 미완 가능. 재실행: STEPS=geocode $0" >&2; fail=1; }
  else
    echo "  (건너뜀) geocode.sqlite 없음: $GDB (09-gen-geocode.py 먼저)"
  fi
fi

# 5) 공공시설 — staged/facility_src/ 아래 (a)평면 CSV(<kind>.csv) 또는 (b)종류별 하위폴더(<kind>/).
#    collect(datago_filedown)는 경찰=staged/facility_src/police/, 소방=fire_station/(zip추출본) 으로 떨군다.
#    load_facility.py 는 디렉토리 인자를 받으면 하위 최신 CSV 를 자동 선택.
if has facility; then
  FSRC="$BUILD_HOME/staged/facility_src"
  if [ -d "$FSRC" ]; then
    found=0
    for entry in "$FSRC"/*; do
      [ -e "$entry" ] || continue
      if [ -f "$entry" ]; then
        case "$entry" in *.csv) kind="$(basename "$entry" .csv)"; src="csv:$kind";; *) continue;; esac
      elif [ -d "$entry" ]; then
        kind="$(basename "$entry")"
        find "$entry" -iname '*.csv' -print -quit | grep -q . \
          || { echo "  (건너뜀) $kind: CSV 없음 ($entry)"; continue; }
        src="dir:$kind"
      else continue; fi
      found=1
      run python3 "$HERE/load_facility.py" --csv "$entry" --kind "$kind" --source "$src" \
        || { echo "  ✗ 공공시설 적재 실패: kind=$kind ($entry)" >&2; fail=1; }
    done
    [ "$found" = 1 ] || echo "  (건너뜀) 적재할 공공시설 CSV/폴더 없음: $FSRC"
  else echo "  (건너뜀) 공공시설 CSV 폴더 없음: $FSRC"; fi
fi

echo; echo "━━ 적재 요약 ━━"
psql -P pager=off -c "
  SELECT 'admin_boundary' t, count(*) FROM admin_boundary
  UNION ALL SELECT 'parcel', count(*) FROM parcel
  UNION ALL SELECT 'building', count(*) FROM building
  UNION ALL SELECT 'address', count(*) FROM address
  UNION ALL SELECT 'poi', count(*) FROM poi
  UNION ALL SELECT 'public_facility', count(*) FROM public_facility
  ORDER BY t;"
if [ "$fail" = 0 ]; then
  echo "OK: load-all 완료"
else
  echo "✗ load-all: 일부 단계 실패(위 ✗ 로그 확인) — 해당 STEPS 만 재실행 필요" >&2
  exit 1
fi
