-- ri_backfill_verify_post.sql — T018 Phase 1 검증 (S5 **이후**).
--   실행: psql -v ON_ERROR_STOP=1 -v sample=1000000 -f scripts/postgis/ri_backfill_verify_post.sql
--
--   측정 항목
--     W1. S5 정합성 4종 (§S5 검증쿼리)
--     W2. 백필 실패 집계 → _ri_backfill_miss (§B-3, 기대 약 148,818행 / 2.16%)
--     W3. left(parcel.pnu,10) 교차검증 — S5 이후 (기대 97% 이상)
--     W4. 구 사전 복수후보 fail-closed 회귀 — 실제 백필 결과 기준
--     W5. 테이블 팽창 실측
\set ON_ERROR_STOP on
\timing on
\if :{?sample}
\else
  \set sample 1000000
\endif

\echo ''
\echo '=== W1. S5 정합성 ==='
SELECT count(*) AS "todo 중 NOT done (기대 0)"                FROM _ri_backfill_todo WHERE NOT done;
SELECT count(*) AS "리자리 채워진 행 (기대 = todo 총수)"        FROM address WHERE kind='addr' AND right(bcode,2) <> '00';
SELECT count(*) AS "todo.new_bcode 와 불일치 (기대 0)"
  FROM address a JOIN _ri_backfill_todo t ON t.id = a.id
 WHERE a.bcode <> t.new_bcode;
SELECT count(*) AS "원본 lawd_code 에 없는 코드 (기대 0)"
  FROM address a
 WHERE a.kind='addr' AND right(a.bcode,2) <> '00'
   AND NOT EXISTS (SELECT 1 FROM lawd_code c WHERE c.bcode = a.bcode);
SELECT count(*) AS "앞 8자리 변조 (기대 0)"
  FROM address a JOIN _ri_backfill_todo t ON t.id = a.id
 WHERE left(a.bcode,8) <> left(t.old_bcode,8);
SELECT count(*) AS "백업과 다른데 todo 에 없는 행 (기대 0 — 범위 밖 오염 검출)"
  FROM address a JOIN address_bcode_bak_20260810 b ON b.id = a.id
 WHERE a.bcode IS DISTINCT FROM b.bcode
   AND NOT EXISTS (SELECT 1 FROM _ri_backfill_todo t WHERE t.id = a.id);

\echo ''
\echo '=== W2. 백필 실패 집계 (§B-3) ==='
-- S5 이후이므로 right(bcode,2)='00' 이면서 ri 를 가진 행 = 백필 실패 행이다.
DROP TABLE IF EXISTS _ri_backfill_miss;
-- 집계를 CTE 로 먼저 끝내고 읍면동명을 붙인다.
--   초안은 SELECT 목록 안에서 (SELECT name ... WHERE c.bcode = left(a.bcode,8)||'00') 를
--   GROUP BY 1,2 와 함께 썼다가 "subquery uses ungrouped column a.bcode from outer query"
--   로 파스 단계에서 실패했다(서수 GROUP BY 는 서브쿼리 안의 같은 표현식을 인식하지 못한다).
CREATE TABLE _ri_backfill_miss AS
WITH agg AS (
  SELECT left(a.bcode,8) AS emd8, a.ri, count(*) AS n
    FROM address a
   WHERE a.kind='addr' AND a.ri IS NOT NULL AND a.ri <> ''
     AND right(a.bcode,2) = '00'
   GROUP BY 1,2
)
SELECT g.emd8, g.ri, g.n,
       (SELECT c.name FROM lawd_code c WHERE c.bcode = g.emd8||'00') AS emd_name
  FROM agg g
 ORDER BY g.n DESC;
COMMENT ON TABLE _ri_backfill_miss IS
  'T018 검증: 리 코드 백필 실패 집계. 코드를 추측해 채우지 않고 남긴 행들(§B-3 #1). 후속 태스크의 별칭 사전 후보.';

SELECT sum(n) AS "실패 행수 (기대 약 148,818)", count(*) AS "실패 조합수" FROM _ri_backfill_miss;
SELECT round(100.0 * (SELECT sum(n) FROM _ri_backfill_miss)
             / nullif((SELECT count(*) FROM address WHERE kind='addr' AND ri IS NOT NULL AND ri <> ''),0), 2)
       AS "실패율 % (기대 2.16)";

\echo '--- 실패 시도(시·도) 분포 ---'
SELECT left(emd8,2) AS 시도, sum(n) AS 행수, count(*) AS 조합수
  FROM _ri_backfill_miss GROUP BY 1 ORDER BY 2 DESC;

\echo '--- 실패 상위 50건 (§B-3 #4 — 사람이 확인할 목록) ---'
SELECT emd8, emd_name, ri, n FROM _ri_backfill_miss ORDER BY n DESC LIMIT 50;

\echo ''
\echo '=== W2-b. 백필 소스 분포 ==='
SELECT src, count(*) AS rows,
       round(100.0*count(*)/sum(count(*)) OVER (), 2) AS pct
  FROM _ri_backfill_todo GROUP BY 1 ORDER BY 2 DESC;

\echo ''
\echo '=== W3. left(parcel.pnu,10) 교차검증 — S5 이후 [기록 전용, 합격판정 금지] ==='
--
-- ⚠ 계획서는 여기 기대값을 "10자리 97% 이상" 으로 적었다. **원리적으로 달성 불가능하다.**
--    이 조인의 축은 emd_cd(8) + ji_main + ji_sub + san 이라 리(뒤 2자리)를 구분하지 못한다.
--    리마다 1번지부터 지번이 매겨지므로 한 읍면동 안 여러 리에 같은 지번이 흔하다.
--
--    [실측 근거]
--      · 경북 전수: 지번 조합 309,390 중 35.5%(109,842)가 복수 리에 존재. 평균 2.12, 최대 24.
--      · 읍면동당 평균 리 10.78개 → 무작위 추측 기대 정답률 9.28%.
--      · **대조군**: 서로 독립인 ri 경로와 jibun 경로가 *같은 코드로 합의한* 행에
--        같은 판정을 적용했더니 정답률 9.97% — 무작위와 구별되지 않는다.
--        즉 이 지표는 백필 품질에 대한 신호를 담고 있지 않다.
--
--    → 10자리 수치는 **기록만** 한다. 낮다고 실패로 읽지 말 것.
--      앞 8자리(읍면동) 검증에는 유효하며, 그 목적으로는 S5 전후 모두 100% 통과했다.
--
--    ⚠ 리 자리의 유효한 검증은 아래 W3-b 가 **아니다** — W3-b 도 쓸 수 없다는 것이
--      사후 진단으로 밝혀졌다(phase1-log.md §6-3(c)). 리 자리를 확인하려면
--      ri_backfill_verify_dict_accuracy.sql 을 쓸 것.
--
SELECT count(*)                                              AS "조인 성립 (표본)",
       count(*) FILTER (WHERE a_bcode = left(pnu,10))        AS "10자리 일치",
       count(*) FILTER (WHERE left(a_bcode,8) = left(pnu,8)) AS "앞 8자리 일치",
       round(100.0 * count(*) FILTER (WHERE a_bcode = left(pnu,10)) / nullif(count(*),0), 2) AS "10자리 %",
       round(100.0 * count(*) FILTER (WHERE left(a_bcode,8) = left(pnu,8)) / nullif(count(*),0), 2) AS "8자리 %"
FROM (
  SELECT a.bcode AS a_bcode, p.pnu
    FROM (SELECT id, bcode, jibun FROM address
           WHERE kind='addr' AND ri IS NOT NULL AND ri <> '' AND jibun IS NOT NULL
           ORDER BY id LIMIT :sample) a
    CROSS JOIN LATERAL (
      SELECT split_part(x,'-',1)::int                          AS m,
             coalesce(nullif(split_part(x,'-',2),''),'0')::int AS s
        FROM (SELECT (string_to_array(btrim(a.jibun),' '))[
                       array_length(string_to_array(btrim(a.jibun),' '),1)] AS x) q
       WHERE x ~ '^[0-9]+(-[0-9]+)?$'
    ) j
    JOIN parcel p
      ON p.sido_cd = left(a.bcode,2)::char(2)
     AND p.emd_cd  = left(a.bcode,8)::char(8)
     AND p.ji_main = j.m AND p.ji_sub = j.s AND p.san = 1
   WHERE p.pnu IS NOT NULL
) s;

-- (구 "10자리 불일치 상위 20" 목록은 제거했다. 위 근거대로 그 목록은 무작위 잡음이라
--  후속 태스크의 입력으로 오해되면 오히려 해롭다. 대신 아래 W3-b 를 쓴다.)

\echo ''
\echo '=== W3-b. 존재성 검증 — 백필한 리에 그 지번이 실제로 있는가 [기록 전용, 합격판정 금지] ==='
--
-- W3 의 1:N 문제를 피하려는 시도였다: "어느 리가 정답인가" 를 묻지 않고
-- "백필한 코드가 가리키는 **바로 그 리**에 이 지번이 실존하는가" 를 묻는다.
-- 리까지 포함한 10자리로 정확 매칭하므로 후보가 갈리지 않는다.
--
-- ⚠ 이 지표도 리 자리 판정에 쓸 수 없다 (phase1-log.md §6-3(c) 진단 결과).
--    1:N 은 피했지만 parcel 커버리지라는 다른 벽에 막힌다.
--    · 정오 판정이 가능한 k=1 구간(그 지번이 실존하는 리가 하나)의 일치율이 17.01%.
--    · 원인 진단: k=1 불일치 11,254 조합 중 **pnu 가 address.ri 와 일치한 것이 0건**.
--      백필이 틀렸다면 그중 일부는 pnu 가 맞혔어야 한다. 한 건도 없다는 것은
--      백필 오류가 아니라 parcel 이 그 리의 해당 지번을 수록하지 않았다는 뜻이다.
--    · 즉 k=1 은 "현실에 리가 하나" 가 아니라 "parcel 에 수록된 리가 하나" 일 뿐이다.
--    · 같은 표본에서 백필 코드의 리명 = address.ri 인 비율은 99.55% 였다.
--
--    → 리 자리 정확도는 ri_backfill_verify_dict_accuracy.sql 로 측정할 것.
--      ok=false 를 오류 건수로 집계하지 말 것 — parcel 미수록분이 대부분이다.
--
-- 대조군으로 "읍면동 기준 아무 리에나 존재하는가"(=W3 의 조인 성립)를 함께 낸다.
WITH s AS (
  SELECT a.bcode, left(a.bcode,2) AS sido, left(a.bcode,8) AS emd8, j.m, j.s
    FROM (SELECT id, bcode, jibun FROM address
           WHERE kind='addr' AND ri IS NOT NULL AND ri <> '' AND jibun IS NOT NULL
             AND right(bcode,2) <> '00'
           ORDER BY id LIMIT :sample) a
    CROSS JOIN LATERAL (
      SELECT split_part(x,'-',1)::int                          AS m,
             coalesce(nullif(split_part(x,'-',2),''),'0')::int AS s
        FROM (SELECT (string_to_array(btrim(a.jibun),' '))[
                       array_length(string_to_array(btrim(a.jibun),' '),1)] AS x) q
       WHERE x ~ '^[0-9]+(-[0-9]+)?$'
    ) j
), chk AS (
  SELECT s.bcode,
         EXISTS (SELECT 1 FROM parcel p
                  WHERE p.sido_cd = s.sido::char(2) AND p.emd_cd = s.emd8::char(8)
                    AND p.ji_main = s.m AND p.ji_sub = s.s AND p.san = 1
                    AND left(p.pnu,10) = s.bcode)          AS exact_ok,
         EXISTS (SELECT 1 FROM parcel p
                  WHERE p.sido_cd = s.sido::char(2) AND p.emd_cd = s.emd8::char(8)
                    AND p.ji_main = s.m AND p.ji_sub = s.s AND p.san = 1) AS emd_ok
    FROM s
)
SELECT count(*)                                              AS "검증 대상",
       count(*) FILTER (WHERE emd_ok)                        AS "읍면동에 지번 존재 (모집단)",
       count(*) FILTER (WHERE exact_ok)                      AS "백필한 리에 실존",
       round(100.0*count(*) FILTER (WHERE exact_ok)
             /nullif(count(*) FILTER (WHERE emd_ok),0),2)    AS "실존율 % (핵심 지표)",
       count(*) FILTER (WHERE emd_ok AND NOT exact_ok)       AS "지번은 있으나 다른 리"
  FROM chk;

\echo '--- 실존 실패 상위 20 (별칭 사전 후보 — 후속 태스크 입력) ---'
WITH s AS (
  SELECT a.bcode, a.ri, left(a.bcode,2) AS sido, left(a.bcode,8) AS emd8, j.m, j.s
    FROM (SELECT id, bcode, ri, jibun FROM address
           WHERE kind='addr' AND ri IS NOT NULL AND ri <> '' AND jibun IS NOT NULL
             AND right(bcode,2) <> '00'
           ORDER BY id LIMIT :sample) a
    CROSS JOIN LATERAL (
      SELECT split_part(x,'-',1)::int                          AS m,
             coalesce(nullif(split_part(x,'-',2),''),'0')::int AS s
        FROM (SELECT (string_to_array(btrim(a.jibun),' '))[
                       array_length(string_to_array(btrim(a.jibun),' '),1)] AS x) q
       WHERE x ~ '^[0-9]+(-[0-9]+)?$'
    ) j
)
SELECT s.emd8, s.ri, s.bcode AS backfilled, count(*) AS n
  FROM s
 WHERE EXISTS (SELECT 1 FROM parcel p
                WHERE p.sido_cd = s.sido::char(2) AND p.emd_cd = s.emd8::char(8)
                  AND p.ji_main = s.m AND p.ji_sub = s.s AND p.san = 1)
   AND NOT EXISTS (SELECT 1 FROM parcel p
                    WHERE p.sido_cd = s.sido::char(2) AND p.emd_cd = s.emd8::char(8)
                      AND p.ji_main = s.m AND p.ji_sub = s.s AND p.san = 1
                      AND left(p.pnu,10) = s.bcode)
 GROUP BY 1,2,3 ORDER BY 4 DESC LIMIT 20;

\echo ''
\echo '=== W4. 구 사전 복수후보 fail-closed 회귀 (§B-5 권고 20) ==='
SELECT count(*) AS "복수후보 조합수" FROM _ri_multi_cand;

SELECT count(DISTINCT (m.emd_cd, m.ri)) AS "영향 조합 중 백필 성공",
       sum(CASE WHEN right(a.bcode,2) <> '00' THEN 1 ELSE 0 END) AS "백필된 행수",
       sum(CASE WHEN right(a.bcode,2)  = '00' THEN 1 ELSE 0 END) AS "fail-closed 행수 (0 이 이상적)"
  FROM address a
  JOIN _ri_multi_cand m ON m.emd_cd = left(a.bcode,8) AND m.ri = a.ri
 WHERE a.kind='addr' AND a.ri IS NOT NULL AND a.ri <> '';

\echo '--- 복수후보 중 신 사전이 구 후보와 다른 코드를 고른 조합 (pnu 로 검증) ---'
SELECT m.emd_cd, m.ri, m.old_cands, l.ri_cd AS new_cd, l.exist
  FROM _ri_multi_cand m
  JOIN lawd_ri l ON l.emd_cd = m.emd_cd AND l.ri = m.ri
 WHERE NOT (l.ri_cd = ANY(m.old_cands))
 ORDER BY 1,2 LIMIT 30;

\echo '--- 폐지 폴백분(exist=false)이 백필에 쓰인 행수 (중점 확인 대상) ---'
SELECT count(*) AS "폐지 폴백 코드로 백필된 행수",
       count(DISTINCT a.bcode) AS "고유 코드수"
  FROM address a
  JOIN lawd_ri l ON l.emd_cd = left(a.bcode,8) AND l.ri_cd = right(a.bcode,2) AND l.ri = a.ri
 WHERE a.kind='addr' AND right(a.bcode,2) <> '00' AND l.exist = false;

\echo ''
\echo '=== W5. 테이블 팽창 ==='
SELECT pg_size_pretty(pg_total_relation_size('address'))      AS "address 총크기 (S5 후)",
       pg_size_pretty(pg_relation_size('address'))            AS "  힙",
       pg_size_pretty(pg_indexes_size('address'))             AS "  인덱스",
       pg_total_relation_size('address')                      AS "bytes";
SELECT n_live_tup, n_dead_tup, last_vacuum, last_autovacuum
  FROM pg_stat_user_tables WHERE relname='address';
