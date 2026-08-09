-- build_ri_dict.sql — lawd_ri 멱등 재생성 (bcode 축 + bd_mgt_sn 리코드)
--   법정동코드 10 = 시도2 + 시군구3 + 읍면동3 + 리2.
--   emd_cd = left(btrim(bcode), 8)          … 현행 법정동코드. 시군구 개편이 반영돼 있다.
--   ri_cd  = substr(btrim(bd_mgt_sn), 9, 2) … 리 일련번호. bcode 끝 2자리는 전 행이 '00' 이라
--                                             리 코드의 유일한 출처가 bd_mgt_sn 9-10 이다.
--
-- ※ 폐기: 「bd_mgt_sn 앞 8자리 + 시도 2자리 리매핑」 단일축 (2026-08-09 실측 폐기)
--   bd_mgt_sn 앞 8자리는 **건물관리번호 발급 시점의 시군구 코드가 그대로 굳어 있다.**
--   청원군→청주시(43710*), 연기군→세종(44730*), 당진군→당진시(44830*), 이천군→이천시(41730*) 등
--   시군구 5자리 개편은 시도 2자리 리매핑으로 구조적으로 교정할 수 없다.
--   로컬 전량 실측: 키 16,419 중 1,407 키(8.57%)가 lawd_dong·parcel 양쪽에 존재하지 않는
--   사(死) 코드 → 정합률 91.43%. 자기검증 0·1 의 중단 임계(99)에 걸려 사전이 무효였다.
--   어긋난 키는 12개 시도에 걸쳐 있었다(41:408 43:310 44:255 47:243 46:67 51:36 27:33 26:23
--   31:14 48:10 52:7 50:1) — 특정 시도 편중이 아니므로 리매핑 규칙 추가로는 해소 불가.
--
-- ※ 이 축을 한때 "이중축 UNION 은 신축 조합에서 충돌 646 → 폐기" 로 기록했었다. 그 646 건은
--   이 축에서도 그대로 나온다(자기검증 3 실측 646행). 폐기 사유였던 것은 충돌의 존재가 아니라
--   PK 가 (emd_cd, ri_cd) 2컬럼이어서 충돌 시 리 이름을 하나만 남기고 버렸다는 점이다.
--   조회 경로는 `WHERE ri = %s AND emd_cd = ANY(...)` (geocode-api-pg.py 변경 ⑤) 로
--   **PK 를 타지 않으므로** PK 를 (emd_cd, ri_cd, ri) 3컬럼으로 넓히면 646 건이 모두 보존되어
--   손실 0 이 된다. 조회 인덱스는 lawd_ri_ri_emd_idx (ri, emd_cd) 가 따로 받는다.
--
-- 로컬 전량 실측 (2026-08-09, address 1,628만행 / address.ri 6,892,473행):
--   행수 16,113 · distinct_ri 6,944 · 자기검증 0 = 100.00% · 자기검증 1 = 100.00%
--   (emd_cd, ri_cd) 복수이름 646건 — 3컬럼 PK 라 소실 0
--   (참고) 동일 (emd_cd, ri) 에 복수 ri_cd 가 붙는 조합 534 건은 폐기 대상이 아니다.
--   조회가 두 쌍을 모두 반환하고 parcel 의 pnu 리코드가 최종 판별하므로 무해하다.
--
-- 한계 — 반드시 인지할 것:
--   이 사전은 address(도로명주소 건물 DB) 에서 유도되므로 **건물이 없는 리는 수록되지 않는다.**
--   법정리 약 19,000 대비 16,113 = 약 3,000 개 리 미수록. 예: 전북 완주군 소양면 월상리는
--   address 에 아예 없어(충북 월상리 207행만 존재) 리 좁힘이 적용되지 않는다.
--   미수록 리는 필터가 걸리지 않을 뿐 종전과 동일하게 동작한다(fail-soft, 무회귀).
--   전 법정리 커버리지가 필요하면 행안부 법정동코드 전량 파일이 별도로 필요하다 — 이 스크립트 범위 밖.
--
-- 데이터소스: address(kind='addr') — kind='addr' 행만 코드 신뢰(09-gen-geocode.py:164).
-- 실행: psql -v ON_ERROR_STOP=1 -f build_ri_dict.sql   (인라인 -c 금지)
-- ※ dollar-quote($build_ri$)는 *.sql 파일 내부이며 psql -f 가 직접 파싱 → zsh _safe_eval 무관.

\timing on

-- DROP/CREATE/INSERT 를 단일 트랜잭션으로 원자화한다.
-- 가드가 RAISE EXCEPTION 하면 트랜잭션이 abort 되어 DROP 까지 롤백 → **기존 사전이 보존된다.**
-- (구축 실패 시 사전이 사라지는 사고를 막는다. 이전 판의 TRUNCATE-in-DO 와 동일한 보호 수준.)
BEGIN;

DROP TABLE IF EXISTS lawd_ri;
CREATE TABLE lawd_ri (
  emd_cd char(8) NOT NULL,     -- 읍면동 8자리 (bcode 앞 8 — 현행 체계)
  ri_cd  char(2) NOT NULL,     -- 리 2자리 (읍면동 내 일련번호, bd_mgt_sn 9-10)
  ri     text    NOT NULL,
  PRIMARY KEY (emd_cd, ri_cd, ri)
);
-- 조회 경로 전용 인덱스: WHERE ri = %s AND emd_cd = ANY(...)
CREATE INDEX lawd_ri_ri_emd_idx ON lawd_ri (ri, emd_cd);

DO $build_ri$
DECLARE
  n_rows   bigint;
  lo       constant bigint := 15000;   -- 하한: address.ri 미적재/부분적재 차단 (실측 16,113)
  hi       constant bigint := 20000;   -- 상한: 코드체계 오염 차단 (법정리 총수 ~19,000 을 상회)
BEGIN
  -- 1) 후보 키 수 사전 집계 (INSERT 와 완전히 동일한 식·동일한 WHERE)
  --    btrim 통일: 선행공백 1칸이면 substr 자리가 밀려 emd_cd·ri_cd 가 통째로 어긋난다.
  SELECT count(*) INTO n_rows FROM (
    SELECT DISTINCT left(btrim(a.bcode),8),
                    substr(btrim(a.bd_mgt_sn),9,2),
                    a.ri
      FROM address a
     WHERE a.kind='addr' AND a.ri IS NOT NULL AND a.ri <> ''
       AND a.bcode      IS NOT NULL AND length(btrim(a.bcode))      >= 8
       AND a.bd_mgt_sn  IS NOT NULL AND length(btrim(a.bd_mgt_sn))  >= 10
       AND substr(btrim(a.bd_mgt_sn),9,2) <> '00'   -- '00' = 리 없음(읍면동 직할)
  ) s;

  RAISE NOTICE 'build_ri_dict: would_rows=% (허용 [%, %])', n_rows, lo, hi;

  -- 2) 양방 가드 — 하한 미달 = address.ri 미적재 의심, 상한 초과 = 코드체계 오염 의심.
  IF n_rows < lo OR n_rows > hi THEN
    RAISE EXCEPTION 'build_ri_dict ABORT: would_rows=% 가 허용범위 [%, %] 밖. address.ri 적재 상태 확인 필요 — INSERT 거부(기존 사전 보존).',
      n_rows, lo, hi;
  END IF;

  -- 3) 적재. PK 가 3컬럼이므로 tie-break 가 불필요하다 — 버려지는 리 이름이 없다.
  INSERT INTO lawd_ri(emd_cd, ri_cd, ri)
  SELECT DISTINCT left(btrim(a.bcode),8)::char(8),
                  substr(btrim(a.bd_mgt_sn),9,2)::char(2),
                  a.ri
    FROM address a
   WHERE a.kind='addr' AND a.ri IS NOT NULL AND a.ri <> ''
     AND a.bcode      IS NOT NULL AND length(btrim(a.bcode))      >= 8
     AND a.bd_mgt_sn  IS NOT NULL AND length(btrim(a.bd_mgt_sn))  >= 10
     AND substr(btrim(a.bd_mgt_sn),9,2) <> '00';
END
$build_ri$;

COMMIT;

ANALYZE lawd_ri;

-- 자기검증 0: lawd_dong 축 정합률 — 실제 조회 경로(geocode-api-pg.py 변경 ⑤)가 타는 축.
-- 이 값이 낮으면 lawd_ri 를 아무리 잘 만들어도 조회가 0건이 되어 기능이 무효다.
-- 기준: pct < 99 면 P1b 진행 금지.  실측 기대치: 100.00
SELECT count(*) FILTER (WHERE ok) AS matched, count(*) AS keys,
       round(100.0 * count(*) FILTER (WHERE ok) / nullif(count(*),0), 2) AS pct
  FROM (SELECT EXISTS (SELECT 1 FROM lawd_dong d WHERE d.emd_cd = r.emd_cd) AS ok
          FROM lawd_ri r) t;

-- 자기검증 1: parcel 축 정합률.  기준: pct < 99 면 중단.  실측 기대치: 100.00
SELECT count(*) FILTER (WHERE ok) AS joinable, count(*) AS keys,
       round(100.0 * count(*) FILTER (WHERE ok) / nullif(count(*),0), 2) AS pct
  FROM (SELECT EXISTS (SELECT 1 FROM parcel p
                        WHERE p.sido_cd = left(r.emd_cd,2)::char(2)   -- 파티션 pruning 필수
                          AND p.emd_cd  = r.emd_cd) AS ok
          FROM lawd_ri r) t;

-- ── 어긋난 키의 시도접두 분포 (자기검증 0·1 이 99 미만일 때 첨부해 보고) ──
--    폐기축에서는 여기에 12개 시도 1,407건이 찍혔다. 현행축 실측은 0행.
SELECT left(emd_cd,2) AS sido2, count(*)
  FROM lawd_ri r
 WHERE NOT EXISTS (SELECT 1 FROM parcel p
                    WHERE p.sido_cd = left(r.emd_cd,2)::char(2)
                      AND p.emd_cd  = r.emd_cd)
 GROUP BY 1 ORDER BY 2 DESC;

-- ── 자기검증 2: 총 키 수 / 리 이름 종수.  실측 기대치: 16,113 / 6,944 ──
SELECT count(*) AS keys, count(DISTINCT ri) AS distinct_ri FROM lawd_ri;

-- ── 자기검증 3: 동일 (emd_cd, ri_cd) 에 복수 리 이름 (기록용, 중단 사유 아님) ──
--    실측 646행. 3컬럼 PK 이므로 어느 쪽도 버려지지 않는다(소실 0).
--    폐기축은 2컬럼 PK 여서 같은 성격의 충돌에서 27건이 실제로 소실됐다.
SELECT emd_cd, ri_cd, string_agg(ri, ' | ' ORDER BY ri) AS ri_names, count(*) AS n
  FROM lawd_ri
 GROUP BY emd_cd, ri_cd
HAVING count(*) > 1
 ORDER BY emd_cd, ri_cd;

-- ── 자기검증 3b: 동일 (emd_cd, ri) 에 복수 리코드 (기록용, 중단 사유 아님) ──
--    실측 534건. 조회가 두 쌍을 모두 반환하고 parcel.pnu 리코드가 최종 판별하므로 무해하다.
SELECT count(*) AS multi_ricd_pairs FROM (
  SELECT emd_cd, ri FROM lawd_ri GROUP BY emd_cd, ri HAVING count(*) > 1
) s;

-- ── 자기검증 4: 청평리 존재 확인 (기준 1 의 사전 조건) ───────────────────
--    실측 기대치: 41820325|21 (가평군 청평면), 51110380|22 (춘천시)
SELECT emd_cd, ri_cd FROM lawd_ri WHERE ri = '청평리' ORDER BY emd_cd;
