-- ============================================================================
-- T026 S3 — 인천 자치구 개편(중구·동구·서구 → 제물포구·영종구·서해구·검단구)
--            읍면동 8자리 대응표 lawd_sgg_remap 생성
--
--   [근거: VWorld dsId=30505 OLD_LAWDCD]
--   이 표의 모든 행은 lawd_code_v2.old_bcode(= VWorld 법정동코드 자료의 OLD_LAWDCD
--   컬럼, 행정안전부가 개편 시 부여한 "옛 법정동코드")에서 **코드 대 코드**로 도출한다.
--   명칭 조인은 일절 하지 않는다 — T026 §0 P1(명칭 조인 SQL 신규 작성 금지).
--   old_sgg_nm / old_emd_nm 도 명칭 대조가 아니라 o.bcode = n.old_bcode 자기조인,
--   즉 코드 조인으로 얻는다. 명칭은 결과값이지 키가 아니다.
--
--   실행:  unset PGOPTIONS
--          psql -v ON_ERROR_STOP=1 -f scripts/postgis/build_incheon_remap_from_old_lawdcd.sql
--          (load-all.sh 의 lawd 단계 (f) 에서 자동 실행)
--
--   선행조건: load_lawd_code_v2.py 로 lawd_code_v2 적재 완료(S2).
--   성격    : **읽기 전용 산출.** 기존 테이블을 일절 변경하지 않는다.
--             생성물은 lawd_sgg_remap 하나뿐이다. address / lawd_code / lawd_dong 무변경.
--   재실행  : 멱등. DROP 후 재생성한다.
--             ※ S4(lawd_sgg_remap_manual_fix.sql)의 보정 1행도 함께 사라진다.
--               재실행 시에는 S4 를 반드시 다시 적용할 것(S4 헤더에 동일 경고).
--
-- ── 도출 규칙 (하드코딩 없음) ───────────────────────────────────────────────
--   신설 4구의 5자리 코드(28125/28155/28275/28290)를 상수로 박지 않는다.
--   코드 사이의 관계만으로 대응쌍을 정의한다:
--
--     left(bcode,2)='28' AND left(old_bcode,2)='28'   인천 내부 개편만
--     left(bcode,5) <> left(old_bcode,5)              시군구가 실제로 바뀐 행만
--     right(bcode,2)='00' AND right(old_bcode,2)='00' 읍면동 레벨(리 자리 00)만
--     right(bcode,5) <> '00000'                       시군구·시도 자체 행 제외
--     btrim(del_dt)=''                                신 코드가 현존하는 행만
--
--   실측 결과 79쌍 · 교차표 28110→28125 43 · 28110→28155 8 · 28140→28125 7 ·
--   28260→28275 11 · 28260→28290 10 (계획서 §1-4 와 일치).
--
-- ── 알려진 결손 1건 ─────────────────────────────────────────────────────────
--   2811010100 (인천 중구 중앙동1가) 는 대응돼야 할 2812510800 행 자체가 VWorld
--   원본에 없어 이 규칙으로는 나오지 않는다. 게이트 G-e/G-f/G-g 가 이를 **목록으로
--   출력**하며, 실패 처리하지 않는다(보정 전 상태가 정상이기 때문).
--   보정은 S4 lawd_sgg_remap_manual_fix.sql 이 별도 커밋으로 수행한다(79 → 80).
--
-- ── 안 A 계약 ───────────────────────────────────────────────────────────────
--   이 표는 **응답 경계 치환 전용**이다. DB 내부값(address.bcode 등)은 옛 28110/
--   28140/28260 으로 남는다. DB 를 직접 조회하면 API 응답과 다른 코드가 보인다 —
--   결함이 아니라 안 A 의 정의다(S11 docs/incheon-sgg-remap.md 에 영구 기록).
--   표가 없거나 비면 API 는 fail-open 으로 현행 응답을 유지한다.
-- ============================================================================

\timing on
\pset pager off
\set ON_ERROR_STOP on

BEGIN;

-- ── 선행조건 가드 ───────────────────────────────────────────────────────────
--   lawd_code_v2 없이 실행하면 빈 대응표가 만들어지고, API 는 표가 "있다"고 판단해
--   스위치를 켠 채 아무것도 치환하지 않는 조용한 무동작에 빠진다.
DO $precond$
BEGIN
  IF to_regclass('public.lawd_code_v2') IS NULL THEN
    RAISE EXCEPTION 'lawd_code_v2 없음 — S2(scripts/postgis/load_lawd_code_v2.py)를 먼저 실행하십시오.';
  END IF;
END
$precond$;

DROP TABLE IF EXISTS lawd_sgg_remap;

CREATE TABLE lawd_sgg_remap (
  old_emd8   char(8) PRIMARY KEY,   -- 옛 읍면동 8자리 (28110xxx / 28140xxx / 28260xxx)
  new_emd8   char(8) NOT NULL,      -- 신 읍면동 8자리 (28125xxx / 28155xxx / 28275xxx / 28290xxx)
  old_sgg_nm text    NOT NULL,      -- 옛 시군구명 (중구 / 동구 / 서구)
  new_sgg_nm text    NOT NULL,      -- 신 시군구명 (제물포구 / 영종구 / 서해구 / 검단구)
  old_emd_nm text    NOT NULL,      -- 옛 읍면동명
  new_emd_nm text    NOT NULL,      -- 신 읍면동명
  n_rows     bigint  NOT NULL,      -- 해당 옛 8자리를 쓰는 address 행수(리뷰용 라벨)
  src        text    NOT NULL       -- 도출 근거
);
COMMENT ON TABLE lawd_sgg_remap IS
  'T026 S3. 인천 개편 옛 읍면동 8자리 → 신 8자리 대응표. 근거는 VWorld dsId=30505 의 '
  'OLD_LAWDCD 뿐이며 명칭 조인은 쓰지 않는다. 안 A 응답 경계 치환 전용 — DB 내부값은 '
  '28110/28140/28260 으로 남는다.';
COMMENT ON COLUMN lawd_sgg_remap.old_emd8 IS
  '치환의 유일한 키. 옛 구명만으로는 결정할 수 없다(옛 중구는 제물포구/영종구로 2분할).';
COMMENT ON COLUMN lawd_sgg_remap.new_sgg_nm IS
  '출력 치환에서는 결과값. 입력 좁힘(§4-4 ⑦ 1단)에서는 사용자 토큰과의 등호 대조 대상 — '
  '대응 추론이 아니므로 P1(명칭 조인 금지) 비위반. 그래서 인덱스를 건다.';
COMMENT ON COLUMN lawd_sgg_remap.n_rows IS
  '리뷰용 라벨. API 는 읽지 않는다. address 부재 시 0(경고 후 진행).';
COMMENT ON COLUMN lawd_sgg_remap.src IS
  'vworld-30505:old_lawdcd = OLD_LAWDCD 자동 도출. manual:* = S4 수기 보정(근거는 해당 파일 헤더).';

-- ── (1) 대응쌍 도출 — OLD_LAWDCD 코드 축 ────────────────────────────────────
--   o 는 옛 코드 행. o.bcode = n.old_bcode 는 **코드 등호**이며 명칭 조인이 아니다.
--   INNER JOIN 인 이유: 옛 행이 원본에 없으면 old_sgg_nm/old_emd_nm 을 만들어낼 수
--   없다. 추측으로 채우느니 빠뜨리고 게이트에 걸리게 한다(실측상 79/79 전부 존재).
CREATE TEMP TABLE _s3_pairs ON COMMIT DROP AS
SELECT left(n.old_bcode,8)::char(8) AS old_emd8,
       left(n.bcode,8)::char(8)     AS new_emd8,
       btrim(o.sgg_nm)              AS old_sgg_nm,
       btrim(n.sgg_nm)              AS new_sgg_nm,
       btrim(o.umd_nm)              AS old_emd_nm,
       btrim(n.umd_nm)              AS new_emd_nm
  FROM lawd_code_v2 n
  JOIN lawd_code_v2 o ON o.bcode = n.old_bcode
 WHERE left(n.bcode,2) = '28' AND left(n.old_bcode,2) = '28'
   AND left(n.bcode,5) <> left(n.old_bcode,5)
   AND right(btrim(n.bcode),2) = '00' AND right(btrim(n.old_bcode),2) = '00'
   AND right(btrim(n.bcode),5) <> '00000'
   AND btrim(n.del_dt) = '';

-- ── (2) address 행수 — 한 번의 스캔으로 집계 ────────────────────────────────
--   ON COMMIT DROP 을 붙이지 않는다 — COMMIT 이후 게이트 G-g 가 이 표를 참조한다.
--   세션 종료 시 자동 소멸하며, 스크립트 말미에서 명시적으로 DROP 한다.
--   address 에 bcode 인덱스가 없어 전체 스캔이다(실측 약 7초). 리뷰용 라벨 하나를
--   위해 빌드를 실패시키지는 않는다 — 표가 없으면 경고 후 0 으로 진행한다.
CREATE TEMP TABLE _s3_nrows (emd8 char(8) PRIMARY KEY, n bigint NOT NULL);
DO $nrows$
BEGIN
  IF to_regclass('public.address') IS NULL THEN
    RAISE WARNING 'address 없음 — n_rows 를 전부 0 으로 채운다(리뷰용 라벨이므로 치환 동작에는 무영향).';
    RETURN;
  END IF;
  EXECUTE $q$
    INSERT INTO _s3_nrows (emd8, n)
    SELECT left(btrim(bcode),8)::char(8), count(*)::bigint
      FROM address
     WHERE left(btrim(bcode),5) IN (SELECT DISTINCT left(old_emd8,5) FROM _s3_pairs)
     GROUP BY 1
  $q$;
END
$nrows$;

-- ── (3) 적재 ────────────────────────────────────────────────────────────────
INSERT INTO lawd_sgg_remap
       (old_emd8, new_emd8, old_sgg_nm, new_sgg_nm, old_emd_nm, new_emd_nm, n_rows, src)
SELECT p.old_emd8, p.new_emd8, p.old_sgg_nm, p.new_sgg_nm, p.old_emd_nm, p.new_emd_nm,
       coalesce(x.n, 0), 'vworld-30505:old_lawdcd'
  FROM _s3_pairs p
  LEFT JOIN _s3_nrows x ON x.emd8 = p.old_emd8;

CREATE INDEX lawd_sgg_remap_new_sgg_nm_idx ON lawd_sgg_remap (new_sgg_nm);

-- ── (4) 구조 붕괴 가드 — 여기서 실패하면 트랜잭션 전체가 롤백된다 ───────────
--   허용 범위를 두는 이유: VWorld 원본이 갱신되면 79 가 소폭 움직일 수 있다.
--   그러나 표가 비거나 자릿수가 달라지는 것은 갱신이 아니라 붕괴다.
--   정확한 기대값 79 는 아래 게이트에서 PASS/FAIL 로 따로 보고한다.
DO $guard$
DECLARE
  n_pairs int;
  n_multi int;
  n_badnew int;
BEGIN
  SELECT count(*) INTO n_pairs FROM lawd_sgg_remap;
  IF n_pairs NOT BETWEEN 70 AND 90 THEN
    RAISE EXCEPTION '대응쌍 % 건 — 허용 범위 [70,90] 이탈. lawd_code_v2 원본 또는 도출 규칙을 확인하십시오.', n_pairs;
  END IF;

  SELECT count(*) INTO n_multi FROM (
    SELECT old_emd8 FROM lawd_sgg_remap GROUP BY 1 HAVING count(*) > 1) t;
  IF n_multi > 0 THEN
    RAISE EXCEPTION '다중매핑 % 건 — 옛 8자리 하나가 신 8자리 둘 이상에 대응. 치환이 비결정적이 된다.', n_multi;
  END IF;

  SELECT count(*) INTO n_badnew FROM lawd_sgg_remap WHERE left(new_emd8,2) <> '28';
  IF n_badnew > 0 THEN
    RAISE EXCEPTION '신 코드가 인천(28) 밖 % 건 — 코드공간 오염.', n_badnew;
  END IF;
END
$guard$;

ANALYZE lawd_sgg_remap;

COMMIT;

-- ============================================================================
-- 게이트 검증 — G-a/G-b/G-c/G-d 가 하나라도 FAIL 이면 S6(API 치환)로 넘어가지 말 것
--   G-e/G-f/G-g 의 미커버는 **보정 전 1건(28110101 중앙동1가)이 정상**이다.
--   S4 적용 후 0 이 되어야 한다.
-- ============================================================================
\echo ''
\echo '── G. lawd_sgg_remap 게이트 ─────────────────────────────────────────'
SELECT 'G-a 대응쌍 (기대 79, S4 후 80)' AS gate, count(*)::text AS actual,
       CASE WHEN count(*) = 79 THEN 'PASS' ELSE 'CHECK' END AS verdict
  FROM lawd_sgg_remap
UNION ALL
SELECT 'G-b 다중매핑 old_emd8 (기대 0)', count(*)::text,
       CASE WHEN count(*) = 0 THEN 'PASS' ELSE 'FAIL' END
  FROM (SELECT old_emd8 FROM lawd_sgg_remap GROUP BY 1 HAVING count(*) > 1) t
UNION ALL
SELECT 'G-c 중복 new_emd8 (기대 0)', count(*)::text,
       CASE WHEN count(*) = 0 THEN 'PASS' ELSE 'FAIL' END
  FROM (SELECT new_emd8 FROM lawd_sgg_remap GROUP BY 1 HAVING count(*) > 1) t
UNION ALL
SELECT 'G-d 옛 코드가 아직 현존 (기대 0)', count(*)::text,
       CASE WHEN count(*) = 0 THEN 'PASS' ELSE 'FAIL' END
  FROM lawd_sgg_remap m
  JOIN lawd_code_v2 o ON o.bcode = (m.old_emd8 || '00')::char(10)
 WHERE btrim(o.del_dt) = ''
UNION ALL
SELECT 'G-e lawd_dong 미커버 (보정 전 1 · S4 후 0)', count(*)::text,
       CASE WHEN count(*) = 0 THEN 'PASS(보정완료)' ELSE 'INFO(S4 대기)' END
  FROM lawd_dong d
 WHERE left(btrim(d.emd_cd),5) IN (SELECT DISTINCT left(old_emd8,5) FROM lawd_sgg_remap)
   AND NOT EXISTS (SELECT 1 FROM lawd_sgg_remap m WHERE btrim(m.old_emd8) = btrim(d.emd_cd))
UNION ALL
SELECT 'G-f admin_boundary emd 미커버 (보정 전 1 · S4 후 0)', count(*)::text,
       CASE WHEN count(*) = 0 THEN 'PASS(보정완료)' ELSE 'INFO(S4 대기)' END
  FROM admin_boundary b
 WHERE b.level = 'emd' AND length(b.code) = 8
   AND left(b.code,5) IN (SELECT DISTINCT left(old_emd8,5) FROM lawd_sgg_remap)
   AND NOT EXISTS (SELECT 1 FROM lawd_sgg_remap m WHERE btrim(m.old_emd8) = b.code)
UNION ALL
SELECT 'G-g address emd8 미커버 (보정 전 1 · S4 후 0)', count(*)::text,
       CASE WHEN count(*) = 0 THEN 'PASS(보정완료)' ELSE 'INFO(S4 대기)' END
  FROM _s3_nrows x
 WHERE NOT EXISTS (SELECT 1 FROM lawd_sgg_remap m WHERE btrim(m.old_emd8) = btrim(x.emd8))
UNION ALL
SELECT 'G-h n_rows 합계 (기록)', coalesce(sum(n_rows),0)::text, 'INFO' FROM lawd_sgg_remap;

\echo ''
\echo '── 미커버 목록 (S4 가 보정해야 할 대상) ─────────────────────────────'
SELECT d.emd_cd, d.sido, d.sigungu, d.emd, coalesce(x.n, 0) AS address_rows
  FROM lawd_dong d
  LEFT JOIN _s3_nrows x ON btrim(x.emd8) = btrim(d.emd_cd)
 WHERE left(btrim(d.emd_cd),5) IN (SELECT DISTINCT left(old_emd8,5) FROM lawd_sgg_remap)
   AND NOT EXISTS (SELECT 1 FROM lawd_sgg_remap m WHERE btrim(m.old_emd8) = btrim(d.emd_cd))
 ORDER BY d.emd_cd;

\echo ''
\echo '── 교차표 (기대 28110→28125 43 · 28110→28155 8 · 28140→28125 7 · 28260→28275 11 · 28260→28290 10) ──'
SELECT left(old_emd8,5) AS old_sgg5, old_sgg_nm,
       left(new_emd8,5) AS new_sgg5, new_sgg_nm,
       count(*) AS pairs, sum(n_rows) AS address_rows
  FROM lawd_sgg_remap
 GROUP BY 1,2,3,4 ORDER BY 1,3;

\echo ''
\echo '── 육안 검토용 전수 목록 ────────────────────────────────────────────'
SELECT old_emd8, new_emd8, old_sgg_nm || ' ' || old_emd_nm AS old_name,
       new_sgg_nm || ' ' || new_emd_nm AS new_name, n_rows, src
  FROM lawd_sgg_remap ORDER BY old_emd8;

DROP TABLE IF EXISTS _s3_nrows;
