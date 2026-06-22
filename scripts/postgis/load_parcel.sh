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
# ★ 같은 시도(=같은 파티션) INSERT 는 시도별 advisory lock 으로 직렬화한다.
#   ON CONFLICT 는 UNIQUE(sido_cd,pnu) arbiter 에 speculative-insert 락을 잡는데, 두 워커가
#   같은 파티션에 중복 PNU 를 다른 순서로 꽂으면 순환대기→교착(실측 parcel_36). 행수준 잠금으론 못 막음.
#   ogr2ogr 재투영/스테이징은 락 밖이라 전 워커 병렬 유지 — 직렬화되는 건 partition INSERT 한 스텝뿐.
# --fresh 로 GiST 를 내린 상태면 인덱스 경합도 없어 병렬 효율 최대.
load_one() {
  local shp="$1" idx="$2" stg="${STG_BASE}_$2"
  local lyr; lyr="$(basename "$shp" .shp)"
  PG_USE_COPY=YES SHAPE_ENCODING="$ENC" ogr2ogr -f PostgreSQL "$PG_OGR" "$shp" \
    -nln "$stg" -overwrite -lco GEOMETRY_NAME=geom -lco UNLOGGED=YES -nlt PROMOTE_TO_MULTI \
    -s_srs "$SRS" -t_srs EPSG:4326 -skipfailures \
    -dialect SQLITE -sql "SELECT \"$PNUF\" AS pnu, \"$JIBUNF\" AS jibun, GEOMETRY FROM \"$lyr\"" 2>/dev/null
  # 같은 시도(=파티션) INSERT 직렬화: pg_advisory_xact_lock(4001, 파티션시도) 를 잡고 ON CONFLICT 수행 →
  # speculative-insert 락 교착 원천차단. 강원 42→51·전북 45→52 는 동일 파티션이라 락키도 통일.
  # ★ INSERT 는 행마다 left(pnu,2) 로 파티션 라우팅하므로(단일 SELECT 1건만 잠그면 혼재/디폴트행 미직렬화→재교착),
  #   스테이징에 존재하는 '모든' 라우팅 키를 오름차순으로 잠근다(키 획득순서 통일=락순서 교착도 방지).
  #   명시 17 시도 외(=parcel_default 로 라우팅되는 비정상 코드)는 단일 sentinel 0 으로 묶어 default 도 직렬화.
  # INSERT 를 CTE+RETURNING 으로 감싸 '실제 적재(inserted)' 건수를 'INS=' 접두로 출력 → dedup(ON CONFLICT)이
  # 삼킨 양이 staged 대비 보이게(가시화). advisory lock SELECT 의 void 출력은 grep 'INS=' 로 무시.
  # ★ psql 출력+stderr 를 통째 캡처하고 psql 종료코드를 '직접' 검사한다.
  #   (예전: ins=$(psql … | grep -oE 'INS='|cut) — grep 무매치 시 pipefail+set -e 로 적재 전체가
  #    '에러 메시지 없이' 죽어 원인이 가려졌다. psql 성공인데 grep 무매치여도 마찬가지로 오인 중단.)
  #   진짜 SQL 오류면 메시지를 그대로 노출하고 return 1(워커 실패→wait -n→fail-fast 유지). grep 은
  #   캡처 문자열에서만 돌리고 무매치는 ins=0 으로 흡수 → '가시화 실패'가 '적재 실패'로 둔갑하지 않게.
  local out ins
  # ★ psql 는 stdin(-f -)으로 먹여야 문장별 결과를 출력한다. -c "…;SELECT 'INS=';COMMIT;" 는
  #   멀티스테이트먼트의 '마지막' 결과(COMMIT=무출력)만 떠서 INS= 가 캡처 안 돼 ins 가 항상 0 으로
  #   오표시됐음(데이터는 정상 적재). printf … | psql -f - 로 바꿔 문장별 결과를 받는다. SQL 은 불변.
  if ! out=$(printf '%s' "
    BEGIN;
    SET LOCAL statement_timeout = 0;   -- 같은 파티션 직렬화는 advisory lock 대기를 '의도적으로' 시킨다.
    SET LOCAL lock_timeout = 0;        -- 상속된 statement/lock_timeout 이 대기 워커를 죽이지 않게(트랜잭션 한정).
    SELECT pg_advisory_xact_lock(4001, k) FROM (
      SELECT DISTINCT CASE
               WHEN left(pnu,2) IN ('11','26','27','28','29','30','31','36','41','43','44','46','47','48','50','51','52')
                    THEN left(pnu,2)::int
               WHEN left(pnu,2) = '42' THEN 51
               WHEN left(pnu,2) = '45' THEN 52
               ELSE 0 END AS k
      FROM ${stg} WHERE pnu IS NOT NULL AND length(pnu) >= 2
    ) d ORDER BY k;
    WITH ins AS (
      INSERT INTO parcel(pnu, jibun, sido_cd, sgg_cd, emd_cd, geom)
      SELECT pnu, jibun, left(pnu,2), left(pnu,5), left(pnu,8),
             ST_Multi(ST_CollectionExtract(ST_MakeValid(geom),3))
      FROM ${stg}
      WHERE geom IS NOT NULL AND pnu IS NOT NULL AND length(pnu) >= 2
      ON CONFLICT (sido_cd, pnu) DO NOTHING
      RETURNING 1
    ) SELECT 'INS='||count(*) FROM ins;
    COMMIT;" | psql -v ON_ERROR_STOP=1 -q -t -A -f - 2>&1); then
    echo "  ✗ [$idx/${N}] $lyr INSERT 실패 — 적재 중단(아래 psql 오류):" >&2
    echo "      (deadlock=교착·canceling statement…timeout=직렬대기 타임아웃·duplicate key=arbiter 등 메시지 확인)" >&2
    printf '%s\n' "$out" | sed 's/^/      /' >&2
    return 1
  fi
  ins=$(printf '%s' "$out" | grep -oE 'INS=[0-9]+' | cut -d= -f2 || true)
  ins=${ins:-0}
  local cnt
  cnt=$(psql_q -c "SELECT count(*) FROM ${stg} WHERE geom IS NOT NULL AND pnu IS NOT NULL;")
  psql -v ON_ERROR_STOP=1 -q -c "DROP TABLE IF EXISTS ${stg};" >/dev/null
  echo "  [$idx/${N}] $lyr → ${ins}/${cnt}건 (적재/스테이징)"
}

# 워커 풀: 최대 JOBS 개 동시 실행, 하나 끝나면 다음 투입(wait -n).
# 진행로그 [i/N] 는 완료 순서라 순번이 뒤섞여 보일 수 있음(정상). 워커 SQL 오류 시 즉시 중단(fail-fast).
# fail-fast(set -e)로 중단될 때 백그라운드 워커와 그 자식(ogr2ogr/psql)까지 정리 — 다음 빌드 단계로 새어나감/락 잔류 방지.
# jobs -p 는 워커 서브셸 PID. pkill -P 로 손자(ogr2ogr/psql)를 먼저 보내고 서브셸을 종료.
# ★ kill 에 '|| true' 필수 + 트랩은 'rc=$?; …; exit $rc': 정상 종료 시점엔 워커가 이미 죽어 kill 이
#   "no such process"(rc≠0)를 반환하는데, set -e 는 EXIT 트랩 안에서도 작동하므로 그 실패가 트랩을 중단시켜
#   스크립트 종료코드를 1 로 오염시킨다 → load-all 이 멀쩡히 끝난 parcel(39.8M·인덱스 OK)을 거짓 ✗ 로 오판.
#   '|| true' 로 set -e 트리거를 막고, 트랩 진입 시 $? 를 잡아 cleanup 후 그 코드로 exit → 진짜 실패(1)는 보존.
_kill_workers() { local p; for p in $(jobs -p 2>/dev/null); do pkill -TERM -P "$p" 2>/dev/null || true; kill -TERM "$p" 2>/dev/null || true; done; }
trap 'rc=$?; _kill_workers; exit $rc' EXIT
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
