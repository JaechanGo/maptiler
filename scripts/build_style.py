#!/usr/bin/env python3
"""style/base.json + style/layers/*.json 조각 → style/style.json 조립.

조각 형식: {"sources": {...}, "layers": [...], "set": {최상위키: 값}}
- sources는 병합, layers는 뒤에 추가(= 위에 그려짐), set은 최상위 키 설정.
조각 파일은 파일명 순으로 적용된다.

STYLE_IMPORT 환경변수가 가리키는 style.json 이 있으면 조립을 건너뛰고 그 파일을 그대로
최종 스타일로 사용한다(Style Studio '내보내기' 산출물 = 완성형 style.json 을 import).
없으면 base+layers+theme 로 기본 스타일을 자동 조립한다.
"""
import json
import os
import pathlib
import sys

root = pathlib.Path(__file__).resolve().parents[1] / "style"


def load_json(path: pathlib.Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        sys.exit(f"JSON 오류 ({path.name}): {e}")


out = root / "style.json"
imported = os.environ.get("STYLE_IMPORT")
if imported and pathlib.Path(imported).is_file():
    # 가져온 스타일(완성형 style.json) 그대로 사용 — 조립/테마 적용 생략.
    style = load_json(pathlib.Path(imported))
    if not isinstance(style.get("layers"), list) or not style["layers"]:
        sys.exit(f"가져온 스타일 형식 오류 ({imported}): layers 배열이 없습니다")
    out.write_text(json.dumps(style, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"OK: {out} (가져온 스타일 사용 ← {imported} · layers={len(style['layers'])})")
else:
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

    # 객체별 색 테마(빌드 스튜디오 색 편집기가 저장) 적용 — 조각 병합 후 최종 paint 덮어쓰기
    theme_path = root / "theme.json"
    if theme_path.exists():
        import style_objects
        n = style_objects.apply_theme(style, load_json(theme_path))
        print(f"테마 적용: {theme_path.name} → {n}개 paint 속성")

    out.write_text(json.dumps(style, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"OK: {out} (layers={len(style['layers'])})")

# 카테고리 아이콘 스프라이트 재생성(style/icons/*.png → sprite{,@2x}.{png,json}).
# Pillow 필요 — 없으면 경고만 남기고 계속(스타일 자체는 이미 기록됨).
import subprocess
try:
    r = subprocess.run([sys.executable, str(pathlib.Path(__file__).resolve().parent / "14-gen-sprite.py")],
                       capture_output=True, text=True, timeout=120)
    print((r.stdout or r.stderr).strip())
except Exception as e:
    print(f"(스프라이트 생성 건너뜀: {e})")
