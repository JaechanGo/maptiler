#!/usr/bin/env python3
"""geocode.sqlite(09-gen-geocode.py 산출) → PostGIS address + poi 적재 (Phase 1 데이터 / Phase 4 POI).

핵심 재사용: navi DB·OSM·상가·인허가·생활편의의 파싱·좌표변환(EPSG:5179/5174→4326)·시설 지오코딩·
dedup(is_primary)이 이미 geocode.sqlite `places`(lon/lat=4326)에 다 들어있다. 원천을 재파싱하지 말고
이 산출물을 그대로 PostGIS 로 옮긴다 → 기존 품질과 100% parity. (지오코더 질의 전환은 Phase 5.)

  places(kind,name,subtype,sido,sigungu,emd,road,road_norm,main_no,sub_no,bld,postal,
         haeng_dong,bd_mgt_sn,bcode,hcode,phone,opened,jibun,cat1,cat2,source,is_primary,lon,lat)
    → address (전체)          : 검색/표시/역지오코딩 원천
    → poi     (kind in biz/facility, 좌표有) : martin POI 레이어(기존 poi.mbtiles 대체)

연결은 libpq 환경변수(PGHOST/PGPORT/PGUSER/PGDATABASE/PGPASSWORD). 기본 cuvia/cuvia@localhost:5433(맵 전용 컨테이너 호스트포트).
  PGPASSWORD=... scripts/postgis/load_geocode.py [--db ~/geocode-build/geocode.sqlite]
"""
import argparse, csv, os, subprocess, sys, tempfile, time

COLS = ["kind","name","subtype","sido","sigungu","emd","road","road_norm","main_no","sub_no",
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
SQL = r"""
\set ON_ERROR_STOP on
DROP TABLE IF EXISTS _stg_places;
CREATE UNLOGGED TABLE _stg_places (
  kind text, name text, subtype text, sido text, sigungu text, emd text,
  road text, road_norm text, main_no text, sub_no text, bld text, postal text,
  haeng_dong text, bd_mgt_sn text, bcode text, hcode text, phone text, opened text,
  jibun text, cat1 text, cat2 text, source text, is_primary text, lon text, lat text
);
\copy _stg_places FROM '__CSV__' WITH (FORMAT csv)

TRUNCATE address;
TRUNCATE poi;

INSERT INTO address(kind,name,subtype,sido,sigungu,emd,road,road_norm,main_no,sub_no,bld,postal,
                    haeng_dong,bd_mgt_sn,bcode,hcode,phone,opened,jibun,cat1,cat2,source,is_primary,geom)
SELECT kind,name,subtype,sido,sigungu,emd,road,road_norm,
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
ANALYZE address; ANALYZE poi;
SELECT 'address' AS t, count(*) FROM address UNION ALL SELECT 'poi', count(*) FROM poi;
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=os.path.expanduser("~/geocode-build/geocode.sqlite"))
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
        sql = SQL.replace("__CSV__", csv_path.replace("'", "''"))
        r = subprocess.run(["psql", "-v", "ON_ERROR_STOP=1"], input=sql, text=True, env=env)
        if r.returncode != 0:
            sys.exit("✗ psql 적재 실패")
    finally:
        try: os.unlink(csv_path)
        except OSError: pass
    print(f"OK: address/poi 적재 완료 · {time.time()-t0:.0f}s", file=sys.stderr)


if __name__ == "__main__":
    main()
