#!/usr/bin/env python3
"""빌드 산출물 QC 검증 — geocode.sqlite + tiles/*.mbtiles + style.json 정합성.

이 프로젝트에서 실제로 데인 사고들(NFC/NFD, 좌표계 오지정, prefix 오염, 조각↔배포 불일치)을
자동검사로 박는다. FAIL 하나라도 있으면 비0 종료(파이프라인 차단용), WARN만 있으면 0 종료.

사용:
  python3 13-qc-check.py \
    --db ~/geocode-build/geocode.sqlite \
    --tiles ~/geocode-build/tiles \
    --style style/style.json --config server/tileserver-config.json \
    --api http://localhost:8082
"""
import argparse, json, math, os, sqlite3, sys, unicodedata, urllib.parse, urllib.request, pathlib

R = []  # (sev, name, detail)  sev: PASS/WARN/FAIL
ICON = {"PASS": "✓", "WARN": "△", "FAIL": "✗"}
def rec(sev, name, detail=""):  # 즉시 출력(스트리밍) → 빌드 스튜디오에서 항목별 라이브 표시
    R.append((sev, name, detail))
    print(f"  {ICON[sev]} [{sev}] {name}" + (f" — {detail}" if detail else ""), flush=True)
    return sev
NFC = lambda s: unicodedata.normalize("NFC", s)

# 한국 좌표 범위(build-localdata 가드와 동일)
LON0, LON1, LAT0, LAT1 = 124.0, 132.0, 33.0, 39.0
EXP_SIDO = 17  # 17개 시도

def q1(db, sql, *a):
    r = db.execute(sql, a).fetchone()
    return r[0] if r else None


# ── A. 인코딩·정규화 ──────────────────────────────────────────────
def check_nfc(db):
    bad = 0; n = 0
    for kind in ("addr", "biz", "place", "poi", "road", "station"):
        for (name,) in db.execute(
                "SELECT name FROM places WHERE kind=? AND name IS NOT NULL LIMIT 20000", (kind,)):
            n += 1
            if NFC(name) != name: bad += 1
    if bad: rec("FAIL", "NFC 정규화", f"샘플 {n:,}건 중 NFD 잔존 {bad:,}건 — 검색이 조용히 깨짐")
    else:   rec("PASS", "NFC 정규화", f"샘플 {n:,}건 모두 NFC")


# ── B/C/D. 좌표·건수·커버리지·채움률 (단일 GROUP BY 스캔으로 통합) ──
def check_db_scan(db):
    # places 15M을 7번 풀스캔하면 수십 초 → 조건부 집계로 한 번에 끝낸다.
    rows = db.execute(f"""
      SELECT kind, count(*) n,
             sum(CASE WHEN lon IS NULL OR lat IS NULL
                       OR lon NOT BETWEEN {LON0} AND {LON1}
                       OR lat NOT BETWEEN {LAT0} AND {LAT1} THEN 1 ELSE 0 END) oob,
             sum(CASE WHEN lon=0 AND lat=0 THEN 1 ELSE 0 END) zero,
             count(jibun) jibun, count(cat1) cat1,
             count(DISTINCT CASE WHEN sido<>'' THEN sido END) nsido,
             sum(CASE WHEN bcode IS NOT NULL AND bcode<>'' THEN 1 ELSE 0 END) bcode,
             sum(CASE WHEN hcode IS NOT NULL AND hcode<>'' THEN 1 ELSE 0 END) hcode
      FROM places GROUP BY kind""").fetchall()
    K = {r[0]: r for r in rows}
    total = sum(r[1] for r in rows)
    oob = sum(r[2] for r in rows); zero = sum(r[3] for r in rows)

    meta = dict(db.execute("SELECT k,v FROM meta").fetchall()) if \
        q1(db, "SELECT count(*) FROM sqlite_master WHERE name='meta'") else {}
    if meta.get("places") and int(meta["places"]) != total:
        rec("WARN", "meta.places 일치", f"meta {meta['places']} ≠ 실제 {total:,}")

    rec("FAIL" if oob else "PASS", "좌표 범위",
        f"범위밖/NULL {oob:,}건 (좌표계 오지정 의심)" if oob else f"전 좌표 {LON0}–{LON1}E / {LAT0}–{LAT1}N 이내")
    if zero: rec("WARN", "더미 좌표(0,0)", f"{zero:,}건")

    rec("PASS", "총 건수", f"{total:,}건 · " + " ".join(f"{r[0]}={r[1]:,}" for r in rows))
    addr_n = K.get("addr", (None, 0))[1]
    if addr_n < 5_000_000:
        rec("FAIL", "주소 적재량", f"addr {addr_n:,} (전국이면 1,000만대 기대)")
    nsido = K.get("addr", (None,)*6 + (0,))[6]
    rec("FAIL" if nsido < EXP_SIDO else "PASS", "시도 커버리지",
        f"addr 시도 {nsido}/{EXP_SIDO} — 적재 누락 의심" if nsido < EXP_SIDO else f"{nsido}개 시도")

    if addr_n:
        jr = K["addr"][4] / addr_n
        rec("PASS" if jr > 0.98 else "WARN", "주소 지번 채움률", f"{jr*100:.2f}% ({K['addr'][4]:,}/{addr_n:,})")
        br = K["addr"][7] / addr_n
        rec("PASS" if br >= 0.999 else "FAIL", "주소 법정동코드(b_code) 채움률", f"{br*100:.2f}% ({K['addr'][7]:,}/{addr_n:,})")
        hr = K["addr"][8] / addr_n
        rec("PASS" if hr >= 0.999 else "FAIL", "주소 행정동코드(h_code) 채움률", f"{hr*100:.2f}% ({K['addr'][8]:,}/{addr_n:,})")
    biz_n = K.get("biz", (None, 0))[1]
    if biz_n:
        cr = K["biz"][5] / biz_n
        rec("PASS" if cr > 0.95 else "WARN", "시설 업종대분류 채움률", f"{cr*100:.1f}% ({K['biz'][5]:,}/{biz_n:,})")


# ── E. 인덱스 무결성 ──────────────────────────────────────────────
def check_index(db):
    total = q1(db, "SELECT count(*) FROM places")
    has = lambda t: q1(db, "SELECT count(*) FROM sqlite_master WHERE name=?", t)
    if has("places_fts"):
        # external-content FTS5는 count(*)가 content 컬럼을 읽다 실패 → 섀도우 docsize로 문서수 집계
        fts = q1(db, "SELECT count(*) FROM places_fts_docsize")
        rec("PASS" if fts == total else "FAIL", "FTS 인덱스", f"{fts:,} vs places {total:,}")
    else:
        rec("FAIL", "FTS 인덱스", "places_fts 없음")
    if has("place_rtree"):
        # rtree 본체 count(*)는 트리 순회로 느림 → 섀도우 rowid 테이블로 문서수 집계
        rt = q1(db, "SELECT count(*) FROM place_rtree_rowid")
        rec("PASS" if rt == total else "WARN", "R-tree 인덱스", f"{rt:,} vs places {total:,}")
    else:
        rec("FAIL", "R-tree 인덱스", "place_rtree 없음")


# ── E2. 행정경계 폴리곤(역지오코딩 point-in-polygon) ──────────────
def check_areas(db):
    has = lambda t: q1(db, "SELECT count(*) FROM sqlite_master WHERE name=?", t)
    if not has("areas"):
        rec("WARN", "행정경계 areas", "areas 테이블 없음 — 역지오 동 폴리곤 미적재"); return
    by = dict(db.execute("SELECT type,count(*) FROM areas GROUP BY type").fetchall())
    total = sum(by.values())
    if total == 0:
        rec("WARN", "행정경계 areas", "0건 — 역지오 동 경계 판정 불가(06-gen-areas/areas.sqlite 미적재?)"); return
    miss = [t for t in ("legal-dong", "admin-dong") if by.get(t, 0) == 0]
    if miss: rec("FAIL", "행정경계 단위", f"누락 {', '.join(miss)} (현재 {by})")
    else:    rec("PASS", "행정경계 areas", f"{total:,}건 · " + " ".join(f"{k}={v:,}" for k, v in by.items()))
    bad = q1(db, "SELECT count(*) FROM areas WHERE rings IS NULL OR rings='' OR rings='[]'")
    rec("FAIL" if bad else "PASS", "areas rings 무결성", f"빈/깨진 rings {bad:,}건" if bad else f"전 {total:,}건 polygon 보유")
    if has("area_rtree"):
        rt = q1(db, "SELECT count(*) FROM area_rtree_rowid")
        rec("PASS" if rt == total else "WARN", "area R-tree", f"{rt:,} vs areas {total:,}")
    else:
        rec("FAIL", "area R-tree", "area_rtree 없음")


# ── E3. 카테고리 표준(canonical) 정합 — 권위 style/poi-taxonomy.json ─
def check_categories(db, taxonomy_path):
    if not (taxonomy_path and os.path.exists(taxonomy_path)):
        rec("WARN", "카테고리 표준", "poi-taxonomy.json 없음 — 스킵"); return
    canon = set(json.load(open(taxonomy_path, encoding="utf-8")).get("cat1_order", []))
    if not canon:
        rec("WARN", "카테고리 표준", "poi-taxonomy.json cat1_order 비어있음 — 스킵"); return
    seen = [c for (c,) in db.execute("SELECT DISTINCT cat1 FROM places WHERE cat1 IS NOT NULL AND cat1<>''")]
    off = sorted(c for c in seen if c not in canon)
    if off: rec("FAIL", "카테고리 canonical 정합", f"표준 밖 cat1 {len(off)}종: {', '.join(off[:8])} (기준 poi-taxonomy {len(canon)}종)")
    else:   rec("PASS", "카테고리 canonical 정합", f"cat1 {len(seen)}종 모두 표준({len(canon)}종) 부분집합")


# ── F. 검색 품질(골든 질의 회귀) — API 필요 ───────────────────────
GOLDEN = [
    ("사직로 161", "사직로", "161"),      # prefix 오염으로 125 나오던 회귀
    ("한누리대로 1811", "한누리대로", "1811"),
    ("스타벅스 강남", "스타벅스", None),
    ("강남역", "강남", None),
]
def check_golden(api):
    if not api:
        rec("WARN", "골든 질의", "--api 미지정 — 스킵"); return
    ok = 0
    for q, must, must2 in GOLDEN:
        try:
            url = f"{api.rstrip('/')}/geocode?q=" + urllib.parse.quote(q)
            d = json.load(urllib.request.urlopen(url, timeout=8))
            res = d.get("results", [])
            top = (json.dumps(res[0], ensure_ascii=False) if res else "")
            hit = bool(res) and must in top and (must2 is None or must2 in top)
            ok += hit
            if not hit: rec("FAIL", f"골든: {q}", f"기대 '{must}'{('+'+must2) if must2 else ''} 불일치 (top={top[:80]})")
        except Exception as e:
            rec("FAIL", f"골든: {q}", f"API 오류 {e}")
    if ok == len(GOLDEN): rec("PASS", "골든 질의", f"{ok}/{len(GOLDEN)} 통과")


# ── G. 타일 + 스타일↔mbtiles 정합성 ───────────────────────────────
def mbtiles_meta(path):
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    m = dict(db.execute("SELECT name,value FROM metadata").fetchall())
    src = "tiles" if q1(db, "SELECT count(*) FROM sqlite_master WHERE name='tiles'") else \
          ("map" if q1(db, "SELECT count(*) FROM sqlite_master WHERE name='map'") else None)
    cnt = q1(db, f"SELECT count(*) FROM {src}") if src else 0
    # 최대 타일크기: 전수 블롭 스캔은 느리므로 샘플 근사(과대타일 WARN). map-기반엔 tile_data 없음→스킵.
    try:
        maxsz = (q1(db, f"SELECT max(length(tile_data)) FROM (SELECT tile_data FROM {src} LIMIT 8000)") or 0) \
                if cnt and src == "tiles" else 0
    except Exception:
        maxsz = 0
    db.close()
    # 래스터(png/webp 등)는 source-layer 무관 → None 센티넬. 벡터는 vector_layers 리스트(빈 리스트면 깨진 타일).
    if m.get("format", "") in ("png", "jpg", "jpeg", "webp"):
        vlayers = None
    else:
        try: vlayers = [v["id"] for v in json.loads(m.get("json", "{}")).get("vector_layers", [])]
        except Exception: vlayers = []
    return m, cnt, maxsz, vlayers

def check_tiles(tiles_dir, style_path, config_path):
    if not tiles_dir or not pathlib.Path(tiles_dir).is_dir():
        rec("WARN", "타일", "--tiles 미지정 — 스킵"); return
    metas = {}
    for mb in sorted(pathlib.Path(tiles_dir).glob("*.mbtiles")):
        try:
            m, cnt, maxsz, vl = mbtiles_meta(mb)
            metas[mb.stem] = vl
            sev = "PASS"; note = f"{m.get('format','?')} z{m.get('minzoom','?')}–{m.get('maxzoom','?')} · {cnt:,}타일"
            if cnt == 0: sev = "FAIL"; note += " · 빈 타일셋"
            if maxsz > 900_000: sev = "WARN"; note += f" · 최대타일 {maxsz//1024}KB(과대)"
            rec(sev, f"mbtiles: {mb.name}", note)
        except Exception as e:
            rec("FAIL", f"mbtiles: {mb.name}", f"열기 실패 {e}")

    # 스타일이 참조하는 source-layer가 실제 mbtiles에 존재하는가 (안 보임 함정)
    if not (style_path and config_path and os.path.exists(style_path) and os.path.exists(config_path)):
        rec("WARN", "스타일↔타일 정합", "--style/--config 미지정 — 스킵"); return
    style = json.load(open(style_path, encoding="utf-8"))
    cfg = json.load(open(config_path, encoding="utf-8"))
    data = cfg.get("data", {})  # tileserver key → {mbtiles: file}
    src_key = {}  # style source name → mbtiles stem
    for sname, sdef in style.get("sources", {}).items():
        url = sdef.get("url", "")
        if url.startswith("mbtiles://{") and url.endswith("}"):
            k = url[len("mbtiles://{"):-1]
            f = data.get(k, {}).get("mbtiles", "")
            src_key[sname] = pathlib.Path(f).stem if f else None
    miss = []
    for L in style.get("layers", []):
        sl = L.get("source-layer"); src = L.get("source")
        if not sl or not src: continue
        stem = src_key.get(src)
        if stem is None: continue                 # mbtiles 소스 아님
        if stem not in metas:                      # 참조 파일이 tiles_dir에 아예 없음 (안 보임 함정)
            miss.append(f"{L.get('id')} → {src}: mbtiles '{stem}' 파일이 tiles_dir에 없음"); continue
        vl = metas[stem]
        if vl is None: continue                    # 래스터(terrain) → source-layer 무관
        if sl not in vl:
            miss.append(f"{L.get('id')} → {src}/{sl} (vector_layers에 없음: {vl})")
    if miss:
        for m in miss: rec("FAIL", "스타일 source-layer", m)
    else:
        rec("PASS", "스타일↔타일 정합", f"{len([L for L in style['layers'] if L.get('source-layer')])}개 레이어 source-layer 일치")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=os.path.expanduser("~/geocode-build/geocode.sqlite"))
    ap.add_argument("--tiles", default=os.path.expanduser("~/geocode-build/tiles"))
    ap.add_argument("--style"); ap.add_argument("--config")
    ap.add_argument("--taxonomy", default=str(pathlib.Path(__file__).resolve().parents[1] / "style" / "poi-taxonomy.json"))
    ap.add_argument("--api", default="http://localhost:8082")
    a = ap.parse_args()

    print(f"QC: {a.db}")
    if os.path.exists(a.db):
        db = sqlite3.connect(f"file:{a.db}?mode=ro", uri=True)
        check_nfc(db); check_db_scan(db); check_index(db); check_areas(db); check_categories(db, a.taxonomy); db.close()
    else:
        rec("FAIL", "DB", f"{a.db} 없음")
    check_golden(a.api)
    check_tiles(a.tiles, a.style, a.config)

    n_fail = sum(s == "FAIL" for s, _, _ in R); n_warn = sum(s == "WARN" for s, _, _ in R)
    print("=" * 64)
    print(f"결과: PASS {sum(s=='PASS' for s,_,_ in R)} · WARN {n_warn} · FAIL {n_fail}")
    sys.exit(1 if n_fail else 0)


if __name__ == "__main__":
    main()
