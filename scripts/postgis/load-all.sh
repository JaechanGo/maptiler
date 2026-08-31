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
STEPS="${STEPS:-schema admin parcel building geocode lawd facility}"
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
    # load-all 의 parcel 은 항상 --fresh(TRUNCATE 후 재적재) → ji_main/ji_sub/san/geom_pt 전부 NULL 로 리셋.
    # 적재 성공 직후 ji_main 자동복구(backfill_parcel_jibun.sql)를 체인한다(--fresh 한정 트리거가 구조로 보장).
    #   · opt-out: PARCEL_SKIP_BACKFILL=1 (백업 복원 직전 시간절약·schema 미선행 단독 parcel 적재 등).
    #   · 전제: san/ji_main/ji_sub 컬럼·parcel_jibun_lookup 인덱스(schema 단계=21-parcel-jibun.sql 산출) 선존.
    #     schema 미선행 단독 STEPS=parcel 은 PARCEL_SKIP_BACKFILL=1 또는 STEPS="schema parcel" 로 돌릴 것.
    #   · geom_pt 는 자동 체인 제외(런타임 COALESCE 폴백·비차단) — 필요 시 STEPS=backfill 수동 경로.
    if run "$HERE/load_parcel.sh" --shp "$PARCEL" --fresh; then
      if [ -z "${PARCEL_SKIP_BACKFILL:-}" ]; then
        run psql -v ON_ERROR_STOP=1 -f "$HERE/backfill_parcel_jibun.sql" \
          || { echo "  ✗ backfill_parcel_jibun.sql 실패 — 사전조건(컬럼·인덱스 존재)/가드 확인. 재실행: STEPS=backfill $0" >&2; fail=1; }
      fi
    else
      echo "  ✗ 연속지적 적재 실패 — parcel 미완·GiST/pnu 인덱스 누락 가능. 재실행: STEPS=parcel $0" >&2; fail=1
    fi
  else
    echo "  (건너뜀) 연속지적 SHP 없음: $PARCEL"
  fi
fi

# 2.5) parcel 정규화 백필 (옵트인 전용 — 기본 STEPS 미포함, 자동 stale 편입 금지: Global Constraint L20)
#      jibun(san/본번/부번) + geom_pt 대표점. parcel 적재 이후 의존. 둘 다 증분 가드(WHERE ... IS NULL).
#      ⚠ ji_main 은 parcel 단계에 자동 체인됨(PARCEL_SKIP_BACKFILL=1 로 opt-out). 이 backfill 토큰은
#         geom_pt 포함 전체 수동 백필·부분실패 재시도 경로로 유지한다(정규 full-build 기본 STEPS 미포함).
#         STEPS="parcel backfill" 동시지정은 backfill_parcel_jibun.sql 의 인덱스를 2회 통째 재빌드(수 분)하므로
#         비권장 — ji_main 은 자동 체인이 채우니 geom_pt 만 필요하면 이 토큰을 단독으로 쓸 것.
#      ⚠ 사전조건(N1): parcel 적재 완료 + 21-parcel-jibun.sql 의 san/ji_main/ji_sub/geom_pt 컬럼·
#         parcel_jibun_lookup 인덱스가 schema 선행으로 이미 존재해야 함. STEPS=backfill 단독 호출 시
#         컬럼/인덱스 부재면 즉시 실패(ON_ERROR_STOP=1) — schema 단계를 먼저 돌릴 것.
#      ⚠ 39.9M 급 대량 UPDATE(파티션별 독립 커밋) — STEPS="backfill" 로 명시 호출할 때만.
#      실행 순서: jibun(인덱스 단독 DROP/CREATE 래핑 동반) → geom_pt(파티션별). 둘 다 ON_ERROR_STOP=1, fail 누적.
if has backfill; then
  run psql -v ON_ERROR_STOP=1 -f "$HERE/backfill_parcel_jibun.sql" \
    || { echo "  ✗ backfill_parcel_jibun.sql 실패 — 인덱스 재생성/가드/사전조건(컬럼·인덱스 존재) 확인. 재실행: STEPS=backfill $0" >&2; fail=1; }
  run psql -v ON_ERROR_STOP=1 -f "$HERE/backfill_geom_pt.sql" \
    || { echo "  ✗ backfill_geom_pt.sql 실패 — 파티션별 UPDATE 확인. 재실행: STEPS=backfill $0" >&2; fail=1; }
fi

# 3) 건물통합정보 (시도 파티션)
if has building; then
  GIS="$BUILD_HOME/staged/gis"
  # --mgt-field A1 = GIS건물통합식별번호(컬럼정의서+ogrinfo 실측 확정) → 중복 SHP/행 ON CONFLICT 방어.
  # --pnu-field A2 = PNU(19자리) → building.pnu 적재(필지↔건물 조인키, parcel.pnu 와 대칭).
  #   데이터 버전별 A코드 변동 시 BUILDING_MGT_FIELD·BUILDING_PNU_FIELD 로 override(빈값이면 각각 OFF).
  if [ -d "$GIS" ]; then
    run "$HERE/load_building.sh" --shp "$GIS" --fresh \
        --mgt-field "${BUILDING_MGT_FIELD:-A1}" --pnu-field "${BUILDING_PNU_FIELD:-A2}" \
      || { echo "  ✗ 건물 적재 실패 — building 미완·인덱스 누락 가능. 재실행: STEPS=building $0" >&2; fail=1; }
  else
    echo "  (건너뜀) 건물 SHP 없음: $GIS"
  fi
  # juso 건물도형 패치 — AL_D010 이 놓친 신축(신개발지구, 수년 지연) 증분 보완.
  # AL_D010 적재 **직후**에 돌아야 dedup(기존 건물 공간 대조)이 최신 기준으로 선다.
  # 원천 없으면 스크립트가 스스로 건너뛴다(옵션 원천 — 실패로 치지 않음).
  run "$HERE/load_building_juso_all.sh" \
    || { echo "  ✗ juso 건물 패치 실패 — 신축 보완 미완(기본 건물은 적재됨). 재실행: STEPS=building $0" >&2; fail=1; }
fi

# 4) 주소 + POI (geocode.sqlite 재사용 — 09-gen-geocode.py 산출 필요)
if has geocode; then
  GDB="$BUILD_HOME/geocode.sqlite"
  if [ -f "$GDB" ]; then
    run python3 "$HERE/load_geocode.py" --db "$GDB" \
      || { echo "  ✗ geocode 적재 실패 — address/poi 미완 가능. 재실행: STEPS=geocode $0" >&2; fail=1; }
    run python3 "$HERE/backfill_poi_tier.py" \
      || { echo "  ✗ backfill_poi_tier 실패 — tier_minzoom 미갱신. 재실행: STEPS=geocode $0" >&2; fail=1; }
  else
    echo "  (건너뜀) geocode.sqlite 없음: $GDB (09-gen-geocode.py 먼저)"
  fi
fi

# 4.5) 지역 사전 — lawd_dong(address.bcode 파생, 멱등 SQL) + lawd_sigungu(navi 권위 빌더).
#       ⚠ 반드시 geocode(address 최종) 이후·facility 앞. lawd_dong 은 address 기반이므로 순서 의존
#         (스펙 DAG: D2(address) → C2-lawd). facility 는 lawd 와 무관 → 상대순서 무방하나 plan §2.3 배치 준수.
#       R2: A2(backfill)도 load-all.sh 를 수정(STEPS 가산)하므로 conductor 머지 조율 대상.
if has lawd; then
  # (a) lawd_dong: 소스=DB(address). psql -f 멱등 SQL(would_rows 가드 내장).
  #     가드 위반/address 미적재 시 RAISE EXCEPTION → ON_ERROR_STOP=1 비-0 → fail=1(거짓 PASS 차단).
  run psql -v ON_ERROR_STOP=1 -f "$HERE/build_dong_dict.sql" \
    || { echo "  ✗ build_dong_dict.sql 실패 — would_rows 가드 또는 address 미적재. 재실행: STEPS=geocode,lawd $0" >&2; fail=1; }

  # (b) lawd_sigungu: navi staged/7z 소스 있을 때만(비치명 skip). build_sigungu_dict.sh 내부도 exit0 skip 이중방어.
  #     R4: navi 권위표기 보존(address 파생표기 drift·sigungu_nm LIKE 회귀 회피) — build_sigungu_dict.sh 헤더 근거 참조.
  NAVI_7Z="$BUILD_HOME/sources/juso_navi/202605_내비게이션용DB_전체분.7z"
  NAVI_STAGED="$BUILD_HOME/staged/navi"
  if [ -d "$NAVI_STAGED" ] || [ -f "$NAVI_7Z" ]; then
    run env bash "$HERE/build_sigungu_dict.sh" \
      || { echo "  ✗ build_sigungu_dict.sh 실패: navi 소스 적재 오류" >&2; fail=1; }
  else
    echo "  (건너뜀) navi 소스 없음 — lawd_sigungu 재생성 skip(기존 254 보존): $NAVI_STAGED / $NAVI_7Z"
  fi

  # (c) lawd_code: 법정동코드 전체자료(code.go.kr) 원본 적재. **lawd_ri 의 유일한 진실 원천.**
  #     원본 인자를 안 주면 fetch_lawd_code.sh 로 스스로 취득한다 → **네트워크 의존**이라
  #     build_sigungu_dict.sh 와 같은 등급의 **비치명 skip** 으로 감싼다.
  #     ⚠ 단 조용히 넘기지 않는다 — 조용한 skip 이 T018/T021 의 미배선을 만든 원인이다(R9).
  #     (a) 와 무관하나 (d)(e) 가 이것을 읽으므로 순서상 여기다.
  if run python3 "$HERE/load_lawd_code.py"; then
    # (d) lawd_ri 재구축: (c) 의존. 폐기된 build_ri_dict.sql(address 역산) 대체.
    #     원본 기반이라 건물 없는 리도 살아난다. 실패는 치명 — 사전이 반쯤 갱신된 상태를 남기지 않는다.
    run psql -v ON_ERROR_STOP=1 -f "$HERE/build_ri_dict_from_lawd_code.sql" \
      || { echo "  ✗ build_ri_dict_from_lawd_code.sql 실패 — lawd_ri 미갱신. 재실행: STEPS=lawd $0" >&2; fail=1; }

    # (e) lawd_sido_remap: 전남·광주 통합(46·29 → 12) 매핑표. (a)+(c) 의존
    #     — 우리 DB 의 옛 코드(lawd_dong)와 원본 신구 코드(lawd_code)를 맞춰 만들기 때문이다.
    run psql -v ON_ERROR_STOP=1 -f "$HERE/build_sido_remap.sql" \
      || { echo "  ✗ build_sido_remap.sql 실패 — lawd_sido_remap 미갱신(API 는 fail-open)" >&2; fail=1; }
  else
    # (c) 실패 시 (d)(e) 를 **호출하지 않는다**. lawd_code 없이 돌리면 어차피 실패하는 데다,
    #     사전·매핑표가 반쯤 갱신된 부분 상태가 아무것도 안 한 것보다 나쁘다((f) 와 같은 원칙).
    echo "  ⚠ load_lawd_code.py 실패/원본 없음 — lawd_ri 재구축·lawd_sido_remap 생성을 건너뜁니다(부분 갱신 방지)" >&2
  fi

  # (f) VWorld 30505 → lawd_code_v2 → lawd_sgg_remap (인천 자치구 개편)
  #     [근거: VWorld dsId=30505 OLD_LAWDCD] — 옛→신 대응은 원천이 한 행에 함께 주는
  #     OLD_LAWDCD 컬럼에서만 나온다. 명칭 조인으로 되살리지 말 것(폐기된 build_incheon_remap.sql).
  #     **파일 존재 가드**: 두 자산이 아직 없는 리비전에서도 이 단계가 빌드를 죽이지 않는다
  #     → S7 을 S2·S3 보다 먼저 커밋해도 안전하다(순서 제약 없음).
  if [ -f "$HERE/load_lawd_code_v2.py" ] && [ -f "$HERE/build_incheon_remap_from_old_lawdcd.sql" ]; then
    if ! run python3 "$HERE/load_lawd_code_v2.py"; then
      echo "  ⚠ load_lawd_code_v2.py 실패 — lawd_sgg_remap 생성을 건너뜁니다(부분 치환 방지)" >&2
    elif ! run psql -v ON_ERROR_STOP=1 -f "$HERE/build_incheon_remap_from_old_lawdcd.sql"; then
      # 표가 없으면 API 가 fail-open(현행 응답) 이므로 전체 빌드를 멈출 이유가 없다 — (c) 와 같은 등급.
      echo "  ⚠ build_incheon_remap_from_old_lawdcd.sql 실패 — 인천 치환은 fail-open 으로 비활성" >&2
    fi
  else
    echo "  ℹ lawd_code_v2 자산 미배치 — 인천 치환 단계 건너뜀(현행 동작 유지)"
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
  UNION ALL SELECT 'lawd_dong',    count(*) FROM lawd_dong
  UNION ALL SELECT 'lawd_sigungu', count(*) FROM lawd_sigungu
  ORDER BY t;"
if [ "$fail" = 0 ]; then
  echo "OK: load-all 완료"
else
  echo "✗ load-all: 일부 단계 실패(위 ✗ 로그 확인) — 해당 STEPS 만 재실행 필요" >&2
  exit 1
fi
