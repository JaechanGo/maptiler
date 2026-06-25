#!/usr/bin/env python3
"""CUVIA Style Studio — 경량 스타일 디자이너(배포 가능, 무의존 stdlib).

빌드 파이프라인 없이 '스타일 디자인'만 담당한다. 운영(지도) 스택 옆에 함께 배포해
배포된 지도의 색·POI·글꼴 테마를 현장에서 지정한다(저장 → build_style → tileserver reload).
원천데이터/tippecanoe 불필요. style_objects + build_style.py + style/ 조각 + glyphs 만 있으면 동작.

기동:  python3 scripts/style-studio.py                      # http://localhost:8091
       HOST=0.0.0.0 STUDIO_TOKEN=secret PORT=8091 \
       COMPOSE_FILE=/path/docker-compose.yml python3 scripts/style-studio.py
- HOST 기본 127.0.0.1(로컬 전용). LAN 노출 시 HOST=0.0.0.0 + STUDIO_TOKEN 설정 권장.
- STUDIO_TOKEN 설정 시 변경 API(POST)는 X-Studio-Token 헤더 필요. 페이지는 ?token=… 로 받음.
"""
import json, os, pathlib, re, subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import style_objects

ROOT = pathlib.Path(__file__).resolve().parents[1]
BUILD_HOME = pathlib.Path(os.environ.get("BUILD_HOME", os.path.expanduser("~/geocode-build")))
HOST = os.environ.get("HOST", "0.0.0.0")   # 기본 외부(LAN) 노출. 로컬 전용은 HOST=127.0.0.1 (LAN 노출 시 STUDIO_TOKEN 권장)
PORT = int(os.environ.get("PORT", "8091"))
TILE_PORT = int(os.environ.get("TILE_PORT", "8080"))            # 미리보기·재시작 대상 tileserver 포트
COMPOSE_FILE = os.environ.get("COMPOSE_FILE", str(BUILD_HOME / "deploy/docker-compose.yml"))
STUDIO_TOKEN = os.environ.get("STUDIO_TOKEN", "")               # 설정 시 POST에 X-Studio-Token 요구
MAX_CTRL = 256 * 1024
MAX_STYLE = 4 * 1024 * 1024
MAX_ICON = 4 * 1024 * 1024          # 카테고리 아이콘 이미지 업로드 상한
HEXRE = re.compile(r"^#[0-9a-fA-F]{6}$")
VALID_KEYS = {o["key"] for o in style_objects.OBJECTS}


def apply_style_theme(theme, commit_pending=True):
    # 유효 키 + #rrggbb + 설치된 글꼴만 통과(주입 방지) → style/theme.json → build_style → tileserver 재시작
    theme = theme or {}
    # 기존 theme.json 위에 병합 — 부분 저장(색만)이 fonts/zoom 을 지우지 않게
    try:
        clean = json.loads((ROOT / "style" / "theme.json").read_text(encoding="utf-8"))
        if not isinstance(clean, dict): clean = {}
    except Exception:
        clean = {}
    for k, v in theme.items():
        if k in VALID_KEYS and isinstance(v, str) and HEXRE.match(v):
            clean[k] = v
    fonts = theme.get("fonts")
    if isinstance(fonts, dict):
        avail = set(style_objects.available_fonts(str(ROOT / "style/glyphs")))
        cf = {k: v for k, v in fonts.items()
              if (k == "all" or k in style_objects.FONT_LABELS) and isinstance(v, str) and v in avail}
        if cf:
            clean["fonts"] = cf
    zc = style_objects.sanitize_zoom(theme.get("zoom"))   # 노출 줌(레이어 minzoom/maxzoom)
    if zc:
        clean["zoom"] = zc
    oc = style_objects.sanitize_opacity(theme.get("opacity"))   # 투명도(opacity)
    if oc:
        clean["opacity"] = oc
    ic = style_objects.sanitize_icons(theme.get("icons"))   # 카테고리 아이콘(대분류 slug)
    if ic:
        clean["icons"] = {**(clean.get("icons") or {}), **ic}
    iov = style_objects.sanitize_icon_overrides(theme.get("icon_overrides"))   # 중/소 아이콘 오버라이드
    if iov:
        cov = clean.get("icon_overrides") or {}
        for lvl in ("cat2", "cat"):
            if iov.get(lvl):
                cov[lvl] = {**(cov.get(lvl) or {}), **iov[lvl]}
        clean["icon_overrides"] = cov
    sc = style_objects.sanitize_sources(theme.get("sources"))   # 데이터 출처 토글
    if sc:
        clean["sources"] = {**(clean.get("sources") or {}), **sc}
    vis = style_objects.sanitize_visibility(theme.get("visibility"))   # 객체 표시 on/off
    if vis:
        clean["visibility"] = {**(clean.get("visibility") or {}), **vis}
    if theme.get("language") in ("ko", "en", "local"):   # 표시 언어
        clean["language"] = theme["language"]
    szc = style_objects.sanitize_sizes(theme.get("sizes"))   # 크기(text-size/circle-radius/line-width)
    if szc:
        clean["sizes"] = {**(clean.get("sizes") or {}), **szc}
    ex = style_objects.sanitize_extras(theme.get("extras"))   # 고급 paint(halo/stroke/outline)
    if ex:
        clean["extras"] = {**(clean.get("extras") or {}), **ex}
    gr = style_objects.sanitize_gradient(theme.get("gradient"))   # 속성별 색 그라데이션
    if gr:
        clean["gradient"] = {**(clean.get("gradient") or {}), **gr}
    pl = style_objects.sanitize_placement(theme.get("placement"))   # 라벨 배치(offset·anchor·생략)
    if pl:
        clean["placement"] = {**(clean.get("placement") or {}), **pl}
    # 건물 2D/3D pitch 전환 임계값(숫자 0~85 또는 None/삭제)
    bpv = theme.get("building_pitch_3d")
    if isinstance(bpv, (int, float)) and not isinstance(bpv, bool) and 0 <= bpv <= 85:
        clean["building_pitch_3d"] = round(float(bpv), 1)
    elif "building_pitch_3d" in theme and (bpv is None or bpv == ""):
        clean.pop("building_pitch_3d", None)
    if isinstance(theme.get("poi_colors"), dict):   # 시설 그룹별 글자색 — 전체 맵 교체(빈 {}=전부 해제→평면 복원)
        clean["poi_colors"] = style_objects.sanitize_poi_colors(theme.get("poi_colors"))
    pt = style_objects.sanitize_poi_tiers(theme.get("poi_tiers"))   # POI 노출 티어(줌)
    if pt:
        clean["poi_tiers"] = pt
    ctt = style_objects.sanitize_cat_tiers(theme.get("cat_tiers"))   # 카테고리→티어
    if ctt:
        base = clean.get("cat_tiers") or {}
        for lvl in ("cat1", "cat2", "cat"):
            if ctt.get(lvl):
                base[lvl] = {**(base.get(lvl) or {}), **ctt[lvl]}
        clean["cat_tiers"] = base
    pend = ROOT / "style" / "icons" / ".pending"   # 스테이징된 아이콘 업로드를 이제 커밋(저장 시점)
    if commit_pending and pend.is_dir():           # import 경로는 아이콘 스테이징과 무관 → 커밋 안 함
        for f in pend.glob("*.png"):
            try:
                f.replace(ROOT / "style" / "icons" / f.name)
            except Exception:
                pass
    (ROOT / "style").mkdir(exist_ok=True)
    theme_file = ROOT / "style" / "theme.json"
    prev_theme = theme_file.read_bytes() if theme_file.is_file() else None   # 빌드 실패 시 롤백용
    theme_file.write_text(json.dumps(clean, ensure_ascii=False, indent=2), encoding="utf-8")
    log = []
    try:
        r = subprocess.run(["python3", str(ROOT / "scripts/build_style.py")],
                           capture_output=True, text=True, cwd=str(ROOT), timeout=60)
        log.append(r.stdout.strip() or r.stderr.strip())
        if r.returncode != 0:
            if prev_theme is not None: theme_file.write_bytes(prev_theme)   # theme.json 원복(부분상태 방지)
            return {"ok": False, "error": "build_style 실패", "log": log}
    except Exception as e:
        if prev_theme is not None: theme_file.write_bytes(prev_theme)        # theme.json 원복
        return {"ok": False, "error": f"build_style 오류: {e}", "log": log}
    # tileserver는 스타일을 시작 시 캐시 → 서빙본 갱신엔 재시작 필요. detached로 띄우고 즉시 반환.
    reloaded = "스킵(compose 없음)"
    if os.path.exists(COMPOSE_FILE):
        try:
            subprocess.Popen(["docker", "compose", "-f", COMPOSE_FILE, "restart", "tileserver"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
            reloaded = "재시작 요청됨(수십초 소요)"
        except Exception as e:
            reloaded = f"오류: {e}"
    return {"ok": True, "applied": len(clean), "reloaded": reloaded, "log": log}


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    def log_message(self, *a): pass

    def _json(self, obj, code=200):
        b = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code); self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)

    def _send(self, body, ctype, code=200):
        b = body if isinstance(body, bytes) else body.encode()
        self.send_response(code); self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)

    def _static(self, rel):
        base = ROOT.resolve(); p = (base / rel).resolve()
        if base not in p.parents or not p.is_file():   # 경로조작·접두사형제 우회 차단
            return self.send_error(404)
        ext = p.suffix.lower()
        ctype = {".js": "application/javascript", ".css": "text/css", ".png": "image/png",
                 ".json": "application/json", ".svg": "image/svg+xml"}.get(ext, "application/octet-stream")
        self._send(p.read_bytes(), ctype + ("; charset=utf-8" if ext in (".js", ".css", ".json", ".svg") else ""))

    def _authed(self):
        return not STUDIO_TOKEN or self.headers.get("X-Studio-Token", "") == STUDIO_TOKEN

    def do_GET(self):
        if self.path == "/" or self.path.startswith("/?"):
            return self._send(STYLE_PAGE, "text/html; charset=utf-8")
        if self.path.startswith("/vendor/"):
            return self._static(self.path.lstrip("/").split("?")[0])
        if self.path.startswith("/style/icons/"):    # 카테고리 아이콘 썸네일
            return self._static(self.path.lstrip("/").split("?")[0])
        if self.path == "/api/style/objects":
            try:
                style = json.loads((ROOT / "style/style.json").read_text(encoding="utf-8"))
                cur = style_objects.current_colors(style)
                fonts_cur = style_objects.current_fonts(style); zoom_cur = style_objects.current_zoom(style)
                opacity_cur = style_objects.current_opacity(style)
                icons_cur = style_objects.current_icons(style); sources_cur = style_objects.current_sources(style)
                vis_cur = style_objects.current_visibility(style); lang_cur = style_objects.current_language(style)
                sizes_cur = style_objects.current_sizes(style); extras_cur = style_objects.current_extras(style)
                iov_cur = style_objects.current_icon_overrides(style); grad_cur = style_objects.current_gradient(style)
                poicol_cur = style_objects.current_poi_colors(style)
                place_cur = style_objects.current_placement(style)
            except Exception:
                cur = {}; fonts_cur = {}; zoom_cur = {}; opacity_cur = {}; icons_cur = {}; sources_cur = {}
                vis_cur = {}; lang_cur = "ko"; sizes_cur = {}; extras_cur = {}; iov_cur = {}; grad_cur = {}; poicol_cur = {}; place_cur = {}
            objs = [{"key": o["key"], "label": o["label"], "targets": o["targets"],
                     "color": cur.get(o["key"]) or "#888888", "zoom": zoom_cur.get(o["key"]) or {},
                     "visible": bool(vis_cur.get(o["key"], True))}
                    for o in style_objects.OBJECTS]
            labels = {o["key"]: o["label"] for o in style_objects.OBJECTS}
            fonts = {"available": style_objects.available_fonts(str(ROOT / "style/glyphs")),
                     "current": fonts_cur,
                     "labels": [{"key": k, "label": labels.get(k, k)} for k in style_objects.FONT_LABELS]}
            opa = [{"key": t["key"], "label": t["label"], "targets": t["targets"],
                    "value": opacity_cur.get(t["key"], 1.0)} for t in style_objects.opacity_objects()]
            icon_groups = [{"key": g["key"], "label": g["label"], "cats": g["cats"],
                            "icon": icons_cur.get(g["key"]) or g["icon"]} for g in style_objects.ICON_GROUPS]
            try:
                taxonomy = json.loads((ROOT / "style/poi-taxonomy.json").read_text(encoding="utf-8"))
            except Exception:
                taxonomy = {}
            try:
                _thm = json.loads((ROOT / "style/theme.json").read_text(encoding="utf-8"))
            except Exception:
                _thm = {}
            # 건물 pitch 임계값 — style.metadata 우선, theme.json fallback
            _bpv = style.get("metadata", {}).get("cuvia:building_pitch_3d")
            if _bpv is None:
                _bpv = _thm.get("building_pitch_3d")
            building_pitch_3d_cur = _bpv if isinstance(_bpv, (int, float)) and not isinstance(_bpv, bool) else None
            poi_tiers = style_objects.sanitize_poi_tiers(_thm.get("poi_tiers")) or style_objects.POI_TIERS_DEFAULT
            cat_tiers = style_objects.sanitize_cat_tiers(_thm.get("cat_tiers")) or style_objects.CAT_TIER_DEFAULT
            source_groups = [{"key": g["key"], "label": g["label"], "kind": g["kind"],
                              "src": g.get("src"), "source": g.get("source"),
                              "on": bool(sources_cur.get(g["key"], True))} for g in style_objects.SOURCE_GROUPS]
            return self._json({"objects": objs, "fonts": fonts,
                               "opacity_objects": opa, "icon_groups": icon_groups, "source_groups": source_groups,
                               "presets": style_objects.PRESETS,
                               "language": lang_cur,
                               "poi_colors": poicol_cur,
                               "size_objects": [{"key": t["key"], "label": t["label"], "layer": t["layer"], "prop": t["prop"],
                                                 "min": (sizes_cur.get(t["key"]) or {}).get("min"),
                                                 "max": (sizes_cur.get(t["key"]) or {}).get("max")}
                                                for t in style_objects.size_targets()],
                               "extra_objects": [{"key": t["key"], "label": t["label"], "type": t["type"],
                                                  "layer": t["layer"], "prop": t["prop"],
                                                  "value": extras_cur.get(t["key"])} for t in style_objects.extra_targets()],
                               "placement_objects": [{"key": t["key"], "label": t["label"], "type": t["type"],
                                                      "layer": t["layer"], "prop": t["prop"],
                                                      "anchors": (style_objects.LABEL_ANCHORS if t["type"] == "anchor" else None),
                                                      "default": t.get("default"),
                                                      "value": place_cur.get(t["key"])} for t in style_objects.placement_targets()],
                               "taxonomy": taxonomy, "icon_overrides": iov_cur,
                               "poi_tiers": poi_tiers, "cat_tiers": cat_tiers,
                               "gradient_objects": [{"key": g["key"], "label": g["label"], **(grad_cur.get(g["key"]) or {})}
                                                    for g in style_objects.gradient_targets()],
                               "building_pitch_3d": building_pitch_3d_cur,
                               "tile_port": TILE_PORT, "auth": bool(STUDIO_TOKEN)})
        if self.path == "/api/style/export":
            p = ROOT / "style/style.json"
            if not p.is_file():
                return self.send_error(404)
            b = p.read_bytes()
            self.send_response(200); self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Disposition", 'attachment; filename="style.json"')
            self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
            return
        self.send_error(404)

    def do_POST(self):
        if not self._authed():
            return self._json({"error": "인증 필요(X-Studio-Token)"}, 401)
        if self.path == "/api/style":
            n = int(self.headers.get("Content-Length", "0"))
            if n > MAX_CTRL: return self._json({"error": "본문 과대"}, 413)
            body = json.loads(self.rfile.read(n) or "{}")
            return self._json(apply_style_theme(body.get("theme", {})))
        if self.path == "/api/style/import":
            n = int(self.headers.get("Content-Length", "0"))
            if n > MAX_STYLE: return self._json({"error": "style.json 과대(4MB 초과)"}, 413)
            try:
                imported = json.loads(self.rfile.read(n) or "{}")
            except Exception as e:
                return self._json({"error": f"style.json 파싱 실패: {e}"}, 400)
            if not isinstance(imported.get("layers"), list) or not imported["layers"]:
                return self._json({"error": "style.json 형식 아님(layers 배열 없음)"}, 400)
            # 가져온 스타일에서 '무손실 추출 가능한' 시각 테마(색·시설그룹색·크기)만 뽑아 theme 에 병합.
            # apply_style_theme 가 기존 theme.json 에 '병합'하므로 글꼴·투명도·줌·그라데이션·티어 등
            # 나머지 설정은 서버의 현재 값이 그대로 유지된다(가져오기가 그것들을 지우지 않음).
            # 색/그라데이션 같은 파생값은 추출→재적용이 멱등이 아니라 의도치 않게 평탄화되므로 확장하지 않음.
            colors = style_objects.current_colors(imported)
            patch = {k: v for k, v in colors.items() if v}
            poi_colors = style_objects.current_poi_colors(imported)
            if poi_colors:                                   # 시설 그룹별 글자색(비면 생략 — 평탄화/덮어쓰기 방지)
                patch["poi_colors"] = poi_colors
            patch["sizes"] = {k: mm for k, mm in style_objects.current_sizes(imported).items()
                              if isinstance(mm, dict) and mm.get("min") is not None and mm.get("max") is not None}
            patch["placement"] = {k: v for k, v in style_objects.current_placement(imported).items()
                                  if v is not None}   # 배치(offset·anchor·생략) — 정확값이라 멱등, 라운드트립 안전
            res = apply_style_theme(patch, commit_pending=False)   # 병합+빌드+재시작 / 아이콘 스테이징은 미커밋
            res["colors"] = colors
            res["imported_layers"] = len(imported["layers"])
            return self._json(res)
        if self.path.split("?")[0] == "/api/icon":
            from urllib.parse import urlparse, parse_qs
            q = parse_qs(urlparse(self.path).query)
            level = (q.get("level") or [""])[0]; name = (q.get("name") or [""])[0]
            key = (q.get("group") or [""])[0]
            if level in ("cat2", "cat") and name:          # 중/소 오버라이드 — 이름→ASCII slug
                import zlib
                slug = "ov" + format(zlib.crc32(name.encode("utf-8")) & 0xffffffff, "x")
            else:
                grp = {g["key"]: g for g in style_objects.ICON_GROUPS}.get(key)
                if not grp:
                    return self._json({"error": "알 수 없는 카테고리 키/레벨"}, 400)
                slug = grp["icon"]
            n = int(self.headers.get("Content-Length", "0"))
            if n <= 0 or n > MAX_ICON:
                return self._json({"error": f"이미지 크기 오류(0 초과 {MAX_ICON // 1024 // 1024}MB 이하)"}, 413)
            data = self.rfile.read(n)
            dest = ROOT / "style" / "icons" / ".pending" / f"{slug}.png"   # 스테이징 — [저장 & 적용] 때 커밋
            dest.parent.mkdir(parents=True, exist_ok=True)
            try:                                  # png/jpg/webp → 투명 RGBA PNG 정규화(Pillow)
                import io
                from PIL import Image
                Image.open(io.BytesIO(data)).convert("RGBA").save(dest)
            except ImportError:                   # Pillow 없으면 PNG 원본만 허용
                if data[:8] != b"\x89PNG\r\n\x1a\n":
                    return self._json({"error": "Pillow 미설치 — PNG 파일만 업로드 가능"}, 400)
                dest.write_bytes(data)
            except Exception as e:
                return self._json({"error": f"이미지 파일 아님: {e}"}, 400)
            # 스프라이트 재생성·tileserver 반영은 [저장 & 적용](apply_style_theme)에서 — 여기선 스테이징만.
            return self._json({"ok": True, "group": key, "level": level, "name": name, "icon": slug,
                               "staged": True, "preview": f"/style/icons/.pending/{slug}.png"})
        if self.path == "/api/icon/discard":      # 되돌리기 — 스테이징 폐기
            pend = ROOT / "style" / "icons" / ".pending"; cnt = 0
            if pend.is_dir():
                for f in pend.glob("*.png"):
                    try:
                        f.unlink(); cnt += 1
                    except Exception:
                        pass
            return self._json({"ok": True, "discarded": cnt})
        self.send_error(404)


STYLE_PAGE = r"""<!doctype html><html lang=ko><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>CUVIA Style Studio</title>
<link rel=stylesheet href="/vendor/maplibre/maplibre-gl.css">
<style>
 body{margin:0;background:#0f1420;color:#e8edf5;font:14px/1.5 -apple-system,system-ui,'Apple SD Gothic Neo',sans-serif;height:100vh;overflow:hidden}
 .top{display:flex;align-items:center;gap:12px;padding:11px 18px;border-bottom:1px solid #26304a}
 .brand{display:flex;align-items:center;gap:9px;font-size:15px;font-weight:600}
 .dot{width:9px;height:9px;border-radius:99px;background:#5b9bd5}
 .main{display:flex;height:calc(100vh - 50px)}
 .side{width:310px;border-right:1px solid #26304a;overflow:auto;padding:12px 16px;flex:none}
 #map{flex:1;height:100%}
 .row{display:flex;align-items:center;gap:9px;padding:7px 0;border-bottom:1px solid #1a2233}
 .row label{flex:1;font-size:13px}
 input[type=color]{width:36px;height:28px;border:1px solid #26304a;border-radius:6px;background:none;padding:0;cursor:pointer}
 input[type=text]{width:82px;background:#0a0e16;border:1px solid #26304a;color:#e8edf5;border-radius:6px;padding:5px 7px;font:12px ui-monospace,Menlo,monospace}
 input.zn{width:42px;background:#0a0e16;border:1px solid #26304a;color:#e8edf5;border-radius:6px;padding:5px 6px;font:12px ui-monospace,Menlo,monospace;text-align:center}
 .oprow label{flex:0 0 92px}
 .oprow input[type=range]{flex:1;accent-color:#5b9bd5;margin:0}
 .opv{font:12px ui-monospace,Menlo,monospace;color:#8d9bb5;width:40px;text-align:right;flex:none}
 button{background:#5b9bd5;color:#06121f;border:0;border-radius:8px;padding:8px 15px;font-weight:600;cursor:pointer}
 button.g{background:transparent;color:#e8edf5;border:1px solid #26304a}
 .st{font-size:12px;color:#8d9bb5}
 .hint{font-size:11px;color:#5f6b80;margin-top:14px;line-height:1.6}
 .tb{display:flex;flex-direction:column;gap:7px;margin-bottom:12px;padding-bottom:12px;border-bottom:1px solid #26304a}
 .tbrow{display:flex;align-items:center;gap:6px} .tbrow>span{font-size:11px;color:#8d9bb5;width:40px;flex:none}
 .mini{background:#1a2233;color:#cfe0ff;border:1px solid #26304a;border-radius:6px;padding:6px 9px;font-size:12px;font-weight:500;cursor:pointer;flex:1}
 .mini:hover{border-color:#5b9bd5}
 h2{font-size:11px;color:#8d9bb5;margin:14px 0 5px;font-weight:600;letter-spacing:.04em}
 .tabs{display:flex;gap:4px;margin:2px 0 8px;flex-wrap:wrap}
 .tab{background:#1a2233;color:#8d9bb5;border:1px solid #26304a;border-radius:7px;padding:6px 10px;font-size:12px;font-weight:600;cursor:pointer;flex:none}
 .tab.active{background:#5b9bd5;color:#06121f;border-color:#5b9bd5}
 .pane[hidden]{display:none}
 .icrow{display:flex;align-items:center;gap:10px;padding:7px 0;border-bottom:1px solid #1a2233}
 .icrow label{flex:1;font-size:13px}
 .icrow img{width:30px;height:30px;border-radius:7px;background:#0a0e16;border:1px solid #26304a;object-fit:contain;flex:none}
 .icrow .up{background:#1a2233;color:#cfe0ff;border:1px solid #26304a;border-radius:6px;padding:5px 10px;font-size:12px;cursor:pointer;flex:none}
 .icrow .up:hover{border-color:#5b9bd5}
 .srow{display:flex;align-items:center;gap:9px;padding:8px 0;border-bottom:1px solid #1a2233}
 .srow label{flex:1;font-size:13px;cursor:pointer} .srow input[type=checkbox]{width:17px;height:17px;accent-color:#5b9bd5;cursor:pointer;flex:none}
 select{background:#0a0e16;border:1px solid #26304a;color:#e8edf5;border-radius:6px;padding:5px 8px;font-size:12px;cursor:pointer}
 details.tx1{border-bottom:1px solid #1a2233} details.tx1>summary{padding:7px 2px;cursor:pointer;font-size:13px;font-weight:600;color:#cfe0ff}
 .txr{display:flex;align-items:center;gap:8px;padding:5px 0 5px 14px;font-size:12px}
 .txr .nm{flex:1;color:#bcc8de} .txr img.ic{width:22px;height:22px;border-radius:5px;border:1px solid #26304a;background:#0a0e16;object-fit:contain}
 .txr .noic{color:#5f6b80;width:22px;text-align:center} .txr .exp,.txr .tup{background:#1a2233;border:1px solid #26304a;color:#8d9bb5;border-radius:5px;padding:3px 8px;font-size:11px;cursor:pointer}
 .txr .tup{color:#cfe0ff} .subwrap{padding-left:14px}
</style>
<div class=top>
 <div class=brand><span class=dot></span>CUVIA Style Studio</div>
 <button class=g id=reset style="margin-left:auto">되돌리기</button>
 <button id=save>저장 & 적용</button>
 <span class=st id=status></span>
</div>
<div class=main>
 <div class=side>
   <div class=tb>
     <div class=tbrow><span>프리셋</span><button class=mini id=pl>화이트</button><button class=mini id=pd>다크</button></div>
     <div class=tbrow><span>파일</span><button class=mini id=exp>내보내기</button><button class=mini id=imp>가져오기</button></div>
   </div>
   <input type=file id=impf accept=".json,application/json" style="display:none">
   <div class=tabs>
     <button class="tab active" data-t=color>색상</button>
     <button class=tab data-t=icon>아이콘</button>
     <button class=tab data-t=data>데이터</button>
     <button class=tab data-t=zoom>줌·투명도</button>
     <button class=tab data-t=adv>고급</button>
   </div>
   <div class=pane data-p=color>
     <h2>객체</h2><div id=rows></div>
     <h2>시설 라벨 — 카테고리 그룹별 글자색</h2><div id=poicolrows></div>
     <p class=hint>그룹별로 시설(POI) 라벨 글자색을 다르게 지정합니다(아이콘과 동일한 10개 대분류 그룹).
     <br>체크 해제한 그룹은 위 ‘시설 라벨’ 기본색을 따릅니다. 지명·도로·동 라벨 색은 위 ‘객체’에서 종류별로 조정합니다.</p>
   </div>
   <div class=pane data-p=icon hidden>
     <h2>카테고리 아이콘 — 이미지 업로드(투명 PNG 권장)</h2><div id=iconrows></div>
     <p class=hint>이미지를 올리면 해당 업종 대분류 아이콘이 교체됩니다(png·jpg·webp → 투명 PNG로 정규화).
     <br><b>‘저장 & 적용’ 후</b> 지도에 반영됩니다(스프라이트 재생성 + 타일서버 재시작).</p>
     <h2>세부 분류 아이콘 (대 펼쳐 중·소 오버라이드)</h2><div id=icontree></div>
     <p class=hint>우선순위: 소분류 &gt; 중분류 &gt; 대분류. 지정한 세부분류만 별도 아이콘, 나머지는 대분류 아이콘.</p>
   </div>
   <div class=pane data-p=data hidden>
     <h2>표시 언어 (라벨)</h2>
     <div class=srow><label for=langsel>라벨 언어</label>
       <select id=langsel><option value=ko>한국어</option><option value=en>English</option><option value=local>원어(name)</option></select></div>
     <h2>지도에 표시할 데이터(출처)</h2><div id=srcrows></div>
     <p class=hint>체크 해제 시 해당 데이터가 지도에서 숨겨집니다. 상가·인허가는 POI 출처별 토글이며,
     둘 다 켜면 같은 점포는 대표 1건만 표시됩니다(중복 숨김).</p>
     <h2>객체 표시 on/off</h2><div id=visrows></div>
   </div>
   <div class=pane data-p=zoom hidden>
     <h2>노출 레벨(줌) · min–max <span id=zlevel style="color:#5b9bd5;font-weight:500;margin-left:4px">현재 z 14.5</span></h2><div id=zoomrows></div>
     <h2>크기 · 줌 min~max 값 (글자·선·점)</h2><div id=sizerows></div>
     <h2>라벨 배치 — 세로 위치 · 기준점 · 충돌 시 생략</h2><div id=placerows></div>
     <h2>투명도(불투명도) %</h2><div id=oprows></div>
   </div>
   <div class=pane data-p=adv hidden>
     <h2>건물 2D/3D pitch 자동전환</h2>
     <div class=row><label>전환 임계값 (도, 비우면 끄기)</label>
       <input type=number class=zn id=bpitch min=0 max=85 step=1 placeholder="끄기"></div>
     <p class=hint>지도가 이 각도(pitch) 이상이면 3D 건물만, 미만이면 2D 건물만 표시합니다.<br>비워두면 자동전환 꺼짐(기본) — 2D·3D 둘 다 표시, 수동 버튼 유지.</p>
     <h2>외곽선(halo)·도트 테두리·건물 외곽</h2><div id=extrarows></div>
     <h2>글꼴</h2><div id=fontrows></div>
     <h2>속성별 색 그라데이션</h2><div id=gradrows></div>
     <h2>POI 노출 우선순위 (줌 티어) — 중요한 건 낮은 줌부터</h2>
     <div id=tierrows></div>
     <p class=hint style="margin:6px 0">카테고리별 티어 지정(대분류 펼쳐 중·소 오버라이드, (상속)=상위 따름):</p>
     <div id=cattree></div>
     <p class=hint>외곽선(halo) 색·두께는 어두운 배경 라벨 가독성의 핵심입니다. 글꼴은 설치된 글리프만 선택됩니다.
     <br>건물 높이 그라데이션은 render_height 기반(데이터드리븐) — 켜면 건물 단색 대신 높이별 색.</p>
   </div>
   <p class=hint>색을 바꾸면 미리보기에 즉시 반영. ‘저장 & 적용’ → style.json·theme.json 기록 + 타일서버 재시작(영구 반영).</p>
 </div>
 <div id=map></div>
</div>
<script src="/vendor/maplibre/maplibre-gl.js"></script>
<script>
 const $=s=>document.querySelector(s); let OBJ=[], PRE={}, map=null, INIT={}, INITZ={}, OPA=[], INITO={}, TPORT=8080, ICONG=[], SRCG=[], INITS={}, INITV={}, INITLANG='ko', SIZ=[], INITSZ={}, EXTRA=[], INITX={}, INITF={}, TAX={}, IOV={cat2:{},cat:{}}, INITIOV={cat2:{},cat:{}}, IOVdirty=false, GRAD=[], INITG={}, PENDING={}, PT=[], CTT={}, INITPT='', INITCTT='', POICOL=[], PCUR={}, INITPC={}, PLACE=[], INITPL={}, INITBP=null;
 const TOKEN=new URLSearchParams(location.search).get('token')||'';
 function post(url,body){return fetch(url,{method:'POST',headers:{'Content-Type':'application/json','X-Studio-Token':TOKEN},body});}
 fetch('/api/style/objects').then(r=>r.json()).then(d=>{
   OBJ=d.objects; PRE=d.presets||{}; TPORT=d.tile_port;
   $('#rows').innerHTML=OBJ.map(row).join('');
   $('#zoomrows').innerHTML=OBJ.map(zrow).join('');
   OBJ.forEach(o=>{ INIT[o.key]=o.color; wire(o,v=>apply(o,v)); });
   OBJ.forEach(o=>{ INITZ[o.key]={min:(o.zoom||{}).min??'',max:(o.zoom||{}).max??''}; wireZoom(o); });
   $('#oprows').innerHTML=(OPA=d.opacity_objects||[]).map(orow).join('');
   OPA.forEach(o=>{ INITO[o.key]=Math.round((o.value==null?1:o.value)*100); wireOpacity(o); });
   ICONG=d.icon_groups||[]; $('#iconrows').innerHTML=ICONG.map(irow).join(''); ICONG.forEach(wireIcon);
   TAX=d.taxonomy||{}; IOV=d.icon_overrides||{cat2:{},cat:{}}; IOV.cat2=IOV.cat2||{}; IOV.cat=IOV.cat||{};
   INITIOV=JSON.parse(JSON.stringify(IOV)); renderTree();
   POICOL=ICONG; PCUR=d.poi_colors||{};   // 시설 그룹별 글자색 — 아이콘과 동일 그룹(cats 포함) 재사용
   $('#poicolrows').innerHTML=POICOL.map(pcrow).join('');
   INITPC={}; POICOL.forEach(g=>{ if(PCUR[g.key])INITPC[g.key]=PCUR[g.key]; wirePc(g); });
   ['#c_poilabel','#h_poilabel'].forEach(s=>{const e=$(s); if(e)e.addEventListener('input',applyPoiCol);});
   SRCG=d.source_groups||[]; $('#srcrows').innerHTML=SRCG.map(srow).join(''); SRCG.forEach(g=>{INITS[g.key]=g.on; wireSrc(g);});
   $('#visrows').innerHTML=OBJ.map(visrow).join(''); OBJ.forEach(o=>{INITV[o.key]=o.visible!==false; wireVis(o);});
   INITLANG=d.language||'ko'; $('#langsel').value=INITLANG; $('#langsel').onchange=applyLang;
   SIZ=d.size_objects||[]; $('#sizerows').innerHTML=SIZ.map(szrow).join('');
   SIZ.forEach(t=>{INITSZ[t.key]={min:t.min,max:t.max}; wireSize(t);});
   EXTRA=d.extra_objects||[]; $('#extrarows').innerHTML=EXTRA.map(exrow).join('');
   EXTRA.forEach(t=>{INITX[t.key]=t.value; wireExtra(t);});
   PLACE=d.placement_objects||[]; $('#placerows').innerHTML=PLACE.map(plrow).join('');
   PLACE.forEach(t=>{INITPL[t.key]=plDisp(t); wirePlace(t);});
   renderFonts(d.fonts||{});
   GRAD=d.gradient_objects||[]; $('#gradrows').innerHTML=GRAD.map(gradrow).join('');
   GRAD.forEach(g=>{INITG[g.key]={on:g.on,low:g.low,high:g.high,max:g.max}; wireGrad(g);});
   PT=d.poi_tiers||[]; CTT=d.cat_tiers||{cat1:{},cat2:{},cat:{}}; CTT.cat1=CTT.cat1||{}; CTT.cat2=CTT.cat2||{}; CTT.cat=CTT.cat||{};
   INITPT=JSON.stringify(PT); INITCTT=JSON.stringify(CTT); renderTiers();
   INITBP=(d.building_pitch_3d!=null)?d.building_pitch_3d:null;
   const bpe=$('#bpitch'); if(bpe)bpe.value=(INITBP!=null)?INITBP:'';
   map=new maplibregl.Map({container:'map',
     style:`http://${location.hostname}:${TPORT}/styles/cuvia/style.json`,
     // maplibre 워커는 document base 가 없어 스타일의 상대경로 /dyn/* (martin 동적타일=건물·필지·POI)로
     // Request 생성에 실패한다("Failed to parse URL"). 타일 베이스(스타일을 받은 host:TPORT)로 절대화
     // — demo/js/map.js 와 동일 처리. TPORT=게이트웨이(18080)면 /dyn 도 게이트웨이 경유로 정상 로드.
     transformRequest:(u)=>(u&&u.charAt(0)==='/'?{url:`http://${location.hostname}:${TPORT}`+u}:{url:u}),
     center:[126.9784,37.5666], zoom:14.5, pitch:55, bearing:-18, attributionControl:false});
   map.addControl(new maplibregl.NavigationControl());
   const zl=()=>{const e=$('#zlevel'); if(e)e.textContent='현재 z '+map.getZoom().toFixed(1);};
   map.on('zoom',zl); map.on('load',zl);
 }).catch(e=>$('#status').textContent='객체 로드 실패: '+e);
 function row(o){return `<div class=row><label>${o.label}</label>
   <input type=text id=h_${o.key} value="${o.color}" maxlength=7 spellcheck=false>
   <input type=color id=c_${o.key} value="${o.color}"></div>`;}
 function wire(o,fn){const c=$('#c_'+o.key),h=$('#h_'+o.key);
   c.oninput=()=>{h.value=c.value; fn(c.value)};
   h.oninput=()=>{if(/^#[0-9a-fA-F]{6}$/.test(h.value)){c.value=h.value; fn(h.value)}};}
 function setColor(k,v){const c=$('#c_'+k),h=$('#h_'+k); if(c){c.value=v;h.value=v;}}
 function apply(o,color){ if(!map||!map.isStyleLoaded())return;
   o.targets.forEach(t=>{ try{map.setPaintProperty(t[0],t[1],color)}catch(e){} }); }
 // 시설 라벨 — 카테고리 그룹별 글자색 (cat1 match, 미지정 그룹은 '시설 라벨' 기본색 fallback)
 function pcFb(){const e=$('#c_poilabel'); return e?e.value:'#e8edf2';}
 function pcrow(g){const cur=(PCUR||{})[g.key], v=cur||pcFb();
   return `<div class=row><input type=checkbox id=pck_${g.key} ${cur?'checked':''} style="width:16px;height:16px;accent-color:#5b9bd5;flex:none;cursor:pointer">
     <label>${g.label}</label>
     <input type=text id=pch_${g.key} value="${v}" maxlength=7 spellcheck=false>
     <input type=color id=pcc_${g.key} value="${v}"></div>`;}
 function wirePc(g){const c=$('#pcc_'+g.key),h=$('#pch_'+g.key),k=$('#pck_'+g.key); if(!c)return;
   const chk=()=>{if(k&&!k.checked)k.checked=true;};
   c.oninput=()=>{h.value=c.value; chk(); applyPoiCol();};
   h.oninput=()=>{if(/^#[0-9a-fA-F]{6}$/.test(h.value)){c.value=h.value; chk(); applyPoiCol();}};
   if(k)k.onchange=applyPoiCol;}
 function poiColExpr(){const fb=pcFb(), p=[];
   (POICOL||[]).forEach(g=>{const k=$('#pck_'+g.key); if(k&&k.checked)p.push(g.cats, $('#pcc_'+g.key).value);});
   return p.length?['match',['get','cat1']].concat(p,[fb]):fb;}
 function applyPoiCol(){ if(!map||!map.isStyleLoaded()||!map.getLayer('poi-label'))return;
   try{map.setPaintProperty('poi-label','text-color', poiColExpr());}catch(e){}
   $('#status').textContent='시설 글자색 (미저장)'; }
 function poiColMap(){const m={}; (POICOL||[]).forEach(g=>{const k=$('#pck_'+g.key); if(k&&k.checked)m[g.key]=$('#pcc_'+g.key).value;}); return m;}
 function poiColChanged(){const m=poiColMap(); return JSON.stringify(m)!==JSON.stringify(INITPC)?m:null;}
 function zrow(o){const z=o.zoom||{};return `<div class=row><label>${o.label}</label>
   <input type=number class=zn id=zmin_${o.key} min=0 max=24 placeholder=0 value="${z.min??''}">
   <span style="color:#8d9bb5">–</span>
   <input type=number class=zn id=zmax_${o.key} min=0 max=24 placeholder=24 value="${z.max??''}"></div>`;}
 function wireZoom(o){['zmin_','zmax_'].forEach(p=>{const e=$('#'+p+o.key); if(e)e.oninput=()=>applyZoom(o);});}
 function applyZoom(o){ if(!map||!map.isStyleLoaded())return;
   const mn=$('#zmin_'+o.key).value, mx=$('#zmax_'+o.key).value, lo=mn===''?0:+mn, hi=mx===''?24:+mx;
   o.targets.forEach(t=>{try{map.setLayerZoomRange(t[0], lo, Math.max(lo,hi))}catch(e){}}); }
 function zoomChanged(){const z={}; OBJ.forEach(o=>{const mn=$('#zmin_'+o.key).value, mx=$('#zmax_'+o.key).value, i=INITZ[o.key]||{};
   if(String(mn)!==String(i.min)||String(mx)!==String(i.max)) z[o.key]={min:mn===''?null:+mn, max:mx===''?null:+mx};}); return z;}
 function orow(o){const p=Math.round((o.value==null?1:o.value)*100);return `<div class="row oprow" id=or_${o.key}><label>${o.label}</label>
   <input type=range class=op id=o_${o.key} min=0 max=100 step=1 value="${p}">
   <span class=opv id=ov_${o.key}>${p}%</span></div>`;}
 function wireOpacity(o){const e=$('#o_'+o.key); if(e)e.oninput=()=>applyOpacity(o);}
 function applyOpacity(o){ const e=$('#o_'+o.key); if(!e)return; const v=+e.value; const r=$('#ov_'+o.key); if(r)r.textContent=v+'%';
   if(!map||!map.isStyleLoaded())return;
   o.targets.forEach(t=>{try{map.setPaintProperty(t[0],t[1],v/100)}catch(e){}}); }
 function opacityChanged(){const o={}; OPA.forEach(x=>{const p=+$('#o_'+x.key).value; if(p!==INITO[x.key]) o[x.key]=p/100;}); return o;}
 function preset(name){ const p=PRE[name]||{};
   OBJ.forEach(o=>{ if(p[o.key]){setColor(o.key,p[o.key]); apply(o,p[o.key]);} });
   $('#status').textContent=name+' 프리셋 적용(미저장)'; }
 $('#pl').onclick=()=>preset('light'); $('#pd').onclick=()=>preset('dark');
 $('#reset').onclick=()=>{
   OBJ.forEach(o=>setColor(o.key,INIT[o.key]));
   OBJ.forEach(o=>{const i=INITZ[o.key]||{}; $('#zmin_'+o.key).value=i.min??''; $('#zmax_'+o.key).value=i.max??'';});
   OPA.forEach(o=>{const e=$('#o_'+o.key); if(e){e.value=INITO[o.key]; const r=$('#ov_'+o.key); if(r)r.textContent=INITO[o.key]+'%';}});
   SRCG.forEach(g=>{const e=$('#s_'+g.key); if(e)e.checked=INITS[g.key];});
   OBJ.forEach(o=>{const e=$('#v_'+o.key); if(e)e.checked=INITV[o.key];}); $('#langsel').value=INITLANG;
   SIZ.forEach(t=>{const i=INITSZ[t.key]||{}; $('#szmin_'+t.key).value=i.min??''; $('#szmax_'+t.key).value=i.max??'';});
   EXTRA.forEach(t=>{ if(t.type==='color'){const v=INITX[t.key]||'#000000',c=$('#xc_'+t.key),h=$('#xh_'+t.key); if(c){c.value=v;h.value=v;}} else {const e=$('#xn_'+t.key); if(e)e.value=INITX[t.key]??'';}});
   PLACE.forEach(t=>{const e=$('#pl_'+t.key); if(!e)return; if(t.type==='bool')e.checked=!!INITPL[t.key]; else e.value=INITPL[t.key]??'';});
   Object.keys(INITF).forEach(k=>{const e=$('#font_'+k); if(e)e.value=INITF[k]||'';}); if($('#font_all'))$('#font_all').value='';
   fetch('/api/icon/discard',{method:'POST',headers:{'X-Studio-Token':TOKEN}}).catch(()=>{});
   PENDING={}; ICONG.forEach(g=>{const im=$('#ic_'+g.key); if(im)im.src='/style/icons/'+g.icon+'.png?t='+Date.now();});
   IOV=JSON.parse(JSON.stringify(INITIOV)); IOVdirty=false; renderTree();
   GRAD.forEach(g=>{const i=INITG[g.key]||{}; const c=$('#g_'+g.key); if(c)c.checked=!!i.on; const lo=$('#glo_'+g.key); if(lo)lo.value=i.low||'#3a4a6b'; const hi=$('#ghi_'+g.key); if(hi)hi.value=i.high||'#9db4e0'; const mx=$('#gmx_'+g.key); if(mx)mx.value=i.max||100;});
   POICOL.forEach(g=>{const cur=INITPC[g.key]; const k=$('#pck_'+g.key); if(k)k.checked=!!cur; const v=cur||pcFb(); const c=$('#pcc_'+g.key),h=$('#pch_'+g.key); if(c){c.value=v;h.value=v;}});
   PT=JSON.parse(INITPT); CTT=JSON.parse(INITCTT); renderTiers();
   const bpr=$('#bpitch'); if(bpr)bpr.value=(INITBP!=null)?INITBP:'';
   map.setStyle(`http://${location.hostname}:${TPORT}/styles/cuvia/style.json`,{diff:false});
   $('#status').textContent='되돌림 — 서버 스타일 다시 로드'; };
 $('#exp').onclick=()=>location.href='/api/style/export';
 $('#imp').onclick=()=>$('#impf').click();
 $('#impf').onchange=e=>{const f=e.target.files[0]; if(!f)return; e.target.value='';
   $('#status').textContent='가져오는 중…';
   f.text().then(t=>post('/api/style/import',t))
    .then(r=>r.json()).then(d=>{ if(!d.ok){$('#status').textContent='✗ '+(d.error||'오류');return;}
      OBJ.forEach(o=>{const v=(d.colors||{})[o.key]; if(v){setColor(o.key,v); apply(o,v);}});
      $('#status').textContent=`✓ 색·시설색·크기 반영 (글꼴·투명도 등 나머지는 현재 설정 유지) · 타일서버 ${d.reloaded} — 재시작 후 새로고침`;
    }).catch(err=>$('#status').textContent='✗ 실패: '+err); };
 // 탭 전환
 document.querySelectorAll('.tab').forEach(t=>t.onclick=()=>{
   document.querySelectorAll('.tab').forEach(x=>x.classList.toggle('active',x===t));
   document.querySelectorAll('.pane').forEach(p=>p.hidden=(p.dataset.p!==t.dataset.t));
 });
 // 카테고리 아이콘(이미지 업로드)
 function irow(g){return `<div class=icrow><label>${g.label}</label>
   <img id=ic_${g.key} src="/style/icons/${g.icon}.png?t=${Date.now()}" alt="">
   <button class=up data-k="${g.key}">변경</button>
   <input type=file id=if_${g.key} accept="image/*" style="display:none"></div>`;}
 function wireIcon(g){const btn=document.querySelector('.up[data-k="'+g.key+'"]'), inp=$('#if_'+g.key);
   if(!btn||!inp)return; btn.onclick=()=>inp.click();
   inp.onchange=e=>{const f=e.target.files[0]; if(!f)return; e.target.value='';
     $('#status').textContent='아이콘 업로드 중…';
     fetch('/api/icon?group='+encodeURIComponent(g.key),{method:'POST',
        headers:{'X-Studio-Token':TOKEN,'Content-Type':f.type||'image/png'},body:f})
      .then(r=>r.json()).then(d=>{ if(!d.ok){$('#status').textContent='✗ '+(d.error||'오류');return;}
        $('#ic_'+g.key).src=(d.preview||('/style/icons/'+d.icon+'.png'))+'?t='+Date.now(); PENDING[d.icon]=true;
        $('#status').textContent='✓ 업로드(미저장) — [저장 & 적용] 해야 지도에 반영됩니다'; })
      .catch(err=>$('#status').textContent='✗ 실패: '+err); };}
 // 대>중>소 아이콘 오버라이드 트리
 function txThumb(level,name){const s=(IOV[level]||{})[name]; if(!s)return '<span class=noic>—</span>';
   const dir=PENDING[s]?'/style/icons/.pending/':'/style/icons/'; return `<img class=ic src="${dir}${s}.png?t=${Date.now()}">`;}
 function txr(level,name,extra){return `<div class=txr><span class=nm>${name}</span>${txThumb(level,name)}<button class="tup" data-l="${level}" data-n="${encodeURIComponent(name)}">아이콘</button>${extra||''}</div>`;}
 function renderTree(){const order=TAX.cat1_order||[],tree=TAX.tree||{}; let h='';
   order.forEach(c1=>{const mids=tree[c1]||{}; h+=`<details class=tx1><summary>${c1}</summary>`;
     Object.keys(mids).forEach(c2=>{const subs=mids[c2]||[];
       h+=txr('cat2',c2, subs.length?`<button class=exp>소 ${subs.length}</button>`:'');
       if(subs.length) h+=`<div class=subwrap hidden>`+subs.map(s=>txr('cat',s,'')).join('')+`</div>`;});
     h+=`</details>`;});
   const el=$('#icontree'); if(!el)return; el.innerHTML=h;
   el.querySelectorAll('.exp').forEach(b=>b.onclick=()=>{const w=b.closest('.txr').nextElementSibling; if(w&&w.classList.contains('subwrap'))w.hidden=!w.hidden;});
   el.querySelectorAll('.tup').forEach(b=>b.onclick=()=>txUpload(b.dataset.l, decodeURIComponent(b.dataset.n)));}
 function txUpload(level,name){const inp=document.createElement('input'); inp.type='file'; inp.accept='image/*';
   inp.onchange=e=>{const f=e.target.files[0]; if(!f)return; $('#status').textContent='오버라이드 업로드 중…';
     fetch('/api/icon?level='+level+'&name='+encodeURIComponent(name),{method:'POST',headers:{'X-Studio-Token':TOKEN,'Content-Type':f.type||'image/png'},body:f})
      .then(r=>r.json()).then(d=>{ if(!d.ok){$('#status').textContent='✗ '+(d.error||'오류');return;}
        IOV[level]=IOV[level]||{}; IOV[level][name]=d.icon; PENDING[d.icon]=true; IOVdirty=true; renderTree();
        $('#status').textContent='✓ '+name+' 업로드(미저장) — [저장 & 적용] 해야 반영';})
      .catch(err=>$('#status').textContent='✗ 실패: '+err);};
   inp.click();}
 // 데이터 출처 토글
 function srow(g){return `<div class=srow><input type=checkbox id=s_${g.key} ${g.on?'checked':''}>
   <label for=s_${g.key}>${g.label}</label></div>`;}
 function wireSrc(g){const e=$('#s_'+g.key); if(e)e.onchange=applySrc;}
 function applySrc(){ if(!map||!map.isStyleLoaded())return;
   const on={}; SRCG.forEach(g=>on[g.key]=$('#s_'+g.key).checked);
   const layers=map.getStyle().layers;
   SRCG.forEach(g=>{
     if(g.kind==='src_visibility'){ layers.forEach(L=>{ if(L.source===g.src){
        try{map.setLayoutProperty(L.id,'visibility',on[g.key]?'visible':'none')}catch(e){} }}); }
     else if(g.kind==='terrain'){ try{ on[g.key]?map.setTerrain({source:'terrain',exaggeration:1.3}):map.setTerrain(null);}catch(e){} }
   });
   applyPoiSrc(on.sangga, on.localdata); }
 function applyPoiSrc(sangga, local){ ['poi-label'].forEach(lid=>{
     if(!map.getLayer(lid))return;
     if(!sangga&&!local){ try{map.setLayoutProperty(lid,'visibility','none')}catch(e){} return; }
     try{map.setLayoutProperty(lid,'visibility','visible')}catch(e){}
     const pred=(sangga&&local)?['!=',['get','is_primary'],0]:['==',['get','source'],sangga?'sangga':'localdata'];
     const cl=(lid==='poi-label'?[['!',['has','point_count']]]:[]).concat([pred]);
     try{map.setFilter(lid, cl.length===1?cl[0]:['all'].concat(cl))}catch(e){} }); }
 function sourcesChanged(){const s={}; let ch=false;
   SRCG.forEach(g=>{const v=$('#s_'+g.key).checked; s[g.key]=v; if(v!==INITS[g.key])ch=true;}); return ch?s:null;}
 // 객체 표시 on/off
 function visrow(o){return `<div class=srow><input type=checkbox id=v_${o.key} ${o.visible!==false?'checked':''}><label for=v_${o.key}>${o.label}</label></div>`;}
 function wireVis(o){const e=$('#v_'+o.key); if(e)e.onchange=()=>applyVis(o);}
 function applyVis(o){ if(!map||!map.isStyleLoaded())return; const on=$('#v_'+o.key).checked;
   [...new Set(o.targets.map(t=>t[0]))].forEach(lid=>{if(map.getLayer(lid))try{map.setLayoutProperty(lid,'visibility',on?'visible':'none')}catch(e){}}); }
 function visChanged(){const v={}; let ch=false; OBJ.forEach(o=>{const x=$('#v_'+o.key).checked; v[o.key]=x; if(x!==INITV[o.key])ch=true;}); return ch?v:null;}
 // 표시 언어
 const LANGL=["place-label","road-label","poi-label","poi-civic-label","poi-station-label","peak-label","aerodrome-label"];
 function langExpr(l){ if(l==='en')return ['coalesce',['get','name:en'],['get','name:latin'],['get','name']];
   if(l==='local')return ['get','name']; return ['coalesce',['get','name:ko'],['get','name']]; }
 function applyLang(){ if(!map||!map.isStyleLoaded())return; const e=langExpr($('#langsel').value);
   LANGL.forEach(lid=>{if(map.getLayer(lid))try{map.setLayoutProperty(lid,'text-field',e)}catch(x){}});
   $('#status').textContent='언어 '+$('#langsel').value+' (미저장)'; }
 // 크기(줌 min~max 값) — 멱등 remap
 function szrow(t){return `<div class=row><label>${t.label}</label>
   <input type=number class=zn id=szmin_${t.key} step=0.1 min=0 value="${t.min??''}">
   <span style="color:#8d9bb5">–</span>
   <input type=number class=zn id=szmax_${t.key} step=0.1 min=0 value="${t.max??''}"></div>`;}
 function wireSize(t){['szmin_','szmax_'].forEach(p=>{const e=$('#'+p+t.key); if(e)e.oninput=()=>applySize(t);});}
 function remapJS(v,mn,mx){ if(typeof v==='number')return mx;
   if(Array.isArray(v)&&v[0]==='interpolate'){ v=v.slice(); const o=[]; for(let i=4;i<v.length;i+=2)if(typeof v[i]==='number')o.push(v[i]);
     if(!o.length)return v; const a=Math.min(...o),b=Math.max(...o),sp=(b-a)||1;
     for(let i=4;i<v.length;i+=2)if(typeof v[i]==='number')v[i]=Math.round((mn+(v[i]-a)/sp*(mx-mn))*100)/100; return v;} return v; }
 function applySize(t){ if(!map||!map.isStyleLoaded())return;
   const mn=$('#szmin_'+t.key).value, mx=$('#szmax_'+t.key).value; if(mn===''||mx==='')return;
   const lay=(t.prop==='text-size'||t.prop==='icon-size'); let v;
   try{ v=lay?map.getLayoutProperty(t.layer,t.prop):map.getPaintProperty(t.layer,t.prop);}catch(e){return;}
   const nv=remapJS(v,+mn,+mx);
   try{ lay?map.setLayoutProperty(t.layer,t.prop,nv):map.setPaintProperty(t.layer,t.prop,nv);}catch(e){} }
 function sizesChanged(){const s={}; let ch=false;
   SIZ.forEach(t=>{const mn=$('#szmin_'+t.key).value,mx=$('#szmax_'+t.key).value; if(mn===''||mx==='')return;
     const i=INITSZ[t.key]||{}; if(+mn!==i.min||+mx!==i.max){s[t.key]={min:+mn,max:+mx}; ch=true;}}); return ch?s:null;}
 // 고급 paint(외곽선·테두리·건물외곽)
 function exrow(t){ if(t.type==='color'){const v=t.value||'#000000';
     return `<div class=row><label>${t.label}</label>
       <input type=text id=xh_${t.key} value="${v}" maxlength=7 spellcheck=false>
       <input type=color id=xc_${t.key} value="${v}"></div>`;}
   return `<div class=row><label>${t.label}</label>
     <input type=number class=zn id=xn_${t.key} step=0.1 min=0 max=10 value="${t.value??''}"></div>`;}
 function wireExtra(t){ if(t.type==='color'){const c=$('#xc_'+t.key),h=$('#xh_'+t.key);
     c.oninput=()=>{h.value=c.value; applyExtra(t,c.value);};
     h.oninput=()=>{if(/^#[0-9a-fA-F]{6}$/.test(h.value)){c.value=h.value; applyExtra(t,h.value);}};}
   else {const e=$('#xn_'+t.key); if(e)e.oninput=()=>applyExtra(t, e.value===''?null:+e.value);} }
 function applyExtra(t,v){ if(!map||!map.isStyleLoaded()||v==null)return; try{map.setPaintProperty(t.layer,t.prop,v)}catch(e){} }
 function extrasChanged(){const x={}; let ch=false;
   EXTRA.forEach(t=>{let v; if(t.type==='color'){v=$('#xh_'+t.key).value; if(!/^#[0-9a-fA-F]{6}$/.test(v))return;}
     else{const e=$('#xn_'+t.key).value; if(e==='')return; v=+e;}
     if(v!==INITX[t.key]){x[t.key]=v; ch=true;}}); return ch?x:null;}
 // 배치(라벨 offset·anchor·충돌생략) — layout 속성
 function plDisp(t){ return (t.value!=null)?t.value:t.default; }   // 값 미설정 시 기본값 표시(빈칸 방지)
 function plrow(t){
   if(t.type==='offset') return `<div class=row><label>${t.label}</label>
     <input type=number class=zn id=pl_${t.key} step=0.05 min=-10 max=10 value="${plDisp(t)??0}"></div>`;
   if(t.type==='anchor'){ const sel=plDisp(t); const o=(t.anchors||[]).map(a=>`<option value="${a}"${a===sel?' selected':''}>${a}</option>`).join('');
     return `<div class=row><label>${t.label}</label><select class=zn id=pl_${t.key}>${o}</select></div>`;}
   return `<div class=row><label>${t.label}</label><input type=checkbox id=pl_${t.key}${plDisp(t)?' checked':''}></div>`;}
 function wirePlace(t){ const e=$('#pl_'+t.key); if(!e)return;
   if(t.type==='bool') e.onchange=()=>applyPlace(t,e.checked);
   else if(t.type==='anchor') e.onchange=()=>applyPlace(t,e.value||null);
   else e.oninput=()=>applyPlace(t, e.value===''?null:+e.value); }
 function applyPlace(t,v){ if(!map||!map.isStyleLoaded())return; try{
     if(t.type==='offset'){ if(v==null)return; const c=map.getLayoutProperty(t.layer,'text-offset'); const x=(Array.isArray(c)&&typeof c[0]==='number')?c[0]:0; map.setLayoutProperty(t.layer,t.prop,[x,v]); }
     else if(t.type==='anchor'){ if(v==null)return; map.setLayoutProperty(t.layer,t.prop,v); }
     else map.setLayoutProperty(t.layer,t.prop,!!v);
   }catch(e){} }
 function placeChanged(){ const p={}; let ch=false;
   PLACE.forEach(t=>{ if(t.type==='bool'){ const v=$('#pl_'+t.key).checked; if(v!==(INITPL[t.key]===true)){p[t.key]=v; ch=true;} return; }
     let v; if(t.type==='anchor'){ const s=$('#pl_'+t.key).value; v=s===''?null:s; }
     else { const e=$('#pl_'+t.key).value; v=e===''?null:+e; }
     if(v!=null && v!==INITPL[t.key]){p[t.key]=v; ch=true;} }); return ch?p:null; }
 // 글꼴
 const FONTMAP={donglabel:['dong-label'],poilabel:['poi-label'],placelabel:['place-label','road-label']};
 function renderFonts(F){ const av=F.available||[], cur=F.current||{}, labs=F.labels||[];
   const opt=(sel)=>av.map(f=>`<option ${f===sel?'selected':''}>${f}</option>`).join('');
   let h=`<div class=row><label>전체 라벨</label><select id=font_all><option value="">(개별 유지)</option>${opt('')}</select></div>`;
   labs.forEach(l=>{INITF[l.key]=cur[l.key]||''; h+=`<div class=row><label>${l.label}</label><select id=font_${l.key}><option value="">(기본)</option>${opt(cur[l.key]||'')}</select></div>`;});
   $('#fontrows').innerHTML=h;
   const fa=$('#font_all'); if(fa)fa.onchange=()=>{const f=fa.value; if(!f||!map)return;
     (map.getStyle().layers||[]).forEach(L=>{if(L.type==='symbol')try{map.setLayoutProperty(L.id,'text-font',[f])}catch(e){}});
     labs.forEach(l=>{const e=$('#font_'+l.key); if(e)e.value=f;}); $('#status').textContent='글꼴 전체 '+f+' (미저장)';};
   labs.forEach(l=>{const e=$('#font_'+l.key); if(e)e.onchange=()=>{const f=e.value; if(!f||!map)return;
     (FONTMAP[l.key]||[]).forEach(lid=>{if(map.getLayer(lid))try{map.setLayoutProperty(lid,'text-font',[f])}catch(x){}});};});}
 function fontsChanged(){const f={}; let ch=false; const fa=$('#font_all'); const all=fa?fa.value:'';
   if(all){f.all=all; ch=true;}
   Object.keys(INITF).forEach(k=>{const e=$('#font_'+k); if(e&&e.value&&e.value!==INITF[k]){f[k]=e.value; ch=true;}}); return ch?f:null;}
 // 속성별 색 그라데이션(건물 높이)
 const GRADLAYERS={bld_height:[['building-2d','fill-color'],['Building 3D','fill-extrusion-color']]};
 const GRADREVERT={'building-2d':'building2d','Building 3D':'building3d'};
 function gradrow(g){const lo=g.low||'#3a4a6b',hi=g.high||'#9db4e0',mx=g.max||100;
   return `<div class=srow><input type=checkbox id=g_${g.key} ${g.on?'checked':''}><label for=g_${g.key}>${g.label} (낮음→높음, 최대 m)</label></div>
   <div class=row style="padding-left:14px"><label>색·최대높이</label>
     <input type=color id=glo_${g.key} value="${lo}"><input type=color id=ghi_${g.key} value="${hi}">
     <input type=number class=zn id=gmx_${g.key} min=1 max=500 value="${mx}"></div>`;}
 function wireGrad(g){['g_','glo_','ghi_','gmx_'].forEach(p=>{const e=$('#'+p+g.key); if(e){e.oninput=()=>applyGrad(g); e.onchange=()=>applyGrad(g);}});}
 function gradExpr(g){return ['interpolate',['linear'],['get','render_height'],0,$('#glo_'+g.key).value,(+$('#gmx_'+g.key).value)||100,$('#ghi_'+g.key).value];}
 function applyGrad(g){ if(!map||!map.isStyleLoaded())return; const on=$('#g_'+g.key).checked;
   (GRADLAYERS[g.key]||[]).forEach(([lid,prop])=>{ if(!map.getLayer(lid))return;
     try{ map.setPaintProperty(lid,prop, on?gradExpr(g):(INIT[GRADREVERT[lid]]||'#495679')); }catch(e){} });
   $('#status').textContent='건물 그라데이션 '+(on?'ON':'OFF')+' (미저장)'; }
 function gradChanged(){const o={}; let ch=false;
   GRAD.forEach(g=>{const c={on:$('#g_'+g.key).checked,low:$('#glo_'+g.key).value,high:$('#ghi_'+g.key).value,max:+$('#gmx_'+g.key).value};
     const i=INITG[g.key]||{}; if(c.on!==i.on||c.low!==i.low||c.high!==i.high||c.max!==i.max){o[g.key]=c; ch=true;}}); return ch?o:null;}
 // POI 노출 우선순위 티어 (줌 게이트)
 function tierRow(t){return `<div class=row><label>${t.label}</label>
   <input type=number class=zn id=tz_${t.key} min=0 max=24 value="${t.minzoom}"></div>`;}
 function tierSel(level,name){ const cur=(CTT[level]||{})[name]||'';
   const opts=['<option value="">(상속)</option>'].concat(PT.map(t=>`<option value="${t.key}" ${cur===t.key?'selected':''}>${t.label} (z${t.minzoom})</option>`)).join('');
   return `<select class=tiersel data-l="${level}" data-n="${encodeURIComponent(name)}">${opts}</select>`;}
 function renderTiers(){
   $('#tierrows').innerHTML=PT.map(tierRow).join('');
   PT.forEach((t,i)=>{const e=$('#tz_'+t.key); if(e)e.oninput=()=>{PT[i].minzoom=+e.value||0; refreshTierLabels(); applyTiers();};});
   const order=TAX.cat1_order||[],tree=TAX.tree||{}; let h='';
   order.forEach(c1=>{const mids=tree[c1]||{};
     h+=`<details class=tx1><summary>${c1} ${tierSel('cat1',c1)}</summary>`;
     Object.keys(mids).forEach(c2=>{const subs=mids[c2]||[];
       h+=`<div class=txr><span class=nm>${c2}</span>${tierSel('cat2',c2)}${subs.length?'<button class=exp>소 '+subs.length+'</button>':''}</div>`;
       if(subs.length) h+=`<div class=subwrap hidden>`+subs.map(sb=>`<div class=txr><span class=nm>${sb}</span>${tierSel('cat',sb)}</div>`).join('')+`</div>`;});
     h+=`</details>`;});
   const el=$('#cattree'); if(!el)return; el.innerHTML=h;
   el.querySelectorAll('.exp').forEach(b=>b.onclick=()=>{const w=b.closest('.txr').nextElementSibling; if(w&&w.classList.contains('subwrap'))w.hidden=!w.hidden;});
   el.querySelectorAll('.tiersel').forEach(s=>s.onchange=()=>{const lv=s.dataset.l,nm=decodeURIComponent(s.dataset.n);
     CTT[lv]=CTT[lv]||{}; if(s.value)CTT[lv][nm]=s.value; else delete CTT[lv][nm]; applyTiers();});}
 function catMatch(field,pairs,inner){return pairs.length?['match',['get',field]].concat(pairs,[inner]):inner;}
 function buildTierMz(){ const tmz={}; PT.forEach(t=>tmz[t.key]=t.minzoom);
   const fb=tmz['t3']!=null?tmz['t3']:(PT.length?PT[PT.length-1].minzoom:17);
   const conv=(m)=>{const o=[]; Object.keys(m||{}).forEach(nm=>{const v=tmz[m[nm]]; if(v!=null)o.push(nm,v);}); return o;};
   let e=catMatch('cat1',conv(CTT.cat1),fb); e=catMatch('cat2',conv(CTT.cat2),e); e=catMatch('cat',conv(CTT.cat),e); return e;}
 function applyTiers(){ if(!map||!map.isStyleLoaded()||!map.getLayer('poi-label'))return;
   const mz=buildTierMz(); let ii; try{ii=map.getLayoutProperty('poi-label','icon-image');}catch(e){ii=null;}
   if(Array.isArray(ii)&&ii[0]==='case')ii=ii[2];
   try{ map.setLayoutProperty('poi-label','text-field',['case',['>=',['zoom'],mz],['get','name'],'']);
     if(ii)map.setLayoutProperty('poi-label','icon-image',['case',['>=',['zoom'],mz],ii,'']);
     map.setLayerZoomRange('poi-label', Math.min.apply(null,PT.map(t=>t.minzoom)), 24);
   }catch(e){}
   $('#status').textContent='티어 미리보기 (미저장)';}
 function tiersChanged(){ const p=(JSON.stringify(PT)!==INITPT), c=(JSON.stringify(CTT)!==INITCTT);
   return (p||c)?{poi_tiers:p?PT:null, cat_tiers:c?CTT:null}:null;}
 // 티어 줌 변경 시 카테고리 드롭다운의 'tX zN' 텍스트만 즉시 갱신(지정값·확장상태 유지)
 function refreshTierLabels(){ document.querySelectorAll('#cattree .tiersel option').forEach(o=>{
   const t=PT.find(x=>x.key===o.value); if(t)o.textContent=t.label+' (z'+t.minzoom+')'; }); }
 $('#save').onclick=()=>{
   const theme={}; OBJ.forEach(o=>theme[o.key]=$('#c_'+o.key).value);
   const bpv=$('#bpitch').value; theme.building_pitch_3d=(bpv==='')?null:+bpv;
   const z=zoomChanged(); if(Object.keys(z).length) theme.zoom=z;
   const op=opacityChanged(); if(Object.keys(op).length) theme.opacity=op;
   const sc=sourcesChanged(); if(sc) theme.sources=sc;
   const vc=visChanged(); if(vc) theme.visibility=vc;
   if($('#langsel').value!==INITLANG) theme.language=$('#langsel').value;
   const szc=sizesChanged(); if(szc) theme.sizes=szc;
   const xc=extrasChanged(); if(xc) theme.extras=xc;
   const plc=placeChanged(); if(plc) theme.placement=plc;
   const fc=fontsChanged(); if(fc) theme.fonts=fc;
   if(IOVdirty) theme.icon_overrides=IOV;
   const grc=gradChanged(); if(grc) theme.gradient=grc;
   const pcc=poiColChanged(); if(pcc) theme.poi_colors=pcc;
   const tc=tiersChanged(); if(tc){ if(tc.poi_tiers)theme.poi_tiers=tc.poi_tiers; if(tc.cat_tiers)theme.cat_tiers=tc.cat_tiers; }
   $('#status').textContent='적용 중…';
   post('/api/style',JSON.stringify({theme})).then(r=>r.json()).then(d=>{
     if(d.ok){  // 적용 성공 → 현재값을 새 기준선으로(되돌리기 일관성)
       OBJ.forEach(o=>INIT[o.key]=$('#c_'+o.key).value);
       OBJ.forEach(o=>{INITZ[o.key]={min:$('#zmin_'+o.key).value,max:$('#zmax_'+o.key).value};});
       OPA.forEach(o=>{INITO[o.key]=+$('#o_'+o.key).value;});
       SRCG.forEach(g=>{INITS[g.key]=$('#s_'+g.key).checked;});
       OBJ.forEach(o=>{INITV[o.key]=$('#v_'+o.key).checked;}); INITLANG=$('#langsel').value;
       SIZ.forEach(t=>{const mn=$('#szmin_'+t.key).value,mx=$('#szmax_'+t.key).value; INITSZ[t.key]={min:mn===''?null:+mn,max:mx===''?null:+mx};});
       EXTRA.forEach(t=>{INITX[t.key]= t.type==='color'?$('#xh_'+t.key).value : ($('#xn_'+t.key).value===''?null:+$('#xn_'+t.key).value);});
       PLACE.forEach(t=>{const e=$('#pl_'+t.key); if(!e)return; INITPL[t.key]= t.type==='bool'?e.checked : (t.type==='anchor'?(e.value===''?null:e.value):(e.value===''?null:+e.value));});
       Object.keys(INITF).forEach(k=>{const e=$('#font_'+k); if(e)INITF[k]=e.value;}); if($('#font_all'))$('#font_all').value='';
       INITIOV=JSON.parse(JSON.stringify(IOV)); IOVdirty=false;
       GRAD.forEach(g=>{INITG[g.key]={on:$('#g_'+g.key).checked,low:$('#glo_'+g.key).value,high:$('#ghi_'+g.key).value,max:+$('#gmx_'+g.key).value};});
       INITPC=poiColMap();
       PENDING={}; ICONG.forEach(g=>{const im=$('#ic_'+g.key); if(im)im.src='/style/icons/'+g.icon+'.png?t='+Date.now();}); renderTree();
       INITPT=JSON.stringify(PT); INITCTT=JSON.stringify(CTT);
       const bpve=$('#bpitch'); INITBP=(bpve&&bpve.value!=='')?+bpve.value:null;
     }
     $('#status').textContent=d.ok?`✓ 저장 ${d.applied}개 · 타일서버 ${d.reloaded}`:('✗ '+(d.error||'오류'));
   }).catch(e=>$('#status').textContent='✗ 실패: '+e);
 };
</script></html>"""


if __name__ == "__main__":
    shown = "localhost" if HOST in ("127.0.0.1", "localhost") else HOST
    warn = "" if (HOST in ("127.0.0.1", "localhost") or STUDIO_TOKEN) else "  ⚠ 외부노출+무토큰 — STUDIO_TOKEN 설정 권장"
    print(f"CUVIA Style Studio → http://{shown}:{PORT}  (bind {HOST}"
          f"{', token 보호' if STUDIO_TOKEN else ''}, tileserver :{TILE_PORT}){warn}", flush=True)
    ThreadingHTTPServer((HOST, PORT), H).serve_forever()
