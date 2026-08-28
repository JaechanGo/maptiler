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
-- ri_backfill_verify_pre.sql — T018 Phase 1 검증 (S5 **이전**).
--   S5 가 bcode 를 바꾸고 나면 재현할 수 없는 기준선만 여기서 잡는다.
--   실행 위치: S4(백업) 완료 후, S5 착수 직전.
--
--   측정 항목
--     V1. left(parcel.pnu,10) 교차검증 기준선 (§B-5 권고 19-a)
--     V2. src='ri' 경로 vs src='jibun' 경로 교차일치율 (§B-3 #5, 기대 99.25%)
--     V3. 구 사전 (emd_cd, ri) 복수후보 조합 추출 (§B-5 권고 20, 기대 534건)
--
--   V1·V2 는 674만 행 규모 조인이라 무겁다. 표본 크기는 :sample 로 조절한다.
-- 실행: psql -v ON_ERROR_STOP=1 -v sample=1000000 -f scripts/postgis/ri_backfill_verify_pre.sql
\set ON_ERROR_STOP on
\timing on
\if :{?sample}
\else
  \set sample 1000000
\endif
-- V2 는 표본 1행마다 jibun 문자열 분해가 들어가 V1 보다 훨씬 비싸다. 표본을 따로 잡는다.
\if :{?v2sample}
\else
  \set v2sample 200000
\endif

-- S5 가 이미 돌았으면 기준선의 의미가 없다. 확인하고 중단.
DO $chk$
DECLARE n bigint;
BEGIN
  SELECT count(*) INTO n FROM address WHERE kind='addr' AND right(bcode,2) <> '00';
  IF n > 0 THEN
    RAISE EXCEPTION 'S5 가 이미 % 행을 갱신했다. 이 스크립트는 S5 이전 기준선용이다.', n;
  END IF;
END $chk$;

\echo ''
\echo '=== V1. left(parcel.pnu,10) 교차검증 — S5 이전 기준선 ==='
\echo '   조인: address.bcode 앞8 = parcel.emd_cd, 지번 본번/부번 정확일치, san=1'
\echo '   (geocode-api-pg.py:490-505 의 지번 경로 조인 조건과 동일 축)'
-- parcel 은 sido_cd 파티션이다. sido_cd 조건을 함께 걸어야 전 파티션 Seq Scan 을 피한다.
SELECT count(*)                                                   AS "조인 성립 (표본)",
       count(*) FILTER (WHERE a_bcode = left(pnu,10))             AS "10자리 일치 (기대 ≈0)",
       count(*) FILTER (WHERE left(a_bcode,8) = left(pnu,8))      AS "앞 8자리 일치",
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

\echo ''
\echo '=== V2. src=ri 경로 vs src=jibun 경로 교차일치율 (기대 99.25%) ==='
\echo '   두 경로가 모두 코드를 내는 행에서, 서로 다른 코드로 귀결되는 비율'
\echo '   모집단은 _ri_backfill_todo — 백필 대상 그 자체다.'
--
-- 표본을 address 에서 직접 뽑지 않는 이유 [실측]:
--   초안은 address 를 ORDER BY id LIMIT n 으로 훑으면서 right(bcode,2)='00' 을 필터했다.
--   bcode 에 인덱스가 없어 병렬 Seq Scan 이 되고, 그 위에 표본 전체의 jibun 분해가 얹혀
--   100만 표본에서 20분을 넘겨도 끝나지 않았다(워커 3/4 가 IPC 대기).
--   _ri_backfill_todo 는 이미 확정된 674만 행 모집단이고 PK 가 id 다.
--   todo 를 id 순으로 잘라 address 를 PK 조인하면 같은 통계량을 훨씬 싸게 얻는다.
--
WITH tgt AS (
  SELECT t.id, left(t.old_bcode,8) AS emd8, a.ri, a.jibun
    FROM _ri_backfill_todo t
    JOIN address a ON a.id = t.id
   ORDER BY t.id LIMIT :v2sample
), ri_path AS (
  SELECT g.id, l.ri_cd
    FROM tgt g JOIN lawd_ri l ON l.emd_cd = g.emd8 AND l.ri = g.ri
   WHERE g.ri IS NOT NULL AND g.ri <> ''
), jibun_path AS (
  SELECT g.id, l.ri_cd
    FROM tgt g
    CROSS JOIN LATERAL (
      SELECT x FROM unnest(string_to_array(btrim(g.jibun),' ')) AS x
       WHERE x LIKE '%리' AND char_length(x) >= 2 LIMIT 1
    ) j
    JOIN lawd_ri l ON l.emd_cd = g.emd8 AND l.ri = j.x
   WHERE g.jibun IS NOT NULL
)
SELECT (SELECT count(*) FROM tgt)                                    AS "표본",
       (SELECT count(*) FROM ri_path)                                AS "ri 경로 성공",
       (SELECT count(*) FROM jibun_path)                             AS "jibun 경로 성공",
       count(*)                                                      AS "양쪽 모두 성공",
       count(*) FILTER (WHERE r.ri_cd = j.ri_cd)                     AS "일치",
       count(*) FILTER (WHERE r.ri_cd <> j.ri_cd)                    AS "불일치",
       round(100.0*count(*) FILTER (WHERE r.ri_cd = j.ri_cd)/nullif(count(*),0), 2) AS "일치율 %"
  FROM ri_path r JOIN jibun_path j USING (id);

\echo ''
\echo '=== V3. 구 사전 (emd_cd, ri) 복수후보 조합 (기대 534건) ==='
\echo '   구 PK 는 (emd_cd, ri_cd, ri) 3컬럼이라 한 (emd_cd,ri) 에 복수 ri_cd 를 허용했다.'
\echo '   신 PK 는 (emd_cd, ri) — 후보 1개만 반환하므로 틀리면 조인 0건(fail-closed)이 된다.'
DROP TABLE IF EXISTS _ri_multi_cand;
CREATE TABLE _ri_multi_cand AS
SELECT emd_cd, ri, count(*) AS n_cand,
       array_agg(ri_cd ORDER BY ri_cd) AS old_cands
  FROM lawd_ri_bak_20260810
 GROUP BY 1,2 HAVING count(*) > 1;
COMMENT ON TABLE _ri_multi_cand IS
  'T018 검증: 구 사전에서 (emd_cd,ri) 하나에 복수 ri_cd 를 갖던 조합. 신 사전 fail-closed 회귀 대상.';

SELECT count(*) AS "복수후보 조합 (기대 534)" FROM _ri_multi_cand;
SELECT n_cand, count(*) AS 조합수 FROM _ri_multi_cand GROUP BY 1 ORDER BY 1;

\echo '--- 신 사전이 이 조합들을 어떻게 처리했는지 ---'
SELECT count(*)                                            AS "조합 총수",
       count(*) FILTER (WHERE l.ri_cd IS NULL)             AS "신 사전에 없음 (fail-closed 위험)",
       count(*) FILTER (WHERE l.ri_cd = ANY(m.old_cands))  AS "구 후보 중 하나 선택",
       count(*) FILTER (WHERE l.ri_cd IS NOT NULL AND NOT (l.ri_cd = ANY(m.old_cands))) AS "구 후보에 없던 코드",
       count(*) FILTER (WHERE l.exist = false)             AS "폐지 폴백분 (중점 확인)"
  FROM _ri_multi_cand m
  LEFT JOIN lawd_ri l ON l.emd_cd = m.emd_cd AND l.ri = m.ri;

\echo '--- 신 사전에 없는 조합 상위 20 (있다면 예외 목록 후보) ---'
SELECT m.emd_cd, m.ri, m.old_cands
  FROM _ri_multi_cand m
  LEFT JOIN lawd_ri l ON l.emd_cd = m.emd_cd AND l.ri = m.ri
 WHERE l.ri_cd IS NULL
 ORDER BY m.emd_cd, m.ri LIMIT 20;

\echo ''
\echo '=== V3-b. 복수후보 조합의 실제 주소 행 영향 ==='
SELECT count(*) AS "영향 행수",
       count(DISTINCT (m.emd_cd, m.ri)) AS "영향 조합수"
  FROM address a
  JOIN _ri_multi_cand m ON m.emd_cd = left(a.bcode,8) AND m.ri = a.ri
 WHERE a.kind='addr' AND a.ri IS NOT NULL AND a.ri <> '';

SELECT count(*) AS "그중 신 사전 미수록 = 백필 실패 확정 행수"
  FROM address a
  JOIN _ri_multi_cand m ON m.emd_cd = left(a.bcode,8) AND m.ri = a.ri
  LEFT JOIN lawd_ri l   ON l.emd_cd = m.emd_cd AND l.ri = m.ri
 WHERE a.kind='addr' AND a.ri IS NOT NULL AND a.ri <> '' AND l.ri_cd IS NULL;
