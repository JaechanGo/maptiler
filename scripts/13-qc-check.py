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
import argparse, json, math, os, sqlite3, sys, unicodedata, urllib.parse, urllib.request, urllib.error, pathlib, subprocess, shutil

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
def _load_geocoder(db_path):
    """server/geocode-api.py 를 인프로세스 로드 — connect()/geocode() 순수함수 재사용(서버 불필요).
    geocode.sqlite 를 직접 질의해 골든 회귀를 빌드 단계에서도 '실제로' 검사한다(서버 미기동에 의존 X)."""
    import importlib.util
    gp = pathlib.Path(__file__).resolve().parent.parent / "server" / "geocode-api.py"
    if not gp.exists():
        return None
    os.environ["GEOCODE_DB"] = str(db_path)   # 모듈 로드 시 DB_PATH 가 이 env 를 읽음(기본 ~/geocode-build)
    spec = importlib.util.spec_from_file_location("geocode_api_inproc", str(gp))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _golden_check(get_top, mode):
    """get_top(q)->top문자열(없으면 ""). None=조회불가(연결실패). 전건 일치 PASS, 오답 FAIL. (인프로세스/HTTP 공용)"""
    rows = []
    for q, must, must2 in GOLDEN:
        top = get_top(q)
        if top is None:   # 조회 자체 불가(연결실패 등) → 호출부가 처리
            return None
        rows.append((q, must, must2, top))
    if not any(t for _, _, _, t in rows):   # 전건 0응답 → 미적재/미완성 DB(데이터 회귀 아님) → 스킵(WARN)
        rec("WARN", "골든 질의", f"지오코드 DB 미적재/미완성(골든 {len(rows)}건 0응답) — 회귀검사 스킵 ({mode})")
        return "skip"
    ok = 0
    for q, must, must2, top in rows:
        hit = bool(top) and must in top and (must2 is None or must2 in top)
        ok += hit
        if not hit:
            rec("FAIL", f"골든: {q}", f"기대 '{must}'{('+'+must2) if must2 else ''} 불일치 (top={top[:80]})")
    if ok == len(GOLDEN):
        rec("PASS", "골든 질의", f"{ok}/{len(GOLDEN)} 통과 ({mode})")
    return ok


def check_golden(api, db_path=None):
    # 1순위: geocode.sqlite 인프로세스 직접질의 — 서버 불필요. 빌드 단계에서도 회귀가드가 실동작한다.
    if db_path and os.path.exists(db_path):
        try:
            mod = _load_geocoder(db_path)
            if mod is not None:
                con = mod.connect()
                def top_inproc(q):
                    res = mod.geocode(con, q, 5) or []
                    return json.dumps(res[0], ensure_ascii=False) if res else ""
                _golden_check(top_inproc, "인프로세스"); return
        except Exception as e:
            rec("WARN", "골든 질의", f"인프로세스 지오코더 실패 — HTTP 폴백 ({str(e)[:80]})")
    # 2순위: HTTP API. 연결실패=WARN 스킵(서버 미기동 ≠ 데이터결함), HTTP오류/오답=FAIL.
    if not api:
        rec("WARN", "골든 질의", "인프로세스 불가 + --api 미지정 — 스킵"); return
    def top_http(q):
        try:
            url = f"{api.rstrip('/')}/geocode?q=" + urllib.parse.quote(q)
            d = json.load(urllib.request.urlopen(url, timeout=8))
        except urllib.error.HTTPError as e:
            rec("FAIL", f"골든: {q}", f"API HTTP {e.code}"); return ""
        except (urllib.error.URLError, ConnectionError, TimeoutError, OSError):
            return None   # 서버 미기동 → 스킵 신호
        except Exception as e:
            rec("FAIL", f"골든: {q}", f"API 응답오류 {e}"); return ""
        res = d.get("results", [])
        return json.dumps(res[0], ensure_ascii=False) if res else ""
    if _golden_check(top_http, "HTTP") is None:
        rec("WARN", "골든 질의", f"지오코드 API({api}) 연결 실패 — 골든 스킵(서버 미기동)")


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


# ── G2. maplibre style-spec 실검증 (tileserver-gl 실로드 동치) ────────
#   실측 사고: poi-label 의 icon-image/text-field 가 ["case",[">=",["zoom"],mz],...] 로 zoom 을 case 안에
#   중첩(spec 은 zoom 을 step/interpolate 최상위 입력으로만 허용) → 파일수준 검사(스타일↔타일 정합)는 통과했으나
#   폐쇄망 tileserver-gl v5.6.0 이 스타일 전체를 거부 → /styles.json=[] · /styles/<id>/style.json=404 → 지도 미표시.
#   (베이스 mbtiles·/health 는 200 이라 deploy 가 '성공'으로 보였음.) 이 검사로 빌드 단계에서 차단한다.
INTERP_OPS = {"interpolate", "interpolate-hcl", "interpolate-lab"}

def _find_zoom_violations(style):
    """["zoom"] 이 step/interpolate 의 최상위 입력이 아닌 위치(case/match/비교 등 안에 중첩)에 쓰인 곳을 모두 보고.
    Node 미설치 빌드호스트에서도 동작하는 표적 회귀가드(위 사고류 직접 탐지). 반환: ["layer.group.prop", ...]."""
    viol = []
    def walk(node, in_curve_input, where):
        if not isinstance(node, list) or not node:
            return
        op = node[0]
        if op == "zoom":
            if not in_curve_input:           # step/interpolate 입력 슬롯이 아니면 위반
                viol.append(where)
            return
        if op == "literal":                  # ["literal", <원시데이터>] — 2번째 인자는 표현식이 아님(해석 금지).
            return                            #   안 그러면 ["literal",["zoom",…]] 의 데이터를 zoom 으로 오탐 → 정상 번들 오차단.
        if op == "step":                     # ["step", input, out0, stop1, out1, ...] — input(=[1])만 zoom 허용
            for i, ch in enumerate(node[1:], 1):
                walk(ch, i == 1, where)
        elif op in INTERP_OPS:               # ["interpolate(-hcl/-lab)", type, input, ...] — input(=[2])만 zoom 허용
            for i, ch in enumerate(node[1:], 1):
                walk(ch, i == 2, where)
        elif op == "match":                  # ["match", input, label, out, …, default] — label 슬롯은 원시데이터(표현식 아님) → 스킵
            walk(node[1], False, where)       # input (zoom 이 여기 오면 위반=정탐)
            body = node[2:]
            for j in range(0, len(body) - 1, 2):
                walk(body[j + 1], False, where)   # out 만 walk(label=body[j] 은 데이터라 스킵)
            if len(body) % 2 == 1:
                walk(body[-1], False, where)       # 마지막 = default(표현식)
        else:                                # 그 외 표현식의 인자에는 zoom 중첩 불허
            for ch in node[1:]:
                walk(ch, False, where)
    for L in style.get("layers", []):
        for grp in ("layout", "paint"):
            for prop, val in (L.get(grp) or {}).items():
                walk(val, False, f"{L.get('id')}.{grp}.{prop}")
    return viol


def check_style_spec(style_path):
    if not (style_path and os.path.exists(style_path)):
        rec("WARN", "스타일 spec 검증", "--style 미지정/없음 — 스킵"); return
    try:
        style = json.load(open(style_path, encoding="utf-8"))
    except Exception as e:
        rec("FAIL", "스타일 JSON", f"{style_path} 파싱 실패: {str(e)[:120]}"); return

    # (1) 표적 회귀가드 — zoom 중첩(위 사고류). Node 없이도 항상 동작 → 폐쇄망/노드리스 빌드호스트도 보호.
    viol = _find_zoom_violations(style)
    if viol:
        for v in viol[:8]:
            rec("FAIL", "스타일 zoom 배치", f"{v}: ['zoom']이 step/interpolate 최상위 입력 아님(case 등 중첩) — spec 위반")
        if len(viol) > 8:
            rec("FAIL", "스타일 zoom 배치", f"… 외 {len(viol)-8}건")
    else:
        rec("PASS", "스타일 zoom 배치", "['zoom'] 모두 step/interpolate 최상위 입력")

    # (2) 권위 검증 — @maplibre/maplibre-gl-style-spec gl-style-validate (tileserver-gl 실로드와 동치).
    #     검증오류=FAIL(차단), npx 미설치=WARN(스킵), npx/네트워크 실패=WARN(차단 안 함). QC_SKIP_NODE_VALIDATE=1 로 끔.
    if os.environ.get("QC_SKIP_NODE_VALIDATE"):
        rec("WARN", "스타일 spec 검증(Node)", "QC_SKIP_NODE_VALIDATE=1 — gl-style-validate 스킵(표적 zoom 검사만 수행)"); return
    npx = shutil.which("npx")
    if not npx:
        rec("WARN", "스타일 spec 검증(Node)",
            "npx/node 미설치 — @maplibre/maplibre-gl-style-spec 전체검증 스킵(표적 zoom 검사만 수행). 빌드호스트 Node 설치 권장")
        return
    try:   # npx -p 형: 이 패키지는 bin 이 여러개라 'npx <pkg> <bin>' 로는 실행 불가('-p' 로 패키지 지정 후 bin 실행).
        p = subprocess.run(
            [npx, "-y", "-p", "@maplibre/maplibre-gl-style-spec", "gl-style-validate", style_path],
            capture_output=True, text=True, timeout=300)
    except subprocess.TimeoutExpired:
        rec("WARN", "스타일 spec 검증(Node)", "gl-style-validate 시간초과(300s) — npx 캐시/네트워크 확인(차단 안 함)"); return
    except Exception as e:
        rec("WARN", "스타일 spec 검증(Node)", f"실행 실패: {str(e)[:120]} (차단 안 함)"); return
    out = (p.stdout or "").strip()
    err = (p.stderr or "").strip()
    if p.returncode == 0:
        rec("PASS", "스타일 spec 검증(Node)", "gl-style-validate 통과(@maplibre/maplibre-gl-style-spec)")
    elif out:                                # 검증 위반은 stdout 에 1건/라인 출력 → 차단(FAIL)
        lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
        for ln in lines[:12]:
            rec("FAIL", "스타일 spec 위반", ln[:200])
        if len(lines) > 12:
            rec("FAIL", "스타일 spec 위반", f"… 외 {len(lines)-12}건")
    else:                                    # stdout 비고 비0 종료 → npx 패키지 패치/네트워크 등 도구 실패 → 차단 안 함
        rec("WARN", "스타일 spec 검증(Node)",
            f"gl-style-validate 미수행(code={p.returncode}) — npx 패키지 패치 실패 의심: {err[:160]} (표적 zoom 검사만 적용)")


# ── H. PostGIS 동적 레이어(martin /dyn 백본) — 적재 완전성·인덱스 무결성 (--pg) ──
#   위 SQLite/타일/스타일 검사는 PostGIS 를 전혀 보지 않는다. parcel/building 적재가 교착·중단으로
#   미완(예: parcel 5.6M/≈39.6M)·무인덱스(--fresh 가 DROP 후 재생성 전 중단)여도 PASS 로 통과하던
#   사각을 메운다. 손상 DB 가 그대로 pg_dump→폐쇄망 번들로 새어나가는 것을 빌드 단계에서 차단.
#   접속: host psql(PGPORT 기본 5433) 우선 → docker exec <PG_CONTAINER> 폴백. --pg 일 때만 동작.
PG_SIDO = ['11','26','27','28','29','30','31','36','41','43','44','46','47','48','50','51','52']  # 17 시도

def _pg_runner():
    """psql 질의 실행기 반환: host psql(우선) → docker exec 폴백. 둘 다 불가면 None.
    반환 q(sql) -> list[list[str]] (탭분리 행); psql 오류 시 RuntimeError."""
    env = {**os.environ}
    for k, v in (("PGHOST","localhost"),("PGPORT","5433"),("PGUSER","cuvia"),
                 ("PGDATABASE","cuvia"),("PGPASSWORD","cuvia")):
        env.setdefault(k, v)
    args = ["-tAX", "-F", "\t", "-v", "ON_ERROR_STOP=1"]
    container = os.environ.get("PG_CONTAINER", "server-postgis-1")
    def mk(prefix, use_env):
        def q(sql, timeout=60):   # timeout=hang 방어(psql/docker 미응답 시 무한대기 방지). TimeoutExpired 는 호출부 except 가 처리.
            p = subprocess.run(prefix + args + ["-c", sql], capture_output=True, text=True,
                               env=(env if use_env else None), timeout=timeout)
            if p.returncode != 0:
                raise RuntimeError((p.stderr or p.stdout).strip()[:200])
            return [ln.split("\t") for ln in p.stdout.splitlines() if ln != ""]
        return q
    if shutil.which("psql"):                                  # 1) host psql (build host 표준)
        q = mk(["psql"], True)
        try: q("SELECT 1", timeout=10); return q
        except Exception: pass
    if shutil.which("docker"):                                # 2) 컨테이너 폴백
        q = mk(["docker","exec","-e",f"PGPASSWORD={env['PGPASSWORD']}",container,
                "psql","-U",env["PGUSER"],"-d",env["PGDATABASE"]], False)
        try: q("SELECT 1", timeout=10); return q
        except Exception: pass
    return None


def check_postgis():
    q = _pg_runner()
    if q is None:
        rec("FAIL", "PostGIS 접속", "psql/docker 로 PostGIS 접속 불가 — compose --profile postgis 기동 확인(--pg 요청됨)")
        return
    def scalar(sql):
        try:
            r = q(sql); return int(r[0][0]) if r and r[0] and r[0][0] != "" else 0
        except Exception:
            return None  # 테이블 없음/조회 실패

    p_min = int(os.environ.get("PARCEL_MIN", 30_000_000))    # package.sh 게이트와 동일 임계(우회 가능)
    b_min = int(os.environ.get("BUILDING_MIN", 5_000_000))
    a_min = int(os.environ.get("ADDRESS_MIN", 5_000_000))

    # 1) 적재 행수 밴드 — 부분적재/dedup 붕괴를 전국 합계로 포착(임계 미만=FAIL).
    for tbl, lo, env_key in (("parcel", p_min, "PARCEL_MIN"), ("building", b_min, "BUILDING_MIN"),
                             ("address", a_min, "ADDRESS_MIN")):
        n = scalar(f"SELECT count(*) FROM {tbl}")
        if n is None:
            rec("FAIL", f"{tbl} 행수", "조회 실패(테이블 없음·미적재?)")
        else:
            rec("FAIL" if n < lo else "PASS", f"{tbl} 행수",
                f"{n:,} (임계 {lo:,}; 우회 {env_key}=0)" + ("" if n >= lo else " — 적재 미완 의심"))
    poic = scalar("SELECT count(*) FROM poi")
    if poic is not None:
        rec("WARN" if poic == 0 else "PASS", "poi 행수", f"{poic:,}" + (" — 0건(POI 미적재?)" if poic == 0 else ""))

    # 2) 파티션 커버리지 — 17 시도가 모두 비어있지 않아야(교착 중단 시 일부 파티션만 적재됨).
    for tbl in ("parcel", "building"):
        try:
            have = {r[0].strip(): int(r[1]) for r in q(f"SELECT sido_cd, count(*) FROM {tbl} GROUP BY sido_cd")
                    if len(r) >= 2 and r[1] != ""}
            empty = [s for s in PG_SIDO if have.get(s, 0) == 0]
            if empty: rec("FAIL", f"{tbl} 파티션 커버리지", f"빈 시도 {len(empty)}/17: {','.join(empty)} — 부분적재/중단 의심")
            else:     rec("PASS", f"{tbl} 파티션 커버리지", "17/17 시도 적재")
        except Exception as e:
            rec("FAIL", f"{tbl} 파티션 커버리지", f"조회 실패 {str(e)[:80]}")

    # 3) 핵심 인덱스 유효성 — --fresh 가 DROP 후 재생성 전 중단되면 누락 → martin /dyn 이 seq-scan(타임아웃).
    need = ["parcel_geom_gix", "parcel_pnu_idx", "building_geom_gix"]
    try:
        rows = q("SELECT c.relname, i.indisvalid FROM pg_class c JOIN pg_index i ON i.indexrelid=c.oid "
                 "WHERE c.relname IN ('parcel_geom_gix','parcel_pnu_idx','building_geom_gix')")
        valid = {r[0].strip() for r in rows if len(r) >= 2 and r[1].strip() in ("t", "true")}
        miss = [ix for ix in need if ix not in valid]
        if miss: rec("FAIL", "PostGIS 핵심 인덱스", f"누락/무효 {','.join(miss)} — --fresh 적재 중단 정황, load_parcel/building 재실행")
        else:    rec("PASS", "PostGIS 핵심 인덱스", f"{len(need)}종 유효(geom GiST·parcel pnu)")
    except Exception as e:
        rec("FAIL", "PostGIS 핵심 인덱스", f"조회 실패 {str(e)[:80]}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=os.path.join(os.environ.get("BUILD_HOME") or os.path.expanduser("~/geocode-build"), "geocode.sqlite"))
    ap.add_argument("--tiles", default=os.path.join(os.environ.get("BUILD_HOME") or os.path.expanduser("~/geocode-build"), "tiles"))
    ap.add_argument("--style"); ap.add_argument("--config")
    ap.add_argument("--taxonomy", default=str(pathlib.Path(__file__).resolve().parents[1] / "style" / "poi-taxonomy.json"))
    ap.add_argument("--api", default="http://localhost:8082")
    ap.add_argument("--pg", action="store_true",
                    help="PostGIS 동적 레이어(parcel/building/address/poi) 적재 완전성·파티션·인덱스 검사. 빌드호스트 compose postgis 필요.")
    a = ap.parse_args()

    print(f"QC: {a.db}")
    if os.path.exists(a.db):
        db = sqlite3.connect(f"file:{a.db}?mode=ro", uri=True)
        check_nfc(db); check_db_scan(db); check_index(db); check_areas(db); check_categories(db, a.taxonomy); db.close()
    else:
        rec("FAIL", "DB", f"{a.db} 없음")
    check_golden(a.api, a.db)
    check_tiles(a.tiles, a.style, a.config)
    check_style_spec(a.style)
    if a.pg:
        print("PostGIS 동적 레이어 검사 (--pg)")
        check_postgis()

    n_fail = sum(s == "FAIL" for s, _, _ in R); n_warn = sum(s == "WARN" for s, _, _ in R)
    print("=" * 64)
    print(f"결과: PASS {sum(s=='PASS' for s,_,_ in R)} · WARN {n_warn} · FAIL {n_fail}")
    sys.exit(1 if n_fail else 0)


if __name__ == "__main__":
    main()
