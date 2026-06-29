#!/usr/bin/env bash
# [폐쇄망 단계] 번들 압축 해제 디렉토리에서 실행:
#   tar xzf cuvia-map-bundle.tgz && ./scripts/deploy.sh /path/to/images.tar
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
IMAGES_TAR="${1:-$ROOT/images.tar}"

# C3: Compose v2 플러그인(docker compose) 우선, 없으면 v1 바이너리(docker-compose) 사용
if docker compose version >/dev/null 2>&1; then
  COMPOSE_CMD="docker compose"
elif command -v docker-compose >/dev/null 2>&1; then
  COMPOSE_CMD="docker-compose"
else
  echo "오류: 'docker compose' 플러그인 또는 'docker-compose' 바이너리가 필요합니다." >&2
  exit 1
fi

if [ -f "$IMAGES_TAR" ]; then
  echo "Docker 이미지 적재: $IMAGES_TAR"
  docker load -i "$IMAGES_TAR"
else
  # M1: 이미지 tar 미발견 경고는 stderr 로 출력
  echo "⚠ images.tar 경로를 인자로 주세요 (이미 load 했다면 무시)" >&2
fi

# 번들 파일 권한 정규화 — 빌드호스트(맥 등)에서 tar 로 묶일 때 보존된 700(소유자 전용)·빌드호스트 uid 가
# 그대로 반입되면, tileserver/geocode 컨테이너 내부 사용자(uid≠빌드호스트)가 bind 마운트
# (demo/style/glyphs/tiles/geocode)를 못 읽어 EACCES 로 기동 실패한다(/styles.json=[] 증상).
# 모든 사용자에 읽기 + 디렉토리 진입(a+rX)만 부여(쓰기 미부여 → 안전·멱등). SELinux 는 호스트 정책으로 별도.
for _d in demo style vendor tiles geocode; do
  [ -e "$ROOT/$_d" ] && chmod -R a+rX "$ROOT/$_d" 2>/dev/null || true
done

# tileserver-config.json 이 베이스 mbtiles 3종(korea/terrain/dong)을 참조하므로, 하나라도 없으면
# TileServer-GL 이 기동 자체에 실패한다(벡터 단독 degrade 없음) — 사전 검증.
# (buildings/poi/parcel 동적 레이어는 PostGIS→martin — postgis/cuvia.dump 로 별도 복원)
for mb in korea.mbtiles terrain.mbtiles dong.mbtiles; do   # buildings/poi 는 PostGIS→martin(/dyn)
  if [ ! -s "$ROOT/tiles/$mb" ]; then   # -s: 존재+크기>0 (0바이트 evict/잘린 파일도 차단, package.sh와 통일)
    echo "오류: tiles/$mb 가 없거나 0바이트 — 번들이 불완전합니다. 압축 해제 경로를 확인하세요." >&2
    exit 1
  fi
done

# geocode 서비스용 지오코딩 인덱스 — compose 의 geocode 컨테이너가 참조한다.
if [ ! -s "$ROOT/geocode/geocode.sqlite" ]; then
  echo "오류: geocode/geocode.sqlite 가 없습니다 — 번들이 불완전합니다." >&2
  exit 1
fi

# PostGIS 번들 감지 → --profile postgis 로 기동(동적 레이어 필지·건물·시설·martin·지오코더 포함)
COMPOSE_PROFILE=""
if [ -f "$ROOT/postgis/cuvia.dump" ]; then
  COMPOSE_PROFILE="--profile postgis"
  echo "PostGIS 번들 감지 — 동적 레이어·martin 포함 기동"
fi

# shellcheck disable=SC2086
cd "$ROOT/server" && $COMPOSE_CMD $COMPOSE_PROFILE up -d

# PostGIS 덤프 복원(최초 1회, 멱등) — postgis healthy 대기 후 pg_restore
if [ -f "$ROOT/postgis/cuvia.dump" ]; then
  echo "PostGIS healthy 대기(최대 120초)..."
  PGC="$($COMPOSE_CMD ps -q postgis 2>/dev/null || true)"
  _w=0
  until [ -n "$PGC" ] && docker exec "$PGC" pg_isready -U "${PGUSER:-cuvia}" >/dev/null 2>&1; do
    [ "$_w" -ge 120 ] && { echo "오류: postgis 가 120초 내 미응답" >&2; exit 1; }
    sleep 5; _w=$((_w + 5)); PGC="$($COMPOSE_CMD ps -q postgis 2>/dev/null || true)"
  done
  HAS=$(docker exec "$PGC" psql -tA -U "${PGUSER:-cuvia}" -d "${PGDATABASE:-cuvia}" \
        -c "SELECT to_regclass('public.parcel') IS NOT NULL" 2>/dev/null || echo "")
  if [ "$HAS" = "t" ]; then
    echo "PostGIS 이미 적재됨 — 복원 건너뜀(재복원하려면 docker volume rm server_pgdata 후 재실행)"
  else
    echo "PostGIS 덤프 복원(pg_restore)..."
    docker exec -i "$PGC" pg_restore -U "${PGUSER:-cuvia}" -d "${PGDATABASE:-cuvia}" \
      --clean --if-exists --no-owner < "$ROOT/postgis/cuvia.dump" \
      || echo "  (일부 복원 경고 — 소유권/기존객체 무시 가능)"
    echo "PostGIS 복원 완료"
  fi
  # martin 은 '기동 시점' 카탈로그만 스캔해 table 소스를 발행한다(auto_publish=false+명시테이블).
  # 위 compose up 에서 martin 이 '복원 전 빈 postgis' 를 먼저 스캔하면 소스 0개로 떠
  # /dyn/* 가 전부 404 가 된다(catalog={"tiles":{}}). → 복원 확인 후 재시작해 재스캔시킨다.
  # martin 은 stateless(데이터는 postgis), L2 캐시는 404 를 캐시하지 않으므로 멱등·안전.
  echo "martin 카탈로그 재스캔(restart) — /dyn 소스 발행 보장..."
  $COMPOSE_CMD restart martin >/dev/null 2>&1 || true
fi

# W1: tileserver 헬스 대기 (최대 60초, 5초 간격 × 12회)
echo "tileserver 헬스 확인 중 (최대 60초)..."
_waited=0
until curl -sf http://localhost:8080/health >/dev/null 2>&1; do
  if [ "$_waited" -ge 60 ]; then
    echo "오류: tileserver 가 60초 내에 응답하지 않습니다." >&2
    echo "  로그 확인: $COMPOSE_CMD logs tileserver" >&2
    exit 1
  fi
  sleep 5
  _waited=$((_waited + 5))
done
echo "tileserver 정상 응답 확인"

# 스타일 실로드 스모크 — /health 200 은 베이스 mbtiles 만 보장한다. maplibre style-spec 위반 시 tileserver-gl 은
# 스타일을 거부해 /styles.json 이 [] 가 되고 /styles/<id>/style.json 이 404 인데도 /health 는 200(=deploy '성공' 오인).
# 실측 사고(poi-label zoom 중첩)가 폐쇄망에서 이렇게 새어나갔으므로, 기동 후 스타일이 실제 로드됐는지 직접 어서션한다.
# tileserver-gl 은 기동 시 스타일을 모두 처리한 뒤 HTTP 를 연다 → /health 통과 시점엔 로드/거부가 확정(추가 대기 불필요).
TS="http://localhost:8080"
CFG="$ROOT/server/tileserver-config.json"
read_style_ids() {   # tileserver-config.json 의 styles.<id> 키 — jq→python3→awk 폴백(폐쇄망 최소호스트 대비). 하드코딩 회피.
  local cfg="$1"
  if command -v jq >/dev/null 2>&1; then
    jq -r '.styles | keys[]' "$cfg" 2>/dev/null && return 0
  fi
  if command -v python3 >/dev/null 2>&1; then
    python3 -c 'import json,sys
for k in json.load(open(sys.argv[1])).get("styles",{}): print(k)' "$cfg" 2>/dev/null && return 0
  fi
  awk '            # 폴백: "styles":{ 블록의 depth0 키만(2/4-스페이스 들여쓰기 전제 — 본 repo 포맷). jq/python3 둘 다 없을 때만.
    /^[[:space:]]*"styles"[[:space:]]*:[[:space:]]*\{/ {ins=1; next}
    ins {
      if (depth==0 && match($0,/"[^"]+"[[:space:]]*:/)) { s=substr($0,RSTART+1); sub(/".*/,"",s); print s }
      depth += gsub(/\{/,"{") - gsub(/\}/,"}")
      if (depth<0) ins=0
    }' "$cfg"
}
style_smoke() {   # /styles.json 비어있지 않음 + 설정의 각 style id 가 200 인지 어서션. 실패 사유는 stderr 로 보고.
  local body compact code id rc=0 ids
  body="$(curl -sf "$TS/styles.json" 2>/dev/null || true)"
  compact="$(printf '%s' "$body" | tr -d '[:space:]')"
  if [ -z "$compact" ] || [ "$compact" = "[]" ]; then
    echo "오류: tileserver /styles.json 이 비어있음([]) — 스타일이 거부됨(maplibre style-spec 위반 의심)." >&2
    return 1
  fi
  ids="$(read_style_ids "$CFG" 2>/dev/null || true)"
  if [ -z "$ids" ]; then
    echo "  경고: tileserver-config.json 에서 style id 를 읽지 못함 — /styles.json 비어있지 않음만 확인(개별 id 검사 생략)." >&2
    return 0
  fi
  while IFS= read -r id; do
    [ -n "$id" ] || continue
    code="$(curl -s -o /dev/null -w '%{http_code}' "$TS/styles/$id/style.json" 2>/dev/null || true)"  # 실패해도 curl 이 -w 로 000 출력
    if [ "$code" = "200" ]; then
      echo "  ✓ 스타일 로드 확인: /styles/$id/style.json (200)"
    else
      echo "오류: /styles/$id/style.json HTTP $code (기대 200) — 스타일 '$id' 가 로드되지 않음." >&2
      rc=1
    fi
  done <<EOF
$ids
EOF
  return "$rc"
}
echo "스타일 실로드 스모크 확인 중..."
if ! style_smoke; then
  echo "오류: 스타일이 tileserver 에 로드되지 않았습니다 — maplibre style-spec 위반(예: poi-label zoom 중첩)일 수 있습니다." >&2
  echo "  로그 확인: (cd \"$ROOT/server\" && $COMPOSE_CMD logs tileserver)" >&2
  echo "  재발 방지: 빌드 단계 QC(scripts/13-qc-check.py 스타일 spec 검증)가 차단했어야 합니다 — 번들을 재생성하세요." >&2
  exit 1
fi
echo "스타일 실로드 확인 완료"

# 연동 가이드(frontPage) 스모크 — :8080/ 가 guide.html 을 서빙하는지 확인. /health·/styles 만 보는 기존 스모크는
# frontPage 깨짐(빈 디렉토리/누락)을 못 잡으므로 랜딩을 직접 어서션한다. 가이드는 부가기능이라 실패해도 지도 배포는
# 막지 않고 경고만(빌드단계 package.sh 의 guide.html 게이트가 1차 차단). 마운트/frontPage 오설정 조기 발견용.
if curl -sf "$TS/" 2>/dev/null | grep -q 'styles/cuvia/style.json'; then
  echo "  ✓ 연동 가이드 랜딩 확인: $TS/ (frontPage=demo/guide.html)"
else
  echo "  경고: $TS/ 가 연동 가이드를 서빙하지 않습니다 — compose 의 ../demo:/data/demo 마운트·frontPage 설정 확인(지도 자체는 정상)." >&2
fi

# 게이트웨이 외부 포트 — 환경변수 우선, 없으면 server/.env, 그래도 없으면 80.
GW_PORT="${GATEWAY_PORT:-$(sed -n 's/^GATEWAY_PORT=//p' "$ROOT/server/.env" 2>/dev/null | tail -1 | tr -dc 0-9)}"
GW_PORT="${GW_PORT:-80}"; GW_SFX=""; [ "$GW_PORT" = "80" ] || GW_SFX=":$GW_PORT"
echo "기동 완료 — 외부 노출은 게이트웨이 한 포트(GATEWAY_PORT=$GW_PORT):"
echo "  가이드  http://<이서버IP>${GW_SFX}/         (연동 가이드 · /info 동일 · :8080/ 직결도 동일)"
echo "  데모    http://<이서버IP>${GW_SFX}/demo/"
echo "  스타일  http://<이서버IP>${GW_SFX}/styles/cuvia/style.json"
echo "  지오코딩 http://<이서버IP>${GW_SFX}/geocode?q=서울시청"
echo "  (tileserver:8080·geocode:8082 직결은 loopback 전용 — 외부엔 :$GW_PORT 만 노출. 방화벽도 그 포트만 개방)"
echo
echo "스타일 디자인(Style Studio)은 관리툴 — LAN 비노출, SSH 터널 권장:"
echo "  서버: STUDIO_TOKEN=\$(openssl rand -hex 12) ./scripts/start-style-studio.sh"
echo "  PC:   ssh -L 8091:localhost:8091 -L 8080:localhost:8080 <user>@<이서버IP>  # → http://localhost:8091/?token=…"
echo "  (프리뷰가 브라우저에서 tileserver:8080 을 직접 호출하므로 8080 도 함께 터널)"
