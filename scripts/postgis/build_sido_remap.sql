-- ============================================================================
-- T018 Phase 3 / S7 — 전남·광주 통합(46·29 → 12) 매핑표·리 예외목록 생성
--
--   실행:  unset PGOPTIONS
--          docker exec -i server-postgis-1 psql -U cuvia -d cuvia -v ON_ERROR_STOP=1 \
--            < scripts/postgis/build_sido_remap.sql
--
--   선행조건: Phase 1 완료(lawd_code 적재, lawd_ri 재구축, address.bcode 리 자리 백필).
--   성격    : **읽기 전용 산출.** 기존 테이블을 일절 변경하지 않는다.
--             생성물은 lawd_sido_remap · lawd_ri_remap_exception 두 개뿐이다.
--   재실행  : 멱등. 두 테이블을 DROP 후 재생성한다.
--
-- ── 매핑 규칙 (Master 결정 4 준수) ──────────────────────────────────────────
--   **명칭 기반(가설b)으로만 생성한다. "시도 2자리만 치환"(가설a)은 채택 금지.**
--   근거: 양쪽이 모두 성립하는 402건 중 결론 일치는 244건(60.70%)뿐 — 약 39%가 오답이다.
--
--   읍면동 8자리: lawd_code.name 에서 첫 토큰(시도명)을 제거한 나머지(시군구 이하)가
--                 같은 12 체계 존재(exist) 읍면동을 찾는다.
--                   구  4617025000 "전라남도 나주시 남평읍"          → tail "나주시 남평읍"
--                   신  1275025000 "전남광주통합특별시 나주시 남평읍" → tail "나주시 남평읍"
--   리   2자리: 원칙 보존. 8자리 remap 후 (new_emd8, 리이름) 으로 12 체계 리 코드를 찾아
--               구 리 코드와 다른 것만 lawd_ri_remap_exception 에 담는다.
--
-- ── 안 A 사용법 (server/geocode-api-pg.py) ──────────────────────────────────
--   b_code 변환:  coalesce(예외.new_bcode, 매핑.new_emd8 || right(구bcode,2))
--   ※ 이 표는 **응답 경계 치환 전용**이다. 안 A 에서 DB 내부값은 46/29 로 남는다.
--     DB 를 직접 조회하면 API 응답과 다른 코드가 보인다 — 이는 결함이 아니라 안 A 의 정의다.
--     근본 해소는 안 B(전면 remap, 별도 태스크)에서만 이루어진다.
-- ============================================================================

\timing on
\pset pager off
\set ON_ERROR_STOP on

BEGIN;

-- 선행조건 가드 — lawd_code 없이 실행하면 잘못된(빈) 매핑표가 만들어진다.
DO $$
BEGIN
  IF to_regclass('public.lawd_code') IS NULL THEN
    RAISE EXCEPTION 'lawd_code 없음 — Phase 1 S1(원본 적재)을 먼저 수행하십시오.';
  END IF;
  IF to_regclass('public.lawd_dong') IS NULL THEN
    RAISE EXCEPTION 'lawd_dong 없음.';
  END IF;
END $$;

DROP TABLE IF EXISTS lawd_ri_remap_exception;
DROP TABLE IF EXISTS lawd_sido_remap;

CREATE TABLE lawd_sido_remap (
  old_emd8 char(8) PRIMARY KEY,
  new_emd8 char(8) NOT NULL,
  old_name text   NOT NULL,
  new_name text   NOT NULL,
  n_rows   bigint NOT NULL,          -- Phase 1 이후 address 행수(해당 읍면동 8자리)
  src      text   NOT NULL           -- 'lawd_dong' | 'admin_boundary' — 코드 출처
);
COMMENT ON TABLE lawd_sido_remap IS
  'T018 S7. 46/29(전남·광주) 읍면동 8자리 → 12(전남광주통합특별시) 명칭 기반 매핑. '
  '안 A 응답 경계 치환 전용 — DB 내부값은 46/29 로 남는다(안 B 에서만 해소).';
COMMENT ON COLUMN lawd_sido_remap.src IS
  'lawd_dong: address 실사용 코드(게이트 622행). '
  'admin_boundary: address 에는 없으나 역지오 areas[] 로 노출되는 코드 — 치환 누락 방지용 보강.';

CREATE TABLE lawd_ri_remap_exception (
  old_bcode char(10) PRIMARY KEY,   -- 46/29 체계 10자리
  new_bcode char(10) NOT NULL,      -- 12 체계 10자리
  ri        text     NOT NULL,
  note      text
);
COMMENT ON TABLE lawd_ri_remap_exception IS
  '읍면동 8자리를 remap 해도 리 2자리가 보존되지 않는 예외. 기대 4건 [측정·리뷰재현].';

-- ── 영구 불일치 고지 (T018 §B-4 / Master 조건 5) ─────────────────────────────
--   안 A 를 적용하면 **DB 는 46/29, API 응답은 12** 라는 상태가 무기한 유지된다.
--   이 사실을 코드 주석(geocode-api-pg.py 'T018 안 A' 블록)·런북(docs/ri-dict-runbook.md 부록 B)과
--   함께 DB 자체에도 남긴다 — 세 곳 중 하나만 보고 작업하는 사람이 반드시 생기기 때문이다.
--   lawd_ri.exist=false 행은 그 불일치의 직접적 잔재다(폐지된 46/29 리 코드를 사전에 남겨 둔 것).
DO $$
BEGIN
  IF to_regclass('public.lawd_ri') IS NOT NULL THEN
    EXECUTE $c$
      COMMENT ON COLUMN lawd_ri.exist IS
        'false = 46/29(전남·광주) 폐지 리 코드 폴백.
         ★ 영구 불일치 고지(T018 안 A): 이 DB 의 법정동코드는 46/29 체계로 남아 있고,
           geocode API 응답만 경계에서 12(전남광주통합특별시) 로 치환된다.
           따라서 (1) 응답의 b_code 로 이 DB 를 조회하면 0건이고,
                 (2) WHERE 절·조인에 12 를 넣어서는 안 되며,
                 (3) exist=false 행은 안 A 가 유지되는 한 제거할 수 없다.
           안 A 는 최종 상태가 아니다 — 안 B(parcel/building 파티션 포함 전면 remap)가
           채택되면 DB 가 12 로 이관되고 이 컬럼과 lawd_sido_remap 은 모두 불필요해진다.'
    $c$;
  END IF;
END $$;

-- ── (1) 구 코드 세계 ────────────────────────────────────────────────────────
--   lawd_dong  : address 가 실제로 쓰는 46/29 읍면동 (게이트 622행)
--   admin_boundary: 역지오 areas[].code 로 그대로 노출되는 읍면동
--                   (level='emd' AND length(code)=8 조건 필수 — 아래 주의 참조)
--   ※ 주의: admin_boundary 에서 bare left(code,2) 로 거르면 안 된다.
--     level='adm_dong' 의 '29xxxxxx' 24행은 **광주가 아니라 세종**(통계청 행정동코드 체계)이며
--     치환하면 세종 응답이 파괴된다. len<>8 인 기형 2행('46-1 ??','46-2 ??')도 함께 배제된다.
CREATE TEMP TABLE _s7_old ON COMMIT DROP AS
SELECT e.old_emd8, e.src, c.name AS old_name,
       regexp_replace(c.name, '^[^ ]+ ', '') AS tail
  FROM (
        SELECT emd_cd::char(8) AS old_emd8, 'lawd_dong'::text AS src
          FROM lawd_dong WHERE left(emd_cd,2) IN ('46','29')
        UNION
        SELECT code::char(8), 'admin_boundary'
          FROM admin_boundary
         WHERE level = 'emd' AND length(code) = 8 AND left(code,2) IN ('46','29')
           AND code NOT IN (SELECT emd_cd FROM lawd_dong WHERE left(emd_cd,2) IN ('46','29'))
       ) e
  JOIN lawd_code c ON c.bcode = (e.old_emd8 || '00')::char(10);

-- ── (2) 신 코드 세계 (12 체계 존재 읍면동) ──────────────────────────────────
CREATE TEMP TABLE _s7_new ON COMMIT DROP AS
SELECT left(bcode,8)::char(8) AS new_emd8, name AS new_name,
       regexp_replace(name, '^[^ ]+ ', '') AS tail
  FROM lawd_code
 WHERE left(bcode,2) = '12' AND right(bcode,2) = '00' AND exist;

-- ── (3) address 행수 (한 번의 스캔으로 집계) ────────────────────────────────
--   ON COMMIT DROP 을 붙이지 않는다 — COMMIT 이후 게이트 G1-h 가 이 표를 참조한다.
--   세션 종료 시 자동 소멸하며, 스크립트 말미에서 명시적으로 DROP 한다.
CREATE TEMP TABLE _s7_nrows AS
SELECT left(bcode,8)::char(8) AS emd8, count(*)::bigint AS n
  FROM address WHERE left(bcode,2) IN ('46','29') GROUP BY 1;

-- ── (4) 매핑표 적재 ─────────────────────────────────────────────────────────
INSERT INTO lawd_sido_remap (old_emd8, new_emd8, old_name, new_name, n_rows, src)
SELECT o.old_emd8, n.new_emd8, o.old_name, n.new_name,
       coalesce(x.n, 0), o.src
  FROM _s7_old o
  JOIN _s7_new n ON n.tail = o.tail
  LEFT JOIN _s7_nrows x ON x.emd8 = o.old_emd8;

-- ── (5) 리 예외 적재 ────────────────────────────────────────────────────────
INSERT INTO lawd_ri_remap_exception (old_bcode, new_bcode, ri, note)
SELECT o.old_bcode, n.new_bcode, o.ri,
       format('%s → %s (리코드 %s→%s)', m.old_name, m.new_name, o.old_ri_cd, n.new_ri_cd)
  FROM (SELECT bcode AS old_bcode, left(bcode,8)::char(8) AS old_emd8,
               right(bcode,2) AS old_ri_cd, regexp_replace(name, '^.* ', '') AS ri
          FROM lawd_code
         WHERE left(bcode,2) IN ('46','29') AND right(bcode,2) <> '00') o
  JOIN lawd_sido_remap m ON m.old_emd8 = o.old_emd8
  JOIN (SELECT bcode AS new_bcode, left(bcode,8)::char(8) AS new_emd8,
               right(bcode,2) AS new_ri_cd, regexp_replace(name, '^.* ', '') AS ri
          FROM lawd_code
         WHERE left(bcode,2) = '12' AND right(bcode,2) <> '00' AND exist) n
    ON n.new_emd8 = m.new_emd8 AND n.ri = o.ri
 WHERE n.new_ri_cd <> o.old_ri_cd;

ANALYZE lawd_sido_remap;
ANALYZE lawd_ri_remap_exception;

COMMIT;

-- ============================================================================
-- 게이트 검증 — 하나라도 FAIL 이면 S8(치환 적용)로 넘어가지 말 것
-- ============================================================================
\echo ''
\echo '── G1. 매핑표 게이트 ─────────────────────────────────────────────────'
SELECT 'G1-a lawd_dong 유래 행수 (기대 622)' AS gate,
       count(*)::text AS actual,
       CASE WHEN count(*) = 622 THEN 'PASS' ELSE 'FAIL' END AS verdict
  FROM lawd_sido_remap WHERE src = 'lawd_dong'
UNION ALL
SELECT 'G1-b admin_boundary 보강 행수 (기록)', count(*)::text, 'INFO'
  FROM lawd_sido_remap WHERE src = 'admin_boundary'
UNION ALL
SELECT 'G1-c 총 행수 (기록)', count(*)::text, 'INFO' FROM lawd_sido_remap
UNION ALL
SELECT 'G1-d n_rows 합계 (기대 1,323,387)', sum(n_rows)::text,
       CASE WHEN sum(n_rows) = 1323387 THEN 'PASS' ELSE 'FAIL' END
  FROM lawd_sido_remap
UNION ALL
SELECT 'G1-e new_emd8 중복 (기대 0)', count(*)::text,
       CASE WHEN count(*) = 0 THEN 'PASS' ELSE 'FAIL' END
  FROM (SELECT new_emd8 FROM lawd_sido_remap GROUP BY 1 HAVING count(*) > 1) t
UNION ALL
SELECT 'G1-f 매핑 실패 — lawd_dong 미커버 (기대 0)', count(*)::text,
       CASE WHEN count(*) = 0 THEN 'PASS' ELSE 'FAIL' END
  FROM lawd_dong d
 WHERE left(d.emd_cd,2) IN ('46','29')
   AND NOT EXISTS (SELECT 1 FROM lawd_sido_remap m WHERE m.old_emd8 = d.emd_cd)
UNION ALL
SELECT 'G1-g 매핑 실패 — admin_boundary emd8 미커버 (기대 0)', count(*)::text,
       CASE WHEN count(*) = 0 THEN 'PASS' ELSE 'FAIL' END
  FROM admin_boundary b
 WHERE b.level = 'emd' AND length(b.code) = 8 AND left(b.code,2) IN ('46','29')
   AND NOT EXISTS (SELECT 1 FROM lawd_sido_remap m WHERE m.old_emd8 = b.code)
UNION ALL
SELECT 'G1-h 매핑 실패 — address emd8 미커버 (기대 0)', count(*)::text,
       CASE WHEN count(*) = 0 THEN 'PASS' ELSE 'FAIL' END
  FROM _s7_nrows x
 WHERE NOT EXISTS (SELECT 1 FROM lawd_sido_remap m WHERE m.old_emd8 = x.emd8)
UNION ALL
SELECT 'G1-i 신 코드 전부 12 체계 (기대 0 위반)', count(*)::text,
       CASE WHEN count(*) = 0 THEN 'PASS' ELSE 'FAIL' END
  FROM lawd_sido_remap WHERE left(new_emd8,2) <> '12';

\echo ''
\echo '── G2. 리 예외 게이트 ────────────────────────────────────────────────'
SELECT 'G2-a 예외 행수 (기대 4)' AS gate, count(*)::text AS actual,
       CASE WHEN count(*) = 4 THEN 'PASS' ELSE 'FAIL' END AS verdict
  FROM lawd_ri_remap_exception;

SELECT old_bcode, new_bcode, ri, note FROM lawd_ri_remap_exception ORDER BY old_bcode;

\echo ''
\echo '── G3. 세종 오염 방지 확인 (adm_dong 29 = 세종, 치환 대상 아님) ──────'
SELECT 'G3 adm_dong 29xxxxxx 가 매핑표에 없음 (기대 0)' AS gate, count(*)::text AS actual,
       CASE WHEN count(*) = 0 THEN 'PASS' ELSE 'FAIL' END AS verdict
  FROM admin_boundary b
  JOIN lawd_sido_remap m ON m.old_emd8 = b.code
 WHERE b.level = 'adm_dong';

\echo ''
\echo '── G4. 육안 검토용 전수 목록 (622 + 보강) ────────────────────────────'
SELECT old_emd8, new_emd8, old_name, new_name, n_rows, src
  FROM lawd_sido_remap ORDER BY old_emd8;

DROP TABLE IF EXISTS _s7_nrows;
