-- admin_boundary.full_name 조립 — 코드 계층 self-join (sido+sigungu+emd 모두 적재 후 실행).
-- 예: 법정동 '상동'(code 4119210700) → '경기도 부천시 원미구 상동'.
-- 상위 레벨 미적재 시 그 부분 공란(리프 이름으로 degrade). 행정동(adm_dong)은 코드체계(SGIS)가 달라 시도까지만 정확.
--   사용: psql -f scripts/postgis/build-admin-fullname.sql  (load_admin 으로 시도/시군구/읍면동 적재 후)

UPDATE admin_boundary t SET full_name = trim(regexp_replace(
    coalesce((SELECT name FROM admin_boundary WHERE level='sido'    AND left(code,2)=left(t.code,2) LIMIT 1),'') || ' ' ||
    coalesce((SELECT name FROM admin_boundary WHERE level='sigungu' AND left(code,5)=left(t.code,5) LIMIT 1),'') || ' ' ||
    coalesce(t.name,''), '\s+', ' ', 'g'))
WHERE t.level IN ('emd', 'adm_dong');

-- 상위 레벨 자기자신
UPDATE admin_boundary SET full_name = name WHERE level IN ('sido', 'sigungu');

-- 결과 확인
SELECT level, count(*) AS n, count(full_name) AS with_full FROM admin_boundary GROUP BY level ORDER BY level;
