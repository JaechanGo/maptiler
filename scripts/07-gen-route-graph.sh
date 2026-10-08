#!/usr/bin/env bash
# [온라인 단계] OSM 추출본 → OSRM 길찾기 그래프 (car·foot·bicycle) — FEAT-007/ADR-009
# data/osm/south-korea.osm.pbf → route/{car,foot,bicycle}/south-korea.osrm.* (MLD)
# 실측(.244 16코어, v26.10.0): car 3.5분·2.2G, foot 4.2분·3.7G, bicycle 4.7분·3.2G, 프로필당 피크 RAM 약 2GB.
# 서빙은 server/docker-compose.yml 의 osrm-car·osrm-foot·osrm-bike(osrm-routed --algorithm mld)가 담당.
#
# 흐름 — 서비스 중인 route/<p> 는 새 그래프가 검증을 통과할 때까지 건드리지 않는다:
#   0 사전검사(pbf·버전 고정 정합·디스크) → 1 route/.staging/<p> 에 빌드 + 빌드 이미지 스탬프(.osrm-image)
#   → 2 프로필별 임시 컨테이너로 QC(13i)·소요시간 변화(13k, 서빙 중 그래프 대비)
#   → 3 전 프로필 통과 시 한꺼번에 교체(이전본 route/.prev) → 4 osrm 컨테이너 재생성
#   → 5 게이트웨이 QC. 3 이후 실패하면 이전 그래프·이미지로 자동 롤백(실패본 route/.failed).
# OSRM_IMAGE_NEXT=<ref> 를 주면 그 이미지로 빌드하고, 교체가 끝난 뒤에만 버전 고정값(versions.sh·compose)을
# set-osrm-version.sh 로 바꾼다 — 빌드 스튜디오 "업그레이드" 버튼이 이 경로다. 검증에서 떨어지면 고정값은 그대로.
#
# 환경변수: PROFILES(car foot bicycle) · OSRM_IMAGE_NEXT · ROUTE_BASELINE_API(http://127.0.0.1:18080 — 서빙 중
#   게이트웨이, 소요시간 기준선·교체 후 QC 대상) · ROUTE_DRIFT_SKIP=1(소요시간 게이트 생략 — 데이터가 크게 바뀐 갱신 등)
#   · OSRM_AUTO_RESTART=0(파일 교체만, 컨테이너 재생성·교체 후 QC 생략) · ROUTE_STAGING_PORT_BASE(15900)
#   · ROUTE_DISK_CHECK=0(빈 공간 검사 생략) · PYTHON(python3) · OSRM_THREADS
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
source "$ROOT/scripts/versions.sh"   # OSRM_IMAGE 버전 핀 — compose 와의 정합은 set-osrm-version.sh --check 가 보장

PBF="${PBF:-$ROOT/data/osm/south-korea.osm.pbf}"
PROFILES="${PROFILES:-car foot bicycle}"   # 프로필 추가는 여기 + compose 서비스 1개 + 게이트웨이 분기 + 아래 매핑
THREADS="${OSRM_THREADS:-$(getconf _NPROCESSORS_ONLN 2>/dev/null || echo 4)}"
IMG="${OSRM_IMAGE_NEXT:-$OSRM_IMAGE}"
PY="${PYTHON:-python3}"
BASELINE="${ROUTE_BASELINE_API:-http://127.0.0.1:18080}"
PORT_BASE="${ROUTE_STAGING_PORT_BASE:-15900}"
AUTO_RESTART="${OSRM_AUTO_RESTART:-1}"
COMPOSE_YML="$ROOT/server/docker-compose.yml"
SETVER="$ROOT/scripts/set-osrm-version.sh"
STG="$ROOT/route/.staging"; PREV="$ROOT/route/.prev"; FAILED="$ROOT/route/.failed"
PROBE="126.787199,37.503730;126.753173,37.505836"   # 춘의역→상동역(13i 골든) — 기동 확인용 질의
BASENAME="$(basename "$PBF")"                 # south-korea.osm.pbf
OSRM_BASE="${BASENAME%.osm.pbf}.osrm"         # south-korea.osrm (compose command 가 이 이름 고정 참조)
T0="$(date +%s)"

# 프로필 ↔ compose 서비스 ↔ QC 프로필 문자열 (연관배열은 bash 4+ 전용이라 case 로)
svc_of() { case "$1" in car) echo osrm-car ;; foot) echo osrm-foot ;; bicycle) echo osrm-bike ;; *) echo "" ;; esac; }
qc_of()  { case "$1" in car) echo driving ;; foot) echo walking ;; bicycle) echo cycling ;; *) echo "" ;; esac; }

# 서빙 중(정지분 포함) osrm 컨테이너 — 검증용 임시 컨테이너(osrm-staging-*)는 제외
serving_osrm() {
  command -v docker >/dev/null 2>&1 || return 0
  docker ps -a --format '{{.Names}}' 2>/dev/null | grep -E 'osrm-(car|foot|bike)' | grep -v '^osrm-staging-' || true
}

compose() {
  if docker compose version >/dev/null 2>&1; then docker compose -f "$COMPOSE_YML" "$@"
  else docker-compose -f "$COMPOSE_YML" "$@"; fi
}
# 재생성은 빌드 대상과 무관하게 3서비스 전부 — 대용량 빌드 I/O 가 다른 프로필 컨테이너의 mmap 도 깨뜨린 사례(아래)
recreate() { compose up -d --force-recreate --no-deps osrm-car osrm-foot osrm-bike; }

wait_ok() {   # $1=URL $2=최대초 — HTTP 200 이 될 때까지 2초 간격
  local url="$1" max="$2" t=0 code
  while [ "$t" -lt "$max" ]; do
    code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "$url" 2>/dev/null || true)"
    [ "$code" = "200" ] && return 0
    sleep 2; t=$((t + 2))
  done
  return 1
}

# ★ 실행 중인 osrm-routed 는 그래프를 mmap 으로 붙들고 있다. 이 스크립트의 대용량 I/O 는
#   빌드 대상이 아닌 프로필의 컨테이너까지 그 뷰를 깨뜨린 사례가 있다(2026-09-01 실측:
#   foot 빌드 중 실행 중이던 osrm-car 가 부천 좌표를 서울 '망우로'로 스냅, 3.2km→65km 오답.
#   파일은 정상이었고 재시작만으로 복구 — 즉 런타임 상태 손상). 그래서 교체 전에 실패해도 재시작한다.
#   -a 로 '정지된' 컨테이너까지 잡는다 — 배포 절차가 빌드 전에 docker stop 을 하는 경우가 흔한데,
#   실행 중만 재시작하면 그 컨테이너가 Exited 로 방치돼 길찾기가 통째로 죽는다(2026-09-01 실측).
restart_serving() {
  [ "$AUTO_RESTART" = "1" ] || { echo "  (OSRM_AUTO_RESTART=0 — osrm 재시작 생략)"; return 0; }
  local names; names="$(serving_osrm)"
  [ -n "$names" ] || return 0
  echo "⚠ osrm 컨테이너 재시작(빌드 I/O 의 mmap 손상·정지 방치 방지): $(echo $names)"
  # shellcheck disable=SC2086
  if docker restart $names >/dev/null 2>&1; then echo "  ✓ 재시작 완료"
  else echo "  ⚠ 재시작 실패 — 수동으로 'docker start <이름>' 확인 필요" >&2; fi
}

# 롤백 — 교체(3) 이후 실패 시. 컨테이너는 디렉토리 inode 에 바인드돼 있어, 재생성 전이면 옛 그래프를 계속 서빙 중이다.
rollback() {
  echo "↩ 롤백: 이전 그래프·이미지로 복원" >&2
  mkdir -p "$FAILED"
  local p
  for p in $SWAPPED; do
    if [ -d "$PREV/$p" ]; then
      rm -rf "$FAILED/$p"
      [ -d "$ROOT/route/$p" ] && mv "$ROOT/route/$p" "$FAILED/$p"
      mv "$PREV/$p" "$ROOT/route/$p"
    else
      echo "  ⚠ [$p] 이전본 없음(첫 설치) — 새 그래프를 그대로 둔다" >&2
    fi
  done
  if [ "$PIN_CHANGED" = "1" ]; then "$SETVER" "$PREV_PIN" || echo "  ⚠ 고정값 복원 실패 — set-osrm-version.sh $PREV_PIN 수동 실행" >&2; fi
  if [ "$RECREATED" = "1" ]; then
    recreate || echo "  ⚠ 재생성 실패 — docker compose up -d --force-recreate osrm-car osrm-foot osrm-bike 수동 실행" >&2
    wait_ok "$BASELINE/route/v1/driving/$PROBE?overview=false" 180 || echo "  ⚠ 롤백 후 게이트웨이 응답 없음" >&2
  fi
}

PHASE=pre; STARTED=""; CUR_OUT=""; SWAPPED=""; PIN_CHANGED=0; RECREATED=0; PREV_PIN=""
on_exit() {
  local rc=$? n
  set +e
  for n in $STARTED; do docker rm -f "$n" >/dev/null 2>&1; done   # 임시 검증 컨테이너는 어떤 경로에서도 제거
  # 중단 시 pbf 사본(286MB) 정리 — 실측: docker 데몬 정지로 extract 실패 → 사본 잔재
  [ -n "$CUR_OUT" ] && rm -f "$CUR_OUT/$BASENAME"
  if [ "$rc" -ne 0 ]; then
    case "$PHASE" in
      build|validate)
        echo "✗ 교체 전 단계($PHASE) 실패 — 서빙 중인 route/ 와 버전 고정값은 그대로다" >&2
        rm -rf "$STG"
        restart_serving ;;
      swap|post)
        rollback
        echo "오류: 교체 후 실패 — 이전 그래프·이미지로 롤백함(실패본: route/.failed)" >&2 ;;
    esac
  fi
  exit "$rc"
}
trap on_exit EXIT

# ── 0. 사전검사 — 실패해도 아무것도 만들지 않는다 ─────────────────────
[ -s "$PBF" ] || { echo "오류: OSM 추출본 없음: $PBF — 01-download-data.sh 먼저 실행" >&2; exit 1; }
for p in $PROFILES; do
  [ -n "$(svc_of "$p")" ] || { echo "오류: 알 수 없는 프로필 '$p' (car·foot·bicycle)" >&2; exit 1; }
done
if [ -n "${OSRM_IMAGE_NEXT:-}" ]; then
  for need in car foot bicycle; do
    case " $PROFILES " in *" $need "*) ;;
      *) echo "오류: OSRM_IMAGE_NEXT 는 car·foot·bicycle 을 함께 빌드해야 한다(PROFILES=$PROFILES) — 일부만 새 이미지면 나머지 그래프가 새 이미지에서 기동 실패" >&2; exit 1 ;;
    esac
  done
  [ "$AUTO_RESTART" = "1" ] || { echo "오류: OSRM_IMAGE_NEXT 와 OSRM_AUTO_RESTART=0 은 함께 쓸 수 없다 — 고정값만 바뀌고 컨테이너는 옛 이미지로 남는다" >&2; exit 1; }
fi
[ -x "$SETVER" ] || { echo "오류: $SETVER 없음" >&2; exit 1; }
PREV_PIN="$("$SETVER" --print)"
if [ -f "$COMPOSE_YML" ]; then
  if ! _chk="$("$SETVER" --check 2>&1)"; then
    echo "$_chk" >&2
    echo "오류: versions.sh 와 compose 의 OSRM 이미지가 이미 어긋나 있다 — set-osrm-version.sh <ref> 로 먼저 맞출 것" >&2; exit 1
  fi
fi
rm -rf "$STG"   # 이전 실패 잔재(빈 공간 계산 전에 비운다)
mkdir -p "$ROOT/route"
if [ "${ROUTE_DISK_CHECK:-1}" != "0" ]; then
  # 새 그래프를 옆에 먼저 빌드하므로 한 벌이 더 필요하다: 프로필별 기존 크기×1.15(없으면 4GiB) + 여유 2GiB
  need_kb=$((2 * 1024 * 1024))
  for p in $PROFILES; do
    if [ -d "$ROOT/route/$p" ]; then
      k="$(du -sk "$ROOT/route/$p" | awk '{print $1}')"; need_kb=$((need_kb + k * 115 / 100))
    else
      need_kb=$((need_kb + 4 * 1024 * 1024))
    fi
  done
  avail_kb="$(df -Pk "$ROOT/route" | awk 'NR == 2 {print $4}')"
  if [ "$avail_kb" -lt "$need_kb" ]; then
    echo "오류: 디스크 부족 — 필요 $((need_kb / 1048576)) GB, 가용 $((avail_kb / 1048576)) GB (route/ 파일시스템)" >&2; exit 1
  fi
fi
echo "길찾기 그래프 빌드 — 이미지 $IMG (현재 고정값 $PREV_PIN), 프로필: $PROFILES"

# ── 1. 스테이징 빌드 ──────────────────────────────────────────────────
PHASE=build
mkdir -p "$STG"
for p in $PROFILES; do
  OUT="$STG/$p"; CUR_OUT="$OUT"
  mkdir -p "$OUT"
  echo "[$p] 스테이징 빌드 → route/.staging/$p/ (threads $THREADS)"
  # osrm-extract 는 pbf 와 같은 디렉토리에 산출물을 쓴다 → 프로필 디렉토리에 pbf 복사 후 추출.
  # (하드링크는 도커 볼륨 경계·타 볼륨에서 깨질 수 있어 일반 복사. 286MB — 부담 없음)
  cp "$PBF" "$OUT/$BASENAME"
  # 프로필: 저장소 보정 프로필(scripts/route-profiles/<profile>.lua — 한국 도심 계수) 우선,
  # 없으면 이미지 내장(/opt/car.lua·foot.lua). 보정 프로필은 내장 프로필을 require 하므로
  # /opt/<profile>.lua 를 덮지 않는 별도 이름(kr-*)으로 마운트. MLD 3단계: extract→partition→customize.
  KR_PROFILE="$ROOT/scripts/route-profiles/$p.lua"
  if [ -f "$KR_PROFILE" ]; then
    echo "  보정 프로필 사용: scripts/route-profiles/$p.lua"
    docker run --rm -v "$KR_PROFILE:/opt/kr-$p.lua:ro" -v "$OUT:/data" "$IMG" \
      osrm-extract -p "/opt/kr-$p.lua" -t "$THREADS" "/data/$BASENAME"
  else
    docker run --rm -v "$OUT:/data" "$IMG" \
      osrm-extract -p "/opt/$p.lua" -t "$THREADS" "/data/$BASENAME"
  fi
  docker run --rm -v "$OUT:/data" "$IMG" osrm-partition "/data/$OSRM_BASE"
  docker run --rm -v "$OUT:/data" "$IMG" osrm-customize "/data/$OSRM_BASE"
  rm -f "$OUT/$BASENAME"; CUR_OUT=""
  # customize 까지 완주해야 생기는 MLD 그래프 파일로 완성 검증(중단된 부분 산출물 차단)
  [ -s "$OUT/$OSRM_BASE.mldgr" ] || { echo "오류: [$p] $OSRM_BASE.mldgr 미생성 — customize 실패" >&2; exit 1; }
  printf '%s\n' "$IMG" > "$OUT/.osrm-image"   # 빌드 이미지 스탬프 — package.sh 가 번들 이미지와 대조
  echo "  ✓ [$p] 빌드 완료: $(du -sh "$OUT" | cut -f1)"
done

# ── 2. 스테이징 검증 — 프로필 하나씩(임시 osrm-routed 동시 기동은 RAM 부담) ─────────
PHASE=validate
i=0
for p in $PROFILES; do
  qc="$(qc_of "$p")"; port=$((PORT_BASE + i)); i=$((i + 1))
  name="osrm-staging-$p"; api="http://127.0.0.1:$port"
  echo "[$p] 검증 — 임시 컨테이너 $name ($api)"
  docker rm -f "$name" >/dev/null 2>&1 || true
  STARTED="$STARTED $name"
  docker run -d --name "$name" -p "127.0.0.1:$port:5000" -v "$STG/$p:/data" "$IMG" \
    osrm-routed --algorithm mld "/data/$OSRM_BASE" >/dev/null
  if ! wait_ok "$api/route/v1/$qc/$PROBE?overview=false" 120; then
    echo "오류: [$p] 임시 osrm 기동 실패(120초) — 로그 꼬리:" >&2
    docker logs --tail 20 "$name" >&2 2>&1 || true
    exit 1
  fi
  "$PY" "$ROOT/scripts/13i-route-qc.py" --api "$api" --profiles "$qc" --quick \
    || { echo "오류: [$p] 길찾기 QC(13i) 실패 — 교체하지 않음" >&2; exit 1; }
  if [ "${ROUTE_DRIFT_SKIP:-0}" != "1" ]; then
    drc=0
    "$PY" "$ROOT/scripts/13k-route-drift.py" --a "$BASELINE" --b "$api" --profile "$qc" || drc=$?
    case "$drc" in
      0) ;;
      3) echo "  ⚠ 기준선($BASELINE) 없음 — 소요시간 변화 검사 생략" ;;
      *) echo "오류: [$p] 소요시간 변화 게이트(13k) 실패 — 교체하지 않음(의도한 변화면 ROUTE_DRIFT_SKIP=1)" >&2; exit 1 ;;
    esac
  fi
  docker rm -f "$name" >/dev/null 2>&1 || true
  echo "  ✓ [$p] 검증 통과"
done

# ── 3. 교체 — 전 프로필 통과 후 한꺼번에 ─────────────────────────────
PHASE=swap
mkdir -p "$PREV"
for p in $PROFILES; do
  rm -rf "$PREV/$p"
  SWAPPED="$SWAPPED $p"
  [ -d "$ROOT/route/$p" ] && mv "$ROOT/route/$p" "$PREV/$p"
  mv "$STG/$p" "$ROOT/route/$p"
done
rmdir "$STG" 2>/dev/null || true
if [ -n "${OSRM_IMAGE_NEXT:-}" ] && [ "$OSRM_IMAGE_NEXT" != "$PREV_PIN" ]; then
  PIN_CHANGED=1
  "$SETVER" "$OSRM_IMAGE_NEXT"
fi
echo "✓ 그래프 교체 완료: route/{$(echo $PROFILES | tr ' ' ',')} (이전본 route/.prev)"

# ── 4·5. 재생성 + 교체 후 QC ─────────────────────────────────────────
PHASE=post
SERVING="$(serving_osrm)"
if [ -z "$SERVING" ]; then
  echo "서빙 컨테이너 없음 — 재생성·교체 후 QC 생략(폐쇄망 배포 시 deploy.sh 가 기동)"
elif [ "$AUTO_RESTART" != "1" ]; then
  echo "⚠ OSRM_AUTO_RESTART=0 — 컨테이너는 옛 그래프를 계속 서빙한다. 반영하려면:"
  echo "    docker compose -f server/docker-compose.yml up -d --force-recreate --no-deps osrm-car osrm-foot osrm-bike"
else
  echo "osrm 컨테이너 재생성(이미지 $IMG)…"
  RECREATED=1
  recreate
  for qc in driving walking cycling; do
    wait_ok "$BASELINE/route/v1/$qc/$PROBE?overview=false" 180 \
      || { echo "오류: 게이트웨이 $qc 응답 없음(180초)" >&2; exit 1; }
  done
  "$PY" "$ROOT/scripts/13i-route-qc.py" --api "$BASELINE" --quick \
    || { echo "오류: 교체 후 길찾기 QC(13i) 실패" >&2; exit 1; }
fi

PHASE=done
rm -rf "$PREV" "$FAILED"
echo "길찾기 그래프 생성·교체 완료 — 이미지 $IMG, $(( $(date +%s) - T0 ))초"
for p in $PROFILES; do echo "    $p: $(du -sh "$ROOT/route/$p" | cut -f1)"; done
