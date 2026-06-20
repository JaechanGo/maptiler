-- CUVIA PostGIS 백본 — 코어 테이블: 행정구역 / 도로(분석) / 주소 (Phase 0)
-- 필수 레이어. 지오메트리 SRID 4326. 적재는 Phase 1(loaders).

-- ── 행정구역 경계 (시도/시군구/읍면동) ─────────────────────────────
-- 무인증 대체(SGIS/gisdeveloper) 또는 VWorld SHP → ogr2ogr. 역지오코딩 areas 와 동일 역할.
CREATE TABLE IF NOT EXISTS admin_boundary (
    id        bigserial PRIMARY KEY,
    level     text NOT NULL,            -- 'sido' | 'sigungu' | 'emd'(법정동) | 'adm_dong'(행정동)
    code      text,                     -- 행정/법정동 코드
    name      text,                     -- 명칭(리프, 예: '상동')
    full_name text,                     -- 전체경로(예: '경기도 부천시 원미구 상동') — 적재 후 build-admin-fullname.sql 로 조립(코드 self-join). LLM/표시용
    sido_cd   char(2),                  -- 시도 2자리(코드 앞 2)
    geom      geometry(MultiPolygon, 4326) NOT NULL
);
CREATE INDEX IF NOT EXISTS admin_boundary_geom_gix ON admin_boundary USING gist (geom);
CREATE INDEX IF NOT EXISTS admin_boundary_level_idx ON admin_boundary (level);
CREATE INDEX IF NOT EXISTS admin_boundary_code_idx  ON admin_boundary (code);

-- ── 도로 (분석용) ──────────────────────────────────────────────────
-- 베이스 지도 도로는 Planetiler MBTiles(현행). 여기는 경로/근접 등 '분석'용 도로망(osm2pgsql 산출 정제).
CREATE TABLE IF NOT EXISTS road (
    id        bigserial PRIMARY KEY,
    osm_id    bigint,
    name      text,
    road_class text,                    -- motorway/trunk/primary/... (highway)
    oneway    smallint,
    -- pgRouting 대비 사전 provisioning. 적재 후 build-road-cost.sql 로 채움
    -- (length_m=ST_Length(geom::geography), speed_kph=road_class 휴리스틱, cost=통행시간, reverse_cost=oneway 반영).
    length_m     double precision,
    speed_kph    double precision,
    cost         double precision,
    reverse_cost double precision,
    geom      geometry(MultiLineString, 4326) NOT NULL
);
CREATE INDEX IF NOT EXISTS road_geom_gix  ON road USING gist (geom);
CREATE INDEX IF NOT EXISTS road_class_idx ON road (road_class);

-- ── 주소 (지오코더 원천) ───────────────────────────────────────────
-- navi DB(좌표 backbone) + OSM/지명/POI. 09-gen-geocode.py 의 places 스키마를 PostGIS로 옮긴 형태.
-- tsvector(이름)·pg_trgm(road_norm) 검색 컬럼/인덱스는 Phase 5(지오코더 전환)에서 추가.
CREATE TABLE IF NOT EXISTS address (
    id        bigserial PRIMARY KEY,
    kind      text,                     -- addr|station|place|dong|road|poi|biz|facility
    name      text,
    subtype   text,
    sido      text, sigungu text, emd text,
    road      text, road_norm text,
    main_no   int,  sub_no int,
    bld       text, postal text,
    haeng_dong text, bd_mgt_sn text, bcode text, hcode text,
    phone     text, opened text, jibun text,
    cat1      text, cat2 text,
    source    text,                     -- navi|osm|localdata|sangga|facility
    is_primary smallint,
    geom      geometry(Point, 4326),
    -- 통합 검색용 생성컬럼(이름+도로명+지번). trgm 인덱스는 11-address-search.sql.
    search_text text GENERATED ALWAYS AS (
        trim(coalesce(name,'') || ' ' || coalesce(road,'') || ' ' || coalesce(jibun,''))
    ) STORED
);
CREATE INDEX IF NOT EXISTS address_geom_gix   ON address USING gist (geom);
CREATE INDEX IF NOT EXISTS address_kind_idx   ON address (kind);
CREATE INDEX IF NOT EXISTS address_source_idx ON address (source);
