#!/usr/bin/env python3
"""통합 지오코딩 인덱스 빌드 — 도로명주소(내비게이션용DB) + OSM(역·지명·도로·POI·동) 병합.

출력: ~/geocode-build/geocode.sqlite  (단일 DB; 주소 검색 + 역/POI/지명 이름 검색 + 역지오코딩)
- 주소(kind='addr')   : 내비게이션용DB match_build_*.txt, 좌표 EPSG:5179→4326(순수파이썬)
- OSM(kind=type)      : 기존 geocode.sqlite(07 산출물)에서 복사 — station/place/dong/road/poi/biz + areas
통합 스키마:
  places(id,kind,name,subtype, sido,sigungu,emd,ri,road,road_norm,main_no,sub_no,bld,postal,haeng_dong,bd_mgt_sn, lon,lat)
  places_fts(name, region, road, bld)   ← addr→region/road/bld, OSM→name
  place_rtree, areas, area_rtree, meta
※ 산출물(GB급)은 iCloud 밖 로컬에 둔다.
"""
import argparse, collections, io, json, math, os, pathlib, re, sqlite3, sys, time, unicodedata

ROOT = pathlib.Path(__file__).resolve().parents[1]
SIDO = ["seoul","busan","daegu","incheon","gwangju","daejeon","ulsan","sejong","gyunggi",
        "gangwon","chungbuk","chungnam","jeonbuk","jeonnam","gyeongbuk","gyeongnam","jeju"]
CW_LD, CW_OSM = {}, {}   # cat-crosswalk.json (canonical 카테고리 매핑) — main()에서 로드, add_biz/add_osm 이 사용

# ---- 리(里) 키 충돌 관측 (T018 A-4) ----------------------------------------
# load_jibun 의 rd(법정동코드10 → 리) 는 한 키에 서로 다른 리가 몰리는 것을 **조용히** 삼켜 왔다.
# (구현은 load_jibun 참조 — 값 선택 규칙은 불변, 여기서는 세기만 한다.)
# 이 수치가 0 이 아니면 다음 적재본의 address.ri 는 그만큼 부정확하다는 뜻이고,
# 그 오염이 lawd_ri 사전 → 지오코딩 리 표기로 전파된다.
RI_KEY_COLLISIONS = collections.defaultdict(set)   # 붕괴 키 → 무시된 리 이름 집합
RI_COLLISION_ROWS = {}          # 시도 → 충돌 행수
RI_COLLISION_SAMPLE = 5         # 시도당 stderr 로 뽑아 보여줄 표본 개수(로그 폭주 방지)

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
_DORO_RE = re.compile(r"(\S*(?:대로|로|길))\s+(\d+)(?:-(\d+))?")
_GIL_SP = re.compile(r"([로가])\s+(\d+(?:번|가)?길)")   # '퇴계로 34길' 처럼 번호길 앞 띄어쓰기 → 붙임(안 그러면 '퇴계로'+34번지로 오독)
def _mainno(s):                 # 결정5: 본번 int4 안전정수화(length<=7). 비정상 자릿수·비숫자→None(매칭 불성립). int4 가드 제거 금지(Global Constraint).
    if not s or not s.isdigit() or len(s) > 7: return None
    return int(s)
def _subno(s):                  # 결정5: 부번 안전정수화. 비정상·결측→0(기존 int(... or 0) 동작 보존 + main_no 와 동일 자릿수 가드).
    if not s or not s.isdigit() or len(s) > 7: return 0
    return int(s)
def parse_doro(s):
    # 도로명주소 → (도로명, 도로명정규화=rnorm, 건물본번, 건물부번). 상세주소(층/호)·미파싱은 무시. navi 조인키(rnorm·본/부번)와 동형.
    m = _DORO_RE.search(_GIL_SP.sub(r"\1\2", norm(s).split(",")[0]))
    if not m: return (None,None,None,None)
    mno = _mainno(m.group(2))                              # 본번 int4 가드 — 비정상이면 미파싱 취급
    if mno is None: return (None,None,None,None)
    return (m.group(1), rnorm(m.group(1)), mno, _subno(m.group(3)))


# ── 도로명 사전(gazetteer) 매칭 — navi 의 '실재 도로명'에 최장일치. 정규식이 더러운 입력(붙여쓰기·지하·시군구붙음·
#    내부공백·가지번호)을 '추측'하다 깨지는 것을 방지(개념: geocoder-kr/postcodify 의 타입정규화·사전조인을 navi 로 자체구현).
GAZ = {}                                   # build_gazetteer 가 채움: {"road":{(시도,시군구):[road_norm…]}, "sgg":{시도:[시군구…]}}
_JIHA = re.compile(r"지하|지상|지층")        # 도로명·번호 사이 인필('창경궁로 지하 81') 제거용
def parse_doro_gaz(s):
    """도로명주소 → (도로명, road_norm, 본번, 부번, 시도, 시군구). navi 도로명 사전 최장일치.
    붙여쓰기('대학로120')·지하('창경궁로 지하 81')·시군구붙음('성동구난계로')·내부공백('센텀 7로')·가지번호('여의대방로59나길')
    견고. 시군구는 화이트리스트 접두분리로 보정(세종=시군구 빈값). 사전 미구축/미매칭이면 regex parse_doro 로 폴백(시도·시군구 None)."""
    road = GAZ.get("road")
    if road:
        a = norm(s).split(",")[0]                          # 상세주소(쉼표 뒤) 제거
        t = a.split()
        if len(t) >= 2:
            sido = t[0]; sgg = ""
            for cand in GAZ["sgg"].get(sido, []):          # 시군구 화이트리스트 최장 접두('성동구난계로'→'성동구')
                if t[1] == cand or " ".join(t[1:3]) == cand or t[1].startswith(cand):
                    sgg = cand; break
            roads = road.get((sido, sgg)) or (road.get((sido, "")) if sido.startswith("세종") else None)
            if roads:
                rn = _JIHA.sub("", rnorm(a))               # '지하' 제거 후 정규화
                pre = rnorm(sido) + rnorm(sgg)             # 시도+시군구 접두를 먼저 consume → 남은 데서 도로명 검색
                if rn.startswith(pre): rn = rn[len(pre):]  # ('종로'가 '종로구' 안에서 잘못 매칭되는 충돌 방지)
                for rd in roads:                           # 남은 부분에서 실재 도로명 최장일치(가지번호길까지 정확)
                    if rd and rd in rn:
                        m = re.match(r"(\d+)(?:-(\d+))?", rn[rn.find(rd) + len(rd):])
                        if m:
                            mno = _mainno(m.group(1))        # 본번 int4 가드 — 비정상이면 폴백(regex parse_doro)
                            if mno is None: break
                            return (rd, rd, mno, _subno(m.group(2)), sido, sgg)
                        break
    r, rnm, mno, sno = parse_doro(s)                       # 폴백
    return (r, rnm, mno, sno, None, None)


# ── 지번 폴백 — 도로명으로 못 푸는 시설(도로칸에 지번 기입·지번만 보유)을 법정동+본/부번으로 navi 조인.
_JIBUN_RE = re.compile(r"(\S*[동리가])\s+(산)?\s*(\d+)(?:-(\d+))?")
def _navi_jibun_key(sido, sgg, jibun_text):
    # navi addr 의 jibun 텍스트('혜화동 1-21'·'신영동 산 2-13') → (시도,시군구,법정동norm,산,본번,부번)
    m = _JIBUN_RE.search(norm(jibun_text or ""))
    if not m: return None
    mno = _mainno(m.group(3))                              # 지번 본번 int4 가드 — 비정상이면 키 불성립
    if mno is None: return None
    return (sido, sgg, rnorm(m.group(1)), 1 if m.group(2) else 0, mno, _subno(m.group(4)))
def _facility_jibun_key(jibun_src):
    # 시설 지번주소(또는 도로명칸에 기입된 지번) → 조인키. 시군구는 사전 화이트리스트로 보정(navi 포맷 '수원시 영통구').
    a = norm(jibun_src or ""); t = a.split()
    if len(t) < 3: return None
    sido = t[0]; sgg = ""
    for cand in GAZ.get("sgg", {}).get(sido, []):
        if t[1] == cand or " ".join(t[1:3]) == cand or t[1].startswith(cand):
            sgg = cand; break
    return _navi_jibun_key(sido, sgg, a)

def _norm_sgg(sido, sgg):       # 결정4(M1): 시군구 navi 표기 정규화. 완전일치만 치환, 빈값(세종)·미매칭은 원본 유지(승급형 접두매칭 금지).
    if not sgg: return sgg                        # 빈값(세종 등) 그대로 — 임의 '구' 오승급 차단
    for cand in GAZ.get("sgg", {}).get(sido, []):
        if sgg == cand: return cand               # 완전일치만 치환(접두/최장일치 승급 금지)
    return sgg                                    # 미매칭 → 원본 유지(하위호환)

def _split_emd_ri(jibun_txt, sido, sgg):   # 결정2: 지번 텍스트 → (법정동 bjd, 리 ri). 동지역 ri=None. 좌표 PIP 아님(순수 파싱).
    t = norm(jibun_txt or "").split()
    if len(t) < 2: return (None, None)
    i = 1 if t[0] == sido else 0               # 시도 consume
    rest = t[i:]
    if sgg:                                    # 시군구 consume (1~2토큰; GAZ 화이트리스트와 동일 규칙)
        sgg_tok = sgg.split()
        if rest[:len(sgg_tok)] == sgg_tok: rest = rest[len(sgg_tok):]
    bjd = ri = None
    for j, tok in enumerate(rest):
        if bjd is None and tok.endswith(("동", "읍", "면", "가")):   # (m1) 법정동 = endswith 종결 토큰
            bjd = tok
            if j+1 < len(rest) and rest[j+1].endswith("리"): ri = rest[j+1]   # 다음 토큰이 리(면/읍 하위)면 채움
            break
    return (bjd, ri)
_BIZ_PUNCT=re.compile(r"[\s()\[\]{}<>（）【】·.,/&-]+")
def biznrm(s): return _BIZ_PUNCT.sub("",unicodedata.normalize("NFC",s or "")).lower()  # 12-build-poi.sh _nrm와 동일 — biz 중복(대표) 판정 키
def search_text(name, is_station):
    name=norm(name); v={name, name.replace(' ','')}
    if is_station:
        base=name[:-1] if name.endswith('역') else name
        v|={base, base+'역', base.replace(' ',''), (base+'역').replace(' ','')}
    return ' '.join(x for x in v if x)

SCHEMA = """
  PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF; PRAGMA cache_size=-1048576; PRAGMA temp_store=MEMORY;
  CREATE TABLE places(id INTEGER PRIMARY KEY, kind TEXT, name TEXT, subtype TEXT,
    sido TEXT,sigungu TEXT,emd TEXT,ri TEXT,road TEXT,road_norm TEXT,main_no INTEGER,sub_no INTEGER,
    bld TEXT,postal TEXT,haeng_dong TEXT,bd_mgt_sn TEXT,bcode TEXT,hcode TEXT, phone TEXT,opened TEXT, jibun TEXT,cat1 TEXT,cat2 TEXT, source TEXT,is_primary INTEGER, lon REAL,lat REAL);
  CREATE VIRTUAL TABLE places_fts USING fts5(name, region, road, bld,
    content='places', content_rowid='id', tokenize='unicode61', prefix='2 3');
  CREATE VIRTUAL TABLE place_rtree USING rtree(id,minlon,maxlon,minlat,maxlat);
  CREATE TABLE areas(id INTEGER PRIMARY KEY, name TEXT, type TEXT, rings TEXT, code TEXT);
  CREATE VIRTUAL TABLE area_rtree USING rtree(id,minlon,maxlon,minlat,maxlat);
"""

def _derive_jibun(namemap, mgt):
    # 대표지번 없는 건물 → 건물관리번호로 지번 파생. mgt[:10]=법정동코드→법정동명, [11:15]=본번, [15:19]=부번.
    nm = namemap.get(mgt[:10])
    if not nm or len(mgt) < 19: return None
    try: b = int(mgt[11:15]); s = int(mgt[15:19])
    except ValueError: return None
    if not b: return None
    return f"{nm} {b}" + (f"-{s}" if s else "")


def load_jibun(src, sido):
    # match_jibun_<시도>.txt → (mgt→대표지번 dict, 법정동코드(앞10)→"법정동 [리]" 이름 dict, 법정동코드(앞10)→리(里) dict).
    # c[3]=법정동(면), c[4]=리(시골만; 동지역 빈값), c[5]=산, c[6]=본번, c[7]=부번, c[18]=건물관리번호.
    # 이름 dict 는 대표지번 없는 건물의 지번을 _derive_jibun 으로 채우는 데 쓴다(빠짐없이).
    # ri dict(rd)는 navi addr 의 ri 컬럼(X5) 산출용.
    # ★ 정정(T018, 2026-08-10) — 구 주석의 "법정동코드(mgt[:10]) 단위 리 1:1(면지역만 존재)"은 **거짓**이다.
    #   mgt 는 건물관리번호이고 앞 10자리는 **건물 등록 시점의 법정동코드로 동결**돼 이후 개편을
    #   반영하지 않으며, 끝 2자리(리 자리)도 이 파일에서는 사실상 구분자 역할을 못 한다.
    #   [측정 T018 §A-2] mgt[:10] 키 19,058개 중 **2,699개**가 서로 다른 리를 2개 이상 포함 →
    #   setdefault 가 첫 값만 남기고 **2,698개 키가 붕괴**했다. 이것이 lawd_ri 오염의 근본 원인이다.
    # ★ 정합률 수치 주의 (T018 검증 반영, 2026-08-10) — **아래 두 값을 직접 비교하지 말 것.**
    #   · 구 기준선 97.08% (6,884,543건 중 201,298건 불일치)
    #     → **[재현 실패 — 산출 정의 불명]**. 이 수치는 저장소 전역에 **산문으로만** 존재하고
    #       정의 SQL 이 어디에도 없다. 정의를 6가지(V1~V6)로 바꿔 전수 재시도했으나 어느 것도
    #       재현하지 못했다. 이력으로만 남긴다.
    #   · 실측 재구성치 **96.9384%** (대상 6,892,473 / 불일치 211,017)
    #     → 정의 V1 을 명시한 값: kind='addr' AND ri 비어있지 않음 AND jibun 존재 →
    #       jibun 첫 토큰 == ri. 불일치 분해 = 지번에 리 토큰 자체가 없음 145,317
    #       + 리 토큰이 있으나 ri 와 다름 65,700.
    #   두 값은 **정의가 다르므로 비교가 성립하지 않는다.** "97.08% → 96.94% = 회귀"로 읽으면 오독이다.
    #   근거: 검증 보고서 `verification-report.md` §4-1·§4-2 (기준선 재현 실패 절).
    # ※ 이번 수정의 범위는 **관측뿐**이다 — 값 선택 규칙(첫 값 유지)은 그대로 두어 산출물이 바뀌지
    #   않게 한다. 키를 무엇으로 바꿀지(예: mgt[:10]+리명, 지번 기반)는 원천 재적재를 수반하므로
    #   후속 태스크에서 결정한다. 지금은 "조용히 뭉개던 것"을 "세어서 알리는 것"으로만 바꾼다.
    p = src / f"match_jibun_{sido}.txt"; d = {}; nm = {}; rd = {}
    if not p.exists(): return d, nm, rd
    coll = 0                                    # 이 시도에서 발생한 리 충돌(다른 값 덮어쓰기 시도) 건수
    for line in io.open(p, encoding="cp949", errors="replace"):
        c = line.rstrip("\n").split("|")
        if len(c) < 19: continue
        mgt = c[18]
        ri = c[4].strip()                       # 리(里) — 면 단위 지번에 보존(없으면 동/법정동만)
        dong = f"{c[3]} {ri}" if ri else c[3]
        nm.setdefault(mgt[:10], dong)
        if ri:                                  # 리 분리 컬럼(X5) — emd 문자열 합성과 별개로 보존
            k = mgt[:10]; prev = rd.get(k)
            if prev is None:
                rd[k] = ri                      # 첫 값 채택 — setdefault 와 동일(동작 불변)
            elif prev != ri:                    # 같은 키에 **다른** 리 → 구 코드가 조용히 삼키던 충돌
                coll += 1
                RI_KEY_COLLISIONS[k].add(ri)
                if coll <= RI_COLLISION_SAMPLE:
                    print(f"    [리 충돌] {sido} bcode10={k} 채택='{prev}' 무시='{ri}' "
                          f"(지번='{c[3]} {ri}')", file=sys.stderr)
        if mgt in d: continue
        san = "산 " if c[5] == "1" else ""; bu = c[7]
        d[mgt] = f"{dong} {san}{c[6]}" + (f"-{bu}" if bu and bu != "0" else "")
    if coll:
        RI_COLLISION_ROWS[sido] = coll
        print(f"    [리 충돌] {sido}: 행 {coll:,}건 / 붕괴 키 "
              f"{sum(1 for v in RI_KEY_COLLISIONS.values() if v):,}건(누적)", file=sys.stderr)
    return d, nm, rd

def add_juso(db, src, only, state):
    pid = state["pid"]; seen = state["seen"]
    for s in [x for x in SIDO if (not only or x in only)]:
        path = src / f"match_build_{s}.txt"
        if not path.exists():
            print(f"  (건너뜀) {path.name} 없음", file=sys.stderr); continue
        jdict, jname, jridict = load_jibun(src, s)
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
            jb=jdict.get(mgt) or _derive_jibun(jname, mgt)       # 대표지번 없으면 건물관리번호로 파생(빠짐없이)
            pb.append((pid,'addr',None,None,c[1],c[2],c[3],jridict.get(mgt[:10]),road,rn,mno,sno,bld,c[9],c[14],mgt,c[0],c[13],None,None,jb,None,None,'navi',1,lon,lat))  # bcode=c[0]·hcode=c[13]·ri=리(X5,mgt[:10]키)
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
        oc=CW_OSM.get(sub or ''); oc1,oc2=(oc[0],oc[1] or None) if oc else (None,None)   # canonical 카테고리
        pb.append((pid,typ,name,sub,None,None,None,None,None,None,None,None,None,None,None,None,None,None,None,None,None,oc1,oc2,'osm',1,lon,lat))  # +ri자리 None(X5)
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
    # facility(생활편의) 중 좌표 없는 행(주소만)은 pending 으로 모아 반환 → geocode_facilities 가 navi 로 지오코딩.
    import csv, glob
    pid=state["pid"]; st=time.time(); n0=pid; pb=[]; fb=[]; rb=[]; pending=[]
    for path in sorted(glob.glob(os.path.join(csvdir,"**","*.csv"), recursive=True)):
        base = os.path.basename(path)   # 출처·종류 구분(파일명)
        if base == 'facility_clean.csv': src, kind = 'facility', 'facility'   # 생활편의시설 — biz 와 분리 적재
        elif base == 'localdata_clean.csv': src, kind = 'localdata', 'biz'
        else: src, kind = 'sangga', 'biz'
        with open(path, encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                nm=(row.get("상호명") or "").strip()
                if not nm or nm in ("업소명없음", "상호명없음", "-", "."): continue   # 원본 플레이스홀더 제외
                biz=(row.get("상권업종소분류명") or "").strip()
                sido=(row.get("시도명") or "").strip(); sgg=(row.get("시군구명") or "").strip()
                haeng=(row.get("행정동명") or "").strip() or None   # CSV 행정동명 → haeng_dong 자리/폴백 전용(emd 아님; M2)
                phone=(row.get("전화번호") or "").strip() or None; opened=(row.get("인허가일자") or "").strip() or None
                cat1=(row.get("상권업종대분류명") or "").strip() or None
                cat2=(row.get("상권업종중분류명") or "").strip() or None
                if kind=='facility':     # 시설=좌표없으면 이 파싱이 곧 좌표 → navi 도로명 사전 최장일치(더러운입력 견고) + 시군구 보정
                    road,rn,mno,sno,g_sido,g_sgg = parse_doro_gaz(row.get("도로명주소") or "")
                    if g_sido: sido=g_sido
                    if g_sgg is not None: sgg=g_sgg
                else:                    # 상가/인허가=이미 좌표보유(주소 깨끗) → 기존 regex(건물키 조인용)
                    road,rn,mno,sno = parse_doro(row.get("도로명주소") or "")
                jibun_txt=(row.get("지번주소") or "").strip() or None
                sgg = _norm_sgg(sido, sgg)                          # 결정4(M1): 시군구 navi 표기 정규화(완전일치만; g_sgg 보정 후 입력)
                bjd, ri = _split_emd_ri(jibun_txt, sido, sgg)       # 결정1·2: 지번 텍스트 → 법정동(bjd)/리(ri). 첫 원소=법정동
                if src=='localdata':                                   # localdata 카테고리를 canonical 로 표준화
                    cc=CW_LD.get(f"{cat1}|{biz}")
                    if cc: cat1,cat2=cc[0],(cc[1] or None)
                try: lon=float(row.get("경도") or ""); lat=float(row.get("위도") or "")
                except ValueError: lon=lat=None
                if lon is None or not (124<=lon<=132 and 33<=lat<=39):
                    if kind=='facility':                               # 좌표없는 시설 → 도로명 또는 지번으로 지오코딩 대기
                        jibun_src = jibun_txt or ((row.get("도로명주소") or "").strip() or None)   # 지번칸 없으면 도로명칸(지번 기입분)
                        if (rn and mno is not None) or jibun_src:
                            pending.append((nm,biz,sido,sgg,haeng,road,rn,mno,sno,jibun_txt,cat1,cat2,jibun_src))  # rec[4]=haeng(행정동명) — emit 폴백 전용(M2)
                    continue
                pid+=1
                pb.append((pid,kind,nm,biz,sido,sgg,bjd,ri,road,rn,mno,sno,biz,None,haeng,None,None,None,phone,opened,jibun_txt,cat1,cat2,src,0,round(lon,6),round(lat,6)))  # emd=bjd(법정동)·ri=ri·haeng_dong=haeng(B2)
                fb.append((pid,nm," ".join(x for x in (sido,sgg,bjd,ri,haeng) if x),'',biz))   # FTS region: 시도·시군구·법정동·리·행정동(navi region 과 일관)
                rb.append((pid,lon,lon,lat,lat))
                if len(pb)>=50000: _flush(db,pb,fb,rb); pb.clear(); fb.clear(); rb.clear()
    _flush(db,pb,fb,rb)
    print(f"  biz: +{pid-n0:,}  ({time.time()-st:.1f}s)", file=sys.stderr); state["pid"]=pid
    return pending


def geocode_facilities(db, pending, state):
    # 좌표 없는 facility 행 → navi 좌표. 1) 도로명 사전키 조인  2) 실패 시 지번(법정동+본/부번) 폴백.
    # navi 미존재 주소는 좌표없는 POI 라 버린다(insert 안 함). FTS·rtree 까지 정상행만 적재.
    # rec = (nm,biz,sido,sgg,haeng,road,rn,mno,sno,jibun_txt,cat1,cat2,jibun_src)  ← rec[4]=CSV 행정동명(haeng), emd 폴백 전용(M2)
    if not pending: return
    st=time.time()
    db.execute("CREATE INDEX IF NOT EXISTS idx_addr_key ON places(sido,sigungu,road_norm,main_no,sub_no) WHERE kind='addr'")
    cur=db.cursor(); pb=[]; fb=[]; rb=[]
    def emit(rec, lon, lat, n_bjd, n_ri, n_haeng):     # navi addr 매칭행의 법정동(n_bjd)/리(n_ri)/행정동(n_haeng) 캐리(결정3)
        nm,biz,sido,sgg,haeng_csv,road,rn,mno,sno,jibun_txt,cat1,cat2,_js = rec   # rec[4]=haeng_csv(CSV 행정동명)
        haeng = n_haeng or haeng_csv                  # navi c[14] 우선, null이면 CSV 행정동명 폴백(결정3/M2)
        state["pid"]+=1; pid=state["pid"]
        pb.append((pid,'facility',nm,biz,sido,sgg,n_bjd,n_ri,road,rn,mno,sno,biz,None,haeng,None,None,None,None,None,jibun_txt,cat1,cat2,'facility',1,round(lon,6),round(lat,6)))  # emd=n_bjd·ri=n_ri·haeng_dong=haeng(B2)
        fb.append((pid,nm," ".join(x for x in (sido,sgg,n_bjd,n_ri,haeng) if x),'',biz)); rb.append((pid,lon,lon,lat,lat))
        if len(pb)>=50000: _flush(db,pb,fb,rb); pb.clear(); fb.clear(); rb.clear()
    n_road=0; unmatched=[]
    for rec in pending:                                  # 1) 도로명 조인
        sido,sgg,rn,mno,sno = rec[2],rec[3],rec[6],rec[7],rec[8]
        r=cur.execute("""SELECT lon,lat,emd,ri,haeng_dong FROM places WHERE kind='addr'
            AND sido=? AND sigungu=? AND road_norm=? AND main_no=? AND sub_no=? ORDER BY id LIMIT 1""",
            (sido,sgg,rn,mno,sno)).fetchone() if (rn and mno is not None) else None
        if r: emit(rec, r[0], r[1], r[2], r[3], r[4]); n_road+=1   # r[2]=법정동→n_bjd, r[3]=ri, r[4]=haeng_dong
        elif rec[12]:                                    # 도로 실패 → 지번키 후보
            k=_facility_jibun_key(rec[12])
            if k: unmatched.append((rec,k))
    _flush(db,pb,fb,rb); pb.clear(); fb.clear(); rb.clear()
    n_jibun=0
    if unmatched:                                        # 2) 지번 폴백 — 필요한 시군구의 addr 만 스캔해 지번키→좌표
        need={k for _,k in unmatched}; need_sgg={(k[0],k[1]) for k in need}
        sggs=sorted({g for _,g in need_sgg}); ph=",".join("?"*len(sggs)); jc={}
        for s,g,jb,lo,la,e,ri_,hd in db.execute(
                f"SELECT sido,sigungu,jibun,lon,lat,emd,ri,haeng_dong FROM places WHERE kind='addr' AND jibun IS NOT NULL AND sigungu IN ({ph})", sggs):
            if (s,g) not in need_sgg: continue            # sigungu 동명이(시도 다름) 걸러냄
            kk=_navi_jibun_key(s,g,jb)
            if kk in need and kk not in jc: jc[kk]=(lo,la,e,ri_,hd)   # e=법정동(emd)→n_bjd, ri_=리, hd=haeng_dong 캐리
        for rec,k in unmatched:
            co=jc.get(k)
            if co: emit(rec, co[0], co[1], co[2], co[3], co[4]); n_jibun+=1   # co[2]=법정동→n_bjd
        _flush(db,pb,fb,rb)
    db.execute("DROP INDEX IF EXISTS idx_addr_key")
    print(f"  [geocode] facility 도로 {n_road:,} + 지번폴백 {n_jibun:,} = {n_road+n_jibun:,}/{len(pending):,} ({time.time()-st:.1f}s)", file=sys.stderr)


def build_gazetteer(db):
    """navi addr(kind='addr')에서 도로명 사전(GAZ) 구축 — parse_doro_gaz 가 더러운 주소를 실재 도로명에 최장일치시킴.
    add_juso 직후(navi 적재 완료) 1회 호출. (시도,시군구)→도로명집합, 시도→시군구목록(화이트리스트)."""
    import collections
    st=time.time(); road=collections.defaultdict(set); sgg=collections.defaultdict(set)
    for s,g,rn in db.execute("SELECT DISTINCT sido,sigungu,road_norm FROM places WHERE kind='addr' AND road_norm IS NOT NULL"):
        road[(s,g)].add(rn); sgg[s].add(g)
    GAZ["road"]={k:sorted(v,key=len,reverse=True) for k,v in road.items()}
    GAZ["sgg"]={k:sorted(v,key=len,reverse=True) for k,v in sgg.items()}
    print(f"  [gazetteer] 도로명 {sum(len(v) for v in road.values()):,}개 · {len(road):,}시군구 ({time.time()-st:.0f}s)", file=sys.stderr)

def _flush(db,pb,fb,rb):
    if not pb: return
    db.executemany("INSERT INTO places VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",pb)
    db.executemany("INSERT INTO places_fts(rowid,name,region,road,bld) VALUES(?,?,?,?,?)",fb)
    db.executemany("INSERT INTO place_rtree VALUES(?,?,?,?,?)",rb)

def write_taxonomy(db, out_path):
    """biz의 대>중>소 분류 트리를 style/poi-taxonomy.json 으로 재생성(스튜디오 티어/아이콘 목록용)."""
    import collections
    tree = collections.OrderedDict(); cnt = collections.Counter()
    q = ("SELECT cat1,cat2,subtype,count(*) c FROM places WHERE kind IN ('biz','facility') AND cat1 IS NOT NULL "
         "GROUP BY cat1,cat2,subtype ORDER BY cat1, c DESC")
    for cat1, cat2, sub, c in db.execute(q):
        cnt[cat1] += c
        t = tree.setdefault(cat1, collections.OrderedDict())
        m = t.setdefault(cat2 or "(기타)", [])
        if sub and sub not in m:
            m.append(sub)
    ordered = collections.OrderedDict(sorted(tree.items(), key=lambda kv: -cnt[kv[0]]))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps({"cat1_order": list(ordered.keys()), "tree": ordered}, ensure_ascii=False),
                        encoding="utf-8")


def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--src", default="/Users/jaechango_cudo/Downloads/지도정보/202605_내비게이션용DB_전체분")
    ap.add_argument("--osm", default=os.path.join(os.environ.get("BUILD_HOME") or os.path.expanduser("~/geocode-build"), "osm.sqlite"))
    ap.add_argument("--out", default=os.path.join(os.environ.get("BUILD_HOME") or os.path.expanduser("~/geocode-build"), "geocode.sqlite"))
    ap.add_argument("--only")
    ap.add_argument("--poi-csv-dir", help="소상공인 상가(상권)정보 CSV 폴더(시도별)")
    ap.add_argument("--dedup", choices=["legacy","er"], default="legacy",
                    help="biz 표시용 중복제거: legacy=정규화상호+좌표3자리 1패스(기본), er=엔티티해상도(dedup_er.py: 셀이웃 블로킹+등급가중점수+union-find)")
    ap.add_argument("--areas", help="행정경계 areas.sqlite(06-gen-areas 산출) — 역지오코딩 동 폴리곤 적재")
    args=ap.parse_args()
    only=set(args.only.split(",")) if args.only else None
    cwp=pathlib.Path(__file__).resolve().parent/"cat-crosswalk.json"   # 카테고리 표준화 매핑(localdata/osm→canonical)
    if cwp.exists():
        _cw=json.load(open(cwp,encoding="utf-8")); CW_LD.update(_cw.get("localdata",{})); CW_OSM.update(_cw.get("osm",{}))
        print(f"  cat-crosswalk: localdata {len(CW_LD)} · osm {len(CW_OSM)}", file=sys.stderr)
    out=pathlib.Path(args.out); out.parent.mkdir(parents=True, exist_ok=True)
    if not args.areas:                                   # 관례 경로의 행정경계 자동 적재(06-gen-areas 산출 areas.sqlite)
        cand = out.parent / "areas.sqlite"
        if cand.exists(): args.areas = str(cand)
    tmp=out.with_suffix(".sqlite.tmp"); tmp.unlink(missing_ok=True)
    db=sqlite3.connect(tmp); db.executescript(SCHEMA)
    t0=time.time(); state={"pid":0,"seen":set()}
    print(f"[통합 지오코드 빌드] juso={args.src}\n  osm={args.osm}", file=sys.stderr)
    add_juso(db, pathlib.Path(args.src), only, state)
    build_gazetteer(db)   # navi 도로명 사전 — 시설 주소(더러운 입력) 견고 파싱용(parse_doro_gaz)
    add_osm(db, args.osm, state)
    if args.areas and pathlib.Path(args.areas).exists():     # 행정경계 폴리곤(06-gen-areas) — reverse point-in-polygon용
        ar = sqlite3.connect(f"file:{args.areas}?mode=ro", uri=True)   # ATTACH 아님: rtree 는 cross-attach INSERT 시 'database is locked'
        db.executemany("INSERT INTO areas(id,name,type,rings,code) VALUES(?,?,?,?,?)",
                       ar.execute("SELECT id,name,type,rings,code FROM areas"))
        db.executemany("INSERT INTO area_rtree VALUES(?,?,?,?,?)",
                       ar.execute("SELECT id,minlon,maxlon,minlat,maxlat FROM area_rtree"))
        ar.close()
        print(f"  areas ← {args.areas}: {db.execute('SELECT count(*) FROM areas').fetchone()[0]:,}", file=sys.stderr)
    pending_fac = add_biz(db, args.poi_csv_dir, state) if args.poi_csv_dir else []
    geocode_facilities(db, pending_fac, state)   # 좌표없는 시설(주소만) → navi 도로명주소 지오코딩
    # biz 중복(상가↔LOCALDATA 같은 점포) 표시용 대표 선정 — 출처(source)는 모두 보존하되 그룹당 1건만 is_primary=1.
    db.create_function("nrm", 1, biznrm)   # facility 충돌숨김(아래)에서도 사용 → dedup 방식과 무관하게 등록
    if args.dedup == "er":
        # 엔티티해상도: (선택)건물키 backfill → 셀이웃 블로킹 + 등급가중점수 + 주소/좌표 TF + union-find (dedup_er.py).
        sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
        from dedup_er import dedup_er
        # navi 조인 backfill: biz 행에 건물키(bd_mgt_sn) — sido+sigungu+road_norm+본/부번 일치 navi addr 의 최근접값.
        # 미파싱·미일치는 NULL 유지(dedup_er가 좌표밀도 TF로 폴백). 부분인덱스 후 상관서브쿼리(수백만행→수분), 끝나면 인덱스 제거.
        db.execute("CREATE INDEX IF NOT EXISTS idx_addr_key ON places(sido,sigungu,road_norm,main_no,sub_no) WHERE kind='addr'")
        # 조인키(sido+sigungu+road_norm+본/부번)가 같은 건물을 특정 → 매칭 addr 은 동일 bd_mgt_sn.
        # 좌표 최근접 tiebreak 은 서브쿼리 ORDER BY 에서 바깥 places.lon/lat 을 참조해야 하는데,
        # 낮은 버전 SQLite(빌드 호스트)가 ORDER BY 절의 바깥참조를 해석 못해 "no such column" 으로 실패.
        # 거의 동일 결과인 결정적 inner 정렬(bd_mgt_sn)로 대체 → 전 버전 호환.
        db.execute("""UPDATE places SET bd_mgt_sn=(
            SELECT a.bd_mgt_sn FROM places a WHERE a.kind='addr'
              AND a.sido=places.sido AND a.sigungu=places.sigungu
              AND a.road_norm=places.road_norm AND a.main_no=places.main_no AND a.sub_no=places.sub_no
            ORDER BY a.bd_mgt_sn LIMIT 1)
          WHERE kind='biz' AND road_norm IS NOT NULL AND main_no IS NOT NULL""")
        db.execute("DROP INDEX IF EXISTS idx_addr_key")
        nbiz = db.execute("SELECT count(*) FROM places WHERE kind='biz'").fetchone()[0]
        nmat = db.execute("SELECT count(*) FROM places WHERE kind='biz' AND bd_mgt_sn IS NOT NULL").fetchone()[0]
        print(f"  [backfill] biz 건물키(bd_mgt_sn) 매칭 {nmat:,}/{nbiz:,} ({100*nmat/max(nbiz,1):.0f}%)", file=sys.stderr)
        dedup_er(db)
    else:
        # legacy: 정규화상호 + 좌표3자리(≈90~110m) 그룹당 1건만 is_primary=1. 우선순위 LOCALDATA>sangga, 동률은 작은 id.
        db.execute("""UPDATE places SET is_primary=1 WHERE id IN (
            SELECT id FROM (SELECT id, ROW_NUMBER() OVER (
                PARTITION BY nrm(name), round(lon,3), round(lat,3)
                ORDER BY CASE source WHEN 'localdata' THEN 0 WHEN 'sangga' THEN 1 ELSE 2 END, id) rn
              FROM places WHERE kind='biz') WHERE rn=1)""")
    # 생활편의시설(kind='facility')은 biz 와 별도 — 전부 표시(is_primary=1). 단 낚시터·세차장은 상가/인허가(biz) 와 겹치면 숨김.
    db.execute("UPDATE places SET is_primary=1 WHERE kind='facility'")
    # 충돌숨김은 '어떤 biz 행이든' 동일 nrm+좌표면 적용(b.is_primary 조건 제거) — ER 대표가 이름다른 출처로
    # 승급돼도 견고. nrm(=biznrm)은 corp/branch 미제거라 legacy/er 양쪽에서 동일 동작.
    db.execute("""UPDATE places SET is_primary=0 WHERE kind='facility' AND subtype IN ('낚시터','세차장')
        AND EXISTS (SELECT 1 FROM places b WHERE b.kind='biz'
          AND nrm(b.name)=nrm(places.name) AND round(b.lon,3)=round(places.lon,3) AND round(b.lat,3)=round(places.lat,3))""")
    db.execute("CREATE TABLE meta(k TEXT,v TEXT)")
    db.executemany("INSERT INTO meta VALUES(?,?)", [("places",str(state["pid"])),("srid","4326"),
        ("source","내비게이션용DB 2026.05 + OSM"),("built_s",f"{time.time()-t0:.0f}")])
    db.execute("INSERT INTO places_fts(places_fts) VALUES('optimize')")
    db.commit(); db.close(); tmp.replace(out)
    sz=out.stat().st_size/1048576
    print("="*56); print(f"OK: {out}  총 {state['pid']:,}건 · {sz:.0f}MB · {time.time()-t0:.0f}s")
    # ---- 리(里) 키 충돌 총계 (T018 A-4) — 산출물 품질 경고. 빌드는 실패시키지 않는다.
    _nk = len(RI_KEY_COLLISIONS); _nr = sum(RI_COLLISION_ROWS.values())
    if _nk:
        _top = sorted(RI_KEY_COLLISIONS.items(), key=lambda kv: -len(kv[1]))[:5]
        print(f"  [경고] 리 키 충돌: 붕괴 키 {_nk:,}개 / 충돌 행 {_nr:,}건 "
              f"— 이만큼의 places.ri 가 부정확할 수 있음(첫 값 채택).", file=sys.stderr)
        print("         상위: " + ", ".join(f"{k}({len(v)+1}종)" for k, v in _top), file=sys.stderr)
        print("         원인·후속: load_jibun 주석 및 T018 §A-2 참조(키 설계 변경은 원천 재적재 동반).",
              file=sys.stderr)
    else:
        print("  리 키 충돌: 0건", file=sys.stderr)
    # 카테고리 분류 트리(스튜디오 티어/아이콘 목록용) — 빌드마다 최신 카테고리로 재생성
    try:
        rdb = sqlite3.connect(f"file:{out}?mode=ro", uri=True)
        write_taxonomy(rdb, ROOT / "style" / "poi-taxonomy.json"); rdb.close()
        print(f"  taxonomy → {ROOT / 'style' / 'poi-taxonomy.json'}")
    except Exception as e:
        print(f"  (taxonomy 재생성 스킵: {e})", file=sys.stderr)

if __name__=="__main__":
    main()
