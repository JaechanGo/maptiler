-- ============================================================================
--  ri_backfill_verify_dict_accuracy.sql
--    태스크 018 Phase 1 — 리 자리(법정동코드 뒤 2자리) 백필의 **유효 정확도 지표**
--
--  실행:
--    unset PGOPTIONS            # TEMP TABLE 을 만들므로 read-only 옵션을 끈다
--    PGHOST=localhost PGPORT=5433 PGUSER=cuvia PGDATABASE=cuvia PGPASSWORD=cuvia \
--      psql -X -P pager=off -f scripts/postgis/ri_backfill_verify_dict_accuracy.sql
--
--  소요: 약 30초 (전수 674만 행, 표본 아님)
-- ============================================================================
--
--  ■ 왜 이 스크립트가 따로 필요한가
--
--  ri_backfill_verify_post.sql 의 W3(pnu 10자리 대조)와 W3-b(존재성)는 **둘 다**
--  리 자리 판정에 쓸 수 없다. 근거는 phase1-log.md §6-2 · §6-3(c) 에 있고 요지는:
--
--    · W3   — 조인축(emd_cd 8자리 + 지번)에 리가 없어 1:N 이다. 두 독립 경로가
--             *합의한* 행에서도 정답률 9.97% 로 무작위(9.28%)와 구별되지 않는다.
--    · W3-b — 1:N 은 피했지만 parcel 커버리지에 막힌다. 정오 판정이 가능한 k=1
--             구간의 불일치 11,254 조합 중 **pnu 가 address.ri 와 일치한 것이 0건**.
--             백필이 틀렸다면 일부는 pnu 가 맞혔어야 하므로, 이는 백필 오류가
--             아니라 parcel 미수록이다.
--
--  ■ 이 스크립트가 재는 것
--
--  S5 가 한 일은 오직 하나다 — **address.ri 문자열을 법정동코드 리 2자리로 바꾸는 것.**
--  그러므로 올바른 검증축은 "백필된 코드가 가리키는 리의 **이름**이 address.ri 와
--  같은가" 이고, 여기에는 parcel 이 전혀 필요 없다. 표본이 아니라 전수로 잰다.
--
--  ■ 결과 해석 — 과대 해석 금지
--
--    · src='ri'    의 일치율은 **항등에 가깝다**. 사전을 address.ri 로 조인해 코드를
--      얻었으니 되돌리면 같은 이름이 나오는 것이 당연하다. 이 값이 증명하는 것은
--      **조인 키 무결성** — 잘못된 emd_cd 에 붙거나, 다른 리의 코드가 섞이거나,
--      폐지 코드 폴백이 엉뚱한 값을 넣은 사례가 0건이라는 사실이다.
--    · 이 값은 **address.ri 자체가 옳은가** 를 증명하지 않는다. address.ri 가
--      오염돼 있으면 오염된 채로 정확히 코드화된다. 그 오염은 09-gen-geocode.py 의
--      setdefault 뭉갬(phase1-log.md §9-4)이라는 별개 결함이며 Phase 1 범위 밖이다.
--    · src='jibun' 의 0% 는 **실패가 아니다**. address.ri 공란이 0건이라는 점이
--      핵심이다 — 값이 있는데도 사전에 없는 이름이라 조인이 안 된 행이고, 설계대로
--      필지 실측(지번)으로 판정했다. 오염된 문자열보다 필지를 택한 쪽이 옳다.
--
--  ■ 2026-08-10 실측 (로컬 PostGIS, address addr 10,686,547행)
--      전수 6,743,655 / 리명 일치 6,730,277 / 불일치 13,378 → 99.8016%
--      src='ri'    6,730,277 → 100.0000%
--      src='jibun'    13,378 →   0.0000%  (address.ri 공란 0건)
-- ============================================================================
\set ON_ERROR_STOP on
\timing on

DO $$
BEGIN
  IF to_regclass('public._ri_backfill_todo') IS NULL THEN
    RAISE EXCEPTION '_ri_backfill_todo 가 없습니다. S3 산출물이 필요합니다 (백필 대상 모집단).';
  END IF;
END $$;

-- 리명 사전: bcode(text) → 리명.
--   lawd_code.name 은 공백 구분 전체 주소명이므로 **마지막 토큰**이 리명이다.
--     예) '경기도 이천시 장호원읍 장호원리' → '장호원리'
--   ⚠ lawd_code.bcode 는 character(10) 이다. address 유래 값과 직접 비교하면
--     타입 불일치로 인덱스를 타지 못하고 53,387행 Seq Scan 이 반복된다.
--     반드시 rtrim(bcode)::text 로 한 번만 정규화해 두고 JOIN 할 것.
DROP TABLE IF EXISTS _rin;
CREATE TEMP TABLE _rin AS
SELECT rtrim(bcode)::text AS bcode,
       (string_to_array(btrim(name),' '))[
         array_length(string_to_array(btrim(name),' '),1)] AS ri_name
  FROM lawd_code;
CREATE UNIQUE INDEX ON _rin(bcode);
ANALYZE _rin;

\echo ''
\echo '=== D1. 사전 매핑 정확도 — 전수 (parcel 무관) ==='
SELECT count(*)                                                   AS "백필 성공 행",
       count(*) FILTER (WHERE bn.ri_name = a.ri)                  AS "리명 일치",
       count(*) FILTER (WHERE bn.ri_name IS DISTINCT FROM a.ri)   AS "리명 불일치",
       round(100.0 * count(*) FILTER (WHERE bn.ri_name = a.ri)
             / nullif(count(*),0), 4)                             AS "정확도 %"
  FROM _ri_backfill_todo t
  JOIN address a  ON a.id    = t.id
  JOIN _rin    bn ON bn.bcode = rtrim(a.bcode)::text
 WHERE right(a.bcode,2) <> '00';

\echo ''
\echo '=== D2. src 별 분해 — ri 경로와 jibun 경로는 성격이 다르다 ==='
-- "address.ri 공란" 이 0 이어야 한다. 0 이 아니면 jibun 경로 판정 로직을 재검토할 것.
SELECT t.src,
       count(*)                                                   AS "행수",
       count(*) FILTER (WHERE bn.ri_name = a.ri)                  AS "리명 일치",
       count(*) FILTER (WHERE a.ri IS NULL OR a.ri = '')          AS "address.ri 공란",
       round(100.0 * count(*) FILTER (WHERE bn.ri_name = a.ri)
             / nullif(count(*),0), 4)                             AS "일치율 %"
  FROM _ri_backfill_todo t
  JOIN address a  ON a.id    = t.id
  JOIN _rin    bn ON bn.bcode = rtrim(a.bcode)::text
 WHERE right(a.bcode,2) <> '00'
 GROUP BY 1 ORDER BY 2 DESC;

\echo ''
\echo '--- D3. 불일치 상위 20 (전부 jibun 경로여야 정상) ---'
-- src='ri' 가 여기 나타나면 **진짜 결함**이다. 사전 조인으로 얻은 코드가 원래
-- 문자열과 다른 리를 가리킨다는 뜻이므로 즉시 조사할 것.
SELECT t.src,
       bn.ri_name AS "백필 코드의 리명",
       a.ri       AS "address.ri",
       count(*)   AS n
  FROM _ri_backfill_todo t
  JOIN address a  ON a.id    = t.id
  JOIN _rin    bn ON bn.bcode = rtrim(a.bcode)::text
 WHERE right(a.bcode,2) <> '00'
   AND bn.ri_name IS DISTINCT FROM a.ri
 GROUP BY 1,2,3 ORDER BY 4 DESC LIMIT 20;

\echo ''
\echo '=== D4. 게이트 판정 ==='
SELECT CASE WHEN bad = 0
            THEN 'PASS — src=ri 경로 전건 일치 (조인 키 무결성 확인)'
            ELSE 'FAIL — src=ri 경로에 ' || bad || ' 행 불일치. 즉시 조사할 것.'
       END AS "판정"
  FROM (
    SELECT count(*) FILTER (WHERE t.src = 'ri'
                              AND bn.ri_name IS DISTINCT FROM a.ri) AS bad
      FROM _ri_backfill_todo t
      JOIN address a  ON a.id    = t.id
      JOIN _rin    bn ON bn.bcode = rtrim(a.bcode)::text
     WHERE right(a.bcode,2) <> '00'
  ) q;

DROP TABLE IF EXISTS _rin;
