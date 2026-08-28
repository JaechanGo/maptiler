-- [F1 처분 2026-08-28 · T045] 이 스크립트는 폐기(retired)됐다 — 실행하지 마라.
--   T043 이 09-gen-geocode.py 의 조인 경로를 원천 설계 PK6 으로 교체해, 202607+ 전체
--   재빌드(T045) 산출물의 ri·bcode 는 원천에서 직접 실린다. 이 백필 파이프라인의 전제
--   — address.bcode 전 행 right(bcode,2)='00' — 는 변경 B 이후 거짓이며(3개 시도 A/B
--   실측: addr 968,745행 중 64.04%가 '00' 아님), 재적재(T049) 이후에는 백필 대상 자체가
--   존재하지 않는다. s3_todo 의 하드 게이트(6,743,655±34,000)는 재산정하지 않고 폐기한다
--   (G0 해제 조건 2). 역사적 참조용으로만 보존한다. 실행 차단: s3·s5 에 가드 있음.
-- [T043] 주의: 변경 B 이후 address.bcode 의 끝 2자리는 '00' 이 아닐 수 있다.
--        이 술어/사전검사는 T018 시절 전제(전 행 '00')에 기대고 있으므로
--        아래 진단 메시지는 실제 원인과 다를 수 있다. T018 처분 태스크(F1)에서 정리한다.
--        근거: scripts/09-gen-geocode.py 가 bcode 를 지번측 법정동코드(리 2자리 포함)로
--        싣도록 바뀌었다. 3개 시도 A/B 실측에서 addr 968,745행 중 620,346행(64.04%)의
--        끝 2자리가 '00' 이 아니다. 앞 8자리는 620,346행 전부 불변이다.
-- ri_backfill_s3_todo.sql — T018 Phase 1 / S3.
--   address.bcode 끝 2자리('00')를 리 코드로 채울 **대상 목록**을 만든다.
--   이 단계까지 address 는 **한 행도 바뀌지 않는다.**
--
--   `_ri_backfill_todo` 는 작업 목록이자 **롤백 원본**이다(old_bcode). UNLOGGED 금지 —
--   크래시 시 UNLOGGED 테이블은 truncate 되어 롤백 원본이 통째로 사라진다.
--
-- 선행: S1(load_lawd_code.py) · S2(build_ri_dict_from_lawd_code.sql) 통과.
-- 실행: psql -v ON_ERROR_STOP=1 -f scripts/postgis/ri_backfill_s3_todo.sql
--       (인라인 -c 금지 — dollar-quote 가 셸에서 깨진다)
\set ON_ERROR_STOP on
\timing on

-- [F1] 실행 차단 가드 — 이 파일은 폐기됐다. 아래 예외가 실행을 강제로 막는다.
DO $f1$ BEGIN
  RAISE EXCEPTION 'F1 처분(2026-08-28·T045): ri 백필 파이프라인은 폐기됐다 — PK6 재빌드가 ri/bcode 를 원천에서 직접 싣는다. 이 스크립트를 실행하지 마라.';
END $f1$;

-- ── 0. 재실행 가드 ──────────────────────────────────────────────────────────
--   S5 를 부분 실행한 뒤 S3 를 재실행하면 세 가지가 **조용히** 일어난다:
--     (a) done 플래그 소실,
--     (b) 이미 갱신된 행은 right(bcode,2)='00' 에 걸리지 않아 todo 에서 빠지며 old_bcode 영구 소실,
--     (c) 살아남은 _ri_backfill_progress.last_id 와 새 todo 가 어긋나 재개가 앞 구간을 건너뜀.
--
--   ※ to_regclass + EXECUTE 를 쓰는 이유: PL/pgSQL 의 IF 표현식은 좌변이 거짓이어도
--     전체가 하나의 SQL 로 **파스**되므로, 아직 없는 _ri_backfill_progress 를 정적 참조하면
--     최초 실행에서 파스 단계에 실패한다(T018 리뷰 2회차 Blocking 1, 실측 재현).
DO $guard$
DECLARE n bigint := 0;
BEGIN
  IF to_regclass('public._ri_backfill_progress') IS NOT NULL THEN
    EXECUTE $q$SELECT count(*) FROM _ri_backfill_progress WHERE k='last_id'$q$ INTO n;
    IF n > 0 THEN
      RAISE EXCEPTION 'S3 재실행 금지: 백필이 이미 진행됐다. 롤백을 완료하고 _ri_backfill_progress 를 비운 뒤 재시도하라.';
    END IF;
  END IF;
END $guard$;

-- 선행조건: 신 사전이 자리에 있어야 한다(구 사전 16,113 으로 돌면 대상이 통째로 달라진다).
DO $pre$
DECLARE n bigint;
BEGIN
  IF to_regclass('public.lawd_ri') IS NULL THEN
    RAISE EXCEPTION 'lawd_ri 가 없다. S2(build_ri_dict_from_lawd_code.sql)를 먼저 실행하라.';
  END IF;
  SELECT count(*) INTO n FROM lawd_ri;
  IF n <> 17896 THEN
    RAISE EXCEPTION 'lawd_ri 행수 % — 기대 17,896(T018 신 사전). 구 사전이거나 재구축이 필요하다.', n;
  END IF;
END $pre$;

-- ── 1. 목록 테이블 ──────────────────────────────────────────────────────────
DROP TABLE IF EXISTS _ri_backfill_todo;
CREATE TABLE _ri_backfill_todo (
  id        bigint   PRIMARY KEY,
  old_bcode char(10) NOT NULL,     -- 롤백용 원본값
  new_bcode char(10) NOT NULL,
  src       text     NOT NULL,     -- 'ri' | 'jibun'
  done      boolean  NOT NULL DEFAULT false
);
COMMENT ON TABLE _ri_backfill_todo IS
  'T018 S3: address.bcode 리 자리 백필 대상. old_bcode 가 롤백 원본이다. S5 완료·검증 전 DROP 금지.';

-- (1) address.ri 로 매칭 (우선)
INSERT INTO _ri_backfill_todo (id, old_bcode, new_bcode, src)
SELECT a.id, a.bcode, left(a.bcode,8)||l.ri_cd, 'ri'
  FROM address a
  JOIN lawd_ri l ON l.emd_cd = left(a.bcode,8) AND l.ri = a.ri
 WHERE a.kind='addr'
   AND a.ri IS NOT NULL AND a.ri <> ''
   AND a.bcode IS NOT NULL AND length(btrim(a.bcode)) = 10
   AND right(a.bcode,2) = '00';

-- (2) 의 NOT EXISTS anti-join 을 위해 통계를 먼저 잡는다.
ANALYZE _ri_backfill_todo;

-- (2) 지번 리 토큰으로 폴백 (1에서 못 잡은 것만)
INSERT INTO _ri_backfill_todo (id, old_bcode, new_bcode, src)
SELECT a.id, a.bcode, left(a.bcode,8)||l.ri_cd, 'jibun'
  FROM address a
  CROSS JOIN LATERAL (
     SELECT t FROM unnest(string_to_array(btrim(a.jibun),' ')) AS t
      WHERE t LIKE '%리' AND char_length(t) >= 2 LIMIT 1
  ) j
  JOIN lawd_ri l ON l.emd_cd = left(a.bcode,8) AND l.ri = j.t
 WHERE a.kind='addr'
   AND a.ri IS NOT NULL AND a.ri <> ''
   AND a.bcode IS NOT NULL AND length(btrim(a.bcode)) = 10
   AND right(a.bcode,2) = '00'
   AND NOT EXISTS (SELECT 1 FROM _ri_backfill_todo t2 WHERE t2.id = a.id);

ANALYZE _ri_backfill_todo;

-- ── 2. 검증 (이 단계가 진행 게이트다) ────────────────────────────────────────
\echo '=== S3 검증 ==='
SELECT count(*)                                          AS "총건수 (기대 6,743,655 ± 34,000)" FROM _ri_backfill_todo;
SELECT src, count(*)                                     AS rows FROM _ri_backfill_todo GROUP BY 1 ORDER BY 2 DESC;
SELECT min(id) AS min_id, max(id) AS max_id              FROM _ri_backfill_todo;
SELECT count(*) AS "형식위반 (기대 0)"                     FROM _ri_backfill_todo WHERE new_bcode !~ '^\d{10}$';
SELECT count(*) AS "리자리 00 잔존 (기대 0)"               FROM _ri_backfill_todo WHERE right(new_bcode,2)='00';
SELECT count(*) AS "읍면동8 변조 (기대 0)"                 FROM _ri_backfill_todo WHERE left(new_bcode,8) <> left(old_bcode,8);
SELECT count(*) AS "old=new (무변경, 기대 0)"              FROM _ri_backfill_todo WHERE old_bcode = new_bcode;

-- 게이트: 총건수가 6,743,655 ± 34,000 을 벗어나면 **진행하지 말 것.**
-- 지번 토큰 추출식 차이를 먼저 규명하고, 계획 수치와 §B-3 실패 기대값(148,818)을
-- SQL 실측값으로 갱신한 뒤 재개한다.
DO $gate$
DECLARE n bigint;
BEGIN
  SELECT count(*) INTO n FROM _ri_backfill_todo;
  IF n < 6743655 - 34000 OR n > 6743655 + 34000 THEN
    -- 앞의 INSERT 는 이미 커밋돼 있으므로 이 EXCEPTION 은 _ri_backfill_todo 를 지우지 않는다.
    -- 원인 규명 후에는 S3 를 다시 돌릴 필요 없이 목록을 그대로 쓰면 된다.
    RAISE EXCEPTION '게이트 이탈: 총건수 % (기대 6,743,655 ± 34,000). S4 로 진행하지 말고 지번 토큰 추출식 차이를 먼저 규명하라. (_ri_backfill_todo 는 생성돼 있다 — 재실행 불필요)', n;
  ELSE
    RAISE NOTICE '게이트 통과: 총건수 % (기대 6,743,655 ± 34,000)', n;
  END IF;
END $gate$;
