-- build_ri_dict_from_lawd_code.sql — lawd_ri(리 사전) 재구축, 법정동코드 **원본** 기반 (T018 S2)
--
-- ※ 이 스크립트가 폐기된 build_ri_dict.sql 을 대체한다.
--   구 스크립트는 사전을 address(도로명주소 건물 DB)에서 역산했다. 두 가지가 깨졌다:
--     ① 건물이 없는 리는 통째로 누락 — 원본 15,209개 중 상당수가 사전에 아예 없었다.
--     ② 09-gen-geocode.py:175 `rd.setdefault(mgt[:10], ri)` 가 같은 읍면의 리 이름을
--        **첫 하나만** 남기고 뭉갰다. mgt[:10] 키 19,058개 중 2,699개가 복수 리를 가져
--        2,698개가 붕괴했고, address.ri 정합률이 97.08% 에 그쳤다.
--   원본을 적재하면 두 결함이 함께 사라진다.
--
-- 선행조건: scripts/postgis/load_lawd_code.py 로 `lawd_code` 적재 완료 (T018 S1).
--           CP949 → UTF-8 변환이 필요해 적재만은 순수 SQL 로 할 수 없다.
--
--   PGPASSWORD=... psql -v ON_ERROR_STOP=1 -f scripts/postgis/build_ri_dict_from_lawd_code.sql
--
-- 설계 판단 — **사전의 코드 체계는 DB 의 코드 체계와 일치해야 한다.**
--   전남·광주는 법정동코드가 46/29 → 12 로 개편됐으나 우리 DB 는 아직 46/29 다
--   (parcel·building 의 파티션 키, lawd_dong.emd_cd). 사전을 '존재' 행만으로 만들면
--   전남 리가 전부 12 로 바뀌어 **지금 동작하는 전남 리 지오코딩까지 깨진다.**
--   그래서 존재 우선 + **46/29 한정** 폐지 폴백으로 만든다. 폐지 폴백분은 exist=false 로
--   표시해 두며, Phase 3 에서 전면 remap(안 B)을 택할 때에만 제거할 수 있다.
--
--   전국 폐지분을 다 들이면 목적 외 코드 1,043개가 유입되는데 커버리지 이득은 2행뿐이라
--   (0.000029%) 실익이 없다 [측정].
--
-- 기대 산출 (2026-08-10 원본 53,387행 기준 [측정]):
--   총 17,896행 = 존재 15,209 + 폐지 폴백 2,687(전부 시도 46)
--   존재 티어의 (emd_cd, ri) 유일성 100%, (emd_cd, ri_cd) 유일성 100%
--   원본이 갱신되면 이 수치가 어긋나고 아래 가드가 스크립트를 중단시킨다. 그게 의도다.

\set ON_ERROR_STOP on

-- ── 0. 선행조건·재실행 가드 ─────────────────────────────────────────────
-- 교체가 이미 끝난 상태에서 재실행하면 백업 단계가 **신 사전을 백업으로 덮어써서**
-- 원본 사전이 영구 소실된다. to_regclass 로 먼저 막는다.
DO $pre$
DECLARE n bigint;
BEGIN
  IF to_regclass('public.lawd_code') IS NULL THEN
    RAISE EXCEPTION 'lawd_code 가 없다. 먼저 scripts/postgis/load_lawd_code.py 를 실행하라 (T018 S1).';
  END IF;
  EXECUTE 'SELECT count(*) FROM lawd_code' INTO n;
  IF n <> 53387 THEN
    RAISE EXCEPTION 'lawd_code 행수 % — 기대 53,387. 원본이 갱신됐다면 아래 가드 기대값도 재산정해야 한다.', n;
  END IF;

  IF to_regclass('public.lawd_ri_old_20260810') IS NOT NULL THEN
    RAISE EXCEPTION '교체가 이미 완료된 상태다(lawd_ri_old_20260810 존재). 재실행하면 백업이 신 사전으로 덮어써진다. 롤백 후 재시도하라.';
  END IF;
  IF to_regclass('public.lawd_ri') IS NULL THEN
    RAISE EXCEPTION 'lawd_ri 가 없다 — 교체 대상이 없다. 상태를 먼저 확인하라.';
  END IF;
END $pre$;

-- ── 1. 백업 ────────────────────────────────────────────────────────────
DO $bak$
BEGIN
  IF to_regclass('public.lawd_ri_bak_20260810') IS NULL THEN
    CREATE TABLE lawd_ri_bak_20260810 AS SELECT * FROM lawd_ri;
    RAISE NOTICE '백업 생성: lawd_ri_bak_20260810';
  ELSE
    RAISE NOTICE '백업이 이미 있다 — 보존하고 건너뛴다: lawd_ri_bak_20260810';
  END IF;
END $bak$;

SELECT count(*) AS "백업 행수 (기대 16,113)" FROM lawd_ri_bak_20260810;

-- ── 2. 신 사전 구축 ─────────────────────────────────────────────────────
DROP TABLE IF EXISTS lawd_ri_new;
CREATE TABLE lawd_ri_new (
  emd_cd char(8) NOT NULL,
  ri     text    NOT NULL,
  ri_cd  char(2) NOT NULL,
  exist  boolean NOT NULL,          -- true=존재코드, false=폐지코드 폴백(전남·광주 한정)
  PRIMARY KEY (emd_cd, ri)
);
COMMENT ON TABLE lawd_ri_new IS '법정동코드 원본 기반 리 사전 — T018. 교체 후 lawd_ri 가 된다.';
COMMENT ON COLUMN lawd_ri_new.exist IS
  'false = 46/29(전남·광주) 폐지코드 폴백. Phase 3 안 B 채택 시에만 제거 가능 — 안 A 에서는 영구 잔존.';
-- ※ 이 스크립트를 재실행하면 위 짧은 문구가 lawd_ri.exist 코멘트를 덮어쓴다.
--   안 A 가 적용된 환경이라면 재실행 후 build_sido_remap.sql 을 다시 돌려
--   '영구 불일치 고지'(DB 46/29 ↔ API 12) 전문을 복구할 것. — T018 §B-4

-- 후보: (읍면동8, 리이름) -> ri_cd. 리 이름은 법정동명의 마지막 어절.
-- 같은 티어(존재/폐지)에서 후보가 **유일할 때만** 채택한다. 모호하면 넣지 않는 fail-closed —
-- 틀린 코드를 넣느니 매칭 실패로 두는 편이 낫다(실패는 _ri_backfill_miss 에 집계된다).
WITH cand AS (
  SELECT left(bcode,8) AS emd_cd,
         split_part(name, ' ', array_length(string_to_array(name,' '),1)) AS ri,
         right(bcode,2) AS ri_cd,
         exist
    FROM lawd_code
   WHERE right(bcode,2) <> '00'
     -- 폐지 폴백은 전남·광주 통합 미반영 보상 **전용**이며, DB 가 실제로 쓰는 읍면동으로 한정한다.
     AND (exist OR (left(bcode,2) IN ('46','29')
                    AND left(bcode,8) IN (SELECT emd_cd FROM lawd_dong)))
), ranked AS (
  SELECT emd_cd, ri, ri_cd, exist,
         count(*)     OVER (PARTITION BY emd_cd, ri, exist)                        AS n_same_tier,
         row_number() OVER (PARTITION BY emd_cd, ri ORDER BY exist DESC, ri_cd)    AS rn
    FROM cand
)
INSERT INTO lawd_ri_new (emd_cd, ri, ri_cd, exist)
SELECT emd_cd, ri, ri_cd, exist FROM ranked
 WHERE rn = 1 AND n_same_tier = 1;

ANALYZE lawd_ri_new;

-- ── 3. 가드 — 하나라도 어긋나면 여기서 중단하고 교체하지 않는다 ────────────
DO $gate$
DECLARE
  n_total bigint; n_fb bigint; n_ex bigint; n_emd bigint;
  n_dupname bigint; n_dupcode bigint; n_orphan bigint; n_yuga bigint;
  n_legacy bigint;
  bad_sido text;
BEGIN
  SELECT count(*) INTO n_total FROM lawd_ri_new;
  SELECT count(*) INTO n_fb    FROM lawd_ri_new WHERE NOT exist;
  SELECT count(*) INTO n_ex    FROM lawd_ri_new WHERE exist;
  SELECT count(DISTINCT emd_cd) INTO n_emd FROM lawd_ri_new WHERE exist;

  -- 폐지 폴백은 DB 가 46/29(전남·광주 구코드) 읍면동을 실제로 쓸 때만 생긴다. 2026-08 통합(전남광주통합특별시=12)
  -- 이후 address/lawd_dong 이 12 코드만 쓰면 폴백 0 이 **정답**이다([실측 2026-09-03] lawd_dong 46/29 = 0행,
  -- 12 = 622행 · lawd_code 12 존재코드 3,204행 → 존재 15,209 만 남음). 기대값을 lawd_dong 상태에 조건부로 건다.
  SELECT count(*) INTO n_legacy FROM lawd_dong WHERE left(emd_cd,2) IN ('46','29');
  RAISE NOTICE '총량 % (기대 %) / 폴백 % (기대 %) / 존재 % (기대 15,209) / 존재 읍면 % (기대 1,411) · lawd_dong 46/29=%',
               n_total, CASE WHEN n_legacy > 0 THEN '17,896' ELSE '15,209(폴백 없음)' END,
               n_fb,    CASE WHEN n_legacy > 0 THEN '2,687'  ELSE '0' END, n_ex, n_emd, n_legacy;

  IF n_legacy > 0 AND abs(n_total - 17896) > 50 THEN
    RAISE EXCEPTION '총량 % 이 기대 17,896 ± 50 을 벗어났다. 원본 갱신 또는 CTE 오작성 — S1 검증부터 재확인하라.', n_total;
  END IF;
  IF n_legacy = 0 AND abs(n_ex - 15209) > 50 THEN
    RAISE EXCEPTION '존재 티어 % 이 기대 15,209 ± 50 을 벗어났다(폴백 없는 통합 코드 상태). 원본 갱신 또는 CTE 오작성.', n_ex;
  END IF;

  -- 폴백이 46 밖으로 새면 목적 외 코드가 사전에 들어간다. 초판이 실제로 낸 사고 형태다.
  SELECT string_agg(DISTINCT left(emd_cd,2), ',') INTO bad_sido
    FROM lawd_ri_new WHERE NOT exist AND left(emd_cd,2) <> '46';
  IF bad_sido IS NOT NULL THEN
    RAISE EXCEPTION '폐지 폴백에 시도 46 외가 섞였다: %. 폴백 필터가 새는 중이다 — 즉시 중단.', bad_sido;
  END IF;
  IF n_legacy > 0 AND n_fb < 2600 THEN
    RAISE EXCEPTION '폴백 % 행 — 2,600 미만이다. lawd_dong 의 46/29 분(기대 622행)을 먼저 확인하라.', n_fb;
  END IF;
  IF n_legacy = 0 AND n_fb <> 0 THEN
    RAISE EXCEPTION '폴백 % 행 — lawd_dong 이 46/29 를 안 쓰는데 폐지 폴백이 생겼다. 폴백 필터가 새는 중이다.', n_fb;
  END IF;

  -- 존재 티어 유일성: 한 이름에 복수 코드 / 한 코드에 복수 이름 둘 다 0 이어야 한다.
  SELECT count(*) INTO n_dupname FROM (
    SELECT emd_cd, ri FROM lawd_ri_new WHERE exist GROUP BY 1,2 HAVING count(*) > 1) x;
  SELECT count(*) INTO n_dupcode FROM (
    SELECT emd_cd, ri_cd FROM lawd_ri_new WHERE exist GROUP BY 1,2 HAVING count(*) > 1) x;
  IF n_dupname > 0 OR n_dupcode > 0 THEN
    RAISE EXCEPTION '존재 티어 유일성 붕괴 — 복수코드 %, 복수이름 %. 원본 무결성을 확인하라.', n_dupname, n_dupcode;
  END IF;

  -- 조합한 10자리가 원본에 실재하는 존재코드인지(= VWorld level4LC 와 같은 값인지).
  SELECT count(*) INTO n_orphan FROM lawd_ri_new r
   WHERE r.exist AND NOT EXISTS (
     SELECT 1 FROM lawd_code c WHERE c.bcode = r.emd_cd||r.ri_cd AND c.exist);
  IF n_orphan > 0 THEN
    RAISE EXCEPTION '존재 티어 % 행이 원본에 없는 10자리를 만든다. 사전 논리 결함.', n_orphan;
  END IF;

  -- 육안 검증 기준점: 대구 달성군 유가읍(27710259) 13개 리.
  SELECT count(*) INTO n_yuga FROM lawd_ri_new WHERE emd_cd = '27710259';
  IF n_yuga <> 13 THEN
    RAISE EXCEPTION '유가읍(27710259) 리 수 % — 기대 13. 사전 논리 결함.', n_yuga;
  END IF;

  RAISE NOTICE '가드 통과 — 유일성 0/0, 고아 0, 유가읍 13행';
END $gate$;

-- ── 4. 교체 (원자적) ────────────────────────────────────────────────────
-- ALTER TABLE ... RENAME 은 **인덱스 이름을 바꾸지 않는다**(테이블·인덱스가 pg_class 네임스페이스
-- 를 공유). 구 테이블이 lawd_ri_ri_emd_idx 를 계속 점유한 채로 동명 인덱스를 만들면
-- `already exists` 로 확정 실패한다. 그래서 인덱스 이름을 먼저 비운다.
BEGIN;
  ALTER INDEX lawd_ri_pkey       RENAME TO lawd_ri_old_20260810_pkey;
  ALTER INDEX lawd_ri_ri_emd_idx RENAME TO lawd_ri_old_20260810_ri_emd_idx;

  ALTER TABLE lawd_ri            RENAME TO lawd_ri_old_20260810;
  ALTER TABLE lawd_ri_new        RENAME TO lawd_ri;

  ALTER INDEX lawd_ri_new_pkey   RENAME TO lawd_ri_pkey;

  -- geocode-api-pg.py:468 이 `WHERE ri = %s AND emd_cd = ANY(...)` 로 타는 경로.
  CREATE INDEX lawd_ri_ri_emd_idx ON lawd_ri (ri, emd_cd);
  CREATE INDEX lawd_ri_emd_cd_idx ON lawd_ri (emd_cd, ri_cd);
COMMIT;

ANALYZE lawd_ri;

-- ── 5. 교체 후 확인 ─────────────────────────────────────────────────────
SELECT count(*) AS "신 lawd_ri 행수 (기대 17,896 · 통합코드 상태면 15,209)" FROM lawd_ri;
SELECT ri_cd, ri FROM lawd_ri WHERE emd_cd = '27710259' ORDER BY ri_cd;
--   기대: 21음리 22양리 23용리 24봉리 25쌍계리 26초곡리 27상리
--         28금리 29유곡리 30도의리 31가태리 32한정리 33본말리  (13행)

-- ── 롤백 ────────────────────────────────────────────────────────────────
-- BEGIN;
--   DROP INDEX IF EXISTS lawd_ri_ri_emd_idx;
--   DROP INDEX IF EXISTS lawd_ri_emd_cd_idx;
--   ALTER INDEX lawd_ri_pkey RENAME TO lawd_ri_new_pkey;
--   ALTER TABLE lawd_ri      RENAME TO lawd_ri_new;
--   ALTER TABLE lawd_ri_old_20260810 RENAME TO lawd_ri;
--   ALTER INDEX lawd_ri_old_20260810_pkey       RENAME TO lawd_ri_pkey;
--   ALTER INDEX lawd_ri_old_20260810_ri_emd_idx RENAME TO lawd_ri_ri_emd_idx;
-- COMMIT;
-- 최후 수단은 lawd_ri_bak_20260810 에서 재적재.
