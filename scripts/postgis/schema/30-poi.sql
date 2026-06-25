-- CUVIA PostGIS 백본 — POI / 공공시설 (Phase 0)
-- poi: 상가·인허가·생활편의(기존 무인증 collect) → biz/facility. 기존 poi.mbtiles(12-build-poi.sh) 대체.
-- public_facility: data.go.kr 공공시설(병원·경찰·소방·AED·대피소). 동질 포인트라 단일 테이블+kind.

CREATE TABLE IF NOT EXISTS poi (
    id        bigserial PRIMARY KEY,
    kind      text,                     -- 'biz' | 'facility'
    name      text,
    subtype   text,                     -- 업종 소분류/시설종류
    cat1      text, cat2 text,          -- 업종 대/중분류
    source    text,                     -- localdata|sangga|facility
    is_primary smallint,                -- 표시층(중복제거) 플래그
    phone     text,
    geom      geometry(Point, 4326) NOT NULL
);
CREATE INDEX IF NOT EXISTS poi_geom_gix    ON poi USING gist (geom);
CREATE INDEX IF NOT EXISTS poi_kind_idx    ON poi (kind);
CREATE INDEX IF NOT EXISTS poi_primary_idx ON poi (is_primary);

-- T8: 서버측 tier 디클러터 — tier_minzoom = 해당 POI가 타일에 처음 등장하는 줌 (theme 단일소스 백필).
ALTER TABLE poi ADD COLUMN IF NOT EXISTS tier_minzoom smallint;
CREATE INDEX IF NOT EXISTS poi_tier_idx ON poi (tier_minzoom);

-- 공공시설 — kind 로 레이어 구분(병원/경찰/소방/AED/대피소). 출처별 갱신 = DELETE WHERE kind=... + insert.
CREATE TABLE IF NOT EXISTS public_facility (
    id        bigserial PRIMARY KEY,
    kind      text NOT NULL,            -- 'hospital'|'police'|'fire_station'|'aed'|'shelter'
    name      text,
    addr      text,                     -- 도로명/지번 주소(지오코딩 원천)
    sido_cd   char(2),
    attrs     jsonb,                    -- 출처별 부가속성(전화·구분·수용인원 등)
    source    text,                     -- data.go.kr publicDataPk 등
    geom      geometry(Point, 4326)     -- 좌표 없는 출처는 NULL → 지오코더 후채움
);
CREATE INDEX IF NOT EXISTS public_facility_geom_gix ON public_facility USING gist (geom);
CREATE INDEX IF NOT EXISTS public_facility_kind_idx ON public_facility (kind);
