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

# 시설(POI) 도트 색은 cat1 업종대분류 match 식 → 그룹별 색을 따로 편집. cats=[] 는 fallback(기타).
POI_GROUPS = [
    {"key": "poi_food",    "label": "음식·식품",   "cats": ["음식", "식품"],                       "default": "#e8915a"},
    {"key": "poi_retail",  "label": "소매",        "cats": ["소매"],                               "default": "#5b9bd5"},
    {"key": "poi_health",  "label": "보건·건강",   "cats": ["보건의료", "건강"],                   "default": "#e06c75"},
    {"key": "poi_life",    "label": "생활·수리",   "cats": ["생활", "수리·개인", "시설관리·임대"], "default": "#4db6ac"},
    {"key": "poi_culture", "label": "문화·스포츠", "cats": ["문화", "예술·스포츠"],                "default": "#b18bd0"},
    {"key": "poi_edu",     "label": "교육",        "cats": ["교육"],                               "default": "#81c784"},
    {"key": "poi_stay",    "label": "숙박",        "cats": ["숙박"],                               "default": "#e0a3c8"},
    {"key": "poi_etc",     "label": "기타",        "cats": [],                                     "default": "#9aa6b2"},
]
POI_LAYER = "poi-dot"


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


def build_poi_match(colors):
    """그룹색({poi_food:#hex,...}) → poi-dot circle-color 의 cat1 match 식 재구성."""
    expr = ["match", ["get", "cat1"]]; fallback = "#9aa6b2"
    for g in POI_GROUPS:
        c = colors.get(g["key"]) or g["default"]
        if not g["cats"]:
            fallback = c; continue
        expr += [list(g["cats"]), c]
    expr.append(fallback)
    return expr


def current_poi_colors(style):
    """poi-dot match 식에서 그룹별 현재 색을 #hex 로 추출(없으면 default)."""
    L = _index(style).get(POI_LAYER, {})
    expr = L.get("paint", {}).get("circle-color")
    out = {g["key"]: g["default"] for g in POI_GROUPS}
    if isinstance(expr, list) and expr and expr[0] == "match" and len(expr) >= 4:
        body, fallback = expr[2:-1], expr[-1]
        pairs = list(zip(body[0::2], body[1::2]))
        for g in POI_GROUPS:
            if not g["cats"]:
                out[g["key"]] = to_hex(fallback) or g["default"]; continue
            for labels, color in pairs:
                labs = labels if isinstance(labels, list) else [labels]
                if set(labs) == set(g["cats"]):
                    out[g["key"]] = to_hex(color) or g["default"]; break
    return out


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
    # POI 업종 그룹색 → poi-dot match 식 재구성(현재값 위에 덮어써 누락 그룹 보존)
    poi_keys = {g["key"] for g in POI_GROUPS}
    poi_theme = {k: v for k, v in theme.items() if k in poi_keys and v}
    if poi_theme and POI_LAYER in idx:
        cur = current_poi_colors(style); cur.update(poi_theme)
        idx[POI_LAYER].setdefault("paint", {})["circle-color"] = build_poi_match(cur)
        n += len(poi_theme)
    # 글꼴(layout text-font)
    n += apply_fonts(style, theme.get("fonts") or {})
    # 노출 줌(레이어 minzoom/maxzoom)
    n += apply_zoom(style, theme.get("zoom") or {})
    return n


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
    """각 객체의 현재 색(첫 대상 레이어 기준)을 #hex 로. 식이면 None."""
    idx = _index(style); out = {}
    for o in OBJECTS:
        layer_id, prop = o["targets"][0]
        L = idx.get(layer_id, {})
        out[o["key"]] = to_hex(L.get("paint", {}).get(prop))
    return out
