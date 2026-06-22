-- CUVIA PostGIS — 지오코더 검색 인덱스 (Phase 5)
-- address 적재(load_geocode.py) 후 적용 권장(대량 적재 뒤 인덱스 생성이 빠름).
-- pg_trgm(이름 부분일치) + 도로명주소 정밀매칭(road_norm,main/sub) + addr 전용 부분 GiST(역지오코딩).

-- 통합 검색(이름+도로명+지번 = search_text 생성컬럼) — 부분일치 ILIKE '%term%' 가속
CREATE INDEX IF NOT EXISTS address_search_trgm ON address USING gin (search_text gin_trgm_ops);

-- 건물명(bld) 부분일치 — 이름검색 분기 `search_text ILIKE x OR bld ILIKE x` 의 bld arm.
-- 없으면 OR 가 BitmapOr 로 안 묶여 1570만행 Seq Scan(구청/역 이름검색 2~11초). load_geocode.py 와 동기 유지.
CREATE INDEX IF NOT EXISTS address_bld_trgm ON address USING gin (bld gin_trgm_ops);

-- 도로명주소 경로 — road_norm 정확 + 본번/부번. addr 만(전체의 대부분이지만 partial 로 명시)
CREATE INDEX IF NOT EXISTS address_road_addr_idx
    ON address (road_norm, main_no, sub_no) WHERE kind = 'addr';

-- 역지오코딩 최근접(addr) — 부분 GiST(KNN <-> 가속)
CREATE INDEX IF NOT EXISTS address_addr_geom_gix
    ON address USING gist (geom) WHERE kind = 'addr';

-- 지역 토큰 가산용(시군구/읍면동 동등비교) — 짧아서 btree 로 충분
CREATE INDEX IF NOT EXISTS address_region_idx ON address (sigungu, emd);
