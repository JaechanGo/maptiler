-- parcel.jibun → (san, ji_main, ji_sub) 정규화 백필. 본 파일은 san/본번/부번만 채운다.
-- geom_pt 는 분리됨 → backfill_geom_pt.sql (무거운 ST_PointOnSurface 격리).
-- 증분 가드(ji_main IS NULL)로 이미 채워진 행 재기록 방지(39.9M rewrite 회피).
-- 인덱스 DROP→UPDATE→CREATE 래핑(순서 유지). 단, 단일 트랜잭션 금지:
--   DROP INDEX 의 ACCESS EXCLUSIVE 락은 COMMIT 까지 유지되므로, DROP/39.6M UPDATE/CREATE 를
--   한 트랜잭션으로 묶으면 UPDATE 전 구간 parcel 읽기까지 차단(라이브 geocode-api 블록). 따라서
--   DROP/CREATE 는 단독 문장으로 짧게 AEL/SHARE 만 잡고, UPDATE 는 파티션별 \gexec 독립 커밋.
-- int4 오버플로 가드(length<=7) 보존 — 경북/충남 100% NULL 버그(Global Constraint L23) 방지.
-- geom_pt 백필(backfill_geom_pt.sql)과 동일한 \gexec + pg_inherits 패턴으로 대칭(DRY).
-- 근거: docs/superpowers/plans/2026-06-24-geocode-overhaul-plan.md A2 / docs/geocode-jibun-parcel-plan.md §2.1
\timing on

-- work_mem/maintenance_work_mem 는 세션 SET(SET LOCAL 아님 — 파티션별 \gexec 가 각 auto-commit 이라
-- SET LOCAL 은 문장 간 유지되지 않음). 파일 말미 RESET 후 ANALYZE 로 환원(§4, R2).
SET work_mem = '256MB';                       -- self-join hash 스필 완화(§4, EXPLAIN 근거 explain-evidence.md)
SET maintenance_work_mem = '512MB';           -- 재생성 CREATE INDEX 가속(이제 본 파일이 인덱스 생성)

-- 파티션 부모 인덱스 DROP → 자식 전파 제거(§3). 단독 문장 → AEL 즉시 해제. 멱등: IF EXISTS.
DROP INDEX IF EXISTS parcel_jibun_lookup;

-- 파티션별 self-join UPDATE 를 \gexec 로 생성(각 auto-commit). 증분 가드(ji_main IS NULL)는
-- 서브쿼리 내부에 push → regexp/hash 입력 자체 축소. int4 가드 2줄 보존.
SELECT format(
  $f$UPDATE %1$s p SET
       san     = CASE WHEN left(btrim(p.jibun),1) = '산' THEN 1 ELSE 0 END,
       ji_main = (s.m)[1]::int,
       ji_sub  = COALESCE((s.m)[2], '0')::int
     FROM (
       SELECT id, sido_cd, regexp_match(jibun, '(\d+)(?:-(\d+))?') AS m
       FROM %1$s
       WHERE jibun IS NOT NULL
         AND ji_main IS NULL
     ) s
     WHERE p.id = s.id AND p.sido_cd = s.sido_cd AND s.m IS NOT NULL
       AND length((s.m)[1]) <= 7
       AND ((s.m)[2] IS NULL OR length((s.m)[2]) <= 7);$f$,
  inhrelid::regclass)
FROM pg_inherits
WHERE inhparent = 'parcel'::regclass
ORDER BY inhrelid::regclass::text
\gexec

-- 인덱스 재생성 — 21-parcel-jibun.sql L27 정의와 동일 컬럼·순서(§3 동치 검증). 단독 문장. 멱등: IF NOT EXISTS.
CREATE INDEX IF NOT EXISTS parcel_jibun_lookup ON parcel (emd_cd, ji_main, ji_sub);

RESET work_mem;                               -- 세션 SET 환원(누수 차단, §4 R2)
RESET maintenance_work_mem;
ANALYZE parcel;   -- RESET 후 ANALYZE — 통계 갱신으로 이후 지번질의 플랜 정상화.
