-- T026 §9-4 (a) 인천 외 회귀 가드용 표본 추출 (읽기 전용).
--
-- 목적: 인천(28)·전남광주 계열(46·29) 과 무관한 주소가 이번 변경으로
--       단 한 바이트도 달라지지 않음을 증명할 고정 표본을 만든다.
--
-- 결정성(무작위 금지): ORDER BY md5(id::text) 는 난수가 아니라 id 의 결정적 함수다.
--   ORDER BY bcode 류의 사전식 정렬은 각 시도의 첫 동에 20건이 몰려 대표성이 죽는다.
--   해시 정렬은 재현 가능하면서도 시군구·읍면동이 고루 흩어진다.
--   (추출 결과는 CSV 로 커밋되므로 이후 재현은 CSV 가 정본이다.)
--
-- 편차 기록: 계획서 §9-4 는 "15 개 시도 × 20 건 = 300 건" 을 전제하나,
--   address 의 실측 시도코드는 17 개이고 28·46·29 를 빼면 **14 개** 다.
--   총량 300 을 맞추려면 시도별 건수가 불균등해지므로, 계획서가 명시한
--   "시도별 균등 20 건" 쪽을 지켜 14 × 20 = **280 건** 으로 산출한다.
COPY (
  WITH base AS (
    SELECT
      substr(btrim(bcode), 1, 2) AS sido_cd,
      btrim(sido)                AS sido,
      btrim(sigungu)             AS sigungu,
      btrim(emd)                 AS emd,
      coalesce(btrim(ri), '')    AS ri,
      btrim(jibun)               AS jibun,
      btrim(bcode)               AS bcode,
      id
    FROM address
    WHERE kind = 'addr'
      AND bcode IS NOT NULL
      AND length(btrim(bcode)) = 10
      AND substr(btrim(bcode), 1, 2) NOT IN ('28', '46', '29')
      AND coalesce(btrim(sido), '')    <> ''
      AND coalesce(btrim(emd), '')     <> ''
      -- ⚠ sigungu 는 비어 있어도 통과시킨다. 세종특별자치시(36)는 시군구 계층이 없어
      --    sigungu IS NULL 이고, `<> ''` 로 거르면 **시도 하나가 통째로 가드 사각지대**가 된다.
      --    (첫 추출에서 실제로 13개 시도 260건만 나와 발견했다.)
      --    대신 질의 형태 ②("시군구 읍면동 지번")를 세종에 한해 생성하지 않는다.
      AND coalesce(btrim(jibun), '')   <> ''
      AND btrim(jibun) ~ '[0-9]'          -- 번지 없는 행은 3형태 질의가 성립하지 않는다
  ),
  -- 같은 (시도, 시군구, 지번) 이 여러 행일 수 있다(건물 다세대 등).
  -- 질의 문자열이 같으면 응답도 같으므로 중복 질의는 낭비다 → 먼저 유일화한다.
  uniq AS (
    SELECT DISTINCT ON (sido, sigungu, jibun)
           sido_cd, sido, sigungu, emd, ri, jibun, bcode, id
    FROM base
    ORDER BY sido, sigungu, jibun, id
  ),
  ranked AS (
    SELECT *, row_number() OVER (PARTITION BY sido_cd ORDER BY md5(id::text), id) AS rn
    FROM uniq
  )
  SELECT sido_cd, sido, sigungu, emd, ri, jibun, bcode
  FROM ranked
  WHERE rn <= 20
  ORDER BY sido_cd, rn
) TO STDOUT WITH CSV HEADER;
