#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""biz(상가·인허가) 표시용 중복제거 — 엔티티해상도(ER) 방식. 09-gen-geocode.py 의 legacy 1패스 SQL 대체(opt-in).

설계근거(검증완료): 업계표준(Uber 특허 US11321579B2)·확률링키지(Fellegi-Sunter/Splink)와 정합.
  · 블로킹: 고정격자 round(lon,3) 대신 3×3 셀이웃 윈도 — 격자경계 false-negative 제거.
  · 정규화: corenrm(법인접두사·지점토큰 제거). 지점토큰은 **공백분리 마지막토큰**만 제거(탐욕 과매칭 방지).
  · 점수: 이름을 하드게이트로 쓰지 않고 **등급형 가중점수**(name/거리/전화). 단 AUTO 병합은
          '강신호(강한 이름일치 또는 고유전화 일치)'를 1개 이상 요구 — 근접/약한이름 단독으론 AUTO 금지(REVIEW).
  · 상관신호 보정: 같은건물 다점포(푸드코트·주상복합)에서 근접 증거력이 죽어야 함 →
          좌표밀도 TF(3×3 이웃버킷 평활)로 거리 가중치 down-weight. 전화는 빈도 IDF로 공유회선 감쇠.
  · 군집: union-find 연결요소. 대표선정 localdata<sangga<기타→정보충실(전화보유)→작은 id (is_primary=1).
한계(이번 단계): biz 행에 건물키(bd_mgt_sn)·구조화주소가 NULL이라 주소 TF는 미적용(2단계에서
  navi 조인 backfill 후 location_weight 가 건물키 기반으로 승급). 현재는 좌표밀도 TF로 다점포를 방어.
성능: 3×3 셀블로킹으로 O(Σ cell_k²). corenrm/trigram·밀도·전화빈도는 1패스 사전계산(쌍마다 재계산 안 함).
  과밀셀은 셀중심 거리순 상한(MAX_CELL)으로 비교폭주 차단(누락분 로그). 전역 비교쌍 집합 미보관(union 멱등).
"""
import math, re, sys, unicodedata

# ---- 정규화 ----
_CORP   = re.compile(r"(주식회사|유한회사|유한책임회사|합자회사|합명회사|재단법인|사단법인|의료법인|\(주\)|㈜|\(유\)|\(재\)|\(사\))")
# 지점표시 토큰(공백분리 마지막 토큰에만 적용 → '파리바게뜨신촌점' 같은 무공백 상호를 깎지 않음)
_BRANCH_TOK = re.compile(r"^(본점|직영점|가맹점|영업소|지점|\d{1,3}호점|.{1,5}점)$")
_PUNCT  = re.compile(r"[\s()\[\]{}<>（）【】·.,/&\-]+")
_REP    = re.compile(r"^(1[5-6]\d{2}|070|060|1588|1577|1644|1666)")

def biznrm(s):                                   # 09 의 biznrm 과 동일사상(괄호/구두점/공백 제거+lower)
    return _PUNCT.sub("", unicodedata.normalize("NFKC", s or "")).lower()

def corenrm(s):
    t = unicodedata.normalize("NFKC", s or "")
    t = _CORP.sub("", t)
    toks = t.split()
    if len(toks) >= 2 and _BRANCH_TOK.match(toks[-1]):   # 마지막 공백토큰이 지점표시면 제거
        toks = toks[:-1]
    core = biznrm(" ".join(toks))
    return core or biznrm(s)                      # 전부 깎이면 원본 폴백

def branch_of(s):
    toks = unicodedata.normalize("NFKC", s or "").split()
    if len(toks) >= 2 and _BRANCH_TOK.match(toks[-1]):
        return biznrm(toks[-1])
    return None

def _trigrams(s):
    s = f"  {s} "
    return {s[i:i+3] for i in range(len(s)-2)}

def name_sim(a, b):                              # (외부/테스트용) 문자 3-gram Dice — corenrm 위에서
    ca, cb = corenrm(a), corenrm(b)
    if not ca or not cb: return 0.0
    return _sim_tris(_trigrams(ca), _trigrams(cb))

def _sim_tris(A, B):
    return 2*len(A & B)/(len(A)+len(B)) if (A or B) else 0.0

def is_rep(p): return bool(_REP.match(re.sub(r"\D", "", p or "")))
def _digits(p): return re.sub(r"\D", "", p or "")

def haversine_m(lon1, lat1, lon2, lat2):
    R = 6371000.0; p1, p2 = math.radians(lat1), math.radians(lat2)
    dla = math.radians(lat2-lat1); dlo = math.radians(lon2-lon1)
    h = math.sin(dla/2)**2 + math.cos(p1)*math.cos(p2)*math.sin(dlo/2)**2
    return 2*R*math.asin(math.sqrt(h))

# ---- Fellegi-Sunter 가중치(log2(m/u), bits) ----
PRIOR        = math.log2(0.10/0.90)              # 블록내 사전 매치확률 0.10 → -3.17
STRONG_NAME  = 9.45                              # 강한 이름일치 가중치(= AUTO 자격 강신호)
PHONE_FULL   = 9.97                              # 고유전화 일치 가중치(= AUTO 자격 강신호)
AUTO, REVIEW = 0.90, 0.50

def _name_weight(sim, short):
    if short: return STRONG_NAME if sim >= 0.80 else -1.32   # 짧은상호(≤3): 거의-정확 일치만 신호(우연 3gram 방어)
    return STRONG_NAME if sim >= 0.60 else 2.0 if sim >= 0.30 else -1.32

def _phone_weight(pa, pb, rep_a, rep_b, share):
    if pa and pb and pa == pb:
        if rep_a or rep_b: return 0.0            # 전국대표번호(1588 등) 공유 → 증거 0
        if share >= 10:    return 0.0            # 빌딩 대표/체인 본사/플랫폼 회선 → 변별력 없음(IDF)
        if share >= 3:     return PHONE_FULL*0.5
        return PHONE_FULL                        # 고유 유선/휴대 일치 → 강증거
    if pa and pb: return -1.0                    # 둘 다 있고 다름 → 약한 음
    return 0.0

def _dist_weight(d):
    return 5.3 if d <= 20 else 3.6 if d <= 60 else 0.0 if d <= 150 else -3.32
def _density_factor(dmax):                       # 좌표밀도 TF: 밀집할수록 근접 증거력↓
    return min(1.0, 2.0/dmax) if dmax > 0 else 1.0
def _addr_weight(share):                         # 같은 건물키 공유 distinct상호 수 → 적을수록 강증거(within-building 변별)
    if share <= 1: return 6.0                     # 단독건물: 같은 주소면 거의 같은 점포
    return max(0.0, 6.0 - math.log2(share))       # 50점포 건물 → ≈0 (다점포 방어)
def _pr(M): return (2.0**M)/(1.0+2.0**M)

def _decide(a, b):
    """(decision, pr) — decision ∈ {'AUTO','REVIEW','NO'}. a,b = 사전계산 dict."""
    if a["branch"] and b["branch"] and a["branch"] != b["branch"]:
        return "NO", 0.0                          # 지점 다르면 분리(안전 게이트)
    if not a["core"] or not b["core"]:
        sim = 0.0
    else:
        sim = _sim_tris(a["tris"], b["tris"])
    short = min(len(a["core"]), len(b["core"])) <= 3
    nw  = _name_weight(sim, short)
    pw  = _phone_weight(a["pd"], b["pd"], a["rep"], b["rep"], max(a["pshare"], b["pshare"]))
    ba, bb = a["bld"], b["bld"]
    if ba and bb:                                 # 건물키 둘 다 있음 → 주소 TF 신뢰(좌표와 합산 안 함; 강상관)
        lw = _addr_weight(max(a["bldshare"], b["bldshare"])) if ba == bb else -3.32
    else:                                         # 건물키 없음 → 좌표밀도 보정 거리(폴백)
        d  = haversine_m(a["lon"], a["lat"], b["lon"], b["lat"])
        lw = _dist_weight(d) * _density_factor(max(a["dens"], b["dens"]))
    M   = PRIOR + nw + pw + lw
    pr  = _pr(M)
    dec = "AUTO" if pr >= AUTO else "REVIEW" if pr >= REVIEW else "NO"
    # 강신호 게이트(P4): 강한 이름 OR 고유전화 없이는 근접/약한이름 단독 AUTO 금지 → 검수
    strong = (nw >= STRONG_NAME) or (pw >= PHONE_FULL)
    if dec == "AUTO" and not strong:
        dec = "REVIEW"
    # 전화 단독 게이트(P1): 전화만 강신호이고 이름불일치·근접증거 없으면 자동병합 금지 → 검수
    if dec == "AUTO" and pw >= PHONE_FULL and sim < 0.30 and lw <= 0:
        dec = "REVIEW"
    return dec, pr

# ---- union-find ----
class _UF:
    def __init__(self, ids): self.p = {i: i for i in ids}
    def find(self, x):
        r = x
        while self.p[r] != r: r = self.p[r]
        while self.p[x] != r: self.p[x], x = r, self.p[x]
        return r
    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb: self.p[ra] = rb

CELL = 0.0006           # ≈55m(경도). 3×3 이웃 = ≈165m 윈도
DBUCKET = 4             # 좌표밀도 버킷 소수4자리 ≈ 11m
MAX_CELL = 1200         # 셀당 비교후보 상한(과밀셀 폭주 방지) — 셀중심 거리순 절단, 누락분 로그
_REP_PRIORITY = {"localdata": 0, "sangga": 1}
_STEP = 10**(-DBUCKET)

def dedup_er(db, log=lambda m: print(m, file=sys.stderr), emit_review=False):
    """places(kind='biz')에 대해 ER 군집화 → is_primary 설정. emit_review=True면 REVIEW 쌍을 dedup_review 테이블에."""
    rows = db.execute(
        "SELECT id,name,lon,lat,COALESCE(source,''),COALESCE(phone,''),COALESCE(bd_mgt_sn,'') "
        "FROM places WHERE kind='biz' AND lon IS NOT NULL AND lat IS NOT NULL").fetchall()
    if not rows:
        log("  [dedup_er] biz 행 없음"); return
    # 1패스 사전계산: 좌표밀도 버킷, 전화빈도, 레코드 캐시(core/trigram 포함), 그리드
    bucket = {}; phone_n = {}
    for rid, name, lon, lat, source, phone, bld in rows:
        bk = (round(lon, DBUCKET), round(lat, DBUCKET)); bucket[bk] = bucket.get(bk, 0) + 1
        pd = _digits(phone)
        if pd: phone_n[pd] = phone_n.get(pd, 0) + 1
    def dens9(lon, lat):                          # 3×3 이웃버킷 합 — 건물 풋프린트 분절 평활(P5)
        bx, by = round(lon, DBUCKET), round(lat, DBUCKET)
        return sum(bucket.get((round(bx+i*_STEP, DBUCKET), round(by+j*_STEP, DBUCKET)), 0)
                   for i in (-1, 0, 1) for j in (-1, 0, 1))
    R = {}; grid = {}; bld_cores = {}
    for rid, name, lon, lat, source, phone, bld in rows:
        core = corenrm(name); pd = _digits(phone)
        R[rid] = {"lon": lon, "lat": lat, "source": source, "pd": pd, "rep": is_rep(phone),
                  "branch": branch_of(name), "core": core, "tris": _trigrams(core) if core else None,
                  "dens": dens9(lon, lat), "pshare": phone_n.get(pd, 0) if pd else 0, "bld": bld}
        if bld: bld_cores.setdefault(bld, set()).add(core)
        grid.setdefault((int(lon/CELL), int(lat/CELL)), []).append(rid)
    for rid in R:                                  # 건물키 공유 distinct상호 수(주소 TF) 사전계산
        R[rid]["bldshare"] = len(bld_cores[R[rid]["bld"]]) if R[rid]["bld"] else 0
    uf = _UF(R.keys()); n_pairs = n_auto = n_review = n_capped = 0
    prhist = [0]*10; rev = []
    for (cx, cy), ids in grid.items():
        cand = []
        for nx in (cx-1, cx, cx+1):
            for ny in (cy-1, cy, cy+1):
                cand += grid.get((nx, ny), [])
        if len(cand) > MAX_CELL:                   # 셀중심 거리순 절단(P3) — 가까운 후보 우선보존
            cxc, cyc = (cx+0.5)*CELL, (cy+0.5)*CELL
            cand.sort(key=lambda r: haversine_m(R[r]["lon"], R[r]["lat"], cxc, cyc))
            n_capped += len(cand) - MAX_CELL
            cand = cand[:MAX_CELL]
        for i in ids:
            ri = R[i]
            for j in cand:                         # union 멱등 → 이웃셀 중복방문 무해(전역 seen 불필요, P2)
                if j <= i: continue
                n_pairs += 1
                dec, pr = _decide(ri, R[j])
                prhist[min(9, int(pr*10))] += 1
                if dec == "AUTO":   uf.union(i, j); n_auto += 1
                elif dec == "REVIEW":
                    n_review += 1
                    if emit_review: rev.append((i, j, round(pr, 4)))
    # 군집 → 대표선정
    groups = {}
    for rid in R: groups.setdefault(uf.find(rid), []).append(rid)
    prim = []; nonprim = []
    for members in groups.values():
        members.sort(key=lambda rid: (_REP_PRIORITY.get(R[rid]["source"], 2),
                                       -(1 if R[rid]["pd"] else 0), rid))
        prim.append(members[0]); nonprim += members[1:]
    db.executemany("UPDATE places SET is_primary=1 WHERE id=?", [(i,) for i in prim])
    if nonprim:
        db.executemany("UPDATE places SET is_primary=0 WHERE id=?", [(i,) for i in nonprim])
    if emit_review:
        db.execute("CREATE TABLE IF NOT EXISTS dedup_review(id_a INT,id_b INT,pr REAL)")
        db.executemany("INSERT INTO dedup_review VALUES(?,?,?)", rev)
    log(f"  [dedup_er] biz {len(R):,}건 → 군집 {len(groups):,} (대표 {len(prim):,}) · "
        f"비교 {n_pairs:,} · AUTO병합 {n_auto:,} · REVIEW {n_review:,}"
        + (f" · ⚠과밀셀 누락 {n_capped:,}" if n_capped else ""))
    log(f"  [dedup_er] Pr분포(0.0~1.0, 0.1폭): {prhist}  (경계: REVIEW≥{REVIEW} AUTO≥{AUTO})")


# ============================ 자체 테스트 ============================
def _selftest():
    import sqlite3
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE places(id INTEGER PRIMARY KEY, kind TEXT, name TEXT, source TEXT, "
               "phone TEXT, bd_mgt_sn TEXT, lon REAL, lat REAL, is_primary INTEGER)")
    data = [
        (1, "스타벅스 역삼점", "sangga",   "02-555-1234", 127.03600, 37.50010, "S1"),   # 브랜드↔법인 → DUP
        (2, "(주)에스씨케이컴퍼니", "localdata", "02-555-1234", 127.03600, 37.50014, "S1"),
        (3, "김밥천국", "sangga",   "02-111-1111", 127.02700, 37.49800, "S2a"),          # 밀집다점포 → NOT
        (4, "스타벅스", "localdata", "02-222-2222", 127.02700, 37.49800, "S2b"),
        (5, "파리바게뜨 신촌점", "sangga",   "02-333-3333", 126.93600, 37.55500, "S3"),   # 이름일치 → DUP
        (6, "파리바게뜨", "localdata", "",            126.93600, 37.55504, "S3"),
        (7, "GS25 역삼1호점", "sangga",   "", 127.04000, 37.50100, "S4a"),               # 인접 동일브랜드 → NOT
        (8, "GS25 역삼2호점", "localdata", "", 127.04000, 37.50136, "S4b"),
        (9,  "옛날손칼국수", "sangga",   "", 127.01000, 37.48000, "S5a"),                 # 임차승계 → NOT
        (10, "행복공인중개사", "localdata", "", 127.01000, 37.48003, "S5b"),
        # P1 공유 02 유선(비대표) 다른상호 — share>=3(11,12,13 동일번호) + 근접(11,12) → 강신호 없음 → 미병합
        (11, "강남세무회계", "sangga",   "02-700-7000", 127.02000, 37.49500, "P1a"),
        (12, "리치공인중개사", "localdata", "02-700-7000", 127.02012, 37.49500, "P1b"),
        (13, "튼튼정형외과", "sangga",   "02-700-7000", 127.05000, 37.51000, "P1c"),
        # P4 접두사공유 짧은이름 근접(전화없음) → WEAK 단독 AUTO 금지 → 미병합
        (14, "연세약국", "sangga",   "", 127.06000, 37.52000, "P4a"),
        (15, "연세치과", "localdata", "", 127.06005, 37.52000, "P4b"),
    ]
    pool = ["행복약국","미소카페","한솥치킨","우리분식","예쁜미용실","으뜸세탁","바른문구","열린서점",
            "초록꽃집","황금빵집","대박정육","바다횟집","고향국밥","나폴리피자","수제버거","쫄깃떡집",
            "곱창맛집","왕대포","청춘호프","달빛주점","소나무한정식","장수족발","왕만두","손칼제면",
            "은하수노래방","스타PC방","빨강머리안경","시계나라","가구마을","포근침구","밝은조명","튼튼철물",
            "고운페인트","안전타이어","빠른정비","반짝세차","추억사진관","고려표구","무지개화방","소리악기",
            "꿈동산완구","편한신발","멋쟁이가방","산뜻의류","따뜻속옷","센스모자","비바우산","아늑커튼"]
    dense_ids = []
    for k, nm in enumerate(pool):
        rid = 100 + k; dense_ids.append(rid)
        data.append((rid, nm, "sangga", "", 127.02700, 37.49800, f"F{k}"))
    for rid, name, src, ph, lon, lat, _lab in data:
        db.execute("INSERT INTO places VALUES(?,?,?,?,?,?,?,?,0)", (rid, "biz", name, src, ph, "", lon, lat))
    dedup_er(db, emit_review=True)
    prim = {rid for (rid,) in db.execute("SELECT id FROM places WHERE is_primary=1")}
    def one_primary(*ids): return sum(1 for i in ids if i in prim) == 1   # 같은군집 → 정확히 1개 대표
    def all_primary(*ids): return all(i in prim for i in ids)             # 미병합 → 각자 대표
    dense_all = {3, 4, *dense_ids}
    checks = [
        ("S1 브랜드↔법인 병합(전화+근접)", one_primary(1, 2)),
        ("S1 대표=localdata(id2)",        2 in prim and 1 not in prim),
        ("S2 밀집다점포 미병합",           all_primary(3, 4)),
        ("S3 이름일치 병합",               one_primary(5, 6)),
        ("S3 대표=localdata(id6)",        6 in prim and 5 not in prim),
        ("S4 인접브랜드 미병합(지점)",      all_primary(7, 8)),
        ("S5 임차승계 미병합",             all_primary(9, 10)),
        ("P1 공유유선 단독 미병합",         all_primary(11, 12, 13)),
        ("P4 접두사공유 근접 미병합",       all_primary(14, 15)),
        ("밀집건물 50점포 전부 분리",       sum(1 for i in dense_all if i in prim) == 50),
        ("전체 군집수=61 (63행-병합2)",      len(prim) == 61),
    ]
    print("=" * 60)
    ok = all(res for _, res in checks)
    for label, res in checks:
        print(f"  {'✓' if res else '✗ FAIL'}  {label}")
    print(f"  대표(is_primary=1) 총 {len(prim)}건 · REVIEW기록 "
          f"{db.execute('SELECT count(*) FROM dedup_review').fetchone()[0]}건")
    print("=" * 60)
    print("RESULT:", "ALL PASS ✓" if ok else "FAIL ✗")
    return ok

def _selftest_bld():
    """건물키(bd_mgt_sn) 경로 — 2단계 주소 TF 검증."""
    import sqlite3
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE places(id INTEGER PRIMARY KEY, kind TEXT, name TEXT, source TEXT, "
               "phone TEXT, bd_mgt_sn TEXT, lon REAL, lat REAL, is_primary INTEGER)")
    rows = [
        (1, "스타벅스 역삼점", "sangga",   "", "BRARE", 127.03600, 37.50000),   # B1 브랜드↔법인·단독건물·전화없음
        (2, "(주)에스씨케이컴퍼니", "localdata", "", "BRARE", 127.03600, 37.50000),
        (3, "본죽 강남점", "sangga",   "", "BR2", 127.04000, 37.50100),          # B2 동일상호·동일건물 → 병합
        (4, "본죽", "localdata", "", "BR2", 127.04000, 37.50100),
        (5, "김밥천국", "sangga",   "", "BDENSE", 127.05000, 37.51000),          # B3 밀집건물 → 미병합
        (6, "왕족발집", "localdata", "", "BDENSE", 127.05000, 37.51000),
        (7, "GS25", "sangga",   "", "BX", 127.06000, 37.52000),                  # B4 동일상호·다른건물 → 미병합
        (8, "GS25", "localdata", "", "BY", 127.06010, 37.52000),
    ]
    pool = ["미소카페","한솥치킨","우리분식","예쁜미용실","으뜸세탁","바른문구","열린서점","초록꽃집",
            "황금빵집","대박정육","바다횟집","고향국밥","나폴리피자","수제버거","쫄깃떡집","곱창맛집",
            "청춘호프","달빛주점"]                                                # BDENSE 충진 18종(5,6과 합쳐 distinct 20)
    dense = [5, 6]
    for k, nm in enumerate(pool):
        rid = 200 + k; dense.append(rid)
        rows.append((rid, nm, "sangga", "", "BDENSE", 127.05000, 37.51000))
    for rid, name, src, ph, bld, lon, lat in rows:
        db.execute("INSERT INTO places VALUES(?,?,?,?,?,?,?,?,0)", (rid, "biz", name, src, ph, bld, lon, lat))
    dedup_er(db, emit_review=True)
    prim = {rid for (rid,) in db.execute("SELECT id FROM places WHERE is_primary=1")}
    revp = {frozenset((a, b)) for a, b in db.execute("SELECT id_a,id_b FROM dedup_review")}
    def one_primary(*ids): return sum(1 for i in ids if i in prim) == 1
    def all_primary(*ids): return all(i in prim for i in ids)
    checks = [
        ("B1 브랜드↔법인·단독건물 → 미병합+검수회부", all_primary(1, 2) and frozenset((1, 2)) in revp),
        ("B2 동일상호·동일건물 → 병합",              one_primary(3, 4)),
        ("B2 대표=localdata(id4)",                  4 in prim and 3 not in prim),
        ("B3 밀집건물 20점포 전부 분리",             sum(1 for i in dense if i in prim) == 20),
        ("B4 동일상호·다른건물 → 미병합",            all_primary(7, 8)),
    ]
    print("--- 건물키(2단계) 경로 ---")
    ok = all(res for _, res in checks)
    for label, res in checks:
        print(f"  {'✓' if res else '✗ FAIL'}  {label}")
    return ok

if __name__ == "__main__":
    a = _selftest()
    b = _selftest_bld()
    print("=" * 60)
    print("RESULT(전체):", "ALL PASS ✓" if (a and b) else "FAIL ✗")
    sys.exit(0 if (a and b) else 1)
