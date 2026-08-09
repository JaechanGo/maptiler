-- ri_backfill_rollback.sql — T018 Phase 1 / S5 롤백.
--   address.bcode 를 백필 이전 값으로 되돌린다.
--
--   실행: psql -v ON_ERROR_STOP=1 -f scripts/postgis/ri_backfill_rollback.sql
--   소요 [추정] 20–60분 (정방향과 같은 규모의 UPDATE 다).
--
--   ※ 사전(lawd_ri) 롤백은 별개다 — docs/ri-dict-runbook.md §P-R-2 를 따른다.
--     리 사전만 되돌리고 address 를 그대로 두면 bcode 의 리 코드가 사전에 없는 상태가 된다.
--     **address 롤백을 먼저** 하고 사전 롤백을 나중에 한다.
\set ON_ERROR_STOP on
\timing on

DO $pre$
DECLARE n_todo bigint; n_done bigint;
BEGIN
  IF to_regclass('public._ri_backfill_todo') IS NULL THEN
    RAISE EXCEPTION '_ri_backfill_todo 가 없다. 롤백 원본이 사라졌다 — address_bcode_bak_20260810 기반 롤백(아래 대안)을 쓰라.';
  END IF;
  SELECT count(*), count(*) FILTER (WHERE done) INTO n_todo, n_done FROM _ri_backfill_todo;
  RAISE NOTICE '롤백 대상: done % / 전체 % 행', n_done, n_todo;
END $pre$;

-- 정방향 롤백 — done 인 행만 old_bcode 로 되돌린다.
BEGIN;
  UPDATE address a SET bcode = t.old_bcode
    FROM _ri_backfill_todo t
   WHERE a.id = t.id AND t.done;
COMMIT;

VACUUM (ANALYZE) address;

-- **S3 재실행 가드 해제.** 이 행이 남아 있으면 S3 를 다시 돌릴 수 없다.
DELETE FROM _ri_backfill_progress WHERE k='last_id';
UPDATE _ri_backfill_todo SET done = false WHERE done;

\echo '=== 롤백 검증 ==='
SELECT count(*) AS "리자리 채워진 행 (기대 0)"
  FROM address WHERE kind='addr' AND right(bcode,2) <> '00';
SELECT count(*) AS "백업과 불일치 (기대 0)"
  FROM address a JOIN address_bcode_bak_20260810 b ON b.id = a.id
 WHERE a.bcode IS DISTINCT FROM b.bcode;
SELECT count(*) AS "_ri_backfill_progress.last_id 잔존 (기대 0)"
  FROM _ri_backfill_progress WHERE k='last_id';

-- ── 대안: todo 가 손상됐을 때 — 전량 백업 기반 롤백 ─────────────────────────
--   위 정방향이 불가능한 경우에만 쓴다. kind='addr' 전 행을 스냅샷 값으로 덮는다.
--   (변경되지 않은 행까지 UPDATE 하므로 더 무겁고 데드 튜플도 훨씬 많이 만든다.)
--
--   BEGIN;
--     UPDATE address a SET bcode = b.bcode
--       FROM address_bcode_bak_20260810 b
--      WHERE a.id = b.id AND a.bcode IS DISTINCT FROM b.bcode;
--   COMMIT;
--   VACUUM (ANALYZE) address;
--   DELETE FROM _ri_backfill_progress WHERE k='last_id';
