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

if ! command -v psql >/dev/null 2>&1; then
    echo "psql 미설치 — scripts/setup-build-host.sh 실행(PostgreSQL client) 후 재시도" >&2
    exit 1
fi

echo "PostGIS 스키마 적용 → ${PGUSER}@${PGHOST}:${PGPORT}/${PGDATABASE}"
for f in "$DIR"/*.sql; do
    echo "  [apply] $(basename "$f")"
    psql -v ON_ERROR_STOP=1 -q -f "$f"
done
echo "OK: 스키마 적용 완료"
