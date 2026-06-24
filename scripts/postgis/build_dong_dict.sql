-- build_dong_dict.sql — lawd_dong 멱등 재생성 (address.bcode 파생)
-- 근거: spec C2 / review X-1·A-4(HIGH). emd_cd char(8)=left(btrim(bcode),8).
-- 데이터소스: address(kind='addr', bcode) — kind='addr' 행만 bcode 신뢰(09-gen-geocode.py:164).
-- 실행: psql -v ON_ERROR_STOP=1 -f build_dong_dict.sql  (zsh 회피: 파일+-f, 인라인 -c 금지)
-- ※ dollar-quote($build_dong$)는 *.sql 파일 내부이며 psql -f 가 직접 파싱 → zsh _safe_eval 무관.

\timing on

-- 가드+TRUNCATE+INSERT 를 단일 트랜잭션 + plpgsql DO 로 원자화.
-- would_rows 임계 밖이면 RAISE EXCEPTION → 트랜잭션 abort(TRUNCATE 롤백, 빈 테이블 잔존 방지).
DO $build_dong$
DECLARE
  n_rows   bigint;
  lo       constant bigint := 4794;   -- 5046 * 0.95 (±5% 하한)
  hi       constant bigint := 5298;   -- 5046 * 1.05 (±5% 상한)
BEGIN
  -- 1) 후보 행수(emd_cd 유일) 사전 집계.
  --    R1: 가드·INSERT 양쪽 모두 left(btrim(bcode),8) 로 통일(트림 비대칭 제거 →
  --        선행공백 bcode 의 emd_cd 변형·char(8) 패딩 불일치 차단). length(btrim)>=8 유지.
  SELECT count(DISTINCT left(btrim(bcode),8))
    INTO n_rows
    FROM address
   WHERE kind = 'addr'
     AND bcode IS NOT NULL
     AND length(btrim(bcode)) >= 8
     AND emd IS NOT NULL;

  RAISE NOTICE 'build_dong_dict: would_rows=% (허용 [%, %])', n_rows, lo, hi;

  -- 2) would_rows 가드 — 임계 밖이면 거부(무음 누락 차단).
  --    R3: ±5%(약 252행)는 대형 시도 결손 차단용 임계다. 소형 시도(세종 등) 단독 결손은
  --        5% 미만이라 이 가드의 사각이며, A3 QC(ji_main>=95%·골든셋)가 보완한다(책임경계).
  IF n_rows < lo OR n_rows > hi THEN
    RAISE EXCEPTION 'build_dong_dict ABORT: would_rows=% 가 허용범위 [%, %] 밖. address 적재 누락 의심 — INSERT 거부.',
      n_rows, lo, hi;
  END IF;

  -- 3) 멱등 재적재 (emd_cd 당 1행: DISTINCT ON + tie-break 결정성)
  TRUNCATE lawd_dong;
  INSERT INTO lawd_dong(emd_cd, sido, sigungu, emd)
  SELECT DISTINCT ON (left(btrim(bcode),8))
         left(btrim(bcode),8)::char(8) AS emd_cd,
         sido, sigungu, emd
    FROM address
   WHERE kind = 'addr'
     AND bcode IS NOT NULL
     AND length(btrim(bcode)) >= 8
     AND emd IS NOT NULL
   ORDER BY left(btrim(bcode),8), emd, sigungu, sido;   -- tie-break: emd_cd 외 표기를 결정적으로 1개 선택
END
$build_dong$;

ANALYZE lawd_dong;
