-- CUVIA PostGIS 백본 — 연속지적도 parcel (Phase 0)
-- ≈39.6M 필지. PARTITION BY LIST(sido_cd) 17 시도 + default.
-- 근거: VWorld 시도단위 배포 → 갱신=해당 파티션만 TRUNCATE+재적재(모놀리식 무손상),
--       파티션별 GiST 소형화, 시도필터 질의 pruning. sido_cd = left(pnu,2) (적재 시 채움).
-- 일반화(geom_genN)는 초기 미적용 — parcel은 z16+ 표출이라 single geom 으로 충분(설계만 명시).

CREATE TABLE IF NOT EXISTS parcel (
    id        bigint GENERATED ALWAYS AS IDENTITY,
    pnu       text,                     -- 필지고유번호(19)
    jibun     text,                     -- 지번 표기
    bchk      text,                     -- 대장구분
    sido_cd   char(2) NOT NULL,         -- 파티션 키 = left(pnu,2)
    sgg_cd    char(5),                  -- 시군구(법정동 앞5)
    emd_cd    char(8),                  -- 읍면동(법정동 앞8)
    geom      geometry(MultiPolygon, 4326) NOT NULL,
    PRIMARY KEY (id, sido_cd),
    UNIQUE (sido_cd, pnu)        -- PNU=필지 고유키(파티션키 sido_cd 포함 필수). 중복 PNU 는 적재 시 ON CONFLICT DO NOTHING
) PARTITION BY LIST (sido_cd);

-- 17 시도 파티션 + default. 강원(51/구42)·전북(52/구45)은 신·구 코드 모두 수용.
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
            'CREATE TABLE IF NOT EXISTS parcel_%s PARTITION OF parcel FOR VALUES IN (%s)',
            rec.suffix,
            (SELECT string_agg(quote_literal(c), ',') FROM unnest(rec.codes) AS c)
        );
    END LOOP;
END$$;
CREATE TABLE IF NOT EXISTS parcel_default PARTITION OF parcel DEFAULT;

-- 파티션 부모에 GiST 생성 → 모든 파티션에 전파(PG11+).
CREATE INDEX IF NOT EXISTS parcel_geom_gix ON parcel USING gist (geom);
CREATE INDEX IF NOT EXISTS parcel_pnu_idx  ON parcel (pnu);
