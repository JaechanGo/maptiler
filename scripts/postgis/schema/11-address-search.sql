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

-- 지역(sido/sigungu/emd) 부분 trgm GIN — 다중토큰 이름경로의 지역 arm 인덱스화(BitmapOr 형성).
-- 없으면 지역명 토큰('경기도'·'화성시'·'장안면')이 비인덱스 ILIKE '%t%' 라 OR 전체가 비인덱스화 →
-- 1600만행 Parallel Seq Scan(실측 5.86s, 타임아웃 다수). 세 arm 인덱스화 시 BitmapOr→BitmapAnd(실측 27ms).
-- kind<>'addr' 부분(비-addr ~560만행) — 이름경로가 kind<>'addr' 만 검색하므로 인덱스도 동일 조건으로 축소.
CREATE INDEX IF NOT EXISTS address_sido_trgm    ON address USING gin (sido    gin_trgm_ops) WHERE kind <> 'addr';
CREATE INDEX IF NOT EXISTS address_sigungu_trgm ON address USING gin (sigungu gin_trgm_ops) WHERE kind <> 'addr';
CREATE INDEX IF NOT EXISTS address_emd_trgm     ON address USING gin (emd     gin_trgm_ops) WHERE kind <> 'addr';

-- 우편번호(postal) 정확매칭 — 5자리 신우편번호 검색('06236' 등) 경로. addr 한정 btree.
-- 없으면 postal=%s 가 1068만 addr 행 Seq Scan. (geocode-api-pg.py 우편번호 경로와 한 쌍)
CREATE INDEX IF NOT EXISTS address_postal_idx   ON address (postal) WHERE kind = 'addr';

-- 합성 PNU 키조인 — 역지오코딩에서 PIP 로 확정한 필지(parcel.pnu) 안의 주소점을 집는 경로.
-- (geocode-api-pg.py road_at_parcel() 과 한 쌍. 없으면 CTE 안이 1069만행 Seq Scan)
--
-- 키가 raw bd_mgt_sn 앞 19자가 아니라 bcode||substr(bd_mgt_sn,11,9) 인 이유:
--   bd_mgt_sn 앞 10자리에는 '폐지된' 법정동코드가 박혀 있다(예: 4571041023 전라북도 완주군
--   exist=f / 5271041023 전북특별자치도 완주군 exist=t). raw 로 맞추면 완주군 매칭률이 2.8%
--   까지 무너진다. bcode 를 신뢰하고 앞 10자리를 버리면 97.2% 로 회복된다.
-- 표현식 인덱스인 이유: 합성 PNU 는 컬럼이 아니고, GENERATED 컬럼 추가는 5.7GB 테이블 재작성이다.
--   ||(text) 와 substr(text,int,int) 는 IMMUTABLE 이라 인덱스 가능.
-- 부분 인덱스인 이유: addr(1069만행)만 bd_mgt_sn·bcode 가 100% 충전돼 있다. biz(491만)는 bcode
--   가 전부 NULL, poi/road/facility/place/station(~68만)은 둘 다 NULL 이라 애초에 키가 없다.
--   이 술어가 질의쪽 kind='addr' 강제를 인덱스 층에서 한 번 더 거는 이중 장치를 겸한다.
-- ※ 이 인덱스를 실제로 태우려면 질의가 WITH cand AS MATERIALIZED (…) 형태여야 한다.
--   평문이나 MATERIALIZED 없는 CTE 는 ORDER BY geom <-> pt 때문에 플래너가
--   address_addr_geom_gix 를 골라 버리고 합성키를 Filter 로 강등시킨다(실측).
CREATE INDEX IF NOT EXISTS address_synth_pnu_idx
    ON address ((bcode || substr(bd_mgt_sn, 11, 9))) WHERE kind = 'addr';

-- 같은 표현식의 '독립' 통계 객체. 인덱스가 있는데도 왜 또 필요한가:
--   표현식 인덱스는 ANALYZE 때 자체 통계를 갖지만, 플래너는 그것을 '부분 인덱스일 때는 쓰지
--   않는다'(selfuncs.c examine_variable(): partial index 통계는 전체 릴레이션을 대변하지 못하므로
--   배제). 위 인덱스는 WHERE kind='addr' 부분 인덱스라 정확히 이 배제에 걸린다.
--   결과로 등호 선택도가 DEFAULT_EQ_SEL(0.005) 로 고정된다 — 1076만 addr × 0.005 = 53,815행 추정
--   (실제 1행, 5.4만 배 과대). 그 추정으로 플래너가 병렬 스캔을 깔아 Gather 워커 기동에만
--   35~40ms 를 쓴다(실측). 필지당 1행 집는 질의에 워커 2개가 붙는 셈이다.
-- CREATE STATISTICS(PG14+ 단일 표현식 지원)는 인덱스와 무관한 독립 객체라 위 배제를 받지 않는다.
--   적용 후 추정 rows=1 → 비병렬 Index Scan → 실행 75ms → 0.12ms(실측, 동일 좌표·동일 PNU).
CREATE STATISTICS IF NOT EXISTS address_synth_pnu_stat
    ON (bcode || substr(bd_mgt_sn, 11, 9)) FROM address;

-- 표현식 인덱스·확장통계는 모두 ANALYZE 로만 채워진다. 생성 직후 1회 필요.
ANALYZE address;
