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
HOST = os.environ.get("HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", "8091"))
TILE_PORT = int(os.environ.get("TILE_PORT", "8080"))            # 미리보기·재시작 대상 tileserver 포트
COMPOSE_FILE = os.environ.get("COMPOSE_FILE", str(BUILD_HOME / "deploy/docker-compose.yml"))
STUDIO_TOKEN = os.environ.get("STUDIO_TOKEN", "")               # 설정 시 POST에 X-Studio-Token 요구
MAX_CTRL = 256 * 1024
MAX_STYLE = 4 * 1024 * 1024
HEXRE = re.compile(r"^#[0-9a-fA-F]{6}$")
VALID_KEYS = {o["key"] for o in style_objects.OBJECTS} | {g["key"] for g in style_objects.POI_GROUPS}


def apply_style_theme(theme):
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
    (ROOT / "style").mkdir(exist_ok=True)
    (ROOT / "style" / "theme.json").write_text(json.dumps(clean, ensure_ascii=False, indent=2), encoding="utf-8")
    log = []
    try:
        r = subprocess.run(["python3", str(ROOT / "scripts/build_style.py")],
                           capture_output=True, text=True, cwd=str(ROOT), timeout=60)
        log.append(r.stdout.strip() or r.stderr.strip())
        if r.returncode != 0:
            return {"ok": False, "error": "build_style 실패", "log": log}
    except Exception as e:
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
        if self.path == "/api/style/objects":
            try:
                style = json.loads((ROOT / "style/style.json").read_text(encoding="utf-8"))
                cur = style_objects.current_colors(style); poi = style_objects.current_poi_colors(style)
                fonts_cur = style_objects.current_fonts(style); zoom_cur = style_objects.current_zoom(style)
                opacity_cur = style_objects.current_opacity(style)
            except Exception:
                cur = {}; poi = {}; fonts_cur = {}; zoom_cur = {}; opacity_cur = {}
            objs = [{"key": o["key"], "label": o["label"], "targets": o["targets"],
                     "color": cur.get(o["key"]) or "#888888", "zoom": zoom_cur.get(o["key"]) or {}}
                    for o in style_objects.OBJECTS]
            groups = [{"key": g["key"], "label": g["label"], "cats": g["cats"],
                       "color": poi.get(g["key"]) or g["default"]} for g in style_objects.POI_GROUPS]
            labels = {o["key"]: o["label"] for o in style_objects.OBJECTS}
            fonts = {"available": style_objects.available_fonts(str(ROOT / "style/glyphs")),
                     "current": fonts_cur,
                     "labels": [{"key": k, "label": labels.get(k, k)} for k in style_objects.FONT_LABELS]}
            opa = [{"key": t["key"], "label": t["label"], "targets": t["targets"],
                    "value": opacity_cur.get(t["key"], 1.0)} for t in style_objects.opacity_objects()]
            return self._json({"objects": objs, "poi_groups": groups, "fonts": fonts,
                               "opacity_objects": opa,
                               "presets": style_objects.PRESETS, "poi_layer": style_objects.POI_LAYER,
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
                if not isinstance(imported.get("layers"), list):
                    return self._json({"error": "style.json 형식 아님(layers 배열 없음)"}, 400)
                colors = {**style_objects.current_colors(imported), **style_objects.current_poi_colors(imported)}
            except Exception as e:
                return self._json({"error": f"style.json 파싱 실패: {e}"}, 400)
            res = apply_style_theme({k: v for k, v in colors.items() if v})
            res["colors"] = colors
            return self._json(res)
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
   <h2>객체</h2><div id=rows></div>
   <h2>시설(POI) 업종 대분류별</h2><div id=poirows></div>
   <h2>노출 레벨(줌) · min–max <span id=zlevel style="color:#5b9bd5;font-weight:500;margin-left:4px">현재 z 14.5</span></h2><div id=zoomrows></div>
   <h2>투명도(불투명도) %</h2><div id=oprows></div>
   <p class=hint>색을 바꾸면 미리보기에 즉시 반영. 팔레트(색칸 클릭) 또는 #색상값 입력 모두 가능.
   <br>‘저장 & 적용’ → style.json 기록 + 타일서버 재시작(영구 반영).</p>
 </div>
 <div id=map></div>
</div>
<script src="/vendor/maplibre/maplibre-gl.js"></script>
<script>
 const $=s=>document.querySelector(s); let OBJ=[], POI=[], PRE={}, POILAYER='poi-dot', map=null, INIT={}, INITZ={}, OPA=[], INITO={}, TPORT=8080;
 const TOKEN=new URLSearchParams(location.search).get('token')||'';
 function post(url,body){return fetch(url,{method:'POST',headers:{'Content-Type':'application/json','X-Studio-Token':TOKEN},body});}
 fetch('/api/style/objects').then(r=>r.json()).then(d=>{
   OBJ=d.objects; POI=d.poi_groups||[]; PRE=d.presets||{}; POILAYER=d.poi_layer||'poi-dot'; TPORT=d.tile_port;
   $('#rows').innerHTML=OBJ.map(row).join('');
   $('#poirows').innerHTML=POI.map(row).join('');
   $('#zoomrows').innerHTML=OBJ.map(zrow).join('');
   OBJ.forEach(o=>{ INIT[o.key]=o.color; wire(o,v=>apply(o,v)); });
   POI.forEach(o=>{ INIT[o.key]=o.color; wire(o,()=>applyPoi()); });
   OBJ.forEach(o=>{ INITZ[o.key]={min:(o.zoom||{}).min??'',max:(o.zoom||{}).max??''}; wireZoom(o); });
   $('#oprows').innerHTML=(OPA=d.opacity_objects||[]).map(orow).join('');
   OPA.forEach(o=>{ INITO[o.key]=Math.round((o.value==null?1:o.value)*100); wireOpacity(o); });
   map=new maplibregl.Map({container:'map',
     style:`http://${location.hostname}:${TPORT}/styles/cuvia/style.json`,
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
 function poiExpr(){ const e=['match',['get','cat1']]; let fb='#9aa6b2';
   POI.forEach(g=>{ const v=$('#c_'+g.key).value; if(!g.cats||!g.cats.length){fb=v;return;} e.push(g.cats.slice(),v); });
   e.push(fb); return e; }
 function applyPoi(){ if(!map||!map.isStyleLoaded())return; try{map.setPaintProperty(POILAYER,'circle-color',poiExpr())}catch(e){} }
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
   POI.forEach(o=>setColor(o.key,INIT[o.key]));
   OBJ.forEach(o=>{const i=INITZ[o.key]||{}; $('#zmin_'+o.key).value=i.min??''; $('#zmax_'+o.key).value=i.max??'';});
   OPA.forEach(o=>{const e=$('#o_'+o.key); if(e){e.value=INITO[o.key]; const r=$('#ov_'+o.key); if(r)r.textContent=INITO[o.key]+'%';}});
   map.setStyle(`http://${location.hostname}:${TPORT}/styles/cuvia/style.json`,{diff:false});
   $('#status').textContent='되돌림 — 서버 스타일 다시 로드'; };
 $('#exp').onclick=()=>location.href='/api/style/export';
 $('#imp').onclick=()=>$('#impf').click();
 $('#impf').onchange=e=>{const f=e.target.files[0]; if(!f)return; e.target.value='';
   $('#status').textContent='가져오는 중…';
   f.text().then(t=>post('/api/style/import',t))
    .then(r=>r.json()).then(d=>{ if(!d.ok){$('#status').textContent='✗ '+(d.error||'오류');return;}
      OBJ.forEach(o=>{const v=d.colors[o.key]; if(v){setColor(o.key,v); apply(o,v);}});
      POI.forEach(o=>{const v=d.colors[o.key]; if(v)setColor(o.key,v);}); applyPoi();
      $('#status').textContent=`✓ 가져옴 ${d.applied}개 · 타일서버 ${d.reloaded}`;
    }).catch(err=>$('#status').textContent='✗ 실패: '+err); };
 $('#save').onclick=()=>{
   const theme={}; OBJ.forEach(o=>theme[o.key]=$('#c_'+o.key).value); POI.forEach(o=>theme[o.key]=$('#c_'+o.key).value);
   const z=zoomChanged(); if(Object.keys(z).length) theme.zoom=z;
   const op=opacityChanged(); if(Object.keys(op).length) theme.opacity=op;
   $('#status').textContent='적용 중…';
   post('/api/style',JSON.stringify({theme})).then(r=>r.json()).then(d=>{
     if(d.ok){  // 적용 성공 → 현재값을 새 기준선으로(되돌리기 일관성)
       OBJ.forEach(o=>INIT[o.key]=$('#c_'+o.key).value); POI.forEach(o=>INIT[o.key]=$('#c_'+o.key).value);
       OBJ.forEach(o=>{INITZ[o.key]={min:$('#zmin_'+o.key).value,max:$('#zmax_'+o.key).value};});
       OPA.forEach(o=>{INITO[o.key]=+$('#o_'+o.key).value;});
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
