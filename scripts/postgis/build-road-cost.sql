-- road 라우팅 컬럼 채움 — osm2pgsql 분석용 도로 적재 후 실행. pgRouting 대비.
--   length_m=실거리, speed_kph=road_class 휴리스틱, cost=통행시간(초), reverse_cost=oneway 반영(-1=역방향 불가).
--   사용: psql -f scripts/postgis/build-road-cost.sql
-- 이후 pgRouting: CREATE EXTENSION pgrouting; SELECT pgr_createTopology('road', ...); 로 토폴로지 생성.

UPDATE road SET
  length_m  = ST_Length(geom::geography),
  speed_kph = CASE road_class
                WHEN 'motorway'    THEN 100 WHEN 'trunk'     THEN 80
                WHEN 'primary'     THEN 60  WHEN 'secondary' THEN 50
                WHEN 'tertiary'    THEN 40  WHEN 'residential' THEN 30
                WHEN 'service'     THEN 20  ELSE 30 END;

UPDATE road SET
  cost         = length_m / NULLIF(speed_kph, 0) / (1000.0 / 3600.0),                 -- 통행시간(초)
  reverse_cost = CASE WHEN oneway = 1 THEN -1                                          -- 일방통행 역방향 불가
                      ELSE length_m / NULLIF(speed_kph, 0) / (1000.0 / 3600.0) END;

SELECT count(*) AS roads, round(avg(length_m)::numeric,1) AS avg_len_m,
       count(*) FILTER (WHERE reverse_cost = -1) AS oneway FROM road;
