#!/usr/bin/env python3
"""공공시설 CSV → PostGIS public_facility 적재 (Phase 4): 병원/경찰/소방/AED/대피소.

data.go.kr 등 출처별 CSV 스키마가 제각각 → 11b-build-facility.py 식 휴리스틱 컬럼 감지.
좌표 있으면 geom 생성, 없으면(경찰·소방 등 도로명주소만) geom NULL → 후속 지오코딩(navi 조인) 단계에서 채움.
kind 별로 기존분 멱등 교체. attrs 에 원본 행 전체를 jsonb 로 보존.

  PGPASSWORD=... scripts/postgis/load_facility.py --kind police \
      --csv ~/geocode-build/staged/facility_src/police.csv --source data.go.kr:15077036
  # 컬럼 자동감지 실패 시: --name-col 관서명 --addr-col 주소 --lat-col 위도 --lon-col 경도
"""
import argparse, csv, io, json, os, subprocess, sys, tempfile

# 헤더 키워드 우선순위(앞쪽 우선). 부분일치.
NAME_KW = ["시설명", "기관명", "관서명", "119안전센터명", "병원명", "의료기관명", "명칭", "설치장소", "상호", "name"]
ADDR_KW = ["도로명주소", "소재지도로명주소", "주소", "소재지", "설치위치", "road"]
LAT_KW  = ["위도", "lat", "y좌표", "ycrd"]
LON_KW  = ["경도", "lon", "lng", "x좌표", "xcrd"]


def read_rows(path):
    """인코딩 폴백(utf-8-sig → cp949)으로 (header, rows) 반환."""
    for enc in ("utf-8-sig", "cp949", "euc-kr", "utf-8"):
        try:
            with open(path, encoding=enc, newline="") as f:
                rows = list(csv.reader(f))
            if rows:
                return rows[0], rows[1:]
        except (UnicodeDecodeError, LookupError):
            continue
    sys.exit(f"CSV 인코딩 판별 실패: {path}")


def pick(header, kws, override):
    if override:
        return header.index(override) if override in header else sys.exit(f"지정 컬럼 없음: {override}")
    norm = [h.strip().lower().replace(" ", "") for h in header]
    for kw in kws:
        k = kw.lower()
        for i, h in enumerate(norm):
            if k in h:
                return i
    return None


SQL = r"""
\set ON_ERROR_STOP on
DROP TABLE IF EXISTS _stg_fac;
CREATE UNLOGGED TABLE _stg_fac (kind text, name text, addr text, lon text, lat text, attrs text);
\copy _stg_fac FROM '__CSV__' WITH (FORMAT csv)

DELETE FROM public_facility WHERE kind = '__KIND__';
INSERT INTO public_facility(kind,name,addr,attrs,source,geom)
SELECT kind, name, addr, nullif(attrs,'')::jsonb, '__SRC__',
       CASE WHEN nullif(lon,'') IS NOT NULL AND nullif(lat,'') IS NOT NULL
            THEN ST_SetSRID(ST_MakePoint(lon::float8, lat::float8),4326) END
FROM _stg_fac;
DROP TABLE _stg_fac;
ANALYZE public_facility;
SELECT count(*) AS total, count(geom) AS with_coords FROM public_facility WHERE kind='__KIND__';
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", required=True)
    ap.add_argument("--kind", required=True, choices=["hospital", "police", "fire_station", "aed", "shelter"])
    ap.add_argument("--source", default="")
    ap.add_argument("--name-col"); ap.add_argument("--addr-col")
    ap.add_argument("--lat-col"); ap.add_argument("--lon-col")
    args = ap.parse_args()
    csv_path = args.csv
    if os.path.isdir(csv_path):   # collect 추출 디렉토리 → 최신 CSV 선택(119안전센터 연도별 등)
        import glob
        cands = sorted(glob.glob(os.path.join(csv_path, "**", "*.csv"), recursive=True))
        if not cands:
            sys.exit(f"디렉토리에 CSV 없음: {csv_path}")
        csv_path = cands[-1]
        print(f"디렉토리 → 최신 CSV 선택: {os.path.basename(csv_path)}", file=sys.stderr)
    elif not os.path.exists(csv_path):
        sys.exit(f"CSV 없음: {csv_path}")

    header, rows = read_rows(csv_path)
    iname = pick(header, NAME_KW, args.name_col)
    iaddr = pick(header, ADDR_KW, args.addr_col)
    ilat  = pick(header, LAT_KW,  args.lat_col)
    ilon  = pick(header, LON_KW,  args.lon_col)
    print(f"컬럼 감지: name={header[iname] if iname is not None else '—'} "
          f"addr={header[iaddr] if iaddr is not None else '—'} "
          f"lat={header[ilat] if ilat is not None else '—'} "
          f"lon={header[ilon] if ilon is not None else '—'}", file=sys.stderr)
    if iname is None and iaddr is None:
        sys.exit("✗ 이름·주소 컬럼 모두 감지 실패 — --name-col/--addr-col 로 지정")

    env = dict(os.environ)
    for k, v in (("PGHOST","localhost"),("PGPORT","5432"),("PGUSER","cuvia"),
                 ("PGDATABASE","cuvia"),("PGPASSWORD","cuvia")):
        env.setdefault(k, v)

    tmpd = os.path.dirname(os.path.abspath(csv_path))
    fd, out = tempfile.mkstemp(suffix=".csv", dir=tmpd); os.close(fd)
    g = lambda r, i: (r[i].strip() if i is not None and i < len(r) else "")
    n = 0
    with open(out, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        for r in rows:
            if not r:
                continue
            attrs = {header[i]: r[i] for i in range(min(len(header), len(r))) if r[i].strip()}
            w.writerow([args.kind, g(r, iname), g(r, iaddr), g(r, ilon), g(r, ilat),
                        json.dumps(attrs, ensure_ascii=False)])
            n += 1
    try:
        sql = (SQL.replace("__CSV__", out.replace("'", "''"))
                  .replace("__KIND__", args.kind).replace("__SRC__", args.source.replace("'", "''")))
        r = subprocess.run(["psql", "-v", "ON_ERROR_STOP=1"], input=sql, text=True, env=env)
        if r.returncode != 0:
            sys.exit("✗ psql 적재 실패")
    finally:
        try: os.unlink(out)
        except OSError: pass
    print(f"OK: public_facility kind={args.kind} {n:,}행 처리", file=sys.stderr)


if __name__ == "__main__":
    main()
