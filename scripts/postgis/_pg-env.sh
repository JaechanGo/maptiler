#!/usr/bin/env bash
# CUVIA PostGIS 로더 공유 — libpq 환경변수 기본값 + ogr2ogr PG 연결문자열.
# source 해서 사용. compose postgis 서비스 기본값(cuvia/cuvia)과 일치.
export PGHOST="${PGHOST:-localhost}"
export PGPORT="${PGPORT:-5432}"
export PGUSER="${PGUSER:-cuvia}"
export PGDATABASE="${PGDATABASE:-cuvia}"
export PGPASSWORD="${PGPASSWORD:-cuvia}"

# ogr2ogr 용 PG 접속 문자열(따옴표 없이 1토큰으로 넘길 것).
PG_OGR="PG:dbname=${PGDATABASE} host=${PGHOST} port=${PGPORT} user=${PGUSER} password=${PGPASSWORD}"
export PG_OGR

pg_need() {   # 필수 CLI 점검
  local miss=0
  for t in "$@"; do command -v "$t" >/dev/null 2>&1 || { echo "✗ 필요 도구 없음: $t — scripts/setup-build-host.sh" >&2; miss=1; }
  done
  [ "$miss" = 0 ] || exit 1
}

psql_q() { psql -v ON_ERROR_STOP=1 -q -t -A "$@"; }   # quiet, tuples-only, unaligned
