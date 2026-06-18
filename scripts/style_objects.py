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


def apply_theme(style, theme):
    """theme({key:#hex}) 를 style 의 해당 레이어 paint 속성에 적용. 반환: 적용된 (layer,prop) 수."""
    idx = _index(style); n = 0
    objs = {o["key"]: o for o in OBJECTS}
    for key, color in (theme or {}).items():
        o = objs.get(key)
        if not o or not color:
            continue
        for layer_id, prop in o["targets"]:
            L = idx.get(layer_id)
            if L is None:
                continue
            L.setdefault("paint", {})[prop] = color; n += 1
    return n


def current_colors(style):
    """각 객체의 현재 색(첫 대상 레이어 기준)을 #hex 로. 식이면 None."""
    idx = _index(style); out = {}
    for o in OBJECTS:
        layer_id, prop = o["targets"][0]
        L = idx.get(layer_id, {})
        out[o["key"]] = to_hex(L.get("paint", {}).get(prop))
    return out
