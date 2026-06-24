-- parcel.jibun → (san, ji_main, ji_sub) 정규화 + geom_pt 대표점 백필
-- 단일 패스(테이블 1회 rewrite): san은 선두문자 비교, 번지는 regexp_match 1회, geom_pt 동시.
-- 근거: docs/geocode-jibun-parcel-plan.md §2.1
\timing on
SET maintenance_work_mem = '512MB';
-- ※ int4 오버플로 가드(length<=7): 일부 비정상 jibun 에 10자리+ 숫자가 있으면 ::int 캐스팅이
--   에러나 UPDATE 전체 롤백(경북/충남 100% NULL 버그). 본번/부번은 최대 5자리라 7자리 가드면 안전.
UPDATE parcel p SET
  san     = CASE WHEN left(btrim(p.jibun),1) = '산' THEN 1 ELSE 0 END,
  ji_main = (s.m)[1]::int,
  ji_sub  = COALESCE((s.m)[2], '0')::int,
  geom_pt = ST_PointOnSurface(p.geom)
FROM (
  SELECT id, sido_cd, regexp_match(jibun, '(\d+)(?:-(\d+))?') AS m
  FROM parcel WHERE jibun IS NOT NULL
) s
WHERE p.id = s.id AND p.sido_cd = s.sido_cd AND s.m IS NOT NULL
  AND length((s.m)[1]) <= 7
  AND ((s.m)[2] IS NULL OR length((s.m)[2]) <= 7);

ANALYZE parcel;
