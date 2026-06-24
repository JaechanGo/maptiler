-- parcel.geom_pt 대표점 백필 — backfill_parcel_jibun.sql 에서 분리(무거운 ST_PointOnSurface 격리).
-- 파티션별 UPDATE(\gexec) → 각 UPDATE 독립 커밋(단일 거대 트랜잭션 금지: 스펙 C6/D3 L147).
-- WHERE geom_pt IS NULL 증분 가드(재실행 시 미충족 파티션만 처리, idempotent·재개 가능).
-- work_mem / maintenance_work_mem SET 없음 — 단순 seq scan + 함수 평가, 인덱스 생성도 없음(§4).
-- 근거: docs/superpowers/plans/2026-06-24-geocode-overhaul-plan.md C6 / ops D3
\timing on

-- pg_inherits 로 parcel 자식 파티션 목록을 동적 생성 → 파티션 추가/변경에 자동 적응(DRY).
-- \gexec: 각 생성행을 개별 명령으로 실행 → 둘러싼 BEGIN 없음 → 파티션마다 implicit auto-commit.
SELECT format(
  'UPDATE %s SET geom_pt = ST_PointOnSurface(geom) WHERE geom_pt IS NULL;',
  inhrelid::regclass)
FROM pg_inherits
WHERE inhparent = 'parcel'::regclass
ORDER BY inhrelid::regclass::text
\gexec

ANALYZE parcel;
