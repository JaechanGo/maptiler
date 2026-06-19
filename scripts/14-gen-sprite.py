#!/usr/bin/env python3
"""style/icons/*.png → MapLibre 스프라이트 4종(sprite{,@2x}.{png,json}) 생성.

카테고리 아이콘은 style/icons/<slug>.png 한 장씩(투명 PNG). 사용자가 Style Studio에서
이미지를 올리면 같은 경로를 덮어쓴다. build-style.sh 가 스타일 재생성 때 이 스크립트를
호출해 스프라이트를 다시 굽고, tileserver 가 /styles/cuvia/sprite{,@2x}.{json,png} 로 서빙한다.

- 입력 폴더가 비어도 빈 스프라이트(투명 1x1 시트 + {})를 만든다 → 스타일의 "sprite" 참조가
  404로 깨지는 것을 막는다(tileserver 는 4종이 모두 있어야 안전).
- @2x 는 동일 소스를 2배 셀로 packing(소스가 고해상도면 선명, 저해상도면 업스케일).

의존성: Pillow. 오프라인 빌드호스트는 scripts/setup-build-host.sh 에서 설치.
"""
import json, math, sys, pathlib
from PIL import Image

ROOT = pathlib.Path(__file__).resolve().parents[1]
ICON_DIR = ROOT / "style" / "icons"
OUT_DIR = ROOT / "style"
CELL = 24   # 1x 논리 아이콘 한 변(px). @2x 는 2배(48).
PAD = 2     # 셀 간 여백(1x 기준) — 인접 아이콘 번짐 방지.


def load_icons():
    """style/icons/*.png → [(slug, RGBA Image)] (이름순)."""
    if not ICON_DIR.is_dir():
        return []
    out = []
    for p in sorted(ICON_DIR.glob("*.png")):
        try:
            out.append((p.stem, Image.open(p).convert("RGBA")))
        except Exception as e:
            print(f"  (건너뜀) {p.name}: {e}", file=sys.stderr)
    return out


def build_sheet(icons, scale):
    """icons → (sheet RGBA Image, index dict). scale=1 또는 2."""
    cell, pad = CELL * scale, PAD * scale
    n = len(icons)
    cols = max(1, math.ceil(math.sqrt(n))) if n else 1
    rows = max(1, math.ceil(n / cols)) if n else 1
    W, H = cols * (cell + pad) + pad, rows * (cell + pad) + pad
    sheet = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    index = {}
    for i, (name, im) in enumerate(icons):
        r, c = divmod(i, cols)
        x, y = pad + c * (cell + pad), pad + r * (cell + pad)
        ic = im if im.size == (cell, cell) else im.resize((cell, cell), Image.LANCZOS)
        sheet.paste(ic, (x, y), ic)
        index[name] = {"x": x, "y": y, "width": cell, "height": cell, "pixelRatio": scale}
    return sheet, index


def write_sprite(icons, scale, suffix):
    sheet, index = build_sheet(icons, scale)
    sheet.save(OUT_DIR / f"sprite{suffix}.png")
    (OUT_DIR / f"sprite{suffix}.json").write_text(
        json.dumps(index, ensure_ascii=False), encoding="utf-8")


def main():
    icons = load_icons()
    write_sprite(icons, 1, "")
    write_sprite(icons, 2, "@2x")
    print(f"OK: sprite 4종 생성 — 아이콘 {len(icons)}개 ← {ICON_DIR}")


if __name__ == "__main__":
    main()
