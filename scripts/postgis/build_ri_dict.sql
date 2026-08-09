-- build_ri_dict.sql — lawd_ri 멱등 재생성 (address.bd_mgt_sn 파생, 시도 리매핑 단일축)
-- 근거: design-review R-2/R-3, design-review-2 F-1/F-3/F-4/F-6/F-7.
--   법정동코드 10 = 시도2+시군구3+읍면동3+리2.
--   bd_mgt_sn 앞 10자리에 리 2자리가 살아있으나 시도 접두에 '구 체계' 잔재가 있어
--   parcel.emd_cd/pnu(신 체계)와 조인하려면 시도 리매핑이 필수.
--   (이중축 UNION 은 신축 조합에서 충돌 646 → 폐기)
-- 데이터소스: address(kind='addr') — kind='addr' 행만 코드 신뢰(09-gen-geocode.py:164).
-- 실행: psql -v ON_ERROR_STOP=1 -f build_ri_dict.sql   (인라인 -c 금지)
-- ※ dollar-quote($build_ri$)는 *.sql 파일 내부이며 psql -f 가 직접 파싱 → zsh _safe_eval 무관.
-- ※ 로컬 실측치(키 16,402 / 충돌 27)는 폐기된 5규칙 축에서 나온 값이다 — 2규칙 축의
--   실제 수치는 [미확인]이며 자기검증 0·1·2 로 측정한다.

\timing on

CREATE TABLE IF NOT EXISTS lawd_ri (
  emd_cd char(8) NOT NULL,     -- 읍면동 8자리 (신 체계로 정규화됨)
  ri_cd  char(2) NOT NULL,     -- 리 2자리 (읍면동 내 일련번호)
  ri     text    NOT NULL,
  PRIMARY KEY (emd_cd, ri_cd)
);
CREATE INDEX IF NOT EXISTS lawd_ri_ri_idx ON lawd_ri (ri);

-- 가드+TRUNCATE+INSERT 를 단일 트랜잭션 + plpgsql DO 로 원자화.
-- 임계 밖이면 RAISE EXCEPTION → 트랜잭션 abort(TRUNCATE 롤백, 빈 사전 잔존 방지).
DO $build_ri$
DECLARE
  n_rows   bigint;
  lo       constant bigint := 15000;   -- 실측 16,402 기준 하한 (address.ri 미적재/부분적재 차단)
  hi       constant bigint := 20000;   -- 이중축 UNION 19,429 를 상회하는 상한 (체계 오염 차단)
BEGIN
  -- 1) 후보 키 수 사전 집계 (INSERT 와 완전히 동일한 식·동일한 WHERE)
  --    F-6: 세 CASE 와 WHERE 의 bd_mgt_sn 참조를 전부 btrim(bd_mgt_sn) 으로 통일한다.
  --         (선행공백 1칸이면 substr 자리가 밀려 emd_cd·ri_cd 가 통째로 어긋난다)
  -- 리매핑은 도 전체 개편인 42→51·45→52 만. 44/47 은 현행 유효 시도코드다(20-parcel.sql:27-32, load_parcel.sh:90-95). 세종·군위는 시군구 5자리 변경이라 2자리 리매핑으로 처리 불가 — 별도 과제.
  SELECT count(DISTINCT (
           (CASE substr(m,1,2)
              WHEN '42' THEN '51' WHEN '45' THEN '52'
              ELSE substr(m,1,2)
            END || substr(m,3,6)) || substr(m,9,2)))
    INTO n_rows
    FROM address a, LATERAL (SELECT btrim(a.bd_mgt_sn) AS m) t
   WHERE a.kind='addr' AND a.ri IS NOT NULL AND a.ri <> ''
     AND a.bd_mgt_sn IS NOT NULL AND length(m) >= 10
     AND substr(m,9,2) <> '00';

  RAISE NOTICE 'build_ri_dict: would_rows=% (허용 [%, %])', n_rows, lo, hi;

  -- 2) 양방 가드 — 하한 미달 = address.ri 미적재 의심, 상한 초과 = 코드체계 오염 의심.
  IF n_rows < lo OR n_rows > hi THEN
    RAISE EXCEPTION 'build_ri_dict ABORT: would_rows=% 가 허용범위 [%, %] 밖. address.ri 적재 상태 확인 필요 — INSERT 거부.',
      n_rows, lo, hi;
  END IF;

  -- 3) 멱등 재적재. F-4: DISTINCT ON + ORDER BY 로 tie-break 를 결정적으로 고정한다
  --    (build_dong_dict.sql:41,49 선례). ON CONFLICT DO NOTHING 은 안전망으로만 남긴다.
  -- 리매핑은 도 전체 개편인 42→51·45→52 만. 44/47 은 현행 유효 시도코드다(20-parcel.sql:27-32, load_parcel.sh:90-95). 세종·군위는 시군구 5자리 변경이라 2자리 리매핑으로 처리 불가 — 별도 과제.
  TRUNCATE lawd_ri;
  INSERT INTO lawd_ri(emd_cd, ri_cd, ri)
  SELECT DISTINCT ON (emd_cd, ri_cd) emd_cd, ri_cd, ri
    FROM (
      SELECT (CASE substr(m,1,2)
                WHEN '42' THEN '51' WHEN '45' THEN '52'
                ELSE substr(m,1,2)
              END || substr(m,3,6))::char(8) AS emd_cd,
             substr(m,9,2)::char(2)          AS ri_cd,
             a.ri                            AS ri
        FROM address a, LATERAL (SELECT btrim(a.bd_mgt_sn) AS m) t
       WHERE a.kind='addr' AND a.ri IS NOT NULL AND a.ri <> ''
         AND a.bd_mgt_sn IS NOT NULL AND length(m) >= 10
         AND substr(m,9,2) <> '00'
    ) s
   ORDER BY emd_cd, ri_cd, ri   -- tie-break: 동일 키의 복수 리 이름 중 사전순 1개를 결정적으로 채택
  ON CONFLICT (emd_cd, ri_cd) DO NOTHING;
END
$build_ri$;

ANALYZE lawd_ri;

-- 자기검증 0: lawd_dong 축 정합률 — 실제 조회 경로(geocode-api-pg.py:391-393)가 타는 축.
-- 이 값이 낮으면 lawd_ri 를 아무리 잘 만들어도 조회가 0건이 되어 기능이 무효다.
SELECT count(*) FILTER (WHERE ok) AS matched, count(*) AS keys,
       round(100.0 * count(*) FILTER (WHERE ok) / nullif(count(*),0), 2) AS pct
  FROM (SELECT EXISTS (SELECT 1 FROM lawd_dong d WHERE d.emd_cd = r.emd_cd) AS ok
          FROM lawd_ri r) t;

-- 자기검증 1: parcel 축 정합률 (99% 미만이면 즉시 중단 — 시도 리매핑 오류 의심)
SELECT count(*) FILTER (WHERE ok) AS joinable, count(*) AS keys,
       round(100.0 * count(*) FILTER (WHERE ok) / nullif(count(*),0), 2) AS pct
  FROM (SELECT EXISTS (SELECT 1 FROM parcel p
                        WHERE p.sido_cd = left(r.emd_cd,2)::char(2)   -- 파티션 pruning 필수(함정 #4)
                          AND p.emd_cd  = r.emd_cd) AS ok
          FROM lawd_ri r) t;

-- ── 어긋난 키의 시도접두 분포 (자기검증 0·1 이 99 미만일 때 첨부해 보고) ──
SELECT left(emd_cd,2) AS sido2, count(*)
  FROM lawd_ri r
 WHERE NOT EXISTS (SELECT 1 FROM parcel p
                    WHERE p.sido_cd = left(r.emd_cd,2)::char(2)
                      AND p.emd_cd  = r.emd_cd)
 GROUP BY 1 ORDER BY 2 DESC;

-- ── 자기검증 2: 총 키 수 / 리 이름 종수 (로컬 실측 16,402 과 대조 → U-5) ──
SELECT count(*) AS keys, count(DISTINCT ri) AS distinct_ri FROM lawd_ri;

-- ── 자기검증 3: 충돌 키 덤프 (동일 (emd_cd,ri_cd) 에 복수 리 이름) ───────
--    DISTINCT ON tie-break 로 버려진 쪽을 기록으로 남긴다. 폐기축 실측 27건은 참고치일 뿐 [미확인].
SELECT (CASE substr(m,1,2)
          WHEN '42' THEN '51' WHEN '45' THEN '52'
          ELSE substr(m,1,2)
        END || substr(m,3,6))::char(8)              AS emd_cd,
       substr(m,9,2)::char(2)                       AS ri_cd,
       string_agg(DISTINCT a.ri, ' | ' ORDER BY a.ri) AS ri_names
  FROM address a, LATERAL (SELECT btrim(a.bd_mgt_sn) AS m) t
 WHERE a.kind='addr' AND a.ri IS NOT NULL AND a.ri <> ''
   AND a.bd_mgt_sn IS NOT NULL AND length(m) >= 10
   AND substr(m,9,2) <> '00'
 GROUP BY 1, 2
HAVING count(DISTINCT a.ri) > 1
 ORDER BY 1, 2;

-- ── 자기검증 4: 청평리 존재 확인 (기준 1 의 사전 조건) ───────────────────
SELECT emd_cd, ri_cd FROM lawd_ri WHERE ri = '청평리' ORDER BY emd_cd;
