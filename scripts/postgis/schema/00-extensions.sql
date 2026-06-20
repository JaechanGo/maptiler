-- CUVIA PostGIS 백본 — 확장 (Phase 0)
-- 모든 지오메트리는 SRID 4326(WGS84) 저장. martin이 타일 서빙 시 3857로 reproject.
-- (3857 생성컬럼 최적화는 프로파일링에서 reproject가 병목일 때만 추가 — 일반화 컬럼과 동일 YAGNI)

CREATE EXTENSION IF NOT EXISTS postgis;      -- 공간형/함수/GiST/ST_AsMVT
CREATE EXTENSION IF NOT EXISTS pg_trgm;       -- 도로명 dirty-match 유사도 (지오코더 Phase 5)
CREATE EXTENSION IF NOT EXISTS unaccent;      -- 정규화 보조
CREATE EXTENSION IF NOT EXISTS btree_gist;    -- 복합 GiST(geom + 속성) 필요 시
