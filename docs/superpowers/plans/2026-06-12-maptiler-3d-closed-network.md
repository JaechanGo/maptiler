# 폐쇄망 3D 지도 서비스 구현 계획

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** MapTiler Cloud를 폐쇄망 자체 서버(TileServer-GL)로 드롭인 교체하는 한국 전체 3D 지도 서비스 — 벡터타일(Planetiler) + 지형타일(SRTM→Terrain-RGB) + 스타일/글리프 + 데모/연동가이드 + 패키징.

**Architecture:** 인터넷 Mac에서 모든 빌드(①데이터 수집 → ②벡터타일 → ③지형타일 → ④스타일/에셋 → ⑤패키징) 후 산출물만 폐쇄망(Docker x86_64)에 반입해 실행. 스타일은 `style/base.json` + `style/layers/*.json` 조각을 `build_style.py`가 조립(생성물 `style/style.json`은 gitignore). 브랜치는 main(토대) → feature/3d-building, feature/3d-terrain → main 병합.

**Tech Stack:** Planetiler(Java 21), GDAL, rio-rgbify, TileServer-GL(Docker), nginx, MapLibre GL JS 5.16, Maputnik, Python3(스타일 조립).

**스펙:** `docs/superpowers/specs/2026-06-12-maptiler-3d-closed-network-design.md`

**전제:** 저장소 루트 = `/Users/jaechango/Library/Mobile Documents/com~apple~CloudDocs/maptiler` (경로에 공백 포함 — 모든 스크립트는 변수를 따옴표로 감쌀 것). 원격 = `https://github.com/JaechanGo/maptiler.git` (gh HTTPS 인증 설정 완료).

> ⚠️ **iCloud Drive 주의:** 이 저장소는 iCloud Drive 안에 있어 `data/`·`tiles/`의 수 GB 파일이 클라우드로 동기화될 수 있다. 디스크/대역폭이 아까우면 Task 2 시작 전에 `mkdir -p ~/cuvia-map-data/{data,tiles} && ln -s ~/cuvia-map-data/data data && ln -s ~/cuvia-map-data/tiles tiles` 로 외부 디렉토리에 심볼릭 링크를 걸어도 된다(선택사항, 스크립트는 양쪽 모두 동작).

---

## 검증 좌표 참고표 (서울시청 [126.978, 37.5665] 기준)

| 줌 | x | y | 용도 |
|----|------|------|------|
| z14 | 13970 | 6344 | 벡터 타일 존재 확인 |
| z10 | 873 | 396 | 지형 타일 존재 확인 |

---

### Task 1: 사전 도구 점검 스크립트 [main]

**Files:**
- Create: `scripts/00-check-prereqs.sh`

- [ ] **Step 1: 스크립트 작성**

```bash
#!/usr/bin/env bash
# 빌드 머신(인터넷 가능한 Mac)에 필요한 도구가 모두 있는지 점검한다.
set -uo pipefail
ok=1
need() {
  if command -v "$1" >/dev/null 2>&1; then
    echo "✓ $1"
  else
    echo "✗ $1 없음 — 설치: $2"; ok=0
  fi
}
need java    "brew install openjdk@21 (Planetiler는 Java 21+ 필요)"
need docker  "Docker Desktop 설치"
need python3 "brew install python"
need gdalwarp "brew install gdal"
need gdalbuildvrt "brew install gdal"
need sqlite3 "macOS 기본 포함"
need jq      "brew install jq"
need curl    "macOS 기본 포함"
need rio     "pipx install rio-rgbify (또는 pip3 install rio-rgbify)"
need git     "xcode-select --install"
echo "--- java 버전(21 이상이어야 함) ---"
java -version 2>&1 | head -1
if [ "$ok" -eq 1 ]; then echo "모든 도구 준비 완료"; else echo "누락 도구를 설치한 뒤 다시 실행하세요"; exit 1; fi
```

- [ ] **Step 2: 실행 권한 부여 후 실행**

Run: `chmod +x scripts/00-check-prereqs.sh && ./scripts/00-check-prereqs.sh`
Expected: 모든 항목 `✓`, 마지막 줄 `모든 도구 준비 완료`. `✗`가 있으면 안내된 brew/pipx 명령으로 설치 후 재실행(통과할 때까지 다음 Task로 넘어가지 않는다).

- [ ] **Step 3: 커밋**

```bash
git add scripts/00-check-prereqs.sh
git commit -m "feat: 빌드 도구 사전 점검 스크립트 추가"
```

---

### Task 2: 데이터/에셋 다운로드 스크립트 [main]

**Files:**
- Create: `scripts/01-download-data.sh`

- [ ] **Step 1: 스크립트 작성**

```bash
#!/usr/bin/env bash
# [온라인 단계] 폐쇄망 반입에 필요한 모든 원본/에셋을 내려받는다.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
mkdir -p "$ROOT/planetiler" "$ROOT/data/osm" "$ROOT/data/dem/hgt" "$ROOT/tiles" \
         "$ROOT/vendor/maplibre" "$ROOT/vendor/maputnik" "$ROOT/style/glyphs"

echo "[1/5] Planetiler jar"
[ -f "$ROOT/planetiler/planetiler.jar" ] || curl -fL -o "$ROOT/planetiler/planetiler.jar" \
  https://github.com/onthegomap/planetiler/releases/latest/download/planetiler.jar

echo "[2/5] OSM 한국 추출본 (Geofabrik, ~150MB)"
[ -f "$ROOT/data/osm/south-korea.osm.pbf" ] || curl -fL -o "$ROOT/data/osm/south-korea.osm.pbf" \
  https://download.geofabrik.de/asia/south-korea-latest.osm.pbf

echo "[3/5] 글리프 폰트 (KlokanTech Noto Sans — 한글 포함)"
if [ ! -d "$ROOT/style/glyphs/KlokanTech Noto Sans Regular" ]; then
  git clone --depth 1 https://github.com/klokantech/klokantech-gl-fonts /tmp/kfonts
  cp -R "/tmp/kfonts/KlokanTech Noto Sans Regular" "$ROOT/style/glyphs/"
  cp -R "/tmp/kfonts/KlokanTech Noto Sans Bold" "$ROOT/style/glyphs/" 2>/dev/null || true
  rm -rf /tmp/kfonts
fi

echo "[4/5] MapLibre GL JS (로컬 번들, 소비 프론트와 동일 메이저)"
[ -f "$ROOT/vendor/maplibre/maplibre-gl.js" ] || curl -fL -o "$ROOT/vendor/maplibre/maplibre-gl.js" \
  https://unpkg.com/maplibre-gl@5.16.0/dist/maplibre-gl.js
[ -f "$ROOT/vendor/maplibre/maplibre-gl.css" ] || curl -fL -o "$ROOT/vendor/maplibre/maplibre-gl.css" \
  https://unpkg.com/maplibre-gl@5.16.0/dist/maplibre-gl.css

echo "[5/5] Maputnik (오프라인 스타일 편집기)"
if [ ! -f "$ROOT/vendor/maputnik/index.html" ]; then
  if curl -fL -o /tmp/maputnik.zip https://github.com/maplibre/maputnik/releases/latest/download/maputnik.zip; then
    unzip -oq /tmp/maputnik.zip -d "$ROOT/vendor/maputnik" && rm /tmp/maputnik.zip
  else
    echo "⚠ maputnik.zip 자동 다운로드 실패 — https://github.com/maplibre/maputnik/releases 에서 정적 빌드 zip을 받아 vendor/maputnik/ 에 풀어주세요 (선택 항목, 빌드는 계속 진행 가능)"
  fi
fi
echo "다운로드 완료"
```

- [ ] **Step 2: 실행**

Run: `chmod +x scripts/01-download-data.sh && ./scripts/01-download-data.sh`
Expected: `다운로드 완료`. (수 분 소요. Maputnik만 실패 시 ⚠ 경고와 함께 계속 진행 가능.)

- [ ] **Step 3: 산출물 검증 — 특히 한글 글리프**

```bash
ls -lh data/osm/south-korea.osm.pbf planetiler/planetiler.jar vendor/maplibre/maplibre-gl.js
# 한글 '가'(U+AC00) 범위 글리프가 실제로 차 있는지 (비어있으면 ~수백 byte)
f="style/glyphs/KlokanTech Noto Sans Regular/44032-44287.pbf"
[ "$(stat -f%z "$f")" -gt 5000 ] && echo "한글 글리프 OK" || echo "한글 글리프 비어있음 — 폰트 소스 재확인 필요"
```

Expected: 세 파일 모두 존재(pbf ~150MB), 마지막 줄 `한글 글리프 OK`.

- [ ] **Step 4: 커밋 (코드만 — 대용량 데이터는 gitignore로 제외됨)**

```bash
git add scripts/01-download-data.sh
git commit -m "feat: 원본 데이터/에셋 다운로드 스크립트 추가"
```

---

### Task 3: 벡터 타일 생성 (Planetiler) [main]

**Files:**
- Create: `scripts/02-gen-vector.sh`

- [ ] **Step 1: 스크립트 작성**

```bash
#!/usr/bin/env bash
# [온라인 단계] OSM 추출본 → OpenMapTiles 스키마 벡터타일(.mbtiles)
# --download: Natural Earth/수역 폴리곤 등 보조 데이터 자동 다운로드(최초 1회, ~1GB)
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
java -Xmx6g -jar planetiler/planetiler.jar \
  --osm_path="data/osm/south-korea.osm.pbf" \
  --output="tiles/korea.mbtiles" \
  --download --force
echo "벡터 타일 생성 완료: tiles/korea.mbtiles"
```

- [ ] **Step 2: 실행**

Run: `chmod +x scripts/02-gen-vector.sh && ./scripts/02-gen-vector.sh`
Expected: Planetiler 진행 로그 후 `Finished in ...` 및 `벡터 타일 생성 완료`. (최초 실행은 보조 데이터 다운로드 포함 10~30분, 이후 수 분.)

- [ ] **Step 3: 검증 — 타일 수와 3D 핵심 속성 `render_height`**

```bash
sqlite3 tiles/korea.mbtiles "SELECT count(*) FROM tiles;"
sqlite3 tiles/korea.mbtiles "SELECT value FROM metadata WHERE name='json';" \
  | jq -r '.vector_layers[] | select(.id=="building") | .fields | keys[]' | grep render_height
```

Expected: 타일 수 수만~수십만 행, 두 번째 명령 출력에 `render_height`. **이게 안 나오면 3D 건물이 불가능하므로 여기서 멈추고 Planetiler 버전/로그를 확인한다.**

- [ ] **Step 4: 커밋**

```bash
git add scripts/02-gen-vector.sh
git commit -m "feat: Planetiler 벡터 타일 생성 스크립트 추가"
```

---

### Task 4: 베이스 스타일 + 스타일 조립기 [main]

**Files:**
- Create: `style/base.json` (다크 베이스 스타일 — 소비 프론트 대시보드가 다크 테마)
- Create: `style/layers/.gitkeep` (조각 디렉토리 — 브랜치들이 채움)
- Create: `scripts/build_style.py`
- Create: `scripts/build-style.sh`
- Modify: `.gitignore` (생성물 `style/style.json` 제외)

- [ ] **Step 1: `style/base.json` 작성 (전체 내용)**

```json
{
  "version": 8,
  "name": "CUVIA Base (Dark)",
  "glyphs": "{fontstack}/{range}.pbf",
  "sources": {
    "openmaptiles": {
      "type": "vector",
      "url": "mbtiles://{korea}",
      "attribution": "© OpenMapTiles © OpenStreetMap contributors"
    }
  },
  "layers": [
    { "id": "background", "type": "background",
      "paint": { "background-color": "#0b0e13" } },
    { "id": "landcover", "type": "fill", "source": "openmaptiles", "source-layer": "landcover",
      "filter": ["in", ["get", "class"], ["literal", ["grass", "wood", "forest", "scrub", "farmland"]]],
      "paint": { "fill-color": "#121a14", "fill-opacity": 0.6 } },
    { "id": "park", "type": "fill", "source": "openmaptiles", "source-layer": "park",
      "paint": { "fill-color": "#132016", "fill-opacity": 0.7 } },
    { "id": "water", "type": "fill", "source": "openmaptiles", "source-layer": "water",
      "paint": { "fill-color": "#0e2233" } },
    { "id": "waterway", "type": "line", "source": "openmaptiles", "source-layer": "waterway",
      "paint": { "line-color": "#0e2233",
        "line-width": ["interpolate", ["linear"], ["zoom"], 8, 0.5, 14, 2] } },
    { "id": "boundary", "type": "line", "source": "openmaptiles", "source-layer": "boundary",
      "filter": ["<=", ["get", "admin_level"], 4],
      "paint": { "line-color": "#2c3542", "line-width": 1, "line-dasharray": [3, 2] } },
    { "id": "railway", "type": "line", "source": "openmaptiles", "source-layer": "transportation",
      "filter": ["==", ["get", "class"], "rail"], "minzoom": 11,
      "paint": { "line-color": "#232a35", "line-width": 1.2, "line-dasharray": [4, 3] } },
    { "id": "road-minor", "type": "line", "source": "openmaptiles", "source-layer": "transportation",
      "filter": ["in", ["get", "class"], ["literal", ["minor", "service", "track", "path"]]],
      "minzoom": 12,
      "paint": { "line-color": "#1d242f",
        "line-width": ["interpolate", ["exponential", 1.5], ["zoom"], 12, 0.5, 18, 12] } },
    { "id": "road-secondary", "type": "line", "source": "openmaptiles", "source-layer": "transportation",
      "filter": ["in", ["get", "class"], ["literal", ["secondary", "tertiary"]]],
      "minzoom": 9,
      "paint": { "line-color": "#273140",
        "line-width": ["interpolate", ["exponential", 1.5], ["zoom"], 9, 0.7, 18, 20] } },
    { "id": "road-primary", "type": "line", "source": "openmaptiles", "source-layer": "transportation",
      "filter": ["in", ["get", "class"], ["literal", ["primary", "trunk"]]],
      "minzoom": 7,
      "paint": { "line-color": "#2e3a4d",
        "line-width": ["interpolate", ["exponential", 1.5], ["zoom"], 7, 1, 18, 26] } },
    { "id": "road-motorway", "type": "line", "source": "openmaptiles", "source-layer": "transportation",
      "filter": ["==", ["get", "class"], "motorway"],
      "paint": { "line-color": "#3a4a66",
        "line-width": ["interpolate", ["exponential", 1.5], ["zoom"], 5, 1, 18, 30] } },
    { "id": "building-2d", "type": "fill", "source": "openmaptiles", "source-layer": "building",
      "minzoom": 13,
      "paint": { "fill-color": "#161c25", "fill-outline-color": "#222b38" } },
    { "id": "road-label", "type": "symbol", "source": "openmaptiles", "source-layer": "transportation_name",
      "minzoom": 14,
      "layout": { "symbol-placement": "line",
        "text-field": ["coalesce", ["get", "name:ko"], ["get", "name"]],
        "text-font": ["KlokanTech Noto Sans Regular"], "text-size": 11 },
      "paint": { "text-color": "#8b96a5", "text-halo-color": "#0b0e13", "text-halo-width": 1 } },
    { "id": "place-label", "type": "symbol", "source": "openmaptiles", "source-layer": "place",
      "filter": ["in", ["get", "class"], ["literal", ["city", "town", "village", "suburb", "quarter", "neighbourhood"]]],
      "layout": { "text-field": ["coalesce", ["get", "name:ko"], ["get", "name"]],
        "text-font": ["KlokanTech Noto Sans Regular"],
        "text-size": ["interpolate", ["linear"], ["zoom"], 6, 11, 14, 16] },
      "paint": { "text-color": "#aeb9c8", "text-halo-color": "#0b0e13", "text-halo-width": 1.2 } }
  ]
}
```

설계 의도: 스프라이트(아이콘) 사용 레이어가 없으므로 sprite 속성 자체를 생략 → `styleimagemissing` 자체가 발생하지 않음(스펙 5절 충족). 라벨은 `name:ko` 우선.

- [ ] **Step 2: `scripts/build_style.py` 작성**

```python
#!/usr/bin/env python3
"""style/base.json + style/layers/*.json 조각 → style/style.json 조립.

조각 형식: {"sources": {...}, "layers": [...], "set": {최상위키: 값}}
- sources는 병합, layers는 뒤에 추가(= 위에 그려짐), set은 최상위 키 설정.
조각 파일은 파일명 순으로 적용된다.
"""
import json
import pathlib

root = pathlib.Path(__file__).resolve().parents[1] / "style"
style = json.loads((root / "base.json").read_text(encoding="utf-8"))
for frag_path in sorted((root / "layers").glob("*.json")):
    frag = json.loads(frag_path.read_text(encoding="utf-8"))
    style.setdefault("sources", {}).update(frag.get("sources", {}))
    style["layers"].extend(frag.get("layers", []))
    for key, value in frag.get("set", {}).items():
        style[key] = value
    print(f"적용: {frag_path.name}")
out = root / "style.json"
out.write_text(json.dumps(style, ensure_ascii=False, indent=2), encoding="utf-8")
print(f"OK: {out} (layers={len(style['layers'])})")
```

- [ ] **Step 3: `scripts/build-style.sh` 작성 (셸 래퍼 — 다른 스크립트와 호출 일관성)**

```bash
#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
python3 "$ROOT/scripts/build_style.py"
```

- [ ] **Step 4: `.gitignore`에 생성물 추가**

`.gitignore` 끝에 다음 줄 추가:

```
# build-style.sh 가 생성 (조각 병합 결과 — 브랜치 병합 충돌 방지 위해 비커밋)
style/style.json
```

- [ ] **Step 5: 조립 실행 + 검증**

```bash
mkdir -p style/layers && touch style/layers/.gitkeep
chmod +x scripts/build-style.sh
./scripts/build-style.sh
jq '.layers | length' style/style.json
jq -r '.. | strings | select(test("^https?://"))' style/style.json
```

Expected: `OK: ... (layers=14)`, 레이어 수 `14`, 마지막 명령 **출력 없음**(외부 URL 0건 — 폐쇄망 적합성).

- [ ] **Step 6: 커밋**

```bash
git add style/base.json style/layers/.gitkeep scripts/build_style.py scripts/build-style.sh .gitignore
git commit -m "feat: 다크 베이스 스타일 및 조각 조립기 추가"
```

---

### Task 5: TileServer-GL 서버 구성 [main]

**Files:**
- Create: `server/tileserver-config.json`
- Create: `server/docker-compose.yml`

- [ ] **Step 1: `server/tileserver-config.json` 작성**

```json
{
  "options": {
    "paths": {
      "root": "/data",
      "fonts": "glyphs",
      "styles": "styles",
      "mbtiles": "tiles"
    }
  },
  "styles": {
    "cuvia": {
      "style": "cuvia/style.json",
      "tilejson": { "bounds": [124.5, 33.0, 131.0, 38.7] }
    }
  },
  "data": {
    "korea": { "mbtiles": "korea.mbtiles" }
  }
}
```

- [ ] **Step 2: `server/docker-compose.yml` 작성**

```yaml
services:
  tileserver:
    image: maptiler/tileserver-gl:latest
    platform: linux/amd64        # 폐쇄망 x86_64와 동일 아키텍처 강제 (Apple Silicon에서도 에뮬레이션 구동)
    restart: unless-stopped
    ports:
      - "8080:8080"
    volumes:
      - ../tiles:/data/tiles:ro
      - ../style/glyphs:/data/glyphs:ro
      - ../style:/data/styles/cuvia:ro
      - ./tileserver-config.json:/data/config.json:ro
    command: ["--config", "/data/config.json"]

  demo:
    image: nginx:alpine
    platform: linux/amd64
    restart: unless-stopped
    ports:
      - "8081:80"
    volumes:
      - ../demo:/usr/share/nginx/html/demo:ro
      - ../vendor:/usr/share/nginx/html/vendor:ro
```

- [ ] **Step 3: 기동 + 검증**

```bash
./scripts/build-style.sh   # style.json 최신화
( cd server && docker compose up -d )
sleep 5
curl -sf http://localhost:8080/styles/cuvia/style.json | jq -r '.sources.openmaptiles.url'
curl -sf -o /dev/null -w "%{http_code}\n" "http://localhost:8080/data/korea/14/13970/6344.pbf"
curl -sf -o /dev/null -w "%{http_code}\n" "http://localhost:8080/fonts/KlokanTech%20Noto%20Sans%20Regular/44032-44287.pbf"
```

Expected: 스타일 URL이 `http://localhost:8080/data/korea.json` 형태로 재작성되어 출력(`mbtiles://`가 서빙 시점에 해석됨), 타일·폰트 모두 `200`.

- [ ] **Step 4: 커밋**

```bash
git add server/tileserver-config.json server/docker-compose.yml
git commit -m "feat: TileServer-GL 도커 구성 추가"
```

---

### Task 6: 데모 페이지 [main]

**Files:**
- Create: `demo/index.html`
- Create: `demo/js/map.js`
- Create: `demo/js/buildings.js` (스텁 — feature/3d-building에서 구현)
- Create: `demo/js/terrain.js` (스텁 — feature/3d-terrain에서 구현)
- Create: `demo/js/markers-example.js`

설계 의도: index.html은 **처음부터 4개 js를 모두 로드**하고, 브랜치는 자기 스텁 파일만 교체한다 → 병합 시 공유 파일 충돌 0건.

- [ ] **Step 1: `demo/index.html` 작성**

```html
<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>CUVIA 폐쇄망 지도 데모</title>
<link rel="stylesheet" href="../vendor/maplibre/maplibre-gl.css">
<style>
  html, body, #map { margin: 0; height: 100%; }
  #controls { position: fixed; top: 10px; left: 10px; z-index: 10; display: flex; gap: 8px; }
  .ctl { padding: 8px 12px; background: #1a2029; color: #cdd6e3;
         border: 1px solid #2c3542; border-radius: 6px; cursor: pointer; font-size: 13px; }
</style>
</head>
<body>
<div id="map"></div>
<div id="controls"></div>
<script src="../vendor/maplibre/maplibre-gl.js"></script>
<script src="js/map.js"></script>
<script src="js/buildings.js"></script>
<script src="js/terrain.js"></script>
<script src="js/markers-example.js"></script>
</body>
</html>
```

- [ ] **Step 2: `demo/js/map.js` 작성**

```javascript
// 베이스 지도 초기화. 타일서버 주소는 데모를 띄운 호스트의 8080 포트로 가정한다.
// 다른 서버를 쓰려면 ?server=http://host:port 쿼리로 재지정.
const params = new URLSearchParams(location.search);
const TILESERVER = params.get('server') || `http://${location.hostname}:8080`;

const map = new maplibregl.Map({
  container: 'map',
  style: `${TILESERVER}/styles/cuvia/style.json`,
  center: [126.978, 37.5665],   // [경도, 위도] — 서울시청
  zoom: 15,
  minZoom: 5,
  maxZoom: 22,
  pitch: 45,
  maxPitch: 75,
});
map.addControl(new maplibregl.NavigationControl({ visualizePitch: true }));
window.cuviaMap = map;   // buildings.js / terrain.js / markers-example.js 가 사용
```

- [ ] **Step 3: 스텁 2개 작성**

`demo/js/buildings.js`:

```javascript
// feature/3d-building 브랜치에서 'Building 3D' 토글로 대체된다.
```

`demo/js/terrain.js`:

```javascript
// feature/3d-terrain 브랜치에서 지형 토글로 대체된다.
```

- [ ] **Step 4: `demo/js/markers-example.js` 작성 (연동 가이드용 예시이자 동작 확인)**

```javascript
// 마커는 본 서비스가 아니라 소비 프론트의 책임이다.
// 아래는 "우리 지도 위에 마커가 얹히는지"를 확인하는 예시 코드.
(function () {
  const map = window.cuviaMap;

  // 소량: DOM 마커 — 좌표 순서는 [경도, 위도]!
  new maplibregl.Marker({ color: '#e85c2a' })
    .setLngLat([126.9779, 37.5663])
    .setPopup(new maplibregl.Popup().setHTML('<b>서울시청</b>'))
    .addTo(map);

  // 대량: GeoJSON 소스 + 클러스터링 (수천~수만 개 권장 방식)
  map.on('load', () => {
    const pts = {
      type: 'FeatureCollection',
      features: Array.from({ length: 500 }, (_, i) => ({
        type: 'Feature',
        geometry: { type: 'Point',
          coordinates: [126.9 + Math.random() * 0.2, 37.45 + Math.random() * 0.15] },
        properties: { id: i },
      })),
    };
    map.addSource('demo-points', { type: 'geojson', data: pts, cluster: true, clusterRadius: 40 });
    map.addLayer({
      id: 'demo-clusters', type: 'circle', source: 'demo-points',
      paint: {
        'circle-color': '#4d7cfe', 'circle-opacity': 0.8,
        'circle-radius': ['case', ['has', 'point_count'],
          ['+', 10, ['*', 2, ['sqrt', ['get', 'point_count']]]], 5],
      },
    });
    map.addLayer({
      id: 'demo-cluster-count', type: 'symbol', source: 'demo-points',
      filter: ['has', 'point_count'],
      layout: {
        'text-field': ['to-string', ['get', 'point_count']],
        'text-font': ['KlokanTech Noto Sans Regular'], 'text-size': 11,
      },
      paint: { 'text-color': '#ffffff' },
    });
  });
})();
```

- [ ] **Step 5: 검증 (서버는 Task 5에서 기동된 상태)**

```bash
curl -sf -o /dev/null -w "%{http_code}\n" http://localhost:8081/demo/
grep -RnE "https?://" demo/ | grep -v 'location.hostname'
open http://localhost:8081/demo/
```

Expected: `200`, grep **출력 없음**(외부 URL 0건). 브라우저에서: 다크 지도 + 한글 라벨(서울/도로명) + 주황 마커 + 파란 클러스터 원이 보임. DevTools Network에서 localhost 외 요청 0건.

- [ ] **Step 6: 커밋**

```bash
git add demo/
git commit -m "feat: MapLibre 데모 페이지 추가 (마커 예시 포함)"
```

---

### Task 7: 소비 프론트 연동 가이드 [main]

**Files:**
- Create: `docs/integration-guide.md`

- [ ] **Step 1: 가이드 작성**

````markdown
# 소비 프론트엔드 연동 가이드

본 지도 서비스는 MapTiler Cloud의 드롭인 대체다. 변경은 **style URL 한 줄**이다.

## 1. URL 교체

```diff
 const map = new maplibregl.Map({
-  style: 'https://api.maptiler.com/maps/<맵ID>/style.json?key=<API키>',
+  style: 'http://<사내서버>:8080/styles/cuvia/style.json',   // API 키 불필요
   center: [126.989, 37.426], zoom: 15, minZoom: 9, maxZoom: 22,
   pitch: 45, bearing: 0,
 });
```

center/zoom/pitch/bearing 등 나머지 옵션은 기존 값을 그대로 쓰면 된다.

## 2. 기존 코드와의 호환성

- `map.setLayerZoomRange('Building 3D', 12, 24)` — 본 스타일에 `Building 3D`
  fill-extrusion 레이어가 동일 이름으로 존재하므로 그대로 동작한다.
- 건물 높이 속성: 벡터 타일 `building` 레이어에 `render_height` / `render_min_height`
  가 들어 있다. (`height`, `building:levels` 원본 태그는 타일에 포함되지 않으므로
  기존 case 식의 `render_height` 분기가 사용된다.)
- `map.setTerrain(null)` — 지형은 기본 비활성이다. 켜려면:
  `map.setTerrain({ source: 'terrain', exaggeration: 1.3 })`
  주의: 지형 활성 시 원거리 3D 건물이 가려지는 MapLibre 특성이 있다(기존 코드가
  지형을 꺼둔 이유). 화면 용도에 따라 토글하라.
- 스타일에 아이콘(sprite) 레이어가 없어 `styleimagemissing` 은 발생하지 않는다.

## 3. 마커 찍기 (소비 프론트 책임)

**좌표 순서는 `[경도, 위도]`다.** Leaflet의 `[lat, lng]` 와 반대이니 주의.

소량(수십~수백): DOM 마커

```js
new maplibregl.Marker()
  .setLngLat([127.0, 37.5])            // [lng, lat]
  .setPopup(new maplibregl.Popup().setHTML('<b>이름</b>'))
  .addTo(map);
```

대량(수천~수만): GeoJSON 소스 + 클러스터링 — `demo/js/markers-example.js` 참고.
DB에서 조회한 좌표 배열을 GeoJSON FeatureCollection 으로 변환해 소스 하나로 넣고,
circle/symbol 레이어로 그린다. 갱신은 `map.getSource('id').setData(newGeojson)`.

## 4. 엔드포인트 요약

| 용도 | URL |
|------|-----|
| 스타일 | `http://<서버>:8080/styles/cuvia/style.json` |
| 벡터 타일 TileJSON | `http://<서버>:8080/data/korea.json` |
| 지형 타일 TileJSON | `http://<서버>:8080/data/terrain.json` |
| 글리프 | `http://<서버>:8080/fonts/{fontstack}/{range}.pbf` |
| 데모 | `http://<서버>:8081/demo/` |
| Maputnik(스타일 편집) | `http://<서버>:8081/vendor/maputnik/` |

## 5. 보안 메모

폐쇄망 전환 후 기존 프론트 소스의 MapTiler API 키 문자열은 더 이상 필요 없다.
공개 저장소에 노출된 키는 MapTiler 콘솔에서 폐기할 것.
````

- [ ] **Step 2: 커밋**

```bash
git add docs/integration-guide.md
git commit -m "docs: 소비 프론트 연동 가이드 추가"
```

---

### Task 8: 패키징/배포 스크립트 + main 푸시 [main]

**Files:**
- Create: `scripts/package.sh`
- Create: `scripts/deploy.sh`

- [ ] **Step 1: `scripts/package.sh` 작성**

```bash
#!/usr/bin/env bash
# [온라인 단계] 폐쇄망 반입용 번들 생성: Docker 이미지 tar + 산출물 tgz
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DIST="$ROOT/dist"
mkdir -p "$DIST"

echo "[1/3] 스타일 조립(최신화)"
"$ROOT/scripts/build-style.sh"

echo "[2/3] Docker 이미지 (linux/amd64 강제 — 폐쇄망 x86_64 용)"
docker pull --platform linux/amd64 maptiler/tileserver-gl:latest
docker pull --platform linux/amd64 nginx:alpine
docker save -o "$DIST/images.tar" maptiler/tileserver-gl:latest nginx:alpine

echo "[3/3] 산출물 번들"
tar -czf "$DIST/cuvia-map-bundle.tgz" -C "$ROOT" \
  tiles style demo vendor server scripts/deploy.sh docs/integration-guide.md
ls -lh "$DIST"
echo "반입 대상 2개: dist/images.tar, dist/cuvia-map-bundle.tgz"
```

- [ ] **Step 2: `scripts/deploy.sh` 작성 (폐쇄망 서버에서 실행)**

```bash
#!/usr/bin/env bash
# [폐쇄망 단계] 번들 압축 해제 디렉토리에서 실행:
#   tar xzf cuvia-map-bundle.tgz && ./scripts/deploy.sh /path/to/images.tar
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
IMAGES_TAR="${1:-$ROOT/images.tar}"

if [ -f "$IMAGES_TAR" ]; then
  echo "Docker 이미지 적재: $IMAGES_TAR"
  docker load -i "$IMAGES_TAR"
else
  echo "⚠ images.tar 경로를 인자로 주세요 (이미 load 했다면 무시)"
fi

cd "$ROOT/server" && docker compose up -d
echo "기동 완료:"
echo "  스타일  http://<이서버IP>:8080/styles/cuvia/style.json"
echo "  데모    http://<이서버IP>:8081/demo/"
```

- [ ] **Step 3: 패키징 실행 + 번들 내용 검증**

```bash
chmod +x scripts/package.sh scripts/deploy.sh && ./scripts/package.sh
tar -tzf dist/cuvia-map-bundle.tgz | grep -E "korea.mbtiles|style.json|deploy.sh|index.html" | head
```

Expected: `images.tar`(수백 MB)와 `cuvia-map-bundle.tgz` 생성, 목록에 `tiles/korea.mbtiles`, `style/style.json`, `scripts/deploy.sh`, `demo/index.html` 포함.

- [ ] **Step 4: 커밋 + main 푸시**

```bash
git add scripts/package.sh scripts/deploy.sh
git commit -m "feat: 폐쇄망 반입 패키징/배포 스크립트 추가"
git push origin main
```

---

### Task 9: 3D 건물 [feature/3d-building]

**Files:**
- Create: `style/layers/buildings-3d.json`
- Modify: `demo/js/buildings.js` (스텁 → 구현 교체)

- [ ] **Step 1: 브랜치 생성**

```bash
git checkout main && git checkout -b feature/3d-building
```

- [ ] **Step 2: `style/layers/buildings-3d.json` 작성**

레이어 이름 `Building 3D`는 소비 프론트의 `setLayerZoomRange('Building 3D', ...)` 호환을 위한 **고정 계약**이다.

```json
{
  "layers": [
    {
      "id": "Building 3D",
      "type": "fill-extrusion",
      "source": "openmaptiles",
      "source-layer": "building",
      "minzoom": 13,
      "paint": {
        "fill-extrusion-color": "#222b38",
        "fill-extrusion-height": ["coalesce", ["get", "render_height"], 15],
        "fill-extrusion-base": ["coalesce", ["get", "render_min_height"], 0],
        "fill-extrusion-opacity": 0.85
      }
    }
  ]
}
```

- [ ] **Step 3: `demo/js/buildings.js` 를 구현으로 교체 (전체 내용)**

```javascript
// 'Building 3D' fill-extrusion 레이어 표시 토글.
(function () {
  const map = window.cuviaMap;
  const btn = document.createElement('button');
  btn.className = 'ctl';
  btn.textContent = '3D 건물 ON';
  let visible = true;
  btn.onclick = () => {
    visible = !visible;
    map.setLayoutProperty('Building 3D', 'visibility', visible ? 'visible' : 'none');
    btn.textContent = visible ? '3D 건물 ON' : '3D 건물 OFF';
  };
  document.getElementById('controls').appendChild(btn);
})();
```

- [ ] **Step 4: 스타일 재조립 + 검증**

```bash
./scripts/build-style.sh
jq -r '.layers[] | select(.id=="Building 3D") | .type' style/style.json
( cd server && docker compose restart tileserver )
sleep 5
curl -sf http://localhost:8080/styles/cuvia/style.json | jq -r '.layers[] | select(.id=="Building 3D") | .type'
open http://localhost:8081/demo/
```

Expected: 두 jq 모두 `fill-extrusion`. 브라우저에서 pitch 45° 화면에 건물이 높이대로 솟아 있고, `3D 건물` 버튼 토글이 동작.

- [ ] **Step 5: 커밋 + 푸시**

```bash
git add style/layers/buildings-3d.json demo/js/buildings.js
git commit -m "feat(3d-building): Building 3D fill-extrusion 레이어 및 데모 토글"
git push -u origin feature/3d-building
```

---

### Task 10: 지형 타일 생성 [feature/3d-terrain]

**Files:**
- Create: `scripts/03-gen-terrain.sh`

- [ ] **Step 1: 브랜치 생성 (main에서 분기 — 3d-building과 독립)**

```bash
git checkout main && git checkout -b feature/3d-terrain
```

- [ ] **Step 2: `scripts/03-gen-terrain.sh` 작성**

```bash
#!/usr/bin/env bash
# [온라인 단계] SRTM 30m (AWS elevation-tiles-prod, 인증 불필요)
#   → GDAL 병합/웹메르카토르 투영 → rio-rgbify Terrain-RGB mbtiles
# 한국 범위: 위도 N33~N38, 경도 E124~E131 (48타일, ~1GB)
# 추후 국토지리정보원 DEM 으로 교체 시: data/dem/hgt 대신 GeoTIFF 를
# gdalbuildvrt 입력으로 주면 이후 단계는 동일하다(출처 독립 설계).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
HGT="$ROOT/data/dem/hgt"
mkdir -p "$HGT"

echo "[1/4] SRTM HGT 다운로드"
for lat in 33 34 35 36 37 38; do
  for lon in 124 125 126 127 128 129 130 131; do
    f="N${lat}E${lon}.hgt"
    [ -f "$HGT/$f" ] && continue
    if curl -sfL "https://s3.amazonaws.com/elevation-tiles-prod/skadi/N${lat}/N${lat}E${lon}.hgt.gz" \
         -o /tmp/dem-tile.hgt.gz; then
      gunzip -c /tmp/dem-tile.hgt.gz > "$HGT/$f"
      echo "  ✓ $f"
    else
      echo "  - $f (해당 구역 데이터 없음 — 바다 구역은 정상)"
    fi
  done
done

echo "[2/4] 병합(VRT) 및 EPSG:3857 투영"
gdalbuildvrt "$ROOT/data/dem/korea.vrt" "$HGT"/*.hgt
gdalwarp -overwrite -t_srs EPSG:3857 -r bilinear -multi \
  -co COMPRESS=DEFLATE -co BIGTIFF=YES \
  "$ROOT/data/dem/korea.vrt" "$ROOT/data/dem/korea-3857.tif"

echo "[3/4] Terrain-RGB 인코딩 (z5~z12)"
rm -f "$ROOT/tiles/terrain.mbtiles"
rio rgbify -b -10000 -i 0.1 --min-z 5 --max-z 12 \
  -j "$(sysctl -n hw.ncpu 2>/dev/null || nproc)" --format png \
  "$ROOT/data/dem/korea-3857.tif" "$ROOT/tiles/terrain.mbtiles"

echo "[4/4] mbtiles 메타데이터 보강 (TileServer-GL 서빙용)"
sqlite3 "$ROOT/tiles/terrain.mbtiles" <<'SQL'
INSERT OR REPLACE INTO metadata VALUES('name','terrain');
INSERT OR REPLACE INTO metadata VALUES('format','png');
INSERT OR REPLACE INTO metadata VALUES('minzoom','5');
INSERT OR REPLACE INTO metadata VALUES('maxzoom','12');
INSERT OR REPLACE INTO metadata VALUES('bounds','124.0,33.0,132.0,39.0');
SQL
echo "지형 타일 생성 완료: tiles/terrain.mbtiles"
```

- [ ] **Step 3: 실행 + 검증**

```bash
chmod +x scripts/03-gen-terrain.sh && ./scripts/03-gen-terrain.sh
sqlite3 tiles/terrain.mbtiles "SELECT count(*) FROM tiles;"
sqlite3 tiles/terrain.mbtiles "SELECT name, value FROM metadata;" | head
```

Expected: `지형 타일 생성 완료`, 타일 수 수천 행 이상, metadata에 `format|png`.

- [ ] **Step 4: 타일 픽셀 크기 확인 (다음 Task의 tileSize 설정에 필요 — 기록해 둘 것)**

```bash
sqlite3 tiles/terrain.mbtiles \
  "SELECT writefile('/tmp/sample-terrain.png', tile_data) FROM tiles LIMIT 1;" >/dev/null
python3 -c "import struct; d=open('/tmp/sample-terrain.png','rb').read(); print('tile px:', struct.unpack('>I', d[16:20])[0])"
```

Expected: `tile px: 512` 또는 `tile px: 256` — **이 값을 Task 11 Step 1에서 사용한다.**

- [ ] **Step 5: 커밋**

```bash
git add scripts/03-gen-terrain.sh
git commit -m "feat(3d-terrain): SRTM→Terrain-RGB 지형 타일 생성 스크립트"
```

---

### Task 11: 지형 서빙 + 데모 토글 [feature/3d-terrain]

**Files:**
- Create: `style/layers/terrain.json`
- Modify: `server/tileserver-config.json` (`data`에 terrain 항목 추가)
- Modify: `demo/js/terrain.js` (스텁 → 구현 교체)

- [ ] **Step 1: `style/layers/terrain.json` 작성**

`tileSize`는 Task 10 Step 4에서 확인한 픽셀 값으로 넣는다(512였다면 줄 자체를 생략 — MapLibre 기본값이 512).

```json
{
  "sources": {
    "terrain": {
      "type": "raster-dem",
      "url": "mbtiles://{terrain}",
      "encoding": "mapbox",
      "tileSize": 256
    }
  }
}
```

설계 의도: 소스만 정의하고 terrain을 스타일에서 **활성화하지 않는다**. 소비 프론트/데모가 `map.setTerrain()`으로 토글한다(스펙 5절 — 지형 상시 활성 시 원거리 3D 건물이 가려지는 트레이드오프).

- [ ] **Step 2: `server/tileserver-config.json`의 `data` 블록에 terrain 추가**

```json
  "data": {
    "korea": { "mbtiles": "korea.mbtiles" },
    "terrain": { "mbtiles": "terrain.mbtiles" }
  }
```

- [ ] **Step 3: `demo/js/terrain.js` 를 구현으로 교체 (전체 내용)**

```javascript
// 지형(terrain) 토글. 기본 OFF — 활성 시 원거리 3D 건물이 가려지는
// MapLibre 특성이 있어 소비 프론트도 상황에 따라 켜고 끈다.
(function () {
  const map = window.cuviaMap;
  const btn = document.createElement('button');
  btn.className = 'ctl';
  btn.textContent = '지형 OFF';
  let on = false;
  btn.onclick = () => {
    on = !on;
    map.setTerrain(on ? { source: 'terrain', exaggeration: 1.3 } : null);
    btn.textContent = on ? '지형 ON' : '지형 OFF';
  };
  document.getElementById('controls').appendChild(btn);
})();
```

- [ ] **Step 4: 재조립 + 재기동 + 검증**

```bash
./scripts/build-style.sh
jq -r '.sources.terrain.type' style/style.json
( cd server && docker compose restart tileserver )
sleep 5
curl -sf -o /dev/null -w "%{http_code}\n" "http://localhost:8080/data/terrain/10/873/396.png"
open "http://localhost:8081/demo/?#13/33.36/126.53"   # 한라산 — 지형 확인에 최적
```

Expected: `raster-dem`, 타일 `200`. 브라우저에서 `지형` 버튼 ON 시 한라산 일대가 입체로 솟고, OFF 시 평면 복귀. 서울로 이동해 지형 ON 상태에서 원거리 건물 렌더링 한계(트레이드오프)도 눈으로 확인해 둔다.

- [ ] **Step 5: 커밋 + 푸시**

```bash
git add style/layers/terrain.json server/tileserver-config.json demo/js/terrain.js
git commit -m "feat(3d-terrain): raster-dem 소스, 서버 연결, 데모 지형 토글"
git push -u origin feature/3d-terrain
```

---

### Task 12: main 병합 + 통합 검증 + 최종 패키징 [main]

**Files:**
- Modify: 없음 (병합·검증·패키징만)

- [ ] **Step 1: 두 브랜치를 main에 병합**

```bash
git checkout main
git merge --no-ff feature/3d-building -m "merge: 3D 건물 기능 통합"
git merge --no-ff feature/3d-terrain  -m "merge: 3D 지형 기능 통합"
```

Expected: 두 병합 모두 충돌 없음(각 브랜치가 자기 파일만 추가/교체하도록 설계됨). 충돌 발생 시 양쪽 변경을 모두 보존하는 방향으로 해소한다.

- [ ] **Step 2: 통합 스타일 조립 + 두 기능 공존 확인**

```bash
./scripts/build-style.sh
jq -r '.layers[] | select(.id=="Building 3D") | .type' style/style.json
jq -r '.sources.terrain.type' style/style.json
jq -r '.. | strings | select(test("^https?://"))' style/style.json
```

Expected: `fill-extrusion`, `raster-dem`, 마지막 명령 **출력 없음**(외부 URL 0건).

- [ ] **Step 3: 서버 재기동 + 엔드포인트 일괄 검증**

```bash
( cd server && docker compose up -d --force-recreate )
sleep 5
for url in \
  "http://localhost:8080/styles/cuvia/style.json" \
  "http://localhost:8080/data/korea/14/13970/6344.pbf" \
  "http://localhost:8080/data/terrain/10/873/396.png" \
  "http://localhost:8080/fonts/KlokanTech%20Noto%20Sans%20Regular/44032-44287.pbf" \
  "http://localhost:8081/demo/"; do
  printf "%s → " "$url"; curl -sf -o /dev/null -w "%{http_code}\n" "$url"
done
```

Expected: 5개 모두 `200`.

- [ ] **Step 4: 브라우저 통합 확인**

Run: `open http://localhost:8081/demo/`
Expected: 3D 건물 토글과 지형 토글이 **동시에 존재**하고 각각 독립 동작. 지형 ON+건물 ON 상태에서 근거리 건물은 정상, DevTools Network에 localhost 외 요청 0건.

- [ ] **Step 5: 최종 패키징 (지형 포함 번들 재생성)**

```bash
./scripts/package.sh
tar -tzf dist/cuvia-map-bundle.tgz | grep -E "terrain.mbtiles|buildings-3d.json|terrain.json"
```

Expected: 세 파일 모두 목록에 포함.

- [ ] **Step 6: 푸시 (모든 브랜치)**

```bash
git push origin main feature/3d-building feature/3d-terrain
```

---

## 완료 기준 (스펙 9절 매핑)

| 스펙 검증 항목 | 충족 Task |
|---|---|
| 벡터: render_height 존재 | Task 3 Step 3 |
| 건물: Building 3D extrusion + 토글 | Task 9 Step 4 |
| 지형: raster-dem + setTerrain 토글 + 트레이드오프 확인 | Task 11 Step 4 |
| 드롭인 호환: style URL 교체만으로 동작 | Task 7 가이드 + Task 12 Step 3 |
| 폐쇄망: 외부 요청 0건 | Task 4/6/12 의 grep·jq·DevTools 검증 |
| 아키텍처: amd64 이미지 | Task 5 compose `platform` + Task 8 `--platform` |
