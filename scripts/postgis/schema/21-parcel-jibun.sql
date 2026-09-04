-- CUVIA PostGIS — parcel 지번검색 정규화 (Phase 6 지번 1급화)
-- 근거: docs/geocode-jibun-parcel-plan.md
-- parcel.jibun('[산][공백]본번[-부번][지목문자]', 동명 미포함)을 정규화 컬럼으로 풀어
-- (emd_cd, ji_main, ji_sub) B-tree 정확매칭 + geom_pt 대표점 반환으로 지번검색을 1급 경로화.
-- 동명↔emd_cd 해소는 lawd_dong(정확등가 매칭). 멱등 — 반복 적용 안전.

-- ── parcel 정규화 컬럼 (파티션 부모에 ADD → 전 파티션 전파) ─────────
ALTER TABLE parcel ADD COLUMN IF NOT EXISTS ji_main int;       -- 본번 (jibun 파싱)
ALTER TABLE parcel ADD COLUMN IF NOT EXISTS ji_sub  int;       -- 부번 (없으면 0)
ALTER TABLE parcel ADD COLUMN IF NOT EXISTS san     smallint;  -- 산(임야) 여부 0/1
ALTER TABLE parcel ADD COLUMN IF NOT EXISTS geom_pt geometry(Point,4326);  -- ST_PointOnSurface(geom) 대표점

-- ── 법정동명 → emd_cd 해소 사전 ───────────────────────────────────
-- parcel 엔 emd_cd(8자리, =법정동코드 앞8)만 있고 동명이 없다. address.bcode(법정동코드)에서 파생.
-- 질의: 동명 정확등가(emd=%s) → emd_cd 후보 → parcel 본질의. (ILIKE '%동%' 금지: 2자 동명 Seq Scan 차단)
CREATE TABLE IF NOT EXISTS lawd_dong (
    emd_cd  char(8) PRIMARY KEY,
    sido    text,
    sigungu text,
    emd     text
);
CREATE INDEX IF NOT EXISTS lawd_dong_emd_idx ON lawd_dong (emd);  -- 동명 정확매칭 가속

-- ── 지번 정확 lookup 인덱스 ───────────────────────────────────────
-- 부모에 생성 → 전 파티션 전파(PG11+). sido_cd(파티션키) 동반질의 시 pruning.
-- ※ 대량 백필 후 생성이 효율적 — 운영 적용은 load/backfill 완료 뒤 이 구문만 별도 실행 권장.
CREATE INDEX IF NOT EXISTS parcel_jibun_lookup ON parcel (emd_cd, ji_main, ji_sub);
