-- T8: martin 함수소스 — tier_minzoom <= z 인 POI만 타일에 담음 (z15-16 디클러터, z17+ 전수).
-- 데이터 전수보존(행 삭제 없음), 표시 선택만. tier_minzoom IS NULL = 미분류는 항상 표시(누락 방지).
CREATE OR REPLACE FUNCTION public.poi_mvt(z integer, x integer, y integer)
RETURNS bytea AS $$
  WITH bounds AS (SELECT ST_TileEnvelope(z, x, y) AS env3857,
                         ST_Transform(ST_TileEnvelope(z, x, y), 4326) AS env4326)
  SELECT ST_AsMVT(t, 'poi', 4096, 'geom') FROM (
    SELECT
      ST_AsMVTGeom(ST_Transform(p.geom, 3857), b.env3857, 4096, 64, true) AS geom,
      p.kind, p.name, p.subtype, p.cat1, p.cat2, p.source, p.is_primary
    FROM poi p, bounds b
    WHERE p.geom && b.env4326
      AND (p.tier_minzoom IS NULL OR p.tier_minzoom <= z)   -- NULL=미분류는 항상 표시(누락 방지)
  ) t WHERE t.geom IS NOT NULL;
$$ LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE;

DO $do$ BEGIN
  EXECUTE 'COMMENT ON FUNCTION public.poi_mvt(integer,integer,integer) IS ' || quote_literal('{"minzoom":15,"maxzoom":22}');
END $do$;
