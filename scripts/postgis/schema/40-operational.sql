-- CUVIA PostGIS 백본 — 운영·위험 레이어 (Phase 0)
-- cctv/event: 자체구축 운영 테이블(런타임 적재). safemap: 위험지역(인터넷망 수집→GeoJSON→반입).

-- ── CCTV (자체구축) ────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS cctv (
    id        bigserial PRIMARY KEY,
    name      text,
    azimuth   real,                     -- 방위각(도)
    fov       real,                     -- 화각(도)
    height_m     real,                  -- 설치높이(m) — 시야각/가시권 계산
    max_distance real,                  -- 유효 관측거리(m) — ST_Buffer 시야 부채꼴 반경
    purpose   text,                     -- 용도(방범/교통 등)
    attrs     jsonb,
    geom      geometry(Point, 4326) NOT NULL
);
CREATE INDEX IF NOT EXISTS cctv_geom_gix ON cctv USING gist (geom);

-- (event 테이블 제거: 외부 데이터소스 없는 추정 테이블이라 삭제. 관제 상황기록이 실제 필요해지면
--  그 시점의 요구 컬럼으로 재설계해 추가하는 게 맞음 — YAGNI.)

-- ── 위험지역 SafeMap 계열 (범죄/침수/여성안심/재난) ───────────────
-- ※ SafeMap openAPI 는 WMS 래스터라 직접 못 씀. 각 원천 표준데이터를 GeoJSON 으로 수집해 반입.
-- 폴리곤/라인/포인트 혼재 가능 → geometry(Geometry).
CREATE TABLE IF NOT EXISTS safemap (
    id        bigserial PRIMARY KEY,
    kind      text NOT NULL,            -- 'crime'|'flood'|'women_safe'|'disaster'
    name      text,
    grade     text,                     -- 위험등급
    attrs     jsonb,
    source    text,
    geom      geometry(Geometry, 4326) NOT NULL
);
CREATE INDEX IF NOT EXISTS safemap_geom_gix ON safemap USING gist (geom);
CREATE INDEX IF NOT EXISTS safemap_kind_idx ON safemap (kind);
