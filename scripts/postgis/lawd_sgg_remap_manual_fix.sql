-- ============================================================================
-- T026 S4 — lawd_sgg_remap 결손 1행 수기 보정
--            2811010100 (인천 중구 중앙동1가) → 2812510800 (제물포구 중앙동1가)
--
--   [근거: VWorld dsId=30505 OLD_LAWDCD 의 결손을 보완하는 유일한 예외]
--   S3(build_incheon_remap_from_old_lawdcd.sql) 는 OLD_LAWDCD 축만으로 79쌍을 만든다.
--   그 79쌍에 이 1행이 빠지는 이유는 단 하나 — **신 코드 2812510800 의 행 자체가
--   VWorld 원본(dsId=30505)에 존재하지 않는다.** 옛 행 2811010100 은 존재하지만
--   OLD_LAWDCD 가 비어 있고, 그를 가리키는 신 행이 없어 자기조인이 성립하지 않는다.
--   즉 이것은 도출 규칙의 결함이 아니라 **원본 자료의 결손**이다.
--   (전국 고아 스캔 결과 이 유형의 결함은 정확히 1건뿐이다 — 계획서 §1-4.)
--
--   이 파일은 그 1건에 한해 수기 대응을 넣는다. src 를 'manual:*' 로 구분해
--   자동 도출분('vworld-30505:old_lawdcd')과 언제든 분리·롤백할 수 있게 한다.
--   **이 파일 외의 어떤 수기 대응도 추가하지 말 것.** 늘어나기 시작하면 그것은
--   원본 결손이 아니라 도출 규칙이 틀렸다는 신호이므로 S3 을 고쳐야 한다.
--
-- ── 대응 확정의 근거 3가지 (전부 실측) ──────────────────────────────────────
--   ① lawd_code(code.go.kr 원본, VWorld 와 독립된 계통)에 양쪽 행이 모두 실재하고
--      폐지/현존 상태가 정확히 교대한다:
--        2811010100  인천광역시 중구    중앙동1가   exist = f  (폐지)
--        2812510800  인천광역시 제물포구 중앙동1가  exist = t  (현존)
--      한쪽이 사라지고 다른 쪽이 생긴, 개편의 전형적 짝이다.
--
--   ② 명칭이 인천 코드공간에서 유일하다. lawd_code 에서 '중앙동1가' 를 가진 접두 28
--      행은 위 2건이 전부이므로, 인천 안에서 대응이 1:1 로 유일하게 결정된다.
--      (부산·목포·전주 등 타 시도의 동명이인은 코드 접두가 달라 개입하지 않는다.
--       이 판단은 명칭을 조인 키로 쓰는 것이 아니라, 이미 코드로 좁혀진 2건에 대한
--       사후 확인이다 — P1 비위반.)
--
--   ③ 코드 등차열의 유일한 결번이다. 옛 중구 → 제물포구 대응 43쌍은 **예외 없이
--      전부 꼬리 오프셋 +7**(28110102→28125109 … 28110144→28125151)이고, 옛 꼬리는
--      102~144 로 빈틈없이 연속한다. 그 수열에서 101 하나만 비어 있다.
--        28110101 + 7 = 28125108  ← 이 파일이 넣는 값
--      또한 28125108 은 대응표의 어떤 new_emd8 로도 쓰이지 않았다(충돌 0).
--
--   영향 규모(실측): address 43행 · parcel 50행 · building 37행 = 130행.
--   보정 없이도 시스템은 정상 동작한다(그 130행만 옛 표기로 남는다). 그래서 S3 과
--   분리된 별도 커밋이며, 되돌릴 때도 이 파일의 DELETE 한 줄이면 된다.
--
--   실행:  psql -v ON_ERROR_STOP=1 -f scripts/postgis/lawd_sgg_remap_manual_fix.sql
--   롤백:  DELETE FROM lawd_sgg_remap WHERE src LIKE 'manual:%';
--
--   ⚠ S3 을 재실행하면 lawd_sgg_remap 이 DROP 되어 이 보정도 함께 사라진다.
--     S3 뒤에는 반드시 이 파일을 다시 적용할 것(load-all.sh 는 둘을 연속 실행한다).
--   멱등: 이미 보정돼 있으면 아무것도 하지 않는다.
-- ============================================================================

\timing on
\pset pager off
\set ON_ERROR_STOP on

BEGIN;

DO $fix$
DECLARE
  v_old   constant char(8) := '28110101';
  v_new   constant char(8) := '28125108';
  v_n     bigint := 0;
  v_exist int;
  v_conflict int;
BEGIN
  IF to_regclass('public.lawd_sgg_remap') IS NULL THEN
    RAISE EXCEPTION 'lawd_sgg_remap 없음 — S3(build_incheon_remap_from_old_lawdcd.sql)을 먼저 실행하십시오.';
  END IF;

  -- 멱등: 이미 있으면 조용히 넘어가되, 무엇이 있는지는 알린다.
  SELECT count(*) INTO v_exist FROM lawd_sgg_remap WHERE btrim(old_emd8) = btrim(v_old);
  IF v_exist > 0 THEN
    RAISE NOTICE '% 는 이미 대응표에 있다 — 보정 생략. (원본이 갱신되어 S3 이 자동 도출했을 수 있다: src 컬럼으로 확인할 것)', v_old;
    RETURN;
  END IF;

  -- 충돌 가드: 신 코드가 이미 다른 옛 코드에 배정돼 있으면 근거 ③ 이 무너진 것이다.
  SELECT count(*) INTO v_conflict FROM lawd_sgg_remap WHERE btrim(new_emd8) = btrim(v_new);
  IF v_conflict > 0 THEN
    RAISE EXCEPTION '% 가 이미 다른 대응의 신 코드로 쓰이고 있다 — 수기 보정의 근거 ③(등차열 결번)이 성립하지 않는다. 원본을 재확인하십시오.', v_new;
  END IF;

  -- n_rows 는 리뷰용 라벨이다. address 가 없으면 0 으로 두고 진행한다.
  IF to_regclass('public.address') IS NOT NULL THEN
    EXECUTE 'SELECT count(*)::bigint FROM address WHERE left(btrim(bcode),8) = $1'
       INTO v_n USING btrim(v_old);
  ELSE
    RAISE WARNING 'address 없음 — n_rows 를 0 으로 둔다(치환 동작에는 무영향).';
  END IF;

  INSERT INTO lawd_sgg_remap
         (old_emd8, new_emd8, old_sgg_nm, new_sgg_nm, old_emd_nm, new_emd_nm, n_rows, src)
  VALUES (v_old, v_new, '중구', '제물포구', '중앙동1가', '중앙동1가', v_n,
          'manual:vworld-30505-missing-row');

  RAISE NOTICE '보정 1행 삽입: % → % (address %행)', v_old, v_new, v_n;
END
$fix$;

ANALYZE lawd_sgg_remap;

COMMIT;

-- ============================================================================
-- 게이트 검증 — S3 의 79 가 80 이 되고, 미커버가 0 이 되어야 한다
-- ============================================================================
\echo ''
\echo '── G4. 보정 후 게이트 ───────────────────────────────────────────────'
SELECT 'G4-a 대응쌍 (기대 80)' AS gate, count(*)::text AS actual,
       CASE WHEN count(*) = 80 THEN 'PASS' ELSE 'FAIL' END AS verdict
  FROM lawd_sgg_remap
UNION ALL
SELECT 'G4-b 수기 보정행 (기대 1)', count(*)::text,
       CASE WHEN count(*) = 1 THEN 'PASS' ELSE 'FAIL' END
  FROM lawd_sgg_remap WHERE src LIKE 'manual:%'
UNION ALL
SELECT 'G4-c 자동 도출행 (기대 79)', count(*)::text,
       CASE WHEN count(*) = 79 THEN 'PASS' ELSE 'CHECK' END
  FROM lawd_sgg_remap WHERE src = 'vworld-30505:old_lawdcd'
UNION ALL
SELECT 'G4-d 다중매핑 old_emd8 (기대 0)', count(*)::text,
       CASE WHEN count(*) = 0 THEN 'PASS' ELSE 'FAIL' END
  FROM (SELECT old_emd8 FROM lawd_sgg_remap GROUP BY 1 HAVING count(*) > 1) t
UNION ALL
SELECT 'G4-e 중복 new_emd8 (기대 0)', count(*)::text,
       CASE WHEN count(*) = 0 THEN 'PASS' ELSE 'FAIL' END
  FROM (SELECT new_emd8 FROM lawd_sgg_remap GROUP BY 1 HAVING count(*) > 1) t
UNION ALL
SELECT 'G4-f lawd_dong 미커버 (기대 0)', count(*)::text,
       CASE WHEN count(*) = 0 THEN 'PASS' ELSE 'FAIL' END
  FROM lawd_dong d
 WHERE left(btrim(d.emd_cd),5) IN (SELECT DISTINCT left(old_emd8,5) FROM lawd_sgg_remap)
   AND NOT EXISTS (SELECT 1 FROM lawd_sgg_remap m WHERE btrim(m.old_emd8) = btrim(d.emd_cd))
UNION ALL
SELECT 'G4-g admin_boundary emd 미커버 (기대 0)', count(*)::text,
       CASE WHEN count(*) = 0 THEN 'PASS' ELSE 'FAIL' END
  FROM admin_boundary b
 WHERE b.level = 'emd' AND length(b.code) = 8
   AND left(b.code,5) IN (SELECT DISTINCT left(old_emd8,5) FROM lawd_sgg_remap)
   AND NOT EXISTS (SELECT 1 FROM lawd_sgg_remap m WHERE btrim(m.old_emd8) = b.code);

\echo ''
\echo '── 수기 보정행 ─────────────────────────────────────────────────────'
SELECT old_emd8, new_emd8, old_sgg_nm || ' ' || old_emd_nm AS old_name,
       new_sgg_nm || ' ' || new_emd_nm AS new_name, n_rows, src
  FROM lawd_sgg_remap WHERE src LIKE 'manual:%' ORDER BY old_emd8;
