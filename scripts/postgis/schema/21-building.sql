-- CUVIA PostGIS 백본 — 건물통합정보 building (Phase 0)
-- ≈6.66M 동. parcel 과 동일 시도 LIST 파티션(수급·갱신 단위 일치).
-- 기존 buildings.mbtiles 방식 대체(생성 스크립트는 T028 에서 폐기). render_height/levels = 3D fill-extrusion.
-- building 은 z13+ 표출이라 parcel(z16+)보다 일반화(geom_genN)가 먼저 필요할 후보 — 단 측정 후 추가.

CREATE TABLE IF NOT EXISTS building (
    id            bigint GENERATED ALWAYS AS IDENTITY,
    bld_mgt_no    text,                 -- 건물 고유키 = GIS건물통합식별번호(AL_D010 의 A1, 28자리)
    pnu           text,
    name          text,                 -- 건물명
    road_addr     text,                 -- 도로명주소
    jibun         text,
    use_type      text,                 -- 주용도
    levels        int,                  -- 지상층수
    render_height real,                 -- 높이(m) — A16>0 ? A16 : levels*3.3 (적재 시 산정)
    sido_cd       char(2) NOT NULL,     -- 파티션 키 = left(pnu,2)
    geom          geometry(MultiPolygon, 4326) NOT NULL,
    PRIMARY KEY (id, sido_cd)
) PARTITION BY LIST (sido_cd);

DO $$
DECLARE
    rec record;
BEGIN
    FOR rec IN
        SELECT * FROM (VALUES
            ('11', ARRAY['11']), ('26', ARRAY['26']), ('27', ARRAY['27']),
            ('28', ARRAY['28']), ('29', ARRAY['29']), ('30', ARRAY['30']),
            ('31', ARRAY['31']), ('36', ARRAY['36']), ('41', ARRAY['41']),
            ('43', ARRAY['43']), ('44', ARRAY['44']), ('46', ARRAY['46']),
            ('47', ARRAY['47']), ('48', ARRAY['48']), ('50', ARRAY['50']),
            ('51', ARRAY['51','42']), ('52', ARRAY['52','45'])
        ) AS t(suffix, codes)
    LOOP
        EXECUTE format(
            'CREATE TABLE IF NOT EXISTS building_%s PARTITION OF building FOR VALUES IN (%s)',
            rec.suffix,
            (SELECT string_agg(quote_literal(c), ',') FROM unnest(rec.codes) AS c)
        );
    END LOOP;
END$$;
CREATE TABLE IF NOT EXISTS building_default PARTITION OF building DEFAULT;

CREATE INDEX IF NOT EXISTS building_geom_gix ON building USING gist (geom);
CREATE INDEX IF NOT EXISTS building_pnu_idx  ON building (pnu);
-- 중복방어 — 건물 고유키 bld_mgt_no = GIS건물통합식별번호(컬럼정의서 확정; AL_D010 의 A1, 28자리, 전건 채워짐).
--   load-all.sh 가 load_building.sh --mgt-field A1 로 적재 → ON CONFLICT (sido_cd,bld_mgt_no) 가 중복 SHP/행 방어.
--   부분 UNIQUE 라 미적재(NULL)면 빈 인덱스(무비용). A코드↔의미는 데이터 버전마다 달라질 수 있어(컬럼정의서에
--   복수 layout 존재) 변경 시 ogrinfo 로 재확인. PNU(A2)는 필지당 다건물이라 고유키 아님.
CREATE UNIQUE INDEX IF NOT EXISTS building_mgt_uix ON building (sido_cd, bld_mgt_no) WHERE bld_mgt_no IS NOT NULL;
