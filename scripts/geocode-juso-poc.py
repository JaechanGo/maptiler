#!/usr/bin/env python3
"""전체 도로명주소 지오코딩 PoC (토큰·실데이터 없이 로직 검증용).

행안부 건물DB+좌표를 적재했다고 가정한 '합성 샘플'로, docs/geocode-juso-plan.md 의
검증 시나리오가 실제로 통과하는지 증명한다. 무의존(표준 라이브러리만, in-memory SQLite).
컬럼 레이아웃은 활용가이드 ZIP 확정 전의 '대표 필드' 기준(논리 검증용).
"""
import re, sqlite3, unicodedata, math

# (시도, 시군구, 읍면동, 도로명표시, 본번, 부번, 건물명, lon, lat)
# 화성시엔 실제로 '구'가 없어 봉담읍 사용. '만세구'는 사용자 예시(가상)였음.
SAMPLE = [
    ("경기도", "화성시", "봉담읍", "3.1만세로", 5,   0, "",       126.9001, 37.2101),  # 타깃
    ("서울특별시", "종로구", "청운동", "3.1만세로", 5,   0, "",       126.9700, 37.5800),  # 동명이 도로(전국 중복)
    ("경기도", "화성시", "봉담읍", "3.1만세로", 5,   3, "래미안",  126.9005, 37.2105),  # 본번5-부번3
    ("경기도", "화성시", "봉담읍", "3.1만세로", 523, 0, "",       126.9100, 37.2200),  # prefix 오염 함정(523)
    ("경기도", "화성시", "봉담읍", "만세로",    12,  0, "",       126.9300, 37.2300),  # '만세로' vs '3.1만세로'
    ("경기도", "수원시", "팔달구", "효원로",    1,   0, "",       127.0286, 37.2636),
]

SIDO_ABBR = {"서울": "서울특별시", "경기": "경기도", "부산": "부산광역시", "인천": "인천광역시",
             "대구": "대구광역시", "광주": "광주광역시", "대전": "대전광역시", "울산": "울산광역시",
             "세종": "세종특별자치시", "충북": "충청북도", "충남": "충청남도", "전북": "전라북도",
             "전남": "전라남도", "경북": "경상북도", "경남": "경상남도", "강원": "강원특별자치도",
             "제주": "제주특별자치도"}


def normalize(s):
    """색인·질의 공용 정규화. 핵심: 숫자.숫자 → 점 제거(3.1만세로→31만세로)."""
    s = unicodedata.normalize("NFC", s)
    s = re.sub(r"\s+", " ", s).strip()
    for a, full in SIDO_ABBR.items():       # 약칭 확장 (단독 토큰일 때만)
        s = re.sub(rf"(^|\s){a}(?=\s|$)", lambda m, f=full: m.group(1) + f, s)
    s = re.sub(r"(?<=\d)\.(?=\d)", "", s)   # 3.1 → 31
    return s


def road_norm(road_display):
    return re.sub(r"[.\s]", "", unicodedata.normalize("NFC", road_display))


def build():
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.executescript("""
      CREATE TABLE places(id INTEGER PRIMARY KEY, sido TEXT, sigungu TEXT, emd TEXT,
        road TEXT, road_norm TEXT, main_no INTEGER, sub_no INTEGER, bld TEXT,
        full_addr TEXT, lon REAL, lat REAL);
      CREATE VIRTUAL TABLE places_fts USING fts5(region, road, bld,
        content='places', content_rowid='id', tokenize='unicode61', prefix='2 3');
      CREATE VIRTUAL TABLE place_rtree USING rtree(id, minlon, maxlon, minlat, maxlat);
    """)
    for i, (sido, sgg, emd, road, mno, sno, bld, lon, lat) in enumerate(SAMPLE, 1):
        rn = road_norm(road)
        no = str(mno) + (f"-{sno}" if sno else "")
        full = f"{sido} {sgg} {emd} {road} {no}" + (f" ({bld})" if bld else "")
        con.execute("INSERT INTO places VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (i, sido, sgg, emd, road, rn, mno, sno, bld, full, lon, lat))
        # FTS: region=행정구역 / road=표시명+정규형 동시색인(점 함정 방어) / bld
        con.execute("INSERT INTO places_fts(rowid,region,road,bld) VALUES(?,?,?,?)",
                    (i, f"{sido} {sgg} {emd}", f"{road} {rn}", bld))
        con.execute("INSERT INTO place_rtree VALUES(?,?,?,?,?)", (i, lon, lon, lat, lat))
    con.commit()
    return con


def parse_query(q):
    n = normalize(q)
    house = None; road = None; regions = []
    for t in n.split(" "):
        m = re.fullmatch(r"(\d+)(?:-(\d+))?", t)         # 건물번호: 5 / 5-3
        if m:
            house = (int(m.group(1)), int(m.group(2) or 0)); continue
        if re.search(r"(로|길)$", t):                     # 도로명: ~로/~길
            road = road_norm(t); continue
        regions.append(t)
    return {"road": road, "house": house, "regions": regions, "norm": n}


def geocode(con, q, limit=8):
    p = parse_query(q)
    if not p["road"]:
        return {"matched": "none", "candidates": []}
    # 1차 회수: FTS road 컬럼으로 후보(정규형 prefix). 건물번호는 절대 FTS로 안 감.
    rows = con.execute(
        """SELECT p.* FROM places_fts f JOIN places p ON p.id=f.rowid
           WHERE places_fts MATCH ?""", (f'road:"{p["road"]}"*',)).fetchall()
    cands = []
    for r in rows:
        if r["road_norm"] != p["road"]:
            continue                                      # 정밀: 도로 정규형 완전일치
        score = 100
        matched = "road"
        if p["house"]:
            mno, sno = p["house"]
            if r["main_no"] != mno:
                continue                                  # 본번 불일치 → 제외(523 오염 차단)
            if r["sub_no"] != sno:
                continue                                  # 부번 불일치 → 제외('5'와 '5-3'은 별개 주소)
            score += 60; matched = "road_no"
        for reg in p["regions"]:
            if reg in (r["sido"], r["sigungu"], r["emd"]) or r["sigungu"].startswith(reg):
                score += 15
        cands.append((score, r))
    cands.sort(key=lambda x: -x[0])
    out = [{"full_addr": r["full_addr"], "lon": r["lon"], "lat": r["lat"], "score": s}
           for s, r in cands[:limit]]
    if not out:
        return {"matched": "none", "candidates": []}
    # 1·2등 격차 충분하면 단일 확정, 아니면 후보 반환
    confident = len(out) == 1 or (out[0]["score"] - out[1]["score"] >= 30)
    return {"matched": "exact" if confident and p["house"] else "candidates",
            "single": out[0] if confident else None, "candidates": out}


def reverse(con, lon, lat):
    rows = con.execute("SELECT * FROM places").fetchall()
    cosf = math.cos(math.radians(lat)) ** 2               # 경도 거리 보정
    best = min(rows, key=lambda r: (r["lon"] - lon) ** 2 * cosf + (r["lat"] - lat) ** 2)
    return best["full_addr"]


# ---- 검증 시나리오 (docs/geocode-juso-plan.md) ----
def run():
    con = build()
    ok = 0; total = 0

    def check(desc, cond, detail=""):
        nonlocal ok, total
        total += 1; ok += bool(cond)
        print(f"  [{'PASS' if cond else 'FAIL'}] {desc}" + (f"  → {detail}" if detail else ""))

    print("전체 도로명주소 지오코딩 PoC — 검증")
    print("-" * 64)

    r = geocode(con, "화성시 봉담읍 3.1만세로 5")
    check("화성시 …3.1만세로 5 → 화성 건물 단건 확정",
          r["single"] and "화성시" in r["single"]["full_addr"] and r["single"]["score"] >= 155,
          r["single"]["full_addr"] if r["single"] else str(r))

    r = geocode(con, "화성시 봉담읍 3.1만세로 5")
    addrs = [c["full_addr"] for c in r["candidates"]]
    check("위 결과에 523번지(prefix 오염)가 절대 안 섞임",
          all("523" not in a for a in addrs), str(addrs))

    r = geocode(con, "3.1만세로 5")
    check("시·구 생략 → 동명이 도로 다중 후보(화성+종로)",
          len(r["candidates"]) >= 2 and any("화성" in c["full_addr"] for c in r["candidates"])
          and any("종로" in c["full_addr"] for c in r["candidates"]),
          str([c["full_addr"] for c in r["candidates"]]))

    r = geocode(con, "31만세로 5")
    check("점 없는 표기 '31만세로'도 매칭(토큰 함정 방어)",
          len(r["candidates"]) >= 1 and all("만세로" in c["full_addr"] for c in r["candidates"]),
          str([c["full_addr"] for c in r["candidates"]]))

    r = geocode(con, "화성시 3.1만세로 5-3")
    check("본번5-부번3 정확 매칭(5-0과 구분)",
          r["single"] and "5-3" in r["single"]["full_addr"],
          r["single"]["full_addr"] if r["single"] else str(r))

    addr = reverse(con, 126.9002, 37.2102)
    check("역지오코딩: 좌표 → 풀주소 1줄",
          "화성시" in addr and "3.1만세로 5" in addr, addr)

    print("-" * 64)
    print(f"결과: {ok}/{total} 통과")
    return ok == total


if __name__ == "__main__":
    import sys
    sys.exit(0 if run() else 1)
