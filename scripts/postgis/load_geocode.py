#!/usr/bin/env python3
"""geocode.sqlite(09-gen-geocode.py 산출) → PostGIS address + poi 적재 (Phase 1 데이터 / Phase 4 POI).

핵심 재사용: navi DB·OSM·상가·인허가·생활편의의 파싱·좌표변환(EPSG:5179/5174→4326)·시설 지오코딩·
dedup(is_primary)이 이미 geocode.sqlite `places`(lon/lat=4326)에 다 들어있다. 원천을 재파싱하지 말고
이 산출물을 그대로 PostGIS 로 옮긴다 → 기존 품질과 100% parity. (지오코더 질의 전환은 Phase 5.)

  places(kind,name,subtype,sido,sigungu,emd,ri,road,road_norm,main_no,sub_no,bld,postal,
         haeng_dong,bd_mgt_sn,bcode,hcode,phone,opened,jibun,cat1,cat2,source,is_primary,lon,lat)
    → address (전체)          : 검색/표시/역지오코딩 원천
    → poi     (kind in biz/facility, 좌표有) : martin POI 레이어(기존 poi.mbtiles 대체)

연결은 libpq 환경변수(PGHOST/PGPORT/PGUSER/PGDATABASE/PGPASSWORD). 기본 cuvia/cuvia@localhost:5433(맵 전용 컨테이너 호스트포트).
  PGPASSWORD=... scripts/postgis/load_geocode.py [--db ~/geocode-build/geocode.sqlite]
"""
import argparse, csv, os, re, subprocess, sys, tempfile, time

COLS = ["kind","name","subtype","sido","sigungu","emd","ri","road","road_norm","main_no","sub_no",
        "bld","postal","haeng_dong","bd_mgt_sn","bcode","hcode","phone","opened","jibun",
        "cat1","cat2","source","is_primary","lon","lat"]


def export_csv(db_path, csv_path):
    import sqlite3
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    cur = con.execute(f"SELECT {','.join(COLS)} FROM places")
    n = 0
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        while True:
            rows = cur.fetchmany(50000)
            if not rows:
                break
            w.writerows(rows)
            n += len(rows)
    con.close()
    return n


# 모든 컬럼 TEXT 스테이징 → INSERT 에서 캐스팅(빈칸=NULL). lon/lat 있으면 geom 생성.
# 무손실 적재 가속(load_parcel/building 과 동일 전략) — 결과 불변, 속도만 단축:
#  · synchronous_commit=off: 적재 세션 커밋 fsync 대기 제거(벌크 재적재라 안전).
#  · 대량 INSERT(1,600만+) 전 2차 인덱스 DROP → 적재 후 일괄 재생성. 살아있는 trgm GIN·GiST 의
#    행단위 증분이 최대 병목 — 벌크빌드가 훨씬 빠르고 인덱스도 덜 부푼다. PK(bigserial 순차)는 저렴해 유지.
#  · ※스키마(10-base / 11-address-search / 30-poi)와 동기 유지 — 인덱스 추가/변경 시 아래 DROP/CREATE 목록도 갱신.
SQL = r"""
\set ON_ERROR_STOP on
SET synchronous_commit = off;

DROP TABLE IF EXISTS _stg_places;
CREATE UNLOGGED TABLE _stg_places (
  kind text, name text, subtype text, sido text, sigungu text, emd text, ri text,
  road text, road_norm text, main_no text, sub_no text, bld text, postal text,
  haeng_dong text, bd_mgt_sn text, bcode text, hcode text, phone text, opened text,
  jibun text, cat1 text, cat2 text, source text, is_primary text, lon text, lat text
);
\copy _stg_places FROM '__CSV__' WITH (FORMAT csv)

TRUNCATE address;
TRUNCATE poi;

-- 적재용 2차 인덱스 DROP (PK address_pkey/poi_pkey 는 유지)
DROP INDEX IF EXISTS address_geom_gix;
DROP INDEX IF EXISTS address_kind_idx;
DROP INDEX IF EXISTS address_source_idx;
DROP INDEX IF EXISTS address_search_trgm;
DROP INDEX IF EXISTS address_bld_trgm;
DROP INDEX IF EXISTS address_road_addr_idx;
DROP INDEX IF EXISTS address_addr_geom_gix;
DROP INDEX IF EXISTS address_region_idx;
DROP INDEX IF EXISTS address_synth_pnu_idx;
-- 위 인덱스와 같은 표현식에 묶인 확장통계도 반드시 함께 DROP 한다. 아래 재생성이
-- CREATE STATISTICS IF NOT EXISTS 라, 남겨 두면 표현식을 바꿔도 통계만 옛 정의에 묶인 채
-- 살아남는다 — 인덱스는 새 정의로 갱신되므로 결과는 맞는데 선택도 추정만 DEFAULT_EQ_SEL
-- 로 되돌아가 조용히 병렬 스캔이 부활한다. 둘을 한 블록에 두어 동시 재생성을 보장한다.
DROP STATISTICS IF EXISTS address_synth_pnu_stat;
DROP INDEX IF EXISTS poi_geom_gix;
DROP INDEX IF EXISTS poi_kind_idx;
DROP INDEX IF EXISTS poi_primary_idx;

INSERT INTO address(kind,name,subtype,sido,sigungu,emd,ri,road,road_norm,main_no,sub_no,bld,postal,
                    haeng_dong,bd_mgt_sn,bcode,hcode,phone,opened,jibun,cat1,cat2,source,is_primary,geom)
SELECT kind,name,subtype,sido,sigungu,emd,ri,road,road_norm,
       nullif(main_no,'')::int, nullif(sub_no,'')::int, bld, postal,
       haeng_dong,bd_mgt_sn,bcode,hcode,phone,opened,jibun,cat1,cat2,source,
       nullif(is_primary,'')::smallint,
       CASE WHEN nullif(lon,'') IS NOT NULL AND nullif(lat,'') IS NOT NULL
            THEN ST_SetSRID(ST_MakePoint(lon::float8, lat::float8),4326) END
FROM _stg_places;

INSERT INTO poi(kind,name,subtype,cat1,cat2,source,is_primary,phone,geom)
SELECT kind,name,subtype,cat1,cat2,source, nullif(is_primary,'')::smallint, phone,
       ST_SetSRID(ST_MakePoint(lon::float8, lat::float8),4326)
FROM _stg_places
WHERE kind IN ('biz','facility') AND nullif(lon,'') IS NOT NULL AND nullif(lat,'') IS NOT NULL;

DROP TABLE _stg_places;

-- 인덱스 일괄 재생성(대량 적재 후). trgm GIN 은 maintenance_work_mem 에 가장 민감.
SET maintenance_work_mem = '__MWM__';
SET max_parallel_maintenance_workers = __MPW__;   -- btree 한정 가속(GIN/GiST 는 무시)
CREATE INDEX IF NOT EXISTS address_geom_gix      ON address USING gist (geom);
CREATE INDEX IF NOT EXISTS address_kind_idx      ON address (kind);
CREATE INDEX IF NOT EXISTS address_source_idx    ON address (source);
CREATE INDEX IF NOT EXISTS address_search_trgm   ON address USING gin (search_text gin_trgm_ops);
CREATE INDEX IF NOT EXISTS address_bld_trgm      ON address USING gin (bld gin_trgm_ops);
CREATE INDEX IF NOT EXISTS address_road_addr_idx ON address (road_norm, main_no, sub_no) WHERE kind = 'addr';
CREATE INDEX IF NOT EXISTS address_addr_geom_gix ON address USING gist (geom) WHERE kind = 'addr';
CREATE INDEX IF NOT EXISTS address_region_idx    ON address (sigungu, emd);
-- 합성 PNU 키조인(역지오 도로명축). 표현식+부분 인덱스 — 근거는 11-address-search.sql 주석 참조.
CREATE INDEX IF NOT EXISTS address_synth_pnu_idx ON address ((bcode || substr(bd_mgt_sn, 11, 9))) WHERE kind = 'addr';
-- 위 인덱스는 '부분'이라 플래너가 그 표현식 통계를 안 쓴다(선택도 0.005 고정 → 불필요한 병렬 스캔).
-- 독립 통계 객체로 보정한다. 아래 ANALYZE address 가 채워 준다. 근거는 11-address-search.sql 주석 참조.
CREATE STATISTICS IF NOT EXISTS address_synth_pnu_stat ON (bcode || substr(bd_mgt_sn, 11, 9)) FROM address;
CREATE INDEX IF NOT EXISTS poi_geom_gix          ON poi USING gist (geom);
CREATE INDEX IF NOT EXISTS poi_kind_idx          ON poi (kind);
CREATE INDEX IF NOT EXISTS poi_primary_idx       ON poi (is_primary);
RESET maintenance_work_mem;
RESET max_parallel_maintenance_workers;

ANALYZE address; ANALYZE poi;
SELECT 'address' AS t, count(*) FROM address UNION ALL SELECT 'poi', count(*) FROM poi;
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=os.path.join(os.environ.get("BUILD_HOME") or os.path.expanduser("~/geocode-build"), "geocode.sqlite"))
    args = ap.parse_args()
    if not os.path.exists(args.db):
        sys.exit(f"geocode.sqlite 없음: {args.db} (09-gen-geocode.py 먼저)")

    env = dict(os.environ)
    env.setdefault("PGHOST", "localhost"); env.setdefault("PGPORT", "5433")
    env.setdefault("PGUSER", "cuvia"); env.setdefault("PGDATABASE", "cuvia")
    env.setdefault("PGPASSWORD", "cuvia")

    t0 = time.time()
    tmpd = os.path.join(os.path.dirname(os.path.abspath(args.db)), "tmp")
    os.makedirs(tmpd, exist_ok=True)
    fd, csv_path = tempfile.mkstemp(suffix=".csv", dir=tmpd); os.close(fd)
    try:
        print(f"[1/2] geocode.sqlite → CSV 내보내기 …", file=sys.stderr)
        n = export_csv(args.db, csv_path)
        print(f"      {n:,} rows → {csv_path} ({os.path.getsize(csv_path)/1e6:.0f}MB)", file=sys.stderr)
        print(f"[2/2] PostGIS 적재(address + poi) …", file=sys.stderr)
        # 인덱스 재생성 튜닝(env). MWM 은 잘못된 값이면 기본값으로(SQL 주입 방지), MPW 는 int 강제.
        mwm = os.environ.get("GEOCODE_MAINT_MEM", "2GB")
        if not re.fullmatch(r"\d+[kKmMgG][bB]?", mwm):
            mwm = "2GB"
        mpw = str(int(os.environ.get("GEOCODE_MAINT_WORKERS", "4")))
        sql = (SQL.replace("__CSV__", csv_path.replace("'", "''"))
                  .replace("__MWM__", mwm)
                  .replace("__MPW__", mpw))
        r = subprocess.run(["psql", "-v", "ON_ERROR_STOP=1"], input=sql, text=True, env=env)
        if r.returncode != 0:
            sys.exit("✗ psql 적재 실패")
    finally:
        try: os.unlink(csv_path)
        except OSError: pass
    print(f"OK: address/poi 적재 완료 · {time.time()-t0:.0f}s", file=sys.stderr)


if __name__ == "__main__":
    main()
