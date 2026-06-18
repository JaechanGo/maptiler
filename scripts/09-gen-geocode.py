#!/usr/bin/env python3
"""통합 지오코딩 인덱스 빌드 — 도로명주소(내비게이션용DB) + OSM(역·지명·도로·POI·동) 병합.

출력: ~/geocode-build/geocode.sqlite  (단일 DB; 주소 검색 + 역/POI/지명 이름 검색 + 역지오코딩)
- 주소(kind='addr')   : 내비게이션용DB match_build_*.txt, 좌표 EPSG:5179→4326(순수파이썬)
- OSM(kind=type)      : 기존 geocode.sqlite(07 산출물)에서 복사 — station/place/dong/road/poi/biz + areas
통합 스키마:
  places(id,kind,name,subtype, sido,sigungu,emd,road,road_norm,main_no,sub_no,bld,postal,haeng_dong,bd_mgt_sn, lon,lat)
  places_fts(name, region, road, bld)   ← addr→region/road/bld, OSM→name
  place_rtree, areas, area_rtree, meta
※ 산출물(GB급)은 iCloud 밖 로컬에 둔다.
"""
import argparse, io, math, os, pathlib, re, sqlite3, sys, time, unicodedata

ROOT = pathlib.Path(__file__).resolve().parents[1]
SIDO = ["seoul","busan","daegu","incheon","gwangju","daejeon","ulsan","sejong","gyunggi",
        "gangwon","chungbuk","chungnam","jeonbuk","jeonnam","gyeongbuk","gyeongnam","jeju"]

# ---- EPSG:5179(UTM-K) → WGS84 (Snyder inverse TM, 무의존) ----
_a=6378137.0; _f=1/298.257222101; _e2=2*_f-_f*_f; _ep2=_e2/(1-_e2)
_lat0=math.radians(38.0); _lon0=math.radians(127.5); _k0=0.9996; _FE=1e6; _FN=2e6
_M0=_a*((1-_e2/4-3*_e2**2/64-5*_e2**3/256)*_lat0-(3*_e2/8+3*_e2**2/32+45*_e2**3/1024)*math.sin(2*_lat0)
        +(15*_e2**2/256+45*_e2**3/1024)*math.sin(4*_lat0)-(35*_e2**3/3072)*math.sin(6*_lat0))
_e1=(1-math.sqrt(1-_e2))/(1+math.sqrt(1-_e2)); _den=_a*(1-_e2/4-3*_e2**2/64-5*_e2**3/256)
def utmk_to_wgs84(E,N):
    mu=(_M0+(N-_FN)/_k0)/_den
    p=(mu+(3*_e1/2-27*_e1**3/32)*math.sin(2*mu)+(21*_e1**2/16-55*_e1**4/32)*math.sin(4*mu)
       +(151*_e1**3/96)*math.sin(6*mu)+(1097*_e1**4/512)*math.sin(8*mu))
    sp=math.sin(p); C=_ep2*math.cos(p)**2; T=math.tan(p)**2
    N1=_a/math.sqrt(1-_e2*sp*sp); R1=_a*(1-_e2)/(1-_e2*sp*sp)**1.5; D=(E-_FE)/(N1*_k0)
    lat=p-(N1*math.tan(p)/R1)*(D**2/2-(5+3*T+10*C-4*C**2-9*_ep2)*D**4/24
        +(61+90*T+298*C+45*T**2-252*_ep2-3*C**2)*D**6/720)
    lon=_lon0+(D-(1+2*T+C)*D**3/6+(5-2*C+28*T-3*C**2+8*_ep2+24*T**2)*D**5/120)/math.cos(p)
    return round(math.degrees(lon),6), round(math.degrees(lat),6)

def norm(s): return re.sub(r"\s+"," ",unicodedata.normalize("NFC",s or "")).strip()
def rnorm(s): return re.sub(r"[.\s]","",unicodedata.normalize("NFC",s or ""))
def search_text(name, is_station):
    name=norm(name); v={name, name.replace(' ','')}
    if is_station:
        base=name[:-1] if name.endswith('역') else name
        v|={base, base+'역', base.replace(' ',''), (base+'역').replace(' ','')}
    return ' '.join(x for x in v if x)

SCHEMA = """
  PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF; PRAGMA cache_size=-1048576; PRAGMA temp_store=MEMORY;
  CREATE TABLE places(id INTEGER PRIMARY KEY, kind TEXT, name TEXT, subtype TEXT,
    sido TEXT,sigungu TEXT,emd TEXT,road TEXT,road_norm TEXT,main_no INTEGER,sub_no INTEGER,
    bld TEXT,postal TEXT,haeng_dong TEXT,bd_mgt_sn TEXT, phone TEXT,opened TEXT, jibun TEXT,cat1 TEXT, lon REAL,lat REAL);
  CREATE VIRTUAL TABLE places_fts USING fts5(name, region, road, bld,
    content='places', content_rowid='id', tokenize='unicode61', prefix='2 3');
  CREATE VIRTUAL TABLE place_rtree USING rtree(id,minlon,maxlon,minlat,maxlat);
  CREATE TABLE areas(id INTEGER PRIMARY KEY, name TEXT, type TEXT, rings TEXT);
  CREATE VIRTUAL TABLE area_rtree USING rtree(id,minlon,maxlon,minlat,maxlat);
"""

def load_jibun(src, sido):
    # 건물관리번호(18) → "법정동 [산]지번본번[-부번]" (대표지번). match_jibun_<시도>.txt
    p = src / f"match_jibun_{sido}.txt"; d = {}
    if not p.exists(): return d
    for line in io.open(p, encoding="cp949", errors="replace"):
        c = line.rstrip("\n").split("|")
        if len(c) < 19: continue
        mgt = c[18]
        if mgt in d: continue
        san = "산 " if c[5] == "1" else ""; bu = c[7]
        d[mgt] = f"{c[3]} {san}{c[6]}" + (f"-{bu}" if bu and bu != "0" else "")
    return d

def add_juso(db, src, only, state):
    pid = state["pid"]; seen = state["seen"]
    for s in [x for x in SIDO if (not only or x in only)]:
        path = src / f"match_build_{s}.txt"
        if not path.exists():
            print(f"  (건너뜀) {path.name} 없음", file=sys.stderr); continue
        jdict = load_jibun(src, s)
        st=time.time(); n0=pid; pb=[]; fb=[]; rb=[]
        for line in io.open(path, encoding="cp949", errors="replace"):
            c=line.rstrip("\n").split("|")
            if len(c)<27: continue
            try: E=float(c[25]); N=float(c[26])
            except ValueError: continue
            mgt=c[10]
            if mgt in seen: continue
            seen.add(mgt)
            lon,lat=utmk_to_wgs84(E,N)
            if not (124<=lon<=132 and 33<=lat<=39): continue
            pid+=1; road=c[5]; rn=rnorm(road); mno=int(c[7] or 0); sno=int(c[8] or 0)
            bld=" ".join(dict.fromkeys([x for x in (c[11],c[19]) if x.strip()]))
            pb.append((pid,'addr',None,None,c[1],c[2],c[3],road,rn,mno,sno,bld,c[9],c[14],mgt,None,None,jdict.get(mgt),None,lon,lat))
            fb.append((pid,'',f"{c[1]} {c[2]} {c[3]} {c[14]}",f"{road} {rn}",bld))
            rb.append((pid,lon,lon,lat,lat))
            if len(pb)>=50000:
                _flush(db,pb,fb,rb); pb.clear(); fb.clear(); rb.clear()
        _flush(db,pb,fb,rb)
        print(f"  addr:{s:10s} +{pid-n0:>8,}  ({time.time()-st:.1f}s)", file=sys.stderr)
    state["pid"]=pid

def add_osm(db, osm_path, state):
    if not pathlib.Path(osm_path).exists():
        print(f"  (건너뜀) OSM {osm_path} 없음 — 주소 전용으로 빌드", file=sys.stderr); return
    pid=state["pid"]; o=sqlite3.connect(f"file:{osm_path}?mode=ro", uri=True); st=time.time(); n0=pid
    pb=[]; fb=[]; rb=[]
    for name,typ,sub,lon,lat in o.execute("SELECT name,type,subtype,lon,lat FROM places"):
        if lon is None or lat is None: continue
        pid+=1
        pb.append((pid,typ,name,sub,None,None,None,None,None,None,None,None,None,None,None,None,None,None,None,lon,lat))
        fb.append((pid,search_text(name, typ=='station'),'','',''))
        rb.append((pid,lon,lon,lat,lat))
        if len(pb)>=50000: _flush(db,pb,fb,rb); pb.clear(); fb.clear(); rb.clear()
    _flush(db,pb,fb,rb)
    # 영역(행정동 등) 그대로 복사 — 역지오코딩 포함영역용
    try:
        db.execute(f"ATTACH DATABASE '{osm_path}' AS o")
        db.execute("INSERT INTO areas SELECT * FROM o.areas")
        db.execute("INSERT INTO area_rtree SELECT * FROM o.area_rtree")
        na=db.execute("SELECT count(*) FROM areas").fetchone()[0]
        db.execute("DETACH DATABASE o")
    except sqlite3.OperationalError as e:
        na=0; print(f"  (areas 복사 스킵: {e})", file=sys.stderr)
    o.close(); state["pid"]=pid
    print(f"  osm: +{pid-n0:>8,} · areas {na:,}  ({time.time()-st:.1f}s)", file=sys.stderr)

def add_biz(db, csvdir, state):
    # 소상공인 상가(상권)정보 CSV(시도별) → kind='biz'. 경도/위도 이미 WGS84.
    import csv, glob
    pid=state["pid"]; st=time.time(); n0=pid; pb=[]; fb=[]; rb=[]
    for path in sorted(glob.glob(os.path.join(csvdir,"*.csv"))):
        with open(path, encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                try: lon=float(row.get("경도") or 0); lat=float(row.get("위도") or 0)
                except ValueError: continue
                if not (124<=lon<=132 and 33<=lat<=39): continue
                nm=(row.get("상호명") or "").strip()
                if not nm: continue
                biz=(row.get("상권업종소분류명") or "").strip()
                sido=(row.get("시도명") or "").strip(); sgg=(row.get("시군구명") or "").strip(); emd=(row.get("행정동명") or "").strip()
                phone=(row.get("전화번호") or "").strip() or None; opened=(row.get("인허가일자") or "").strip() or None
                cat1=(row.get("상권업종대분류명") or "").strip() or None
                pid+=1
                pb.append((pid,'biz',nm,biz,sido,sgg,emd,None,None,None,None,biz,None,None,None,phone,opened,None,cat1,round(lon,6),round(lat,6)))
                fb.append((pid,nm,f"{sido} {sgg} {emd}",'',biz))   # FTS: name=상호명, region=시군구·동, bld=업종
                rb.append((pid,lon,lon,lat,lat))
                if len(pb)>=50000: _flush(db,pb,fb,rb); pb.clear(); fb.clear(); rb.clear()
    _flush(db,pb,fb,rb)
    print(f"  biz: +{pid-n0:,}  ({time.time()-st:.1f}s)", file=sys.stderr); state["pid"]=pid

def _flush(db,pb,fb,rb):
    if not pb: return
    db.executemany("INSERT INTO places VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",pb)
    db.executemany("INSERT INTO places_fts(rowid,name,region,road,bld) VALUES(?,?,?,?,?)",fb)
    db.executemany("INSERT INTO place_rtree VALUES(?,?,?,?,?)",rb)

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--src", default="/Users/jaechango_cudo/Downloads/지도정보/202605_내비게이션용DB_전체분")
    ap.add_argument("--osm", default=os.path.expanduser("~/geocode-build/osm.sqlite"))
    ap.add_argument("--out", default=os.path.expanduser("~/geocode-build/geocode.sqlite"))
    ap.add_argument("--only")
    ap.add_argument("--poi-csv-dir", help="소상공인 상가(상권)정보 CSV 폴더(시도별)")
    args=ap.parse_args()
    only=set(args.only.split(",")) if args.only else None
    out=pathlib.Path(args.out); out.parent.mkdir(parents=True, exist_ok=True)
    tmp=out.with_suffix(".sqlite.tmp"); tmp.unlink(missing_ok=True)
    db=sqlite3.connect(tmp); db.executescript(SCHEMA)
    t0=time.time(); state={"pid":0,"seen":set()}
    print(f"[통합 지오코드 빌드] juso={args.src}\n  osm={args.osm}", file=sys.stderr)
    add_juso(db, pathlib.Path(args.src), only, state)
    add_osm(db, args.osm, state)
    if args.poi_csv_dir: add_biz(db, args.poi_csv_dir, state)
    db.execute("CREATE TABLE meta(k TEXT,v TEXT)")
    db.executemany("INSERT INTO meta VALUES(?,?)", [("places",str(state["pid"])),("srid","4326"),
        ("source","내비게이션용DB 2026.05 + OSM"),("built_s",f"{time.time()-t0:.0f}")])
    db.execute("INSERT INTO places_fts(places_fts) VALUES('optimize')")
    db.commit(); db.close(); tmp.replace(out)
    sz=out.stat().st_size/1048576
    print("="*56); print(f"OK: {out}  총 {state['pid']:,}건 · {sz:.0f}MB · {time.time()-t0:.0f}s")

if __name__=="__main__":
    main()
