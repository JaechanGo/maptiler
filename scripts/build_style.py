#!/usr/bin/env python3
"""style/base.json + style/layers/*.json 조각 → style/style.json 조립.

조각 형식: {"sources": {...}, "layers": [...], "set": {최상위키: 값}}
- sources는 병합, layers는 뒤에 추가(= 위에 그려짐), set은 최상위 키 설정.
조각 파일은 파일명 순으로 적용된다.
"""
import json
import pathlib
import sys

root = pathlib.Path(__file__).resolve().parents[1] / "style"


def load_json(path: pathlib.Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        sys.exit(f"JSON 오류 ({path.name}): {e}")


style = load_json(root / "base.json")
for frag_path in sorted((root / "layers").glob("*.json")):
    frag = load_json(frag_path)
    style.setdefault("sources", {}).update(frag.get("sources", {}))
    style["layers"].extend(frag.get("layers", []))
    for key, value in frag.get("set", {}).items():
        style[key] = value
    print(f"적용: {frag_path.name}")

seen = set()
for layer in style["layers"]:
    lid = layer.get("id")
    if lid in seen:
        sys.exit(f"중복 layer id: {lid!r} (조각 충돌 — 같은 id가 두 번 정의됨)")
    seen.add(lid)

out = root / "style.json"
out.write_text(json.dumps(style, ensure_ascii=False, indent=2), encoding="utf-8")
print(f"OK: {out} (layers={len(style['layers'])})")
