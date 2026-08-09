-- ri_backfill_s4_backup.sql — T018 Phase 1 / S4.
--   S5(비가역 UPDATE) 직전 백업. **이 단계를 건너뛰고 S5 를 실행하지 말 것.**
--
--   롤백 원본은 두 겹이다:
--     ① _ri_backfill_todo.old_bcode  — 변경 대상 행의 원본값 (S3 에서 이미 확보)
--     ② address_bcode_bak_20260810   — kind='addr' **전 행**의 (id, bcode) 스냅샷
--   ①만으로도 정방향 롤백은 되지만, todo 자체가 손상된 경우를 대비해 ②를 둔다.
--
-- 선행: S3 게이트 통과.
-- 실행: psql -v ON_ERROR_STOP=1 -f scripts/postgis/ri_backfill_s4_backup.sql
\set ON_ERROR_STOP on
\timing on

-- 선행조건 — todo 가 없거나 비어 있으면 백업할 이유도 없다(순서 착오 방지).
DO $pre$
DECLARE n bigint;
BEGIN
  IF to_regclass('public._ri_backfill_todo') IS NULL THEN
    RAISE EXCEPTION '_ri_backfill_todo 가 없다. S3 를 먼저 실행하라.';
  END IF;
  SELECT count(*) INTO n FROM _ri_backfill_todo;
  IF n = 0 THEN
    RAISE EXCEPTION '_ri_backfill_todo 가 비었다. S3 결과를 확인하라.';
  END IF;
  RAISE NOTICE '백필 대상 % 행 — 백업을 시작한다.', n;
END $pre$;

-- 기존 백업이 있으면 **덮어쓰지 않는다.** 부분 실행 후 재실행 시 백업이
-- '이미 갱신된 값'으로 덮여 롤백 원본이 통째로 사라지는 사고를 막는다.
DO $bak$
BEGIN
  IF to_regclass('public.address_bcode_bak_20260810') IS NOT NULL THEN
    RAISE EXCEPTION '백업 address_bcode_bak_20260810 이 이미 있다. 덮어쓰면 롤백 원본이 사라진다. 의도한 재실행이면 수동으로 이름을 바꾸거나 지운 뒤 다시 실행하라.';
  END IF;
END $bak$;

-- 전 행 스냅샷 (약 10,686,547행 × ~30B ≈ 320 MB)
CREATE TABLE address_bcode_bak_20260810 AS
  SELECT id, bcode FROM address WHERE kind='addr';
CREATE UNIQUE INDEX address_bcode_bak_20260810_id_idx ON address_bcode_bak_20260810 (id);
COMMENT ON TABLE address_bcode_bak_20260810 IS
  'T018 S4: S5 백필 직전 address(kind=addr) bcode 전량 스냅샷. Phase 1 검증 완료 전 DROP 금지.';

ANALYZE address_bcode_bak_20260810;

\echo '=== S4 검증 ==='
SELECT count(*) AS "백업 행수 (기대 10,686,547)" FROM address_bcode_bak_20260810;
SELECT count(*) AS "원본 kind=addr 행수 (동일해야 함)" FROM address WHERE kind='addr';
SELECT count(*) AS "todo 중 백업 누락 (기대 0)"
  FROM _ri_backfill_todo t
 WHERE NOT EXISTS (SELECT 1 FROM address_bcode_bak_20260810 b WHERE b.id = t.id);
SELECT count(*) AS "백업값 ≠ todo.old_bcode (기대 0)"
  FROM _ri_backfill_todo t
  JOIN address_bcode_bak_20260810 b ON b.id = t.id
 WHERE b.bcode IS DISTINCT FROM t.old_bcode;
SELECT pg_size_pretty(pg_total_relation_size('address_bcode_bak_20260810')) AS "백업 크기";
SELECT pg_size_pretty(pg_total_relation_size('address'))                    AS "address 크기 (S5 전)";

DO $gate$
DECLARE n_bak bigint; n_src bigint; n_miss bigint; n_diff bigint;
BEGIN
  SELECT count(*) INTO n_bak  FROM address_bcode_bak_20260810;
  SELECT count(*) INTO n_src  FROM address WHERE kind='addr';
  SELECT count(*) INTO n_miss FROM _ri_backfill_todo t
   WHERE NOT EXISTS (SELECT 1 FROM address_bcode_bak_20260810 b WHERE b.id = t.id);
  SELECT count(*) INTO n_diff FROM _ri_backfill_todo t
    JOIN address_bcode_bak_20260810 b ON b.id = t.id
   WHERE b.bcode IS DISTINCT FROM t.old_bcode;
  IF n_bak <> n_src THEN
    RAISE EXCEPTION '백업 % 행 ≠ 원본 % 행. S5 로 진행하지 말 것.', n_bak, n_src;
  END IF;
  IF n_miss > 0 OR n_diff > 0 THEN
    RAISE EXCEPTION '백업 정합성 실패 — 누락 %, 값불일치 %. S5 로 진행하지 말 것.', n_miss, n_diff;
  END IF;
  RAISE NOTICE '백업 검증 통과 — % 행, 누락 0, 값불일치 0', n_bak;
END $gate$;
