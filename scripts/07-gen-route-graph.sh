#!/usr/bin/env bash
# [온라인 단계] OSM 추출본 → OSRM 길찾기 그래프 (car·foot 2프로필) — FEAT-007/ADR-009
# data/osm/south-korea.osm.pbf → route/{car,foot}/south-korea.osrm.* (MLD)
# 실측(.244 16코어): 프로필당 extract 36초·전 과정 약 3분, 피크 RAM 1.7GB, 디스크 1.9GB.
# 서빙은 server/docker-compose.yml 의 osrm-car·osrm-foot(osrm-routed --algorithm mld)가 담당.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
source "$ROOT/scripts/versions.sh"   # OSRM_IMAGE 버전 핀(compose 와 동일 태그 — 드리프트 금지)

PBF="${PBF:-$ROOT/data/osm/south-korea.osm.pbf}"
PROFILES="${PROFILES:-car foot}"     # 프로필 추가(예: bicycle)는 여기 + compose 서비스 1개
THREADS="${OSRM_THREADS:-$(getconf _NPROCESSORS_ONLN 2>/dev/null || echo 4)}"

[ -s "$PBF" ] || { echo "오류: OSM 추출본 없음: $PBF — 01-download-data.sh 먼저 실행" >&2; exit 1; }

BASENAME="$(basename "$PBF")"                 # south-korea.osm.pbf
OSRM_BASE="${BASENAME%.osm.pbf}.osrm"         # south-korea.osrm (compose command 가 이 이름 고정 참조)

for profile in $PROFILES; do
  OUT="$ROOT/route/$profile"
  mkdir -p "$OUT"
  echo "[$profile] OSRM 그래프 생성 → route/$profile/ (이미지 $OSRM_IMAGE, threads $THREADS)"
  # osrm-extract 는 pbf 와 같은 디렉토리에 산출물을 쓴다 → 프로필 디렉토리에 pbf 복사 후 추출.
  # (하드링크는 도커 볼륨 경계·타 볼륨에서 깨질 수 있어 일반 복사. 286MB — 부담 없음)
  cp "$PBF" "$OUT/$BASENAME"
  # 프로필: 저장소 보정 프로필(scripts/route-profiles/<profile>.lua — 한국 도심 계수) 우선,
  # 없으면 이미지 내장(/opt/car.lua·foot.lua). 보정 프로필은 내장 프로필을 require 하므로
  # /opt/<profile>.lua 를 덮지 않는 별도 이름(kr-*)으로 마운트. MLD 3단계: extract→partition→customize.
  KR_PROFILE="$ROOT/scripts/route-profiles/$profile.lua"
  if [ -f "$KR_PROFILE" ]; then
    echo "  보정 프로필 사용: scripts/route-profiles/$profile.lua"
    docker run --rm -v "$KR_PROFILE:/opt/kr-$profile.lua:ro" -v "$OUT:/data" "$OSRM_IMAGE" \
      osrm-extract -p "/opt/kr-$profile.lua" -t "$THREADS" "/data/$BASENAME"
  else
    docker run --rm -v "$OUT:/data" "$OSRM_IMAGE" \
      osrm-extract -p "/opt/$profile.lua" -t "$THREADS" "/data/$BASENAME"
  fi
  docker run --rm -v "$OUT:/data" "$OSRM_IMAGE" \
    osrm-partition "/data/$OSRM_BASE"
  docker run --rm -v "$OUT:/data" "$OSRM_IMAGE" \
    osrm-customize "/data/$OSRM_BASE"
  rm -f "$OUT/$BASENAME"   # 추출 후 pbf 사본 제거(원본은 data/osm 유지)
  # customize 까지 완주해야 생기는 MLD 그래프 파일로 완성 검증(중단된 부분 산출물 차단)
  [ -s "$OUT/$OSRM_BASE.mldgr" ] \
    || { echo "오류: [$profile] $OSRM_BASE.mldgr 미생성 — customize 실패" >&2; exit 1; }
  echo "  ✓ [$profile] 완료: $(du -sh "$OUT" | cut -f1)"
done

echo "길찾기 그래프 생성 완료: route/{$(echo $PROFILES | tr ' ' ',')}"

# ★ 실행 중인 osrm-routed 는 그래프를 mmap 으로 붙들고 있다. 이 스크립트의 대용량 I/O 는
#   빌드 대상이 아닌 프로필의 컨테이너까지 그 뷰를 깨뜨린 사례가 있다(2026-09-01 실측:
#   foot 빌드 중 실행 중이던 osrm-car 가 부천 좌표를 서울 '망우로'로 스냅, 3.2km→65km 오답.
#   파일은 정상이었고 재시작만으로 복구 — 즉 런타임 상태 손상). 빌드 후 osrm 전체 재시작이 정답.
if command -v docker >/dev/null 2>&1; then
  RUNNING="$(docker ps --filter name=osrm --format '{{.Names}}' 2>/dev/null || true)"
  if [ -n "$RUNNING" ]; then
    echo "⚠ 실행 중인 osrm 컨테이너가 있습니다 — 그래프 교체 후 재시작해야 오답을 막습니다:"
    echo "$RUNNING" | sed 's/^/    /'
    if [ "${OSRM_AUTO_RESTART:-1}" = "1" ]; then
      echo "  → 재시작 중 (건너뛰려면 OSRM_AUTO_RESTART=0)"
      # shellcheck disable=SC2086
      docker restart $RUNNING >/dev/null && echo "  ✓ 재시작 완료"
    fi
  fi
fi
