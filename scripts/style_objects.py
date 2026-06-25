#!/usr/bin/env python3
"""스타일 '객체'(건물·물·녹지·도로·라벨 등) ↔ 실제 레이어/paint 속성 매핑 + 테마 색 적용.

build_style.py(조각 병합 후 theme.json 색 적용)와 build-studio.py(색 편집기 UI/미리보기)가
공유한다. theme.json 형식: {"<object key>": "#rrggbb", ...}.
"""
import re

# 객체 정의: key, 라벨, 적용 대상 [(layer_id, paint_prop), ...]
OBJECTS = [
    {"key": "background", "label": "배경",        "targets": [["background", "background-color"]]},
    {"key": "water",      "label": "물·호수",     "targets": [["water", "fill-color"], ["waterway", "line-color"]]},
    {"key": "greenery",   "label": "녹지·공원",   "targets": [["landcover", "fill-color"], ["park", "fill-color"]]},
    {"key": "boundary",   "label": "행정경계",    "targets": [["boundary", "line-color"]]},
    {"key": "road",       "label": "도로",        "targets": [["road-minor", "line-color"], ["road-secondary", "line-color"],
                                                              ["road-primary", "line-color"], ["road-motorway", "line-color"]]},
    {"key": "building2d", "label": "건물",        "targets": [["building-2d", "fill-color"]]},
    {"key": "building3d", "label": "건물(3D)",    "targets": [["Building 3D", "fill-extrusion-color"]]},
    {"key": "donglabel",  "label": "동(棟) 라벨", "targets": [["dong-label", "text-color"], ["dong-dot", "circle-color"]]},
    {"key": "poilabel",   "label": "시설 라벨",   "targets": [["poi-label", "text-color"]]},
    {"key": "placelabel", "label": "지명 라벨",   "targets": [["place-label", "text-color"], ["road-label", "text-color"]]},
]

# 카테고리 아이콘(스프라이트) 그룹 — cat1 업종대분류(상가 10종) → 스프라이트 아이콘 slug.
# cats 에 LOCALDATA 대분류 동의어를 함께 묶어 단일 아이콘으로 정규화(상가 10 + localdata 7 = 17어휘).
# 아이콘 이미지는 style/icons/<slug>.png (Style Studio에서 사용자가 교체), 14-gen-sprite.py가 패킹.
ICON_GROUPS = [
    {"key": "food",       "label": "음식·식품",       "cats": ["음식", "식품"],               "icon": "food"},
    {"key": "retail",     "label": "소매",            "cats": ["소매", "기타"],               "icon": "retail"},
    {"key": "repair",     "label": "수리·개인·생활",  "cats": ["수리·개인", "생활"],          "icon": "repair"},
    {"key": "tech",       "label": "과학·기술",       "cats": ["과학·기술"],                  "icon": "tech"},
    {"key": "edu",        "label": "교육",            "cats": ["교육"],                       "icon": "edu"},
    {"key": "sports",     "label": "예술·스포츠·문화", "cats": ["예술·스포츠", "문화"],        "icon": "sports"},
    {"key": "facility",   "label": "시설관리·임대",   "cats": ["시설관리·임대", "자원환경"],  "icon": "facility"},
    {"key": "realestate", "label": "부동산",          "cats": ["부동산"],                     "icon": "realestate"},
    {"key": "lodging",    "label": "숙박",            "cats": ["숙박"],                       "icon": "lodging"},
    {"key": "health",     "label": "보건·의료",       "cats": ["보건의료", "건강", "동물"],   "icon": "health"},
]
POI_ICON_LAYER = "poi-label"   # 아이콘+이름을 한 심볼로(충돌 회피) — icon-image는 poi-label에 둠
ICON_FALLBACK = "retail"

# POI 노출 우선순위 티어 — 카테고리별 '몇 줌부터 보일지'. 랜드마크는 낮은 줌, 소상공인은 높은 줌.
# (표준: 중요도 rank→줌별 노출. 여기선 rank를 타일에 굽지 않고 카테고리→티어를 스타일에서 유도 → 고급설정에서 편집 가능.)
POI_TIERS_DEFAULT = [
    {"key": "t1", "label": "높음", "minzoom": 15},
    {"key": "t2", "label": "중간", "minzoom": 16},
    {"key": "t3", "label": "낮음", "minzoom": 17},
]

CACHE_DEFAULT = {
    "static_long": "public, max-age=604800",
    "static_short": "public, max-age=300, stale-while-revalidate=3600",
    "dyn": "public, max-age=3600, stale-while-revalidate=86400",
}
POI_TIER_FALLBACK = "t3"   # 미지정 카테고리 기본 티어
CAT_TIER_DEFAULT = {       # 카테고리→티어 기본(대분류 + 랜드마크성 중/소분류). 고급설정에서 변경.
    "cat1": {"음식": "t3", "소매": "t3", "수리·개인": "t3", "과학·기술": "t3", "교육": "t2",
             "예술·스포츠": "t3", "시설관리·임대": "t2", "부동산": "t3", "숙박": "t2", "보건의료": "t2"},
    "cat2": {"병원": "t1", "대학": "t1"},
}

# 데이터 출처 토글 — "이 지도에 어떤 데이터를 쓸지" 체크.
#  src_visibility: 해당 style source의 모든 레이어 visibility 일괄(레이어 목록 하드코딩 회피).
#  poi_source: poi 레이어(poi-dot/icon/label)를 source 속성으로 필터(상가/인허가 개별 토글).
#  terrain: 스타일 레이어 없음 — 최상위 terrain 속성(raster-dem 소스) 설정/해제, MapLibre 로드 시 자동 적용.
SOURCE_GROUPS = [
    {"key": "base",      "label": "기본도(OSM)",       "kind": "src_visibility", "src": "openmaptiles"},
    {"key": "buildings", "label": "건물(GIS·2D/3D)",   "kind": "src_visibility", "src": "buildings"},
    {"key": "dong",      "label": "동(棟) 라벨",         "kind": "src_visibility", "src": "dong"},
    {"key": "sangga",    "label": "상가(소상공인)",      "kind": "poi_source",     "source": "sangga"},
    {"key": "localdata", "label": "인허가(LOCALDATA)",   "kind": "poi_source",     "source": "localdata"},
    {"key": "terrain",   "label": "지형(3D)",            "kind": "terrain"},
]
POI_FILTER_LAYERS = ["poi-label"]


# 퀵 프리셋 — 객체별 색 한 벌. dark=현재 기본 팔레트, light=밝은 배경+짙은 라벨.
PRESETS = {
    "dark": {
        "background": "#102542", "water": "#85cbfa", "greenery": "#1a2824", "boundary": "#495379",
        "road": "#2e3757", "building2d": "#495679", "building3d": "#54648c",
        "donglabel": "#f3dd9a", "poilabel": "#e8edf2", "placelabel": "#e0e0e0",
    },
    "light": {
        "background": "#eef1f5", "water": "#a9d3f2", "greenery": "#cfe3c8", "boundary": "#aab2c4",
        "road": "#c4cad4", "building2d": "#e0ddd6", "building3d": "#d6d2ca",
        "donglabel": "#8a6d00", "poilabel": "#2f3742", "placelabel": "#1f2630",
    },
}


# 글꼴 개별 설정 대상(라벨 객체) — OBJECTS 중 symbol 레이어를 가진 것
FONT_LABELS = ["donglabel", "poilabel", "placelabel"]


def _label_layers(key):
    o = {x["key"]: x for x in OBJECTS}.get(key, {})
    return [t[0] for t in o.get("targets", []) if t[1] == "text-color"]


def available_fonts(glyphs_dir):
    import os
    if not glyphs_dir or not os.path.isdir(glyphs_dir):
        return []
    return sorted(d for d in os.listdir(glyphs_dir)
                  if os.path.isdir(os.path.join(glyphs_dir, d)) and not d.startswith("."))


def current_fonts(style):
    idx = _index(style); out = {}
    for key in FONT_LABELS:
        for lid in _label_layers(key):
            tf = idx.get(lid, {}).get("layout", {}).get("text-font")
            if isinstance(tf, list) and tf:
                out[key] = tf[0]; break
    return out


def apply_fonts(style, fonts):
    """fonts({all?, <label_key>:font}) → 심볼 레이어 text-font 적용. all 먼저, 라벨별 override."""
    if not fonts:
        return 0
    idx = _index(style); n = 0
    allf = fonts.get("all")
    if allf:
        for L in style.get("layers", []):
            if L.get("type") == "symbol":
                L.setdefault("layout", {})["text-font"] = [allf]; n += 1
    for key in FONT_LABELS:
        f = fonts.get(key)
        if not f:
            continue
        for lid in _label_layers(key):
            if lid in idx:
                idx[lid].setdefault("layout", {})["text-font"] = [f]; n += 1
    return n


def _hsl_to_hex(h, s, l):
    s /= 100.0; l /= 100.0
    c = (1 - abs(2 * l - 1)) * s
    x = c * (1 - abs((h / 60.0) % 2 - 1)); mm = l - c / 2
    r, g, b = [(c, x, 0), (x, c, 0), (0, c, x), (0, x, c), (x, 0, c), (c, 0, x)][int(h // 60) % 6]
    return "#%02x%02x%02x" % (round((r + mm) * 255), round((g + mm) * 255), round((b + mm) * 255))


def to_hex(v):
    """paint 값(#hex / hsl(...) / 등)을 #rrggbb 로. 식(expression)·미지원은 None."""
    if not isinstance(v, str):
        return None
    v = v.strip()
    if v.startswith("#"):
        return "#" + "".join(ch * 2 for ch in v[1:]) if len(v) == 4 else v[:7].lower()
    m = re.match(r"hsla?\(\s*([\d.]+)\s*,\s*([\d.]+)%\s*,\s*([\d.]+)%", v)
    if m:
        return _hsl_to_hex(float(m.group(1)), float(m.group(2)), float(m.group(3)))
    return None


def _index(style):
    return {L.get("id"): L for L in style.get("layers", [])}


def build_poi_icon_match(icons, overrides=None):
    """대분류(cat1) 기본 + 중분류(cat2)·소분류(cat) 오버라이드 → 중첩 match 식.
    평가 우선순위: 소분류 > 중분류 > 대분류 > fallback (가장 구체적인 것이 이김)."""
    overrides = overrides or {}
    expr = ["match", ["get", "cat1"]]                     # 대분류 기본(가장 안쪽 default)
    for g in ICON_GROUPS:
        slug = (icons or {}).get(g["key"]) or g["icon"]
        expr += [list(g["cats"]), slug]
    expr.append(ICON_FALLBACK)
    mid = overrides.get("cat2") or {}
    if mid:
        m = ["match", ["get", "cat2"]]
        for name, slug in mid.items():
            m += [name, slug]
        m.append(expr); expr = m
    return expr


def current_icons(style):
    """poi-icon match 식에서 그룹별 현재 아이콘 slug 추출(없으면 default)."""
    L = _index(style).get(POI_ICON_LAYER, {})
    expr = _gate_inner(L.get("layout", {}).get("icon-image"))
    out = {g["key"]: g["icon"] for g in ICON_GROUPS}
    if isinstance(expr, list) and expr and expr[0] == "match" and len(expr) >= 4:
        pairs = list(zip(expr[2:-1:2], expr[3:-1:2]))
        for g in ICON_GROUPS:
            for labels, slug in pairs:
                labs = labels if isinstance(labels, list) else [labels]
                if set(labs) == set(g["cats"]):
                    out[g["key"]] = slug
                    break
    return out


def sanitize_cache(c):
    """theme['cache'] 정제(주입 방지) — 화이트리스트 키 + 안전문자(영숫자/쉼표/등호/하이픈/공백)만."""
    if not isinstance(c, dict):
        return None
    out = {}
    for k in ("static_long", "static_short", "dyn"):
        v = c.get(k)
        if isinstance(v, str) and re.fullmatch(r"[A-Za-z0-9,= \-]{1,120}", v):
            out[k] = v.strip()
    return out or None


def render_nginx_cache_block(cache):
    """theme cache → gateway-nginx 마커 사이 map 블록 텍스트(끝에 개행 포함)."""
    c = {**CACHE_DEFAULT, **(cache or {})}
    return (
        'map $uri $static_cc {\n'
        '    default              "%s";\n'
        '    ~^/(fonts|sprites)/  "%s";\n'
        '}\n'
        'map $request_uri $dyn_cc { default "%s"; }\n'
    ) % (c["static_short"], c["static_long"], c["dyn"])


def sanitize_icons(icons):
    """theme['icons'] 정제(주입 방지) — 유효 그룹키 + ASCII slug([a-z0-9_-])만."""
    if not isinstance(icons, dict):
        return {}
    keys = {g["key"] for g in ICON_GROUPS}
    out = {}
    for k, v in icons.items():
        if k in keys and isinstance(v, str) and re.fullmatch(r"[a-z0-9_-]+", v or ""):
            out[k] = v
    return out


def sanitize_icon_overrides(ov):
    """theme['icon_overrides'] 정제 — {cat2:{중분류명:slug}}, slug ASCII."""
    if not isinstance(ov, dict):
        return {}
    out = {}
    for lvl in ("cat2",):
        m = ov.get(lvl)
        if not isinstance(m, dict):
            continue
        clean = {}
        for name, slug in m.items():
            if isinstance(name, str) and 0 < len(name) <= 60 and isinstance(slug, str) and re.fullmatch(r"[a-z0-9_]+", slug or ""):
                clean[name] = slug
        if clean:
            out[lvl] = clean
    return out


def current_icon_overrides(style):
    """poi-icon 중첩 match 에서 중분류 오버라이드 추출 → {cat2:{...}}."""
    e = _gate_inner(_index(style).get(POI_ICON_LAYER, {}).get("layout", {}).get("icon-image"))
    out = {"cat2": {}}
    while isinstance(e, list) and len(e) >= 4 and e[0] == "match" and isinstance(e[1], list) and len(e[1]) == 2 and e[1][0] == "get":
        field = e[1][1]
        if field == "cat1":
            break
        for name, slug in zip(e[2:-1:2], e[3:-1:2]):
            if field == "cat2" and isinstance(name, str):
                out[field][name] = slug
        e = e[-1]
    return out


def apply_icons(style, icons, overrides=None):
    """icons({group_key:slug}) + overrides({cat2/cat:{name:slug}}) → poi-icon icon-image. 반환: 적용 수."""
    idx = _index(style)
    if POI_ICON_LAYER not in idx:
        return 0
    cur = current_icons(style)
    cur.update(icons or {})
    idx[POI_ICON_LAYER].setdefault("layout", {})["icon-image"] = build_poi_icon_match(cur, overrides)
    ov = overrides or {}
    return (len(icons or {})) + len(ov.get("cat2") or {})


# ── POI 카테고리 그룹별 글자색 (cat1 → 그룹색, 미지정 그룹은 '시설 라벨' 단일색 fallback) ──
# 아이콘과 동일한 ICON_GROUPS(10개) 단위. poi-label text-color 를 cat1 match 식으로 만든다.
def _match_default(v):
    """match 식이면 default(마지막 원소) 반환, 아니면 None."""
    if isinstance(v, list) and v and v[0] == "match" and len(v) >= 4:
        return v[-1]
    return None


def build_poi_cat_color(colors, fallback):
    """그룹별 글자색({group_key:#hex}) → cat1 match(그룹 cats→색), default=fallback.
    지정된 그룹이 없으면 fallback(평면색) 그대로 반환."""
    pairs = []
    for g in ICON_GROUPS:
        c = (colors or {}).get(g["key"])
        if c:
            pairs.append((list(g["cats"]), c))
    if not pairs:
        return fallback
    expr = ["match", ["get", "cat1"]]
    for cats, c in pairs:
        expr += [cats, c]
    expr.append(fallback)
    return expr


def apply_poi_cat_colors(style, colors):
    """그룹별 시설 글자색을 poi-label text-color 에 적용. fallback = 현재 text-color
    (평면색 또는 기존 match 의 default). colors 비면 평면색으로 복원. 반환 적용 수."""
    L = _index(style).get(POI_ICON_LAYER)   # poi-label
    if L is None:
        return 0
    paint = L.setdefault("paint", {})
    cur = paint.get("text-color")
    fb = _match_default(cur) if isinstance(cur, list) else cur
    fb = fb if isinstance(fb, str) else "#e8edf2"
    if not colors:
        if isinstance(cur, list):           # 기존 그룹색 → 평면 복원
            paint["text-color"] = fb
            return 1
        return 0
    paint["text-color"] = build_poi_cat_color(colors, fb)
    return 1


def current_poi_colors(style):
    """poi-label text-color match 에서 그룹별 색 추출 → {group_key:#hex}(미지정 그룹 제외)."""
    v = _index(style).get(POI_ICON_LAYER, {}).get("paint", {}).get("text-color")
    out = {}
    if isinstance(v, list) and v and v[0] == "match":
        pairs = list(zip(v[2:-1:2], v[3:-1:2]))
        for g in ICON_GROUPS:
            for labels, col in pairs:
                labs = labels if isinstance(labels, list) else [labels]
                if set(labs) == set(g["cats"]):
                    h = to_hex(col)
                    if h:
                        out[g["key"]] = h
                    break
    return out


def sanitize_poi_colors(colors):
    """theme['poi_colors'] 정제(주입 방지) — 유효 그룹키 + #rrggbb 만."""
    if not isinstance(colors, dict):
        return {}
    keys = {g["key"] for g in ICON_GROUPS}
    out = {}
    for k, v in colors.items():
        if k in keys and isinstance(v, str) and re.fullmatch(r"#[0-9a-fA-F]{6}", v or ""):
            out[k] = v
    return out


# ── POI 노출 우선순위 티어 (카테고리별 줌 게이트) ─────────────────────
def _gate_inner(expr):
    """줌×카테고리 게이트에서 inner(원래 값)를 반환, 아니면 그대로(idempotent).
    두 형태 지원 — 구형 ["case",cond,inner,""], 신형 ["step",["zoom"],"",z,["case",["<=",mz,z],inner,""],...]."""
    if isinstance(expr, list) and len(expr) >= 4 and expr[0] == "case":
        return expr[2]
    if isinstance(expr, list) and len(expr) >= 5 and expr[0] == "step":
        out = expr[4]   # 첫 stop 출력 = ["case",["<=",mz,z],inner,""]
        if isinstance(out, list) and len(out) >= 4 and out[0] == "case":
            return out[2]
    return expr


def _cat_match(field, mapping, inner):
    """["match",["get",field], k0,v0,..., inner] — mapping 비면 inner 그대로."""
    if not mapping:
        return inner
    m = ["match", ["get", field]]
    for k, v in mapping.items():
        m += [k, v]
    m.append(inner)
    return m


def _tier_value_expr(tiers, cat_tier, value_of):
    """카테고리→티어→value 중첩 match (소 > 중 > 대 > fallback)."""
    tval = {t["key"]: value_of(t, i) for i, t in enumerate(tiers)}
    default = tval.get(POI_TIER_FALLBACK, value_of(tiers[-1], len(tiers) - 1))
    conv = lambda m: {name: tval[tk] for name, tk in (m or {}).items() if tk in tval}
    expr = _cat_match("cat1", conv((cat_tier or {}).get("cat1")), default)
    expr = _cat_match("cat2", conv((cat_tier or {}).get("cat2")), expr)
    return expr


def apply_poi_tiers(style, tiers, cat_tier):
    """poi-label의 text/icon을 '카테고리 티어 줌 이상에서만 표시'로 게이트 + 티어순 sort-key. 반환 적용 수."""
    L = _index(style).get(POI_ICON_LAYER)   # poi-label
    if L is None or not tiers:
        return 0
    mz = _tier_value_expr(tiers, cat_tier, lambda t, i: t.get("minzoom", 15))
    sk = _tier_value_expr(tiers, cat_tier, lambda t, i: i)   # 티어 index = 충돌 우선순위(낮을수록 먼저)
    lay = L.setdefault("layout", {})
    inner_icon = _gate_inner(lay.get("icon-image"))
    # 카테고리별 줌 게이트 — style-spec은 ["zoom"]을 step/interpolate 최상위 입력으로만 허용한다.
    # ["case",[">=",["zoom"],mz],V,""](불법: zoom을 case/비교 안에 중첩)를, 줌 step의 각 stop에서
    # '데이터식 mz'를 그 stop 줌값과 비교하는 형태로 표현(동작 동일, mz<=현재stop줌이면 V, 아니면 "").
    zlevels = sorted({int(t.get("minzoom", 15)) for t in tiers})
    def _zoom_gate(value):
        g = ["step", ["zoom"], ""]
        for z in zlevels:
            g += [z, ["case", ["<=", mz, z], value, ""]]
        return g
    lay["text-field"] = _zoom_gate(["get", "name"])
    if inner_icon is not None:
        lay["icon-image"] = _zoom_gate(inner_icon)
    lay["symbol-sort-key"] = sk
    L["minzoom"] = min(t.get("minzoom", 15) for t in tiers)
    # 소스 minzoom도 표시 줌(tier 최소)에 맞춰 자동 정렬 — z(min)~ 미만 거대타일 페치 차단(데이터는 전수 보존).
    src_name = L.get("source")
    src = (style.get("sources") or {}).get(src_name) if src_name else None
    if isinstance(src, dict):
        src["minzoom"] = min(int(t.get("minzoom", 15)) for t in tiers)
    return 1


def sanitize_poi_tiers(tiers):
    if not isinstance(tiers, list):
        return None
    out = []
    for t in tiers:
        if isinstance(t, dict) and isinstance(t.get("key"), str) and re.fullmatch(r"t[0-9]+", t["key"]):
            mz = t.get("minzoom")
            if isinstance(mz, (int, float)) and 0 <= mz <= 24:
                out.append({"key": t["key"], "label": str(t.get("label", t["key"]))[:50], "minzoom": int(mz)})
    return out or None


def sanitize_cat_tiers(ct):
    if not isinstance(ct, dict):
        return None
    out = {}
    for lvl in ("cat1", "cat2"):
        m = ct.get(lvl)
        if isinstance(m, dict):
            clean = {name: tk for name, tk in m.items()
                     if isinstance(name, str) and 0 < len(name) <= 60 and isinstance(tk, str) and re.fullmatch(r"t[0-9]+", tk)}
            if clean:
                out[lvl] = clean
    return out or None


def _poi_base_extra(lid):
    """poi 레이어의 출처와 무관한 기본 필터 절(라벨은 클러스터 제외)."""
    return [["!", ["has", "point_count"]]] if lid == "poi-label" else []


def _apply_poi_source_filter(style, enabled):
    """poi 레이어(poi-dot/icon/label) 필터를 활성 출처집합으로 재구성.
    둘 다(또는 기본): is_primary=대표만. 한쪽만: 그 source. 둘 다 끔: visibility none."""
    idx = _index(style); es = set(enabled); n = 0
    src_pred = None
    if not es:
        pass
    elif es >= {"sangga", "localdata"}:
        src_pred = ["!=", ["get", "is_primary"], 0]
    else:                                   # 정확히 하나
        src_pred = ["==", ["get", "source"], next(iter(es))]
    for lid in POI_FILTER_LAYERS:
        L = idx.get(lid)
        if not L:
            continue
        if not es:                          # 둘 다 끔 → 숨김
            L.setdefault("layout", {})["visibility"] = "none"; n += 1; continue
        L.setdefault("layout", {})["visibility"] = "visible"
        clauses = _poi_base_extra(lid) + ([src_pred] if src_pred else [])
        if clauses:
            L["filter"] = clauses[0] if len(clauses) == 1 else ["all"] + clauses
        n += 1
    return n


def apply_sources(style, cfg):
    """cfg({source_key: bool}) → 레이어 visibility / poi 출처 필터 / terrain 적용. 반환: 적용 수."""
    if not cfg:
        return 0
    n = 0
    for g in SOURCE_GROUPS:
        if g["kind"] == "src_visibility" and g["key"] in cfg:
            vis = "visible" if cfg[g["key"]] else "none"
            for L in style.get("layers", []):
                if L.get("source") == g["src"]:
                    L.setdefault("layout", {})["visibility"] = vis; n += 1
        elif g["kind"] == "terrain" and g["key"] in cfg:
            # 최상위 terrain 속성 설정/해제 — MapLibre가 로드 시 자동 적용(클라 setTerrain 불필요).
            # raster-dem 소스가 없으면 설정 금지(스타일 로드 깨짐 방지).
            if cfg[g["key"]] and "terrain" in style.get("sources", {}):
                style["terrain"] = {"source": "terrain", "exaggeration": 1.3}; n += 1
            elif style.pop("terrain", None) is not None:
                n += 1
    poi_keys = [g["key"] for g in SOURCE_GROUPS if g["kind"] == "poi_source"]
    if any(k in cfg for k in poi_keys):
        enabled = [k for k in poi_keys if cfg.get(k, True)]   # 미지정은 활성으로 간주
        n += _apply_poi_source_filter(style, enabled)
    return n


def sanitize_sources(sources):
    """theme['sources'] 정제 — 유효 출처키 + bool 만."""
    if not isinstance(sources, dict):
        return {}
    keys = {g["key"] for g in SOURCE_GROUPS}
    return {k: bool(v) for k, v in sources.items() if k in keys}


def current_sources(style):
    """각 출처의 현재 on/off. src_visibility=레이어 하나라도 visible이면 True;
    poi_source=필터에 그 source 단독 지정이 없으면 True(둘 다 표시 기본);
    terrain=최상위 terrain 속성 존재 여부."""
    idx = _index(style); out = {}
    for g in SOURCE_GROUPS:
        if g["kind"] == "src_visibility":
            ls = [L for L in style.get("layers", []) if L.get("source") == g["src"]]
            out[g["key"]] = any(L.get("layout", {}).get("visibility", "visible") != "none" for L in ls) if ls else True
        elif g["kind"] == "terrain":
            out[g["key"]] = bool(style.get("terrain"))
    # poi 출처: poi-label 필터/visibility로 판정
    pd = idx.get("poi-label", {})
    vis = pd.get("layout", {}).get("visibility", "visible")
    only = None
    f = pd.get("filter")
    def _find_src(expr):
        if isinstance(expr, list):
            if len(expr) == 3 and expr[0] == "==" and expr[1] == ["get", "source"]:
                return expr[2]
            for e in expr:
                r = _find_src(e)
                if r:
                    return r
        return None
    only = _find_src(f)
    for g in SOURCE_GROUPS:
        if g["kind"] == "poi_source":
            if vis == "none":
                out[g["key"]] = False
            elif only:
                out[g["key"]] = (only == g["source"])
            else:
                out[g["key"]] = True
    return out


# 객체 표시 on/off(visibility) — OBJECTS 대상 레이어의 layout.visibility 일괄.
def _object_layers(key):
    o = {x["key"]: x for x in OBJECTS}.get(key, {})
    out = []
    for t in o.get("targets", []):
        if t[0] not in out:
            out.append(t[0])
    return out


def apply_visibility(style, cfg):
    """cfg({object_key: bool}) → 객체 대상 레이어 visibility. 반환: 적용 수."""
    if not cfg:
        return 0
    idx = _index(style); keys = {o["key"] for o in OBJECTS}; n = 0
    for k, on in cfg.items():
        if k not in keys:
            continue
        for lid in _object_layers(k):
            L = idx.get(lid)
            if L is not None:
                L.setdefault("layout", {})["visibility"] = "visible" if on else "none"; n += 1
    return n


def current_visibility(style):
    idx = _index(style); out = {}
    for o in OBJECTS:
        ls = [idx[l] for l in _object_layers(o["key"]) if l in idx]
        out[o["key"]] = any(L.get("layout", {}).get("visibility", "visible") != "none" for L in ls) if ls else True
    return out


def sanitize_visibility(v):
    if not isinstance(v, dict):
        return {}
    keys = {o["key"] for o in OBJECTS}
    return {k: bool(val) for k, val in v.items() if k in keys}


# 표시 언어 — 라벨 text-field 를 한글/영문/원어로 전환.
LANG_LAYERS = ["place-label", "road-label", "poi-label", "poi-civic-label",
               "poi-station-label", "peak-label", "aerodrome-label"]


def _lang_textfield(lang):
    if lang == "en":
        return ["coalesce", ["get", "name:en"], ["get", "name:latin"], ["get", "name"]]
    if lang == "local":
        return ["get", "name"]
    return ["coalesce", ["get", "name:ko"], ["get", "name"]]   # ko 기본


def apply_language(style, lang):
    """lang ∈ {ko,en,local} → 라벨 레이어 text-field 전환. 반환: 적용 수."""
    if lang not in ("ko", "en", "local"):
        return 0
    idx = _index(style); tf = _lang_textfield(lang); n = 0
    for lid in LANG_LAYERS:
        L = idx.get(lid)
        if L is not None and L.get("type") == "symbol":
            L.setdefault("layout", {})["text-field"] = tf; n += 1
    return n


def current_language(style):
    tf = _index(style).get("place-label", {}).get("layout", {}).get("text-field")
    s = str(tf)
    if "name:en" in s:
        return "en"
    if "name:ko" in s:
        return "ko"
    return "local"


# 크기(SIZE) — text-size/circle-radius/line-width 의 줌 보간 출력값 min/max 직접 편집.
# (값 직접 편집 → 누적 없음. interp는 곡선 모양 유지하며 양끝을 [min,max]로 선형 리맵, flat은 max로.)
SIZE_TARGETS = [
    {"key": "sz_place", "label": "지명 라벨 크기", "layer": "place-label", "prop": "text-size"},
    {"key": "sz_road",  "label": "도로 라벨 크기", "layer": "road-label",  "prop": "text-size"},
    {"key": "sz_poi",   "label": "시설 라벨 크기", "layer": "poi-label",   "prop": "text-size"},
    {"key": "sz_poi_icon", "label": "시설 아이콘 크기", "layer": "poi-label", "prop": "icon-size"},
    {"key": "sz_dong",  "label": "동 라벨 크기",   "layer": "dong-label",  "prop": "text-size"},
    {"key": "r_dong",   "label": "동 점 크기",     "layer": "dong-dot",    "prop": "circle-radius"},
    {"key": "w_minor",  "label": "도로-소로 두께", "layer": "road-minor",  "prop": "line-width"},
    {"key": "w_secon",  "label": "도로-2차선 두께", "layer": "road-secondary", "prop": "line-width"},
    {"key": "w_prim",   "label": "도로-1차선 두께", "layer": "road-primary",   "prop": "line-width"},
    {"key": "w_motor",  "label": "도로-고속 두께", "layer": "road-motorway",  "prop": "line-width"},
]


def _size_where(prop):
    return "layout" if prop in ("text-size", "icon-size") else "paint"


def _val_minmax(v):
    if isinstance(v, (int, float)):
        return (float(v), float(v))
    if isinstance(v, list) and v and v[0] == "interpolate":
        outs = [v[i] for i in range(4, len(v), 2) if isinstance(v[i], (int, float))]
        if outs:
            return (min(outs), max(outs))
    return None


def _remap(v, mn, mx):
    if isinstance(v, (int, float)):
        return round(mx, 2)
    if isinstance(v, list) and v and v[0] == "interpolate":
        v = list(v)
        outs = [v[i] for i in range(4, len(v), 2) if isinstance(v[i], (int, float))]
        if not outs:
            return v
        omn, omx = min(outs), max(outs); span = (omx - omn) or 1
        for i in range(4, len(v), 2):
            if isinstance(v[i], (int, float)):
                v[i] = round(mn + (v[i] - omn) / span * (mx - mn), 2)
        return v
    return v


def current_sizes(style):
    idx = _index(style); out = {}
    for t in SIZE_TARGETS:
        v = idx.get(t["layer"], {}).get(_size_where(t["prop"]), {}).get(t["prop"])
        mm = _val_minmax(v)
        out[t["key"]] = {"min": mm[0], "max": mm[1]} if mm else {"min": None, "max": None}
    return out


def apply_sizes(style, cfg):
    """cfg({size_key:{min,max}}) → 대상 레이어의 size prop 값을 [min,max]로 리맵. 반환: 적용 수."""
    if not cfg:
        return 0
    idx = _index(style); tmap = {t["key"]: t for t in SIZE_TARGETS}; n = 0
    for k, mm in cfg.items():
        t = tmap.get(k)
        if not t or not isinstance(mm, dict) or mm.get("min") is None or mm.get("max") is None:
            continue
        L = idx.get(t["layer"])
        if L is None:
            continue
        where = _size_where(t["prop"])
        cur = L.get(where, {}).get(t["prop"])
        # cur 부재 시 _remap 은 None 을 반환 → style-spec 위반(null layout/paint). max 로 평면값 설정.
        L.setdefault(where, {})[t["prop"]] = (round(float(mm["max"]), 2) if cur is None
                                              else _remap(cur, float(mm["min"]), float(mm["max"])))
        n += 1
    return n


def sanitize_sizes(sizes):
    if not isinstance(sizes, dict):
        return {}
    keys = {t["key"] for t in SIZE_TARGETS}; out = {}
    for k, mm in sizes.items():
        if k not in keys or not isinstance(mm, dict):
            continue
        o = {}
        for b in ("min", "max"):
            x = mm.get(b)
            if isinstance(x, (int, float)) and 0 <= x <= 200:
                o[b] = round(float(x), 2)
        if "min" in o and "max" in o:
            out[k] = o
    return out


def size_targets():
    return SIZE_TARGETS


# 고급 paint 속성(EXTRA) — text-halo·circle-stroke·fill-outline 을 제네릭 편집(color/num).
EXTRA_TARGETS = [
    {"key": "halo_place_c", "label": "지명 라벨 외곽색", "layer": "place-label", "prop": "text-halo-color", "type": "color"},
    {"key": "halo_place_w", "label": "지명 외곽 두께", "layer": "place-label", "prop": "text-halo-width", "type": "num"},
    {"key": "halo_poi_c",  "label": "시설 라벨 외곽색", "layer": "poi-label", "prop": "text-halo-color", "type": "color"},
    {"key": "halo_poi_w",  "label": "시설 외곽 두께", "layer": "poi-label", "prop": "text-halo-width", "type": "num"},
    {"key": "halo_dong_c", "label": "동 라벨 외곽색", "layer": "dong-label", "prop": "text-halo-color", "type": "color"},
    {"key": "halo_dong_w", "label": "동 외곽 두께", "layer": "dong-label", "prop": "text-halo-width", "type": "num"},
    {"key": "halo_road_c", "label": "도로 라벨 외곽색", "layer": "road-label", "prop": "text-halo-color", "type": "color"},
    {"key": "halo_road_w", "label": "도로 외곽 두께", "layer": "road-label", "prop": "text-halo-width", "type": "num"},
    {"key": "stroke_dong_c", "label": "동 점 테두리색", "layer": "dong-dot", "prop": "circle-stroke-color", "type": "color"},
    {"key": "stroke_dong_w", "label": "동 점 테두리 두께", "layer": "dong-dot", "prop": "circle-stroke-width", "type": "num"},
    {"key": "outline_bld", "label": "건물 외곽선색", "layer": "building-2d", "prop": "fill-outline-color", "type": "color"},
]


def extra_targets():
    return EXTRA_TARGETS


# 배치(PLACEMENT) — 라벨/아이콘 위치 layout 속성: 세로 offset / 기준점(anchor) / 충돌 시 생략(bool).
LABEL_ANCHORS = ["center", "left", "right", "top", "bottom",
                 "top-left", "top-right", "bottom-left", "bottom-right"]
# default = 해당 prop 의 MapLibre 스펙 기본값. UI 는 값 미설정 시 빈칸 대신 이 기본값을 표시한다.
PLACEMENT_TARGETS = [
    {"key": "off_poi",    "label": "시설 라벨 세로 위치", "layer": "poi-label",       "prop": "text-offset",   "type": "offset", "default": 0},
    {"key": "ta_poi",     "label": "시설 라벨 기준점",    "layer": "poi-label",       "prop": "text-anchor",   "type": "anchor", "default": "center"},
    {"key": "ia_poi",     "label": "시설 아이콘 기준점",  "layer": "poi-label",       "prop": "icon-anchor",   "type": "anchor", "default": "center"},
    {"key": "topt_poi",   "label": "시설 라벨 충돌 시 생략",  "layer": "poi-label",       "prop": "text-optional", "type": "bool", "default": False},
    {"key": "topt_dong",  "label": "동 라벨 충돌 시 생략",    "layer": "dong-label",      "prop": "text-optional", "type": "bool", "default": False},
    {"key": "topt_civic", "label": "공공시설 라벨 충돌 시 생략", "layer": "poi-civic-label", "prop": "text-optional", "type": "bool", "default": False},
    {"key": "topt_peak",  "label": "봉우리 라벨 충돌 시 생략",  "layer": "peak-label",      "prop": "text-optional", "type": "bool", "default": False},
]


def placement_targets():
    return PLACEMENT_TARGETS


def apply_placement(style, cfg):
    """cfg({key:value}) → 대상 layout 속성 설정. offset=text-offset[1](세로, x는 보존/0),
    anchor=enum 문자열, bool=text-optional. 반환: 적용 수."""
    if not cfg:
        return 0
    idx = _index(style); tmap = {t["key"]: t for t in PLACEMENT_TARGETS}; n = 0
    for k, v in cfg.items():
        t = tmap.get(k)
        if not t:
            continue
        L = idx.get(t["layer"])
        if L is None:
            continue
        lay = L.setdefault("layout", {})
        if t["type"] == "offset":
            cur = lay.get(t["prop"])
            x = cur[0] if isinstance(cur, list) and cur and isinstance(cur[0], (int, float)) else 0
            lay[t["prop"]] = [x, round(float(v), 2)]
        elif t["type"] == "anchor":
            lay[t["prop"]] = v
        else:  # bool
            lay[t["prop"]] = bool(v)
        n += 1
    return n


def current_placement(style):
    idx = _index(style); out = {}
    for t in PLACEMENT_TARGETS:
        v = idx.get(t["layer"], {}).get("layout", {}).get(t["prop"])
        if t["type"] == "offset":
            out[t["key"]] = v[1] if isinstance(v, list) and len(v) >= 2 and isinstance(v[1], (int, float)) else None
        elif t["type"] == "anchor":
            out[t["key"]] = v if (isinstance(v, str) and v in LABEL_ANCHORS) else None
        else:  # bool
            out[t["key"]] = v if isinstance(v, bool) else None
    return out


def sanitize_placement(pl):
    if not isinstance(pl, dict):
        return {}
    tmap = {t["key"]: t for t in PLACEMENT_TARGETS}; out = {}
    for k, v in pl.items():
        t = tmap.get(k)
        if not t:
            continue
        if t["type"] == "offset":
            if isinstance(v, (int, float)) and not isinstance(v, bool) and -10 <= v <= 10:
                out[k] = round(float(v), 2)
        elif t["type"] == "anchor":
            if isinstance(v, str) and v in LABEL_ANCHORS:
                out[k] = v
        elif t["type"] == "bool":
            if isinstance(v, bool):
                out[k] = v
    return out


# 속성별 색 그라데이션 — 건물 높이(render_height)에 따른 색 보간.
GRADIENT_TARGETS = [
    {"key": "bld_height", "label": "건물 높이별 색", "attr": "render_height", "amax": 100,
     "layers": [["building-2d", "fill-color"], ["Building 3D", "fill-extrusion-color"]]},
]


def gradient_targets():
    return GRADIENT_TARGETS


def apply_gradient(style, cfg):
    """cfg({key:{on,low,high,max}}) → 대상 레이어 색을 attr 보간식으로(on). 반환: 적용 수."""
    if not cfg:
        return 0
    idx = _index(style); tmap = {g["key"]: g for g in GRADIENT_TARGETS}; n = 0
    for k, c in cfg.items():
        g = tmap.get(k)
        if not g or not isinstance(c, dict) or not c.get("on"):
            continue
        lo = c.get("low") or "#3a4a6b"; hi = c.get("high") or "#9db4e0"; amax = c.get("max") or g["amax"]
        expr = ["interpolate", ["linear"], ["get", g["attr"]], 0, lo, amax, hi]
        for lid, prop in g["layers"]:
            L = idx.get(lid)
            if L is not None:
                L.setdefault("paint", {})[prop] = expr; n += 1
    return n


def current_gradient(style):
    idx = _index(style); out = {}
    for g in GRADIENT_TARGETS:
        L = idx.get(g["layers"][0][0], {}); v = L.get("paint", {}).get(g["layers"][0][1])
        on = isinstance(v, list) and bool(v) and v[0] == "interpolate"
        lo = hi = None; amax = g["amax"]
        if on:
            try:
                lo = to_hex(v[4]); hi = to_hex(v[-1]); amax = v[-2]
            except Exception:
                pass
        out[g["key"]] = {"on": on, "low": lo, "high": hi, "max": amax}
    return out


def sanitize_gradient(gr):
    if not isinstance(gr, dict):
        return {}
    tmap = {g["key"]: g for g in GRADIENT_TARGETS}; out = {}
    for k, c in gr.items():
        if k not in tmap or not isinstance(c, dict):
            continue
        o = {"on": bool(c.get("on"))}
        for b in ("low", "high"):
            if isinstance(c.get(b), str) and re.fullmatch(r"#[0-9a-fA-F]{6}", c.get(b) or ""):
                o[b] = c[b]
        if isinstance(c.get("max"), (int, float)) and 1 <= c["max"] <= 500:
            o["max"] = round(float(c["max"]), 1)
        out[k] = o
    return out


def apply_extras(style, cfg):
    """cfg({extra_key: value}) → 대상 레이어 paint 속성 직접 설정(color #hex / num). 반환: 적용 수."""
    if not cfg:
        return 0
    idx = _index(style); tmap = {t["key"]: t for t in EXTRA_TARGETS}; n = 0
    for k, v in cfg.items():
        t = tmap.get(k)
        if not t:
            continue
        L = idx.get(t["layer"])
        if L is None:
            continue
        L.setdefault("paint", {})[t["prop"]] = v; n += 1
    return n


def current_extras(style):
    idx = _index(style); out = {}
    for t in EXTRA_TARGETS:
        v = idx.get(t["layer"], {}).get("paint", {}).get(t["prop"])
        if t["type"] == "color":
            out[t["key"]] = to_hex(v) if v is not None else None
        else:
            out[t["key"]] = v if isinstance(v, (int, float)) else None
    return out


def sanitize_extras(ex):
    if not isinstance(ex, dict):
        return {}
    tmap = {t["key"]: t for t in EXTRA_TARGETS}; out = {}
    for k, v in ex.items():
        t = tmap.get(k)
        if not t:
            continue
        if t["type"] == "color":
            if isinstance(v, str) and re.fullmatch(r"#[0-9a-fA-F]{6}", v or ""):
                out[k] = v
        elif isinstance(v, (int, float)) and 0 <= v <= 10:
            out[k] = round(float(v), 2)
    return out


def apply_building_pitch3d(style, v):
    """건물 2D/3D pitch 전환 임계값을 style.metadata 에 기록(데모가 읽음). None/무효면 키 제거(기본=현재상태)."""
    md = style.setdefault("metadata", {})
    if isinstance(v, (int, float)) and not isinstance(v, bool) and 0 <= v <= 85:
        md["cuvia:building_pitch_3d"] = round(float(v), 1); return 1
    md.pop("cuvia:building_pitch_3d", None); return 0


def apply_theme(style, theme):
    """theme({key:#hex}) 를 style 에 적용(평면 객체 + POI 그룹 match). 반환: 적용 수."""
    idx = _index(style); n = 0
    theme = theme or {}
    objs = {o["key"]: o for o in OBJECTS}
    for key, color in theme.items():
        o = objs.get(key)
        if not o or not color:
            continue
        for layer_id, prop in o["targets"]:
            L = idx.get(layer_id)
            if L is None:
                continue
            L.setdefault("paint", {})[prop] = color; n += 1
    # 건물 2D/3D pitch 전환 임계값 — style.metadata에 기록(기본: 키 없음=현재상태 유지)
    n += apply_building_pitch3d(style, theme.get("building_pitch_3d"))
    # POI 카테고리 그룹별 글자색 — 평면색 직후(방금 깐 '시설 라벨' 평면색을 fallback으로 감쌈)
    n += apply_poi_cat_colors(style, sanitize_poi_colors(theme.get("poi_colors") or {}))
    # 글꼴(layout text-font)
    n += apply_fonts(style, theme.get("fonts") or {})
    # 노출 줌(레이어 minzoom/maxzoom)
    n += apply_zoom(style, theme.get("zoom") or {})
    # 투명도(opacity)
    n += apply_opacity(style, theme.get("opacity") or {})
    # 카테고리 아이콘(poi-icon icon-image) — 대분류 + 중/소 오버라이드
    n += apply_icons(style, sanitize_icons(theme.get("icons") or {}),
                     sanitize_icon_overrides(theme.get("icon_overrides") or {}))
    # POI 노출 우선순위 티어(카테고리별 줌 게이트) — apply_icons 뒤(icon match를 감쌈)
    n += apply_poi_tiers(style, sanitize_poi_tiers(theme.get("poi_tiers")) or POI_TIERS_DEFAULT,
                         sanitize_cat_tiers(theme.get("cat_tiers")) or CAT_TIER_DEFAULT)
    # 데이터 출처 토글(레이어 visibility / poi 출처 필터)
    n += apply_sources(style, sanitize_sources(theme.get("sources") or {}))
    # 객체 표시 on/off
    n += apply_visibility(style, sanitize_visibility(theme.get("visibility") or {}))
    # 표시 언어(라벨 text-field)
    if theme.get("language"):
        n += apply_language(style, theme.get("language"))
    # 크기(text-size/circle-radius/line-width min~max)
    n += apply_sizes(style, sanitize_sizes(theme.get("sizes") or {}))
    # 고급 paint(halo/stroke/outline)
    n += apply_extras(style, sanitize_extras(theme.get("extras") or {}))
    # 속성별 색 그라데이션(건물 높이)
    n += apply_gradient(style, sanitize_gradient(theme.get("gradient") or {}))
    # 라벨/아이콘 배치(text-offset·anchor·text-optional)
    n += apply_placement(style, sanitize_placement(theme.get("placement") or {}))
    return n


def _opacity_targets():
    """투명도 편집 대상: {key,label,targets:[[layer_id,opacity_prop],...]}.
    OBJECTS 색 prop(-color)을 opacity prop(-opacity)으로 매핑."""
    out = []
    for o in OBJECTS:
        props = [[lid, prop.replace("-color", "-opacity")] for lid, prop in o["targets"]]
        out.append({"key": o["key"], "label": o["label"], "targets": props})
    return out


def opacity_objects():
    """UI/API 용 투명도 대상 목록."""
    return _opacity_targets()


def apply_opacity(style, ocfg):
    """ocfg({key:0~1}) → 대상 레이어 opacity prop 설정(None이면 해제). 반환: 적용 수."""
    if not ocfg:
        return 0
    idx = _index(style); tmap = {t["key"]: t for t in _opacity_targets()}; n = 0
    for key, v in ocfg.items():
        t = tmap.get(key)
        if not t:
            continue
        for layer_id, prop in t["targets"]:
            L = idx.get(layer_id)
            if L is None:
                continue
            if v is None:
                L.get("paint", {}).pop(prop, None)
            else:
                L.setdefault("paint", {})[prop] = v
            n += 1
    return n


def current_opacity(style):
    """각 대상의 현재 opacity(첫 대상 레이어 기준). 미설정이면 1.0(완전 불투명)."""
    idx = _index(style); out = {}
    for t in _opacity_targets():
        layer_id, prop = t["targets"][0]
        v = idx.get(layer_id, {}).get("paint", {}).get(prop)
        out[t["key"]] = float(v) if isinstance(v, (int, float)) else 1.0
    return out


def sanitize_opacity(opacity):
    """theme['opacity'] 정제(주입 방지) — 유효 key + 0~1 float(또는 None)만."""
    if not isinstance(opacity, dict):
        return {}
    keys = {t["key"] for t in _opacity_targets()}; out = {}
    for k, v in opacity.items():
        if k not in keys:
            continue
        if v is None:
            out[k] = None
        elif isinstance(v, (int, float)) and 0 <= v <= 1:
            out[k] = round(float(v), 3)
    return out


def apply_zoom(style, zcfg):
    """zcfg({key:{min,max}}) → 객체 대상 레이어들의 minzoom/maxzoom 설정(None이면 해제). 반환: 적용 수."""
    if not zcfg:
        return 0
    idx = _index(style); objs = {o["key"]: o for o in OBJECTS}; n = 0
    for key, z in zcfg.items():
        o = objs.get(key)
        if not o or not isinstance(z, dict):
            continue
        for layer_id, _ in o["targets"]:
            L = idx.get(layer_id)
            if L is None:
                continue
            for prop, val in (("minzoom", z.get("min")), ("maxzoom", z.get("max"))):
                if val is None:
                    L.pop(prop, None)
                else:
                    L[prop] = val
            n += 1
    return n


def sanitize_zoom(zoom):
    """theme['zoom'] 정제(주입 방지) — 유효 객체키 + min/max(0~24 정수 또는 None)만."""
    if not isinstance(zoom, dict):
        return {}
    keys = {o["key"] for o in OBJECTS}; out = {}
    for k, z in zoom.items():
        if k not in keys or not isinstance(z, dict):
            continue
        zz = {}
        for b in ("min", "max"):
            v = z.get(b)
            if v is None:
                zz[b] = None
            elif isinstance(v, (int, float)) and 0 <= v <= 24:
                zz[b] = int(v)
        out[k] = zz
    return out


def current_zoom(style):
    """각 객체의 현재 minzoom/maxzoom(첫 대상 레이어 기준). 미설정이면 None."""
    idx = _index(style); out = {}
    for o in OBJECTS:
        L = idx.get(o["targets"][0][0], {})
        out[o["key"]] = {"min": L.get("minzoom"), "max": L.get("maxzoom")}
    return out


def current_colors(style):
    """각 객체의 현재 색(첫 대상 레이어 기준)을 #hex 로. 식이면 None(단, match는 default색)."""
    idx = _index(style); out = {}
    for o in OBJECTS:
        layer_id, prop = o["targets"][0]
        L = idx.get(layer_id, {})
        raw = L.get("paint", {}).get(prop)
        d = _match_default(raw)
        out[o["key"]] = to_hex(d if d is not None else raw)
    return out
