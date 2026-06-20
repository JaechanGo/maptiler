#!/usr/bin/env bash
# CUVIA PostGIS 스키마 적용 — schema/*.sql 을 번호순으로 psql 실행(멱등).
# 연결은 표준 libpq 환경변수 사용. compose postgis 서비스 기본값과 일치.
#   PGHOST(기본 localhost) PGPORT(5432) PGUSER(cuvia) PGDATABASE(cuvia) PGPASSWORD
# 사용: PGPASSWORD=... scripts/postgis/apply-schema.sh
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/schema"
export PGHOST="${PGHOST:-localhost}"
export PGPORT="${PGPORT:-5432}"
export PGUSER="${PGUSER:-cuvia}"
export PGDATABASE="${PGDATABASE:-cuvia}"

# 호스트 psql 우선. 없으면(sudo로 설치 불가 등) postgis 컨테이너의 psql 사용 — 호스트 설치·sudo 불필요.
REPO="$(cd "$DIR/../../.." && pwd)"
COMPOSE="docker compose -f $REPO/server/docker-compose.yml"
if command -v psql >/dev/null 2>&1; then
    VIA="host psql → ${PGUSER}@${PGHOST}:${PGPORT}/${PGDATABASE}"
    apply() { psql -v ON_ERROR_STOP=1 -q -f "$1"; }
elif command -v docker >/dev/null 2>&1 && [ -n "$($COMPOSE ps -q postgis 2>/dev/null)" ]; then
    VIA="postgis 컨테이너 psql (호스트 psql 없음 → sudo 불필요)"
    apply() { $COMPOSE exec -T postgis psql -v ON_ERROR_STOP=1 -q -U "$PGUSER" -d "$PGDATABASE" < "$1"; }
else
    echo "✗ psql 없음 + postgis 컨테이너 미가동." >&2
    echo "  → cd server && docker compose --profile postgis up -d postgis  후 재시도(호스트 psql 설치 불필요)." >&2
    exit 1
fi

echo "PostGIS 스키마 적용 ($VIA)"
for f in "$DIR"/*.sql; do
    echo "  [apply] $(basename "$f")"
    apply "$f"
done
echo "OK: 스키마 적용 완료"
