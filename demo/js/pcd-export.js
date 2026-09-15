// 3D 지도(지형 DEM + 건물 압출) → PCD 점군 내보내기.  [A안 — 브라우저 전용, 서버 무변경]
//
// 우리 "3D"는 Cesium 3D Tiles(메시/pnts)가 아니라 ①건물 = 2D 폴리곤 + render_height 를
// MapLibre 가 fill-extrusion 으로 즉석 압출, ②지형 = Terrain-RGB 래스터 DEM 이다.
// 따라서 렌더 결과를 긁는 게 아니라 원본(MVT 폴리곤 / DEM 픽셀)에서 점을 직접 샘플링한다.
//
// 내보내는 레이어:
//   지형   — DEM 격자. 물·녹지 폴리곤 안에 드는 점은 색만 바꿔 구분한다(점 수 안 늘어남).
//   건물   — 지붕 격자 + 벽면 수직 샘플, DEM 지반 위에 얹음.
//   도로   — transportation 중심선을 class 별 실폭 리본으로 펴서 지형 위 ROAD_LIFT 만큼 띄움.
//
// ── 포맷 계약 (A안·B안 공통, 바꾸지 말 것) ────────────────────────────────
//  좌표계 : 로컬 ENU 미터.  x=동(E), y=북(N), z=해발고도(m).
//           원점 = 내보낼 때의 화면 중심. 헤더 주석(# origin_lonlat)과 파일명에 기록한다.
//           ※ 경위도를 그대로 넣으면 안 되고, UTM-K 절대좌표도 float32 에선 ~1.5cm 로
//             양자화되므로 로컬 원점 상대좌표가 정석이다.
//  필드   : x y z rgb — rgb 는 0x00RRGGBB 를 float32 비트로 재해석(PCL 관행, CloudCompare 호환).
//           색상 끄면 x y z.
//  높이   : 지형 z = DEM 해발(SRTM/EGM96). 건물은 그 위에 얹는다(지붕 = 무게중심 지반 + 높이).
//
// ── B안(서버 /pcd) 전환 지점 ──────────────────────────────────────────────
//  아래 §4 '데이터 수집'만 fetch('/pcd?bbox=…') 로 교체하면 §2 포맷·§3 샘플링·§5 UI 는 그대로다.
//  A안의 구조적 한계(B안에서 해소):
//   · martin 이 clip_geom:true 라 타일 경계에서 건물이 잘린다 → 건물 내부에 가짜 벽면이 생긴다.
//   · querySourceFeatures 는 "현재 로드된 타일"만 본다 → 화면 밖 영역은 건물이 빠진다.
//   · 대면적은 브라우저 메모리에서 막힌다.
(function () {
  const map = window.cuviaMap;
  if (!map) { console.warn('pcd-export.js: cuviaMap 미초기화 — map.js 로드 순서 확인'); return; }
  const controls = document.getElementById('controls');
  if (!controls) { console.warn('pcd-export.js: #controls 없음'); return; }

  const params = new URLSearchParams(location.search);
  const TILESERVER = params.get('server') || '';

  const DEM_Z = 12;            // terrain.mbtiles maxzoom (03-gen-terrain.sh) = 약 30m/px
  const DEM_BASE = -10000;     // Terrain-RGB(mapbox 인코딩) — style/layers/terrain.json
  const DEM_STEP = 0.1;
  const BLD_SRC = 'buildings', BLD_LAYER = 'building';   // style/base.json · martin-config.yaml
  const OMT_SRC = 'openmaptiles';                        // 도로·물·녹지 (korea.mbtiles)
  const DEFAULT_H = 15;        // style/layers/buildings-3d.json 의 coalesce 기본값과 동일
  const WARN_POINTS = 5e6;     // 이 이상이면 확인 후 진행
  const ROAD_LIFT = 0.15;      // 도로를 지형보다 이만큼 띄운다(같은 높이면 지형점에 묻힌다)
  // style/base.json 의 landcover 필터와 동일
  const GREEN_CLASS = new Set(['grass', 'wood', 'forest', 'scrub', 'farmland']);

  // ── §1 좌표 변환 (로컬 ENU) ────────────────────────────────────────────
  // 원점 위도에서의 1도당 미터. 수 km 범위에서 cm 수준으로 충분하다.
  function enuScale(lat) {
    const p = lat * Math.PI / 180;
    return {
      my: 111132.92 - 559.82 * Math.cos(2 * p) + 1.175 * Math.cos(4 * p) - 0.0023 * Math.cos(6 * p),
      mx: 111412.84 * Math.cos(p) - 93.5 * Math.cos(3 * p) + 0.118 * Math.cos(5 * p),
    };
  }

  // ── §2 PCD 인코딩 ──────────────────────────────────────────────────────
  // 점 버퍼 — 수백만 점을 다루므로 배열이 아닌 증분 TypedArray.
  function PointBuf() {
    this.n = 0; this.cap = 1 << 16;
    this.xyz = new Float32Array(this.cap * 3);
    this.rgb = new Uint32Array(this.cap);
  }
  PointBuf.prototype.push = function (x, y, z, c) {
    if (this.n === this.cap) {
      this.cap *= 2;
      const xyz = new Float32Array(this.cap * 3); xyz.set(this.xyz); this.xyz = xyz;
      const rgb = new Uint32Array(this.cap); rgb.set(this.rgb); this.rgb = rgb;
    }
    const i = this.n * 3;
    this.xyz[i] = x; this.xyz[i + 1] = y; this.xyz[i + 2] = z;
    this.rgb[this.n] = c; this.n++;
  };

  function pcdHeader(n, color, ascii, meta) {
    const f = color ? ['x y z rgb', '4 4 4 4', 'F F F F', '1 1 1 1'] : ['x y z', '4 4 4', 'F F F', '1 1 1'];
    return '# .PCD v0.7 - Point Cloud Data file format\n' +
      '# CUVIA 3D map export (demo/js/pcd-export.js)\n' +
      '# origin_lonlat ' + meta.lon.toFixed(7) + ' ' + meta.lat.toFixed(7) + '  (WGS84, ENU 원점)\n' +
      '# axes x=east(m) y=north(m) z=elevation(m, SRTM/EGM96 해발)\n' +
      '# extent ' + meta.side + 'm square · terrain_grid ' + meta.tStep + 'm (DEM 원본 ≈30m) · building_grid ' + meta.bStep + 'm\n' +
      '# layers ' + meta.layers + '\n' +
      'VERSION 0.7\n' +
      'FIELDS ' + f[0] + '\nSIZE ' + f[1] + '\nTYPE ' + f[2] + '\nCOUNT ' + f[3] + '\n' +
      'WIDTH ' + n + '\nHEIGHT 1\nVIEWPOINT 0 0 0 1 0 0 0\nPOINTS ' + n + '\n' +
      'DATA ' + (ascii ? 'ascii' : 'binary') + '\n';
  }

  function encodePcd(buf, color, ascii, meta) {
    const n = buf.n, stride = color ? 4 : 3;
    const header = pcdHeader(n, color, ascii, meta);
    if (ascii) {
      // PCL 의 ascii 규약과 동일하게 rgb 도 "float 로 재해석한 값"을 적는다.
      const conv = new DataView(new ArrayBuffer(4));
      const out = [header];
      let chunk = [];
      for (let i = 0; i < n; i++) {
        const j = i * 3;
        let line = buf.xyz[j].toFixed(3) + ' ' + buf.xyz[j + 1].toFixed(3) + ' ' + buf.xyz[j + 2].toFixed(3);
        if (color) { conv.setUint32(0, buf.rgb[i], true); line += ' ' + conv.getFloat32(0, true); }
        chunk.push(line);
        if (chunk.length === 65536) { out.push(chunk.join('\n') + '\n'); chunk = []; }
      }
      if (chunk.length) out.push(chunk.join('\n') + '\n');
      return new Blob(out, { type: 'application/octet-stream' });
    }
    const head = new TextEncoder().encode(header);
    const body = new ArrayBuffer(n * stride * 4);
    const dv = new DataView(body);
    for (let i = 0, o = 0; i < n; i++) {
      const j = i * 3;
      dv.setFloat32(o, buf.xyz[j], true);
      dv.setFloat32(o + 4, buf.xyz[j + 1], true);
      dv.setFloat32(o + 8, buf.xyz[j + 2], true);
      o += 12;
      if (color) { dv.setUint32(o, buf.rgb[i], true); o += 4; }   // 비트패턴 그대로 = float 재해석
    }
    return new Blob([head, body], { type: 'application/octet-stream' });
  }

  // ── §3 색상 램프 ───────────────────────────────────────────────────────
  const rgb = (r, g, b) => ((r & 255) << 16) | ((g & 255) << 8) | (b & 255);
  function ramp(stops, t) {
    t = t < 0 ? 0 : t > 1 ? 1 : t;
    const s = t * (stops.length - 1), i = Math.min(stops.length - 2, Math.floor(s)), f = s - i;
    const a = stops[i], b = stops[i + 1];
    return rgb(a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f, a[2] + (b[2] - a[2]) * f);
  }
  // 뷰어에서 한눈에 갈리도록 레이어마다 다른 계열을 쓴다. 고도에 따라 명암이 지므로 기복도 함께 읽힌다.
  const HYPSO = [[46, 111, 64], [120, 140, 60], [150, 120, 70], [140, 100, 85], [216, 216, 216]];  // 지표
  const WARM = [[110, 60, 35], [230, 120, 40], [255, 215, 140]];                                   // 건물(높이)
  const WATER = [[26, 58, 102], [58, 116, 184], [120, 172, 224]];                                  // 수역
  const GREEN = [[22, 66, 38], [46, 118, 62], [104, 160, 88]];                                     // 녹지
  // 도로 — OSM transportation 의 class → [대표 실폭(m), 색]. 중심선만 있어 폭은 class 로 준다.
  const ROAD = {
    motorway: [24, rgb(255, 140, 66)], trunk: [20, rgb(255, 140, 66)],
    primary: [16, rgb(255, 178, 96)], secondary: [12, rgb(232, 201, 106)],
    tertiary: [10, rgb(232, 201, 106)], minor: [7, rgb(154, 164, 178)],
    service: [5, rgb(130, 140, 154)], track: [4, rgb(130, 140, 154)],
    path: [2, rgb(120, 130, 144)], rail: [5, rgb(180, 138, 214)],
  };

  // ── §4 데이터 수집 ─────────────────────────────────────────────────────
  // (B안 전환 시 이 절만 fetch('/pcd?…') 로 교체)

  // Terrain-RGB 타일을 직접 디코드한다. map.queryTerrainElevation 은 지형 토글이 켜져 있어야
  // 하고 exaggeration 이 섞이므로, 토글 상태와 무관하고 B안(서버 디코드)과 결과가 같은 이 방식을 쓴다.
  function DemSampler(z) {
    this.z = z; this.tiles = new Map(); this.ts = 256; this.missing = false;
    const src = map.getSource('terrain');
    let tpl = (src && src.tiles && src.tiles[0]) || '/data/terrain/{z}/{x}/{y}.png';
    if (tpl.charAt(0) === '/') tpl = (TILESERVER || location.origin) + tpl;   // map.js transformRequest 와 동일 승격
    this.tpl = tpl;
  }
  DemSampler.prototype.pixelOf = function (lon, lat) {
    const s = this.ts * (1 << this.z), p = lat * Math.PI / 180;
    return [
      (lon + 180) / 360 * s,
      (1 - Math.log(Math.tan(p) + 1 / Math.cos(p)) / Math.PI) / 2 * s,
    ];
  };
  DemSampler.prototype.load = async function (bbox) {
    const [a, b] = [this.pixelOf(bbox[0], bbox[3]), this.pixelOf(bbox[2], bbox[1])];   // 좌상, 우하
    const max = (1 << this.z) - 1;
    const x0 = Math.max(0, Math.floor(a[0] / this.ts) - 1), x1 = Math.min(max, Math.floor(b[0] / this.ts) + 1);
    const y0 = Math.max(0, Math.floor(a[1] / this.ts) - 1), y1 = Math.min(max, Math.floor(b[1] / this.ts) + 1);
    const cv = document.createElement('canvas'), jobs = [];
    for (let x = x0; x <= x1; x++) for (let y = y0; y <= y1; y++) jobs.push([x, y]);
    for (const [x, y] of jobs) {
      const url = this.tpl.replace('{z}', this.z).replace('{x}', x).replace('{y}', y);
      try {
        const r = await fetch(url);
        if (!r.ok) { this.missing = true; continue; }          // 바다·범위 밖은 404 가 정상
        const bmp = await createImageBitmap(await r.blob());
        this.ts = bmp.width;
        cv.width = bmp.width; cv.height = bmp.height;
        const ctx = cv.getContext('2d', { willReadFrequently: true });
        ctx.drawImage(bmp, 0, 0);
        this.tiles.set(x + '/' + y, ctx.getImageData(0, 0, bmp.width, bmp.height).data);
        bmp.close();
      } catch (e) { this.missing = true; console.warn('DEM 타일 실패', url, e); }
    }
  };
  DemSampler.prototype.px = function (gx, gy) {      // 전역 픽셀 → 고도(없으면 null)
    const t = this.tiles.get(Math.floor(gx / this.ts) + '/' + Math.floor(gy / this.ts));
    if (!t) return null;
    const i = ((gy % this.ts | 0) * this.ts + (gx % this.ts | 0)) * 4;
    return DEM_BASE + (t[i] * 65536 + t[i + 1] * 256 + t[i + 2]) * DEM_STEP;
  };
  DemSampler.prototype.elevation = function (lon, lat) {   // 이중선형 — 30m DEM 을 부드럽게
    const p = this.pixelOf(lon, lat), fx = p[0] - 0.5, fy = p[1] - 0.5;
    const x0 = Math.floor(fx), y0 = Math.floor(fy), dx = fx - x0, dy = fy - y0;
    const v00 = this.px(x0, y0);
    if (v00 === null) return null;
    const v10 = this.px(x0 + 1, y0), v01 = this.px(x0, y0 + 1), v11 = this.px(x0 + 1, y0 + 1);
    if (v10 === null || v01 === null || v11 === null) return v00;   // 타일 가장자리는 최근접으로
    return v00 * (1 - dx) * (1 - dy) + v10 * dx * (1 - dy) + v01 * (1 - dx) * dy + v11 * dx * dy;
  };

  // 건물 폴리곤을 ENU 링 배열로 수집. 링은 [x0,y0,x1,y1,…] 평탄 배열.
  function collectBuildings(origin, scale, half) {
    let feats;
    try { feats = map.querySourceFeatures(BLD_SRC, { sourceLayer: BLD_LAYER }); }
    catch (e) { console.warn('건물 소스 조회 실패', e); return []; }
    const out = [];
    for (const f of feats) {
      const g = f.geometry;
      if (!g) continue;
      const polys = g.type === 'Polygon' ? [g.coordinates] : g.type === 'MultiPolygon' ? g.coordinates : null;
      if (!polys) continue;
      const p = f.properties || {};
      // 적재 규칙(scripts/postgis/load_building.sh)과 동일: 실측높이 > 층수*3.3 > 기본값
      const h = p.render_height > 0 ? +p.render_height : p.levels > 0 ? p.levels * 3.3 : DEFAULT_H;
      for (const poly of polys) {
        const rings = [];
        let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity, cx = 0, cy = 0, cn = 0;
        for (const ring of poly) {
          const r = new Float64Array(ring.length * 2);
          for (let i = 0; i < ring.length; i++) {
            const x = (ring[i][0] - origin.lon) * scale.mx, y = (ring[i][1] - origin.lat) * scale.my;
            r[i * 2] = x; r[i * 2 + 1] = y;
            if (rings.length === 0) {
              if (x < minX) minX = x; if (x > maxX) maxX = x;
              if (y < minY) minY = y; if (y > maxY) maxY = y;
              cx += x; cy += y; cn++;
            }
          }
          rings.push(r);
        }
        if (!cn) continue;
        if (maxX < -half || minX > half || maxY < -half || minY > half) continue;   // 내보낼 상자 밖
        out.push({ rings: rings, h: h, minX: minX, minY: minY, maxX: maxX, maxY: maxY, cx: cx / cn, cy: cy / cn });
      }
    }
    return out;
  }

  // ── §5 샘플링 ──────────────────────────────────────────────────────────
  function inPolygon(rings, x, y) {   // even-odd — 전체 링을 함께 세면 구멍도 자동 처리
    let inside = false;
    for (const r of rings) {
      for (let i = 0, j = r.length - 2; i < r.length; j = i, i += 2) {
        const xi = r[i], yi = r[i + 1], xj = r[j], yj = r[j + 1];
        if ((yi > y) !== (yj > y) && x < (xj - xi) * (y - yi) / (yj - yi) + xi) inside = !inside;
      }
    }
    return inside;
  }

  function stats(blds) {   // 예상 점 수 산정을 위한 면적·둘레 합
    let area = 0, wall = 0;
    for (const b of blds) {
      const r = b.rings[0];
      let a = 0, per = 0;
      for (let i = 0, j = r.length - 2; i < r.length; j = i, i += 2) {
        a += r[j] * r[i + 1] - r[i] * r[j + 1];
        per += Math.hypot(r[i] - r[j], r[i + 1] - r[j + 1]);
      }
      area += Math.abs(a) / 2;
      wall += per * b.h;
    }
    return { area: area, wall: wall };
  }

  // 지형 격자. 색칠은 지표피복 분류 뒤로 미루므로 격자 인덱스를 돌려준다.
  function sampleTerrain(buf, dem, origin, scale, half, step) {
    const n = Math.floor(half / step), w = 2 * n + 1;
    const idx = new Int32Array(w * w).fill(-1);
    for (let iy = -n; iy <= n; iy++) {
      for (let ix = -n; ix <= n; ix++) {
        const x = ix * step, y = iy * step;
        const z = dem.elevation(origin.lon + x / scale.mx, origin.lat + y / scale.my);
        if (z === null) continue;
        idx[(iy + n) * w + (ix + n)] = buf.n;
        buf.push(x, y, z, 0);
      }
    }
    return { idx: idx, n: n, w: w, step: step };
  }

  // 지표피복 — 물·녹지 폴리곤 안에 드는 지형 격자점을 표시한다.
  // 점을 새로 만들지 않고 "이미 있는 지형점의 색"만 바꾸므로 용량이 늘지 않는다.
  function classifyCover(grid, origin, scale, half) {
    const cover = new Uint8Array(grid.w * grid.w);       // 0=지표 1=물 2=녹지
    const put = (polys, code) => {
      for (const poly of polys) {
        const rings = [];
        let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
        for (const ring of poly) {
          const r = new Float64Array(ring.length * 2);
          for (let i = 0; i < ring.length; i++) {
            const x = (ring[i][0] - origin.lon) * scale.mx, y = (ring[i][1] - origin.lat) * scale.my;
            r[i * 2] = x; r[i * 2 + 1] = y;
            if (!rings.length) {
              if (x < minX) minX = x; if (x > maxX) maxX = x;
              if (y < minY) minY = y; if (y > maxY) maxY = y;
            }
          }
          rings.push(r);
        }
        if (maxX < -half || minX > half || maxY < -half || minY > half) continue;
        // 폴리곤 bbox 안의 격자칸만 검사 — 전수 검사보다 훨씬 싸다
        const i0 = Math.max(-grid.n, Math.ceil(minX / grid.step)), i1 = Math.min(grid.n, Math.floor(maxX / grid.step));
        const j0 = Math.max(-grid.n, Math.ceil(minY / grid.step)), j1 = Math.min(grid.n, Math.floor(maxY / grid.step));
        for (let iy = j0; iy <= j1; iy++) {
          for (let ix = i0; ix <= i1; ix++) {
            const k = (iy + grid.n) * grid.w + (ix + grid.n);
            if (grid.idx[k] < 0 || cover[k] === 1) continue;    // 물이 녹지보다 우선
            if (inPolygon(rings, ix * grid.step, iy * grid.step)) cover[k] = code;
          }
        }
      }
    };
    const polysOf = (layer, filter) => {
      let feats;
      try { feats = map.querySourceFeatures(OMT_SRC, { sourceLayer: layer }); }
      catch (e) { console.warn(layer + ' 조회 실패', e); return []; }
      const out = [];
      for (const f of feats) {
        if (filter && !filter(f.properties || {})) continue;
        const g = f.geometry;
        if (!g) continue;
        if (g.type === 'Polygon') out.push(g.coordinates);
        else if (g.type === 'MultiPolygon') for (const p of g.coordinates) out.push(p);
      }
      return out;
    };
    // style/base.json 의 landcover 필터와 동일한 class 집합
    put(polysOf('landcover', p => GREEN_CLASS.has(p.class)), 2);
    put(polysOf('park'), 2);
    put(polysOf('water'), 1);
    return cover;
  }

  function colorTerrain(buf, grid, cover, color) {
    if (!color) return;
    let lo = Infinity, hi = -Infinity;
    for (const i of grid.idx) {
      if (i < 0) continue;
      const z = buf.xyz[i * 3 + 2];
      if (z < lo) lo = z; if (z > hi) hi = z;
    }
    const d = hi - lo || 1;
    for (let k = 0; k < grid.idx.length; k++) {
      const i = grid.idx[k];
      if (i < 0) continue;
      const t = (buf.xyz[i * 3 + 2] - lo) / d;
      buf.rgb[i] = cover && cover[k] === 1 ? ramp(WATER, t)
        : cover && cover[k] === 2 ? ramp(GREEN, t)
        : ramp(HYPSO, t);
    }
  }

  // 도로·철도 — OpenMapTiles transportation 은 중심선(LineString)이라 폭 정보가 없다.
  // class 별 대표 실폭을 줘서 리본으로 펴고, 지형 위 ROAD_LIFT 만큼 띄운다(지형점과 겹쳐 묻히지 않게).
  function collectRoads(origin, scale, half) {
    let feats;
    try { feats = map.querySourceFeatures(OMT_SRC, { sourceLayer: 'transportation' }); }
    catch (e) { console.warn('transportation 조회 실패', e); return []; }
    const out = [];
    for (const f of feats) {
      const p = f.properties || {};
      const spec = ROAD[p.class];
      if (!spec) continue;
      if (p.brunnel === 'tunnel') continue;          // 터널을 지표에 그리면 지형 판독을 방해한다
      const g = f.geometry;
      if (!g) continue;
      const lines = g.type === 'LineString' ? [g.coordinates] : g.type === 'MultiLineString' ? g.coordinates : null;
      if (!lines) continue;
      for (const line of lines) {
        const r = new Float64Array(line.length * 2);
        let hit = false;
        for (let i = 0; i < line.length; i++) {
          const x = (line[i][0] - origin.lon) * scale.mx, y = (line[i][1] - origin.lat) * scale.my;
          r[i * 2] = x; r[i * 2 + 1] = y;
          if (x >= -half && x <= half && y >= -half && y <= half) hit = true;
        }
        if (hit && line.length > 1) out.push({ pts: r, w: spec[0], c: spec[1] });
      }
    }
    return out;
  }

  function sampleRoads(buf, seen, dem, roads, origin, scale, half, step, color) {
    const ll = (x, y) => [origin.lon + x / scale.mx, origin.lat + y / scale.my];
    for (const road of roads) {
      const r = road.pts, half_w = road.w / 2;
      const across = Math.max(0, Math.round(half_w / step));
      for (let i = 2; i < r.length; i += 2) {
        const x0 = r[i - 2], y0 = r[i - 1], dx = r[i] - x0, dy = r[i + 1] - y0;
        const len = Math.hypot(dx, dy);
        if (!len) continue;
        const nx = -dy / len, ny = dx / len;            // 진행방향 법선 = 도로 폭 방향
        const m = Math.max(1, Math.ceil(len / step));
        for (let k = 0; k <= m; k++) {
          const cx = x0 + dx * k / m, cy = y0 + dy * k / m;
          for (let a = -across; a <= across; a++) {
            const x = cx + nx * a * step, y = cy + ny * a * step;
            if (x < -half || x > half || y < -half || y > half) continue;
            const g = ll(x, y);
            const z = dem.elevation(g[0], g[1]);
            if (z === null) continue;
            const key = voxelKey(x, y, z, step);
            if (key !== -1) { if (seen.has(key)) continue; seen.add(key); }
            buf.push(x, y, z + ROAD_LIFT, color ? road.c : 0);
          }
        }
      }
    }
  }

  function roadLength(roads) {   // 예상 점 수 산정용 — 폭을 감안한 리본 면적
    let a = 0;
    for (const road of roads) {
      const r = road.pts;
      for (let i = 2; i < r.length; i += 2) a += Math.hypot(r[i] - r[i - 2], r[i + 1] - r[i - 1]) * road.w;
    }
    return a;
  }

  // 중복 제거용 복셀 키 — 타일 버퍼(buffer:64)로 겹쳐 들어온 같은 건물의 중복 점을 접는다.
  function voxelKey(x, y, z, s) {
    const ix = Math.round(x / s) + 32768, iy = Math.round(y / s) + 32768, iz = Math.round(z / s) + 32768;
    if (ix < 0 || ix > 65535 || iy < 0 || iy > 65535 || iz < 0 || iz > 65535) return -1;
    return (ix * 65536 + iy) * 65536 + iz;   // < 2^53 이라 Number 로 정확
  }

  function sampleBuilding(buf, seen, dem, b, origin, scale, half, step, walls, color) {
    const ll = (x, y) => [origin.lon + x / scale.mx, origin.lat + y / scale.my];
    const gc = ll(b.cx, b.cy);
    const base = dem.elevation(gc[0], gc[1]);
    if (base === null) return;
    const roof = base + b.h;
    const put = (x, y, z) => {
      if (x < -half || x > half || y < -half || y > half) return;
      const k = voxelKey(x, y, z, step);
      if (k !== -1) { if (seen.has(k)) return; seen.add(k); }
      buf.push(x, y, z, color ? ramp(WARM, b.h > 0 ? (z - base) / b.h : 1) : 0);
    };
    // 지붕 — 전역 격자에 맞춰 찍어야 중복 제거가 동작한다
    for (let iy = Math.ceil(b.minY / step); iy <= Math.floor(b.maxY / step); iy++) {
      const y = iy * step;
      for (let ix = Math.ceil(b.minX / step); ix <= Math.floor(b.maxX / step); ix++) {
        const x = ix * step;
        if (inPolygon(b.rings, x, y)) put(x, y, roof);
      }
    }
    if (!walls) return;
    // 벽면 — 외곽선을 따라가며 지반고도부터 지붕까지 수직 샘플
    for (const r of b.rings) {
      for (let i = 0, j = r.length - 2; i < r.length; j = i, i += 2) {
        const x0 = r[j], y0 = r[j + 1], dx = r[i] - x0, dy = r[i + 1] - y0;
        const len = Math.hypot(dx, dy);
        const m = Math.max(1, Math.ceil(len / step));
        for (let k = 0; k < m; k++) {
          const x = x0 + dx * k / m, y = y0 + dy * k / m;
          const g = ll(x, y);
          let z = dem.elevation(g[0], g[1]);
          if (z === null) z = base;
          if (z > roof) { put(x, y, roof); continue; }
          for (; z < roof; z += step) put(x, y, z);
          put(x, y, roof);
        }
      }
    }
  }

  // ── §6 UI ──────────────────────────────────────────────────────────────
  const BOX_SRC = 'pcd-export-box', BOX_LAYER = 'pcd-export-box-line';
  const btn = document.createElement('button');
  btn.className = 'ctl';
  btn.textContent = 'PCD 내보내기';
  controls.appendChild(btn);

  const panel = document.createElement('div');
  panel.style.cssText = 'position:fixed;top:52px;left:10px;z-index:12;width:268px;display:none;' +
    'background:#141a22;border:1px solid #2c3542;border-radius:8px;padding:12px;' +
    'color:#cdd6e3;font-size:12px;line-height:1.7;font-family:inherit';
  panel.innerHTML =
    '<div style="color:#e8eef7;font-weight:600;margin-bottom:8px">점군(PCD) 내보내기</div>' +
    '<label>범위(화면 중심 기준 한 변) <select id="pcd-side" style="float:right;width:96px">' +
    '<option value="500">500 m</option><option value="1000" selected>1 km</option>' +
    '<option value="2000">2 km</option><option value="4000">4 km</option></select></label><br>' +
    '<label>지형 간격 <select id="pcd-tstep" style="float:right;width:96px">' +
    '<option value="5">5 m</option><option value="10" selected>10 m</option>' +
    '<option value="20">20 m</option><option value="30">30 m</option></select></label><br>' +
    '<label>건물·도로 간격 <select id="pcd-bstep" style="float:right;width:96px">' +
    '<option value="0.5">0.5 m</option><option value="1" selected>1 m</option>' +
    '<option value="2">2 m</option></select></label><br>' +
    '<label><input type="checkbox" id="pcd-terrain" checked> 지형</label> ' +
    '<label><input type="checkbox" id="pcd-bld" checked> 건물</label> ' +
    '<label><input type="checkbox" id="pcd-walls" checked> 벽면</label><br>' +
    '<label><input type="checkbox" id="pcd-road" checked> 도로·철도</label> ' +
    '<label><input type="checkbox" id="pcd-cover" checked> 물·녹지 구분</label><br>' +
    '<label><input type="checkbox" id="pcd-color" checked> 색상(고도 램프)</label> ' +
    '<label><input type="checkbox" id="pcd-ascii"> ASCII</label>' +
    '<div id="pcd-est" style="margin-top:8px;color:#7d8aa0"></div>' +
    '<div id="pcd-warn" style="color:#e8b84a"></div>' +
    '<button id="pcd-go" class="ctl" style="width:100%;margin-top:8px">내보내기</button>' +
    '<div id="pcd-status" style="margin-top:6px;color:#7d8aa0;min-height:1.7em"></div>' +
    '<div style="margin-top:6px;color:#5d6a80;font-size:11px">DEM 원본 30m · 도로는 중심선뿐이라 ' +
    'class 별 대표 실폭으로 폅니다 · 건물 폴리곤은 타일 경계에서 잘려 내부에 가짜 벽면이 ' +
    '생길 수 있습니다(서버 내보내기에서 해소).</div>';
  document.body.appendChild(panel);

  const el = (id) => panel.querySelector('#' + id);
  const opts = () => ({
    side: +el('pcd-side').value, tStep: +el('pcd-tstep').value, bStep: +el('pcd-bstep').value,
    terrain: el('pcd-terrain').checked, bld: el('pcd-bld').checked, walls: el('pcd-walls').checked,
    road: el('pcd-road').checked, cover: el('pcd-cover').checked,
    color: el('pcd-color').checked, ascii: el('pcd-ascii').checked,
  });

  function boxGeoJson(o, half, scale) {
    const dx = half / scale.mx, dy = half / scale.my;
    return {
      type: 'Feature', properties: {}, geometry: {
        type: 'Polygon', coordinates: [[
          [o.lon - dx, o.lat - dy], [o.lon + dx, o.lat - dy],
          [o.lon + dx, o.lat + dy], [o.lon - dx, o.lat + dy], [o.lon - dx, o.lat - dy]]],
      },
    };
  }

  function refresh() {
    if (panel.style.display === 'none') return;
    const o = opts(), c = map.getCenter(), origin = { lon: c.lng, lat: c.lat };
    const scale = enuScale(origin.lat), half = o.side / 2;
    // 내보낼 상자 미리보기
    const gj = boxGeoJson(origin, half, scale);
    const src = map.getSource(BOX_SRC);
    if (src) src.setData(gj);
    else if (map.isStyleLoaded()) {
      map.addSource(BOX_SRC, { type: 'geojson', data: gj });
      map.addLayer({
        id: BOX_LAYER, type: 'line', source: BOX_SRC,
        paint: { 'line-color': '#e8b84a', 'line-width': 2, 'line-dasharray': [2, 2] },
      });
    }
    // 예상 점 수
    let est = 0, warn = [];
    if (o.terrain) est += Math.pow(2 * Math.floor(half / o.tStep) + 1, 2);
    if (o.bld) {
      const blds = collectBuildings(origin, scale, half);
      const s = stats(blds);
      est += s.area / (o.bStep * o.bStep) + (o.walls ? s.wall / (o.bStep * o.bStep) : 0);
      if (map.getZoom() < 13) warn.push('건물 소스는 z13 이상에서만 로드됩니다 — 확대하세요.');
      else if (!blds.length) warn.push('로드된 건물이 없습니다.');
      const b = map.getBounds();
      const dx = half / scale.mx, dy = half / scale.my;
      if (origin.lon - dx < b.getWest() || origin.lon + dx > b.getEast() ||
          origin.lat - dy < b.getSouth() || origin.lat + dy > b.getNorth()) {
        warn.push('상자가 화면 밖으로 나갑니다 — 로드 안 된 타일의 건물은 빠집니다.');
      }
    }
    if (o.road) {
      const roads = collectRoads(origin, scale, half);
      est += roadLength(roads) / (o.bStep * o.bStep);
      if (!roads.length) warn.push('로드된 도로가 없습니다.');
    }
    est = Math.round(est);
    const bytes = est * (o.ascii ? (o.color ? 46 : 33) : o.color ? 16 : 12);
    el('pcd-est').textContent = '예상 ' + est.toLocaleString() + ' 점 · 약 ' +
      (bytes / 1048576).toFixed(1) + ' MB';
    el('pcd-warn').innerHTML = warn.map(w => '⚠ ' + w).join('<br>');
  }

  function clearBox() {
    if (map.getLayer(BOX_LAYER)) map.removeLayer(BOX_LAYER);
    if (map.getSource(BOX_SRC)) map.removeSource(BOX_SRC);
  }

  btn.onclick = () => {
    const open = panel.style.display === 'none';
    panel.style.display = open ? 'block' : 'none';
    if (open) refresh(); else clearBox();
  };
  panel.addEventListener('change', refresh);
  // 패널이 열린 동안만 동작 — 건물 재수집이 무겁지 않도록 디바운스.
  let refreshTimer = null;
  map.on('moveend', () => { clearTimeout(refreshTimer); refreshTimer = setTimeout(refresh, 200); });

  const tick = () => new Promise(r => setTimeout(r, 0));

  el('pcd-go').onclick = async () => {
    const go = el('pcd-go'), status = el('pcd-status');
    if (go.disabled) return;
    const o = opts();
    if (!o.terrain && !o.bld && !o.road) { status.textContent = '지형·건물·도로 중 하나는 선택해야 합니다.'; return; }
    go.disabled = true;
    try {
      const c = map.getCenter(), origin = { lon: c.lng, lat: c.lat };
      const scale = enuScale(origin.lat), half = o.side / 2;
      const dx = half / scale.mx, dy = half / scale.my;
      const bbox = [origin.lon - dx, origin.lat - dy, origin.lon + dx, origin.lat + dy];

      status.textContent = '지형 타일 로드 중…';
      await tick();
      const dem = new DemSampler(DEM_Z);
      await dem.load(bbox);
      if (!dem.tiles.size) throw new Error('지형 타일을 가져오지 못했습니다 (/data/terrain 확인)');

      const buf = new PointBuf();
      const blds = o.bld ? collectBuildings(origin, scale, half) : [];
      const roads = o.road ? collectRoads(origin, scale, half) : [];
      const px = o.bStep * o.bStep;
      const s = stats(blds);
      const est = (o.terrain ? Math.pow(2 * Math.floor(half / o.tStep) + 1, 2) : 0) +
        s.area / px + (o.walls ? s.wall / px : 0) + roadLength(roads) / px;
      if (est > WARN_POINTS &&
          !confirm('예상 ' + Math.round(est).toLocaleString() + ' 점입니다. 브라우저가 느려질 수 있습니다. 계속할까요?')) {
        status.textContent = '취소했습니다.'; go.disabled = false; return;
      }

      if (o.terrain) {
        status.textContent = '지형 샘플링…';
        await tick();
        const grid = sampleTerrain(buf, dem, origin, scale, half, o.tStep);
        let cover = null;
        if (o.cover) {
          status.textContent = '물·녹지 분류…';
          await tick();
          cover = classifyCover(grid, origin, scale, half);
        }
        colorTerrain(buf, grid, cover, o.color);
      }
      if (o.bld) {
        const seen = new Set();
        for (let i = 0; i < blds.length; i++) {
          sampleBuilding(buf, seen, dem, blds[i], origin, scale, half, o.bStep, o.walls, o.color);
          if (i % 200 === 0) {
            status.textContent = '건물 ' + i + '/' + blds.length + ' · ' + buf.n.toLocaleString() + ' 점';
            await tick();
          }
        }
      }
      if (o.road) {
        status.textContent = '도로 샘플링… (' + roads.length + '개 구간)';
        await tick();
        sampleRoads(buf, new Set(), dem, roads, origin, scale, half, o.bStep, o.color);
      }
      if (!buf.n) throw new Error('생성된 점이 없습니다 — 범위·레이어 설정을 확인하세요.');

      status.textContent = 'PCD 인코딩 중… (' + buf.n.toLocaleString() + ' 점)';
      await tick();
      const blob = encodePcd(buf, o.color, o.ascii, {
        lon: origin.lon, lat: origin.lat, side: o.side, tStep: o.tStep, bStep: o.bStep,
        layers: [
          o.terrain && (o.cover ? 'terrain(+water/green)' : 'terrain'),
          o.bld && (o.walls ? 'building(roof+wall)' : 'building(roof)'),
          o.road && 'road+rail(+' + ROAD_LIFT + 'm)',
        ].filter(Boolean).join(' '),
      });
      const name = 'cuvia_' + origin.lon.toFixed(5) + '_' + origin.lat.toFixed(5) + '_' + o.side + 'm.pcd';
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url; a.download = name; a.click();
      setTimeout(() => URL.revokeObjectURL(url), 10000);
      status.textContent = '완료: ' + name + ' · ' + buf.n.toLocaleString() + ' 점 · ' +
        (blob.size / 1048576).toFixed(1) + ' MB' + (dem.missing ? ' (일부 DEM 타일 없음)' : '');
    } catch (e) {
      console.error(e);
      status.textContent = '실패: ' + e.message;
    }
    go.disabled = false;
  };
})();
