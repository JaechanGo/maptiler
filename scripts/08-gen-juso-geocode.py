#!/usr/bin/env python3
"""내비게이션용DB(행안부) → 전국 도로명주소 지오코딩 인덱스 빌드 (무의존: SQLite + 순수파이썬).

입력 : 내비게이션용DB 폴더의 match_build_<시도>.txt (건물단위 도로명주소 + 좌표, EPSG:5179, CP949).
출력 : geocode/geocode-juso.sqlite  (places + places_fts(FTS5) + place_rtree)
좌표 : EPSG:5179(UTM-K, GRS80) → WGS84(4326) 를 빌드 시 순수파이썬으로 1회 변환(폐쇄망 런타임 무의존).

forward : 주소 텍스트 → 좌표.  reverse : 좌표 → 풀주소(R-tree 최근접).
서빙은 server/geocode-api.py 가 담당(전체주소 staged fallback 쿼리).

match_build 컬럼(0-based, 33필드, 실데이터 검증 2026.05):
  1시도 2시군구 3읍면동 5도로명 6지하여부 7건물본번 8건물부번 9우편 10건물관리번호(PK)
  11건물명(시군구용) 14행정동명 19상세건물명 25건물중심점X 26건물중심점Y (23,24=주출입구XY)
"""
import argparse, io, math, os, pathlib, re, sqlite3, sys, time, unicodedata

ROOT = pathlib.Path(__file__).resolve().parents[1]
SIDO = ["seoul","busan","daegu","incheon","gwangju","daejeon","ulsan","sejong","gyunggi",
        "gangwon","chungbuk","chungnam","jeonbuk","jeonnam","gyeongbuk","gyeongnam","jeju"]

# ---- EPSG:5179 (UTM-K) → WGS84 경위도, 순수파이썬(Snyder inverse TM) ----
_a = 6378137.0; _f = 1/298.257222101; _e2 = 2*_f-_f*_f; _ep2 = _e2/(1-_e2)
_lat0 = math.radians(38.0); _lon0 = math.radians(127.5); _k0 = 0.9996; _FE = 1e6; _FN = 2e6
_M0 = _a*((1-_e2/4-3*_e2**2/64-5*_e2**3/256)*_lat0-(3*_e2/8+3*_e2**2/32+45*_e2**3/1024)*math.sin(2*_lat0)
          +(15*_e2**2/256+45*_e2**3/1024)*math.sin(4*_lat0)-(35*_e2**3/3072)*math.sin(6*_lat0))
_e1 = (1-math.sqrt(1-_e2))/(1+math.sqrt(1-_e2))
_den = _a*(1-_e2/4-3*_e2**2/64-5*_e2**3/256)
def utmk_to_wgs84(E, N):
    mu = (_M0+(N-_FN)/_k0)/_den
    p = (mu+(3*_e1/2-27*_e1**3/32)*math.sin(2*mu)+(21*_e1**2/16-55*_e1**4/32)*math.sin(4*mu)
         +(151*_e1**3/96)*math.sin(6*mu)+(1097*_e1**4/512)*math.sin(8*mu))
    sp = math.sin(p); C = _ep2*math.cos(p)**2; T = math.tan(p)**2
    N1 = _a/math.sqrt(1-_e2*sp*sp); R1 = _a*(1-_e2)/(1-_e2*sp*sp)**1.5; D = (E-_FE)/(N1*_k0)
    lat = p-(N1*math.tan(p)/R1)*(D**2/2-(5+3*T+10*C-4*C**2-9*_ep2)*D**4/24
          +(61+90*T+298*C+45*T**2-252*_ep2-3*C**2)*D**6/720)
    lon = _lon0+(D-(1+2*T+C)*D**3/6+(5-2*C+28*T-3*C**2+8*_ep2+24*T**2)*D**5/120)/math.cos(p)
    return round(math.degrees(lon), 6), round(math.degrees(lat), 6)

def rnorm(s):
    return re.sub(r"[.\s]", "", unicodedata.normalize("NFC", s or ""))

def rows_from(path):
    for line in io.open(path, encoding="cp949", errors="replace"):
        c = line.rstrip("\n").split("|")
        if len(c) < 27:
            continue
        try:
            E = float(c[25]); N = float(c[26])      # 건물중심점
        except ValueError:
            continue
        yield c, E, N

def build(src, out, only=None):
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".sqlite.tmp"); tmp.unlink(missing_ok=True)
    db = sqlite3.connect(tmp)
    db.executescript("""
      PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF; PRAGMA cache_size=-1048576; PRAGMA temp_store=MEMORY;
      CREATE TABLE places(id INTEGER PRIMARY KEY, sido TEXT, sigungu TEXT, emd TEXT,
        road TEXT, road_norm TEXT, main_no INTEGER, sub_no INTEGER, bld TEXT,
        postal TEXT, haeng_dong TEXT, bd_mgt_sn TEXT, lon REAL, lat REAL);
      CREATE VIRTUAL TABLE places_fts USING fts5(region, road, bld,
        content='places', content_rowid='id', tokenize='unicode61', prefix='2 3');
      CREATE VIRTUAL TABLE place_rtree USING rtree(id, minlon, maxlon, minlat, maxlat);
    """)
    pid = 0; seen = set(); t0 = time.time()
    sidos = [s for s in SIDO if (not only or s in only)]
    for s in sidos:
        path = src / f"match_build_{s}.txt"
        if not path.exists():
            print(f"  (건너뜀) {path.name} 없음", file=sys.stderr); continue
        st = time.time(); n0 = pid; pb=[]; fb=[]; rb=[]
        for c, E, N in rows_from(path):
            mgt = c[10]
            if mgt in seen:
                continue
            seen.add(mgt)
            lon, lat = utmk_to_wgs84(E, N)
            if not (124 <= lon <= 132 and 33 <= lat <= 39):
                continue
            pid += 1
            road = c[5]; rn = rnorm(road)
            mno = int(c[7] or 0); sno = int(c[8] or 0)
            blds = [x for x in (c[11], c[19]) if x.strip()]
            bld = " ".join(dict.fromkeys(blds))
            pb.append((pid, c[1], c[2], c[3], road, rn, mno, sno, bld, c[9], c[14], mgt, lon, lat))
            fb.append((pid, f"{c[1]} {c[2]} {c[3]} {c[14]}", f"{road} {rn}", bld))
            rb.append((pid, lon, lon, lat, lat))
            if len(pb) >= 50000:
                db.executemany("INSERT INTO places VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", pb)
                db.executemany("INSERT INTO places_fts(rowid,region,road,bld) VALUES(?,?,?,?)", fb)
                db.executemany("INSERT INTO place_rtree VALUES(?,?,?,?,?)", rb)
                pb.clear(); fb.clear(); rb.clear()
        if pb:
            db.executemany("INSERT INTO places VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", pb)
            db.executemany("INSERT INTO places_fts(rowid,region,road,bld) VALUES(?,?,?,?)", fb)
            db.executemany("INSERT INTO place_rtree VALUES(?,?,?,?,?)", rb)
        print(f"  {s:10s} +{pid-n0:>8,}  ({time.time()-st:.1f}s)", file=sys.stderr)
    db.execute("CREATE TABLE meta(k TEXT, v TEXT)")
    db.executemany("INSERT INTO meta VALUES(?,?)", [("places", str(pid)), ("srid", "4326"),
                    ("source", "행안부 내비게이션용DB 2026.05"), ("built_s", f"{time.time()-t0:.0f}")])
    db.execute("INSERT INTO places_fts(places_fts) VALUES('optimize')")
    db.commit(); db.close()
    tmp.replace(out)
    return pid, time.time()-t0

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default="/Users/jaechango_cudo/Downloads/지도정보/202605_내비게이션용DB_전체분")
    # 산출물은 iCloud 동기 폴더 밖(로컬)에 둔다 — 2.6GB DB의 sync 부하·evict 회피.
    ap.add_argument("--out", default=os.path.join(os.environ.get("BUILD_HOME") or os.path.expanduser("~/geocode-build"), "geocode-juso.sqlite"))
    ap.add_argument("--only", help="시도 일부만(쉼표): seoul,gyunggi")
    args = ap.parse_args()
    only = set(args.only.split(",")) if args.only else None
    print(f"[내비게이션용DB → 지오코딩 인덱스] src={args.src}", file=sys.stderr)
    pid, dt = build(pathlib.Path(args.src), pathlib.Path(args.out), only)
    sz = pathlib.Path(args.out).stat().st_size/1048576
    print("="*56)
    print(f"OK: {args.out}  주소 {pid:,}건 · {sz:.0f}MB · {dt:.0f}s")

if __name__ == "__main__":
    main()
