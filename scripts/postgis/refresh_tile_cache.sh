#!/usr/bin/env bash
# 타일 캐시 3겹 교체 — PostGIS 데이터 재적재(load-all·juso 패치 등) 후 반드시 호출.
#
# 데이터만 갈고 이걸 안 돌리면 "줌 레벨마다 다른 시대"가 서빙된다(2026-08-31 .244 실측:
# 삭제한 단지 덮개가 z17/18 에만 잔존, z16 은 적재 이전 시대 — 원인 추적에 반나절).
#   ① martin L1(Moka, 인메모리) — DB 변경을 모른다 → 재시작만이 해법
#   ② 게이트웨이 L2 디스크(proxy_cache, gzip/비gzip 변형 별도 키) → 퍼지 + reload
#   ③ 브라우저 HTTP 캐시(max-age 3600 + SWR 86400) — 서버에서 못 지운다
#      → style.json 의 /dyn/v<BUILD_ID>/ 네임스페이스를 새로 발급해 URL 자체를 바꾼다
#      (style.json 은 max-age 300 이라 5분 내 전 클라이언트 전파)
# 순서 중요: ①→②→③. ③을 먼저 하면 스테일 타일이 새 버전 URL 로 캐시돼 도루묵.
#
# 사용:  scripts/postgis/refresh_tile_cache.sh
#        BUILD_ID=20260901-jusoSep COMPOSE_FILE=... scripts/postgis/refresh_tile_cache.sh
# 서빙 스택(martin/gateway/tileserver)이 안 떠 있으면 해당 단계는 경고 후 건너뛴다
# (빌드 전용 호스트에서 load-all 이 이걸 체인해도 죽지 않도록 — 비치명 skip).
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
COMPOSE_FILE="${COMPOSE_FILE:-$ROOT/server/docker-compose.yml}"
# BUILD_ID: build-style.sh 와 동일 문자집합([0-9A-Za-z._-], 게이트웨이 정규식 계약).
# git 이 없는 배포 호스트(.244 등)를 위해 해시 실패 시 시각으로 폴백.
: "${BUILD_ID:=$(date +%Y%m%d)-$(git -C "$ROOT" rev-parse --short HEAD 2>/dev/null || date +%H%M%S)}"
export BUILD_ID

# martin·geocode-pg 는 profiles:[postgis] 소속 — 프로파일 없이 restart 하면 "no such service" 로
# **조용히** 실패한다(2026-08-31 .244 실측: 이것 때문에 martin 이 데이터 정리 후에도 재시작되지
# 않아 L1 스테일이 반나절 살아남음). ps -q 는 프로파일 무관이라 탐지도 못 한다.
export COMPOSE_PROFILES="${COMPOSE_PROFILES:-postgis}"
cid(){ docker compose -f "$COMPOSE_FILE" ps -q "$1" 2>/dev/null; }
fail=0

echo "━━ 타일 캐시 교체 (BUILD_ID=$BUILD_ID) ━━"

# ① martin L1 — 재시작
if [ -n "$(cid martin)" ]; then
  if docker compose -f "$COMPOSE_FILE" restart martin; then
    echo "  ① martin 재시작 완료(L1 소거)"
  else
    echo "  ✗ martin 재시작 실패" >&2; fail=1
  fi
else
  echo "  ① (건너뜀) martin 미가동"
fi

# ② 게이트웨이 L2 — 컨테이너 안에서 퍼지(호스트 볼륨 경로 차이 무관) + reload
GW="$(cid gateway)"
if [ -n "$GW" ]; then
  docker exec "$GW" sh -c 'rm -rf /data/cache/martin/* && nginx -s reload' \
    || { echo "  ✗ L2 퍼지/reload 실패" >&2; fail=1; }
  echo "  ② 게이트웨이 L2 퍼지 완료"
else
  echo "  ② (건너뜀) gateway 미가동"
fi

# ③ 스타일 버전 범프 — /dyn/v<BUILD_ID>/ 재발급 + tileserver 재시작
#    build_style.py 는 theme.json(스튜디오 저장분)을 그대로 재적용하므로 스타일 내용은 불변,
#    소스 URL 의 버전만 바뀐다. STYLE_IMPORT 사용 배포도 동일(가져온 스타일에 버전만 입힘).
if python3 "$ROOT/scripts/build_style.py"; then
  if [ -n "$(cid tileserver)" ]; then
    docker compose -f "$COMPOSE_FILE" restart tileserver \
      || { echo "  ✗ tileserver 재시작 실패 — 새 style.json 미서빙" >&2; fail=1; }
    echo "  ③ 스타일 v$BUILD_ID 발급·tileserver 재시작 완료"
  else
    echo "  ③ 스타일 v$BUILD_ID 발급(tileserver 미가동 — 다음 기동 시 서빙)"
  fi
else
  echo "  ✗ build_style 실패 — 스타일 버전 미범프(브라우저 캐시가 옛 타일을 계속 씀)" >&2; fail=1
fi

if [ "$fail" = 0 ]; then echo "OK: 캐시 3겹 교체 완료 (v$BUILD_ID)"; else
  echo "✗ 캐시 교체 일부 실패 — 위 로그 확인" >&2; exit 1; fi
