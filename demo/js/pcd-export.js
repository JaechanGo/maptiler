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
// ── 구조 ──────────────────────────────────────────────────────────────────
//  §4 수집(메인 스레드 — querySourceFeatures 는 메인에서만) → §5 샘플링(Web Worker, 지도 조작 안 멈춤)
//  → §2 인코딩·다운로드(메인) + §6 미리보기(평면도·등각 투영 캔버스).
//  [2026-09-15 실측] 강남역 500m·지형 10m·건물 1m = 859,789점: 메인 스레드 격자별 폴리곤 판정 35s
//  → 지붕 스캔라인 채우기 + Worker 로 이동. 미리보기는 같은 샘플러를 굵은 간격(지형 20m·건물 4m,
//  점 수 약 1/16)으로 돌려 수 초 안에 그리고, 내보낸 뒤엔 실제 버퍼를 그대로 그린다.
//
// ── B안(서버 /pcd) 전환 지점 ──────────────────────────────────────────────
//  §4 '데이터 수집'만 fetch('/pcd?bbox=…') 로 교체하면 §2 포맷·§5 샘플링·§6 미리보기·§7 UI 는 그대로다.
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
  const PREVIEW_TSTEP = 20, PREVIEW_BSTEP = 4;   // 미리보기 간격(점 수 ≈ 1/16)
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

  // res = {n, xyz: Float32Array(n*3), rgb: Uint32Array(n)} — Worker 가 돌려준 버퍼
  function encodePcd(res, color, ascii, meta) {
    const n = res.n, xyz = res.xyz, rgb = res.rgb, stride = color ? 4 : 3;
    const header = pcdHeader(n, color, ascii, meta);
    if (ascii) {
      // PCL 의 ascii 규약과 동일하게 rgb 도 "float 로 재해석한 값"을 적는다.
      const conv = new DataView(new ArrayBuffer(4));
      const out = [header];
      let chunk = [];
      for (let i = 0; i < n; i++) {
        const j = i * 3;
        let line = xyz[j].toFixed(3) + ' ' + xyz[j + 1].toFixed(3) + ' ' + xyz[j + 2].toFixed(3);
        if (color) { conv.setUint32(0, rgb[i], true); line += ' ' + conv.getFloat32(0, true); }
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
      dv.setFloat32(o, xyz[j], true);
      dv.setFloat32(o + 4, xyz[j + 1], true);
      dv.setFloat32(o + 8, xyz[j + 2], true);
      o += 12;
      if (color) { dv.setUint32(o, rgb[i], true); o += 4; }   // 비트패턴 그대로 = float 재해석
    }
    return new Blob([head, body], { type: 'application/octet-stream' });
  }

  // ── §3 색상 램프 ───────────────────────────────────────────────────────
  const rgb = (r, g, b) => ((r & 255) << 16) | ((g & 255) << 8) | (b & 255);
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

  // ── §4 데이터 수집 (메인 스레드) ────────────────────────────────────────
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
    for (let x = x0; x <= x1; x++) for (let y = y0; y <= y1; y++) if (!this.tiles.has(x + '/' + y)) jobs.push([x, y]);
    await Promise.all(jobs.map(async ([x, y]) => {
      const url = this.tpl.replace('{z}', this.z).replace('{x}', x).replace('{y}', y);
      try {
        const r = await fetch(url);
        if (!r.ok) { this.missing = true; return; }          // 바다·범위 밖은 404 가 정상
        const bmp = await createImageBitmap(await r.blob());
        this.ts = bmp.width;
        cv.width = bmp.width; cv.height = bmp.height;
        const ctx = cv.getContext('2d', { willReadFrequently: true });
        ctx.drawImage(bmp, 0, 0);
        this.tiles.set(x + '/' + y, ctx.getImageData(0, 0, bmp.width, bmp.height).data);
        bmp.close();
      } catch (e) { this.missing = true; console.warn('DEM 타일 실패', url, e); }
    }));
  };
  DemSampler.prototype.toJob = function () {   // Worker 로 넘길 평탄 표현(구조적 복제)
    return { z: this.z, ts: this.ts, base: DEM_BASE, step: DEM_STEP, tiles: [...this.tiles.entries()] };
  };

  // ENU 평탄 링 배열로 변환. 링은 [x0,y0,x1,y1,…] Float64Array. 반환 null 이면 상자 밖.
  function toEnuRings(poly, origin, scale, half) {
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
    if (!cn) return null;
    if (maxX < -half || minX > half || maxY < -half || minY > half) return null;   // 내보낼 상자 밖
    return { rings: rings, minX: minX, minY: minY, maxX: maxX, maxY: maxY, cx: cx / cn, cy: cy / cn };
  }

  function polysOf(src, layer, filter) {
    let feats;
    try { feats = map.querySourceFeatures(src, { sourceLayer: layer }); }
    catch (e) { console.warn(layer + ' 조회 실패', e); return []; }
    const out = [];
    for (const f of feats) {
      if (filter && !filter(f.properties || {})) continue;
      const g = f.geometry;
      if (!g) continue;
      if (g.type === 'Polygon') out.push([g.coordinates, f.properties || {}]);
      else if (g.type === 'MultiPolygon') for (const p of g.coordinates) out.push([p, f.properties || {}]);
    }
    return out;
  }

  function collectBuildings(origin, scale, half) {
    const out = [];
    for (const [poly, p] of polysOf(BLD_SRC, BLD_LAYER)) {
      // 적재 규칙(scripts/postgis/load_building.sh)과 동일: 실측높이 > 층수*3.3 > 기본값
      const h = p.render_height > 0 ? +p.render_height : p.levels > 0 ? p.levels * 3.3 : DEFAULT_H;
      const b = toEnuRings(poly, origin, scale, half);
      if (b) { b.h = h; out.push(b); }
    }
    return out;
  }

  // 물·녹지 폴리곤 — 지형점 색 분류용
  function collectCover(origin, scale, half) {
    const conv = (list) => list.map(([poly]) => toEnuRings(poly, origin, scale, half)).filter(Boolean);
    return {
      green: conv(polysOf(OMT_SRC, 'landcover', p => GREEN_CLASS.has(p.class))).concat(conv(polysOf(OMT_SRC, 'park'))),
      water: conv(polysOf(OMT_SRC, 'water')),
    };
  }

  // 도로·철도 — OpenMapTiles transportation 은 중심선(LineString)이라 폭 정보가 없다.
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

  function roadLength(roads) {   // 예상 점 수 산정용 — 폭을 감안한 리본 면적
    let a = 0;
    for (const road of roads) {
      const r = road.pts;
      for (let i = 2; i < r.length; i += 2) a += Math.hypot(r[i] - r[i - 2], r[i + 1] - r[i - 1]) * road.w;
    }
    return a;
  }

  // ── §5 샘플링 (Web Worker) ─────────────────────────────────────────────
  // 메인 스레드에서 돌리면 수십 초 동안 지도가 멈춘다. 순수 계산이라 Worker 로 보낸다.
  // 아래 함수는 toString() 으로 직렬화되어 Blob Worker 가 되므로 바깥 스코프를 참조하면 안 된다(cfg 로 전달).
  function workerMain() {
    function Dem(d) { this.z = d.z; this.ts = d.ts; this.base = d.base; this.step = d.step; this.tiles = new Map(d.tiles); }
    Dem.prototype.pixelOf = function (lon, lat) {
      const s = this.ts * (1 << this.z), p = lat * Math.PI / 180;
      return [(lon + 180) / 360 * s, (1 - Math.log(Math.tan(p) + 1 / Math.cos(p)) / Math.PI) / 2 * s];
    };
    Dem.prototype.px = function (gx, gy) {      // 전역 픽셀 → 고도(없으면 null)
      const t = this.tiles.get(Math.floor(gx / this.ts) + '/' + Math.floor(gy / this.ts));
      if (!t) return null;
      const i = ((gy % this.ts | 0) * this.ts + (gx % this.ts | 0)) * 4;
      return this.base + (t[i] * 65536 + t[i + 1] * 256 + t[i + 2]) * this.step;
    };
    Dem.prototype.elevation = function (lon, lat) {   // 이중선형 — 30m DEM 을 부드럽게
      const p = this.pixelOf(lon, lat), fx = p[0] - 0.5, fy = p[1] - 0.5;
      const x0 = Math.floor(fx), y0 = Math.floor(fy), dx = fx - x0, dy = fy - y0;
      const v00 = this.px(x0, y0);
      if (v00 === null) return null;
      const v10 = this.px(x0 + 1, y0), v01 = this.px(x0, y0 + 1), v11 = this.px(x0 + 1, y0 + 1);
      if (v10 === null || v01 === null || v11 === null) return v00;   // 타일 가장자리는 최근접으로
      return v00 * (1 - dx) * (1 - dy) + v10 * dx * (1 - dy) + v01 * (1 - dx) * dy + v11 * dx * dy;
    };

    // 점 버퍼 — 수백만 점을 다루므로 배열이 아닌 증분 TypedArray.
    function PointBuf() { this.n = 0; this.cap = 1 << 16; this.xyz = new Float32Array(this.cap * 3); this.rgb = new Uint32Array(this.cap); }
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

    const rgb = (r, g, b) => ((r & 255) << 16) | ((g & 255) << 8) | (b & 255);
    function ramp(stops, t) {
      t = t < 0 ? 0 : t > 1 ? 1 : t;
      const s = t * (stops.length - 1), i = Math.min(stops.length - 2, Math.floor(s)), f = s - i;
      const a = stops[i], b = stops[i + 1];
      return rgb(a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f, a[2] + (b[2] - a[2]) * f);
    }

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

    // 중복 제거용 복셀 키 — 타일 버퍼(buffer:64)로 겹쳐 들어온 같은 건물의 중복 점을 접는다.
    function voxelKey(x, y, z, s) {
      const ix = Math.round(x / s) + 32768, iy = Math.round(y / s) + 32768, iz = Math.round(z / s) + 32768;
      if (ix < 0 || ix > 65535 || iy < 0 || iy > 65535 || iz < 0 || iz > 65535) return -1;
      return (ix * 65536 + iy) * 65536 + iz;   // < 2^53 이라 Number 로 정확
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

    // 지표피복 — 물·녹지 폴리곤 안에 드는 지형 격자점을 표시한다(점을 새로 만들지 않아 용량 불변).
    function classifyCover(grid, cover) {
      const out = new Uint8Array(grid.w * grid.w);       // 0=지표 1=물 2=녹지
      const put = (polys, code) => {
        for (const p of polys) {
          // 폴리곤 bbox 안의 격자칸만 검사 — 전수 검사보다 훨씬 싸다
          const i0 = Math.max(-grid.n, Math.ceil(p.minX / grid.step)), i1 = Math.min(grid.n, Math.floor(p.maxX / grid.step));
          const j0 = Math.max(-grid.n, Math.ceil(p.minY / grid.step)), j1 = Math.min(grid.n, Math.floor(p.maxY / grid.step));
          for (let iy = j0; iy <= j1; iy++) {
            for (let ix = i0; ix <= i1; ix++) {
              const k = (iy + grid.n) * grid.w + (ix + grid.n);
              if (grid.idx[k] < 0 || out[k] === 1) continue;    // 물이 녹지보다 우선
              if (inPolygon(p.rings, ix * grid.step, iy * grid.step)) out[k] = code;
            }
          }
        }
      };
      put(cover.green, 2);
      put(cover.water, 1);
      return out;
    }

    function colorTerrain(buf, grid, cover, color, cfg) {
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
        buf.rgb[i] = cover && cover[k] === 1 ? ramp(cfg.WATER, t)
          : cover && cover[k] === 2 ? ramp(cfg.GREEN, t)
          : ramp(cfg.HYPSO, t);
      }
    }

    // 도로 리본 — 중심선을 따라가며 폭 방향으로 step 간격 샘플, 지형 위 lift 만큼 띄움
    function sampleRoads(buf, seen, dem, roads, origin, scale, half, step, color, lift) {
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
              const z = dem.elevation(origin.lon + x / scale.mx, origin.lat + y / scale.my);
              if (z === null) continue;
              const key = voxelKey(x, y, z, step);
              if (key !== -1) { if (seen.has(key)) continue; seen.add(key); }
              buf.push(x, y, z + lift, color ? road.c : 0);
            }
          }
        }
      }
    }

    // 건물 — 지붕은 스캔라인 채우기(행마다 교차점 정렬 → even-odd 구간 채움: 격자마다 폴리곤 판정하던
    // 것보다 수십 배 싸다, 구멍도 자동), 벽면은 외곽선을 따라 지반→지붕 수직 샘플.
    function sampleBuilding(buf, seen, dem, b, origin, scale, half, step, walls, color, cfg, xs) {
      const gc0 = origin.lon + b.cx / scale.mx, gc1 = origin.lat + b.cy / scale.my;
      const base = dem.elevation(gc0, gc1);
      if (base === null) return;
      const roof = base + b.h;
      const put = (x, y, z) => {
        if (x < -half || x > half || y < -half || y > half) return;
        const k = voxelKey(x, y, z, step);
        if (k !== -1) { if (seen.has(k)) return; seen.add(k); }
        buf.push(x, y, z, color ? ramp(cfg.WARM, b.h > 0 ? (z - base) / b.h : 1) : 0);
      };
      const iy0 = Math.max(Math.ceil(b.minY / step), Math.ceil(-half / step)), iy1 = Math.min(Math.floor(b.maxY / step), Math.floor(half / step));
      for (let iy = iy0; iy <= iy1; iy++) {
        const y = iy * step;
        let m = 0;
        for (const r of b.rings) {
          for (let i = 0, j = r.length - 2; i < r.length; j = i, i += 2) {
            const yi = r[i + 1], yj = r[j + 1];
            if ((yi > y) !== (yj > y)) xs[m++] = r[j] + (y - yj) * (r[i] - r[j]) / (yi - yj);
          }
        }
        if (m < 2) continue;
        const seg = xs.subarray(0, m); seg.sort();
        for (let k = 0; k + 1 < m; k += 2) {
          const ix0 = Math.ceil(seg[k] / step), ix1 = Math.floor(seg[k + 1] / step);
          for (let ix = ix0; ix <= ix1; ix++) put(ix * step, y, roof);
        }
      }
      if (!walls) return;
      for (const r of b.rings) {
        for (let i = 0, j = r.length - 2; i < r.length; j = i, i += 2) {
          const x0 = r[j], y0 = r[j + 1], dx = r[i] - x0, dy = r[i + 1] - y0;
          const len = Math.hypot(dx, dy);
          const m = Math.max(1, Math.ceil(len / step));
          for (let k = 0; k < m; k++) {
            const x = x0 + dx * k / m, y = y0 + dy * k / m;
            if (x < -half || x > half || y < -half || y > half) continue;
            let z = dem.elevation(origin.lon + x / scale.mx, origin.lat + y / scale.my);
            if (z === null) z = base;
            if (z > roof) { put(x, y, roof); continue; }
            for (; z < roof; z += step) put(x, y, z);
            put(x, y, roof);
          }
        }
      }
    }

    self.onmessage = (e) => {
      const j = e.data, o = j.opts, cfg = j.cfg, t0 = Date.now();
      const progress = (text) => self.postMessage({ type: 'progress', id: j.id, text: text });
      try {
        const dem = new Dem(j.dem);
        const buf = new PointBuf();
        if (o.terrain) {
          progress('지형 샘플링…');
          const grid = sampleTerrain(buf, dem, j.origin, j.scale, j.half, o.tStep);
          let cover = null;
          if (o.cover) { progress('물·녹지 분류…'); cover = classifyCover(grid, j.cover); }
          colorTerrain(buf, grid, cover, o.color, cfg);
        }
        if (o.bld) {
          const seen = new Set(), xs = new Float64Array(4096);
          for (let i = 0; i < j.blds.length; i++) {
            sampleBuilding(buf, seen, dem, j.blds[i], j.origin, j.scale, j.half, o.bStep, o.walls, o.color, cfg, xs);
            if (i % 500 === 0) progress('건물 ' + i + '/' + j.blds.length + ' · ' + buf.n.toLocaleString() + ' 점');
          }
        }
        if (o.road) {
          progress('도로 샘플링… (' + j.roads.length + '개 구간)');
          sampleRoads(buf, new Set(), dem, j.roads, j.origin, j.scale, j.half, o.bStep, o.color, cfg.ROAD_LIFT);
        }
        const xyz = buf.xyz.slice(0, buf.n * 3), c = buf.rgb.slice(0, buf.n);
        self.postMessage({ type: 'done', id: j.id, n: buf.n, xyz: xyz, rgb: c, ms: Date.now() - t0 }, [xyz.buffer, c.buffer]);
      } catch (err) {
        self.postMessage({ type: 'error', id: j.id, message: String(err && err.message || err) });
      }
    };
  }

  let worker = null, jobSeq = 0;
  const pending = new Map();   // id → {resolve, reject, onProgress}
  function getWorker() {
    if (worker) return worker;
    const src = '(' + workerMain.toString() + ')();';
    worker = new Worker(URL.createObjectURL(new Blob([src], { type: 'text/javascript' })));
    worker.onmessage = (e) => {
      const m = e.data, p = pending.get(m.id);
      if (!p) return;
      if (m.type === 'progress') { if (p.onProgress) p.onProgress(m.text); return; }
      pending.delete(m.id);
      if (m.type === 'done') p.resolve(m); else p.reject(new Error(m.message));
    };
    worker.onerror = (e) => { for (const p of pending.values()) p.reject(new Error(e.message || 'worker 오류')); pending.clear(); };
    return worker;
  }
  function runSampling(job, onProgress) {
    job.id = ++jobSeq;
    job.cfg = { HYPSO: HYPSO, WARM: WARM, WATER: WATER, GREEN: GREEN, ROAD_LIFT: ROAD_LIFT };
    return new Promise((resolve, reject) => {
      pending.set(job.id, { resolve: resolve, reject: reject, onProgress: onProgress });
      getWorker().postMessage(job);
    });
  }

  // 수집 + 샘플링 한 번 — 미리보기와 내보내기가 공유. 반환 {n, xyz, rgb, ms, blds, roads, dem}
  async function generate(o, origin, scale, half, onProgress) {
    const dx = half / scale.mx, dy = half / scale.my;
    const bbox = [origin.lon - dx, origin.lat - dy, origin.lon + dx, origin.lat + dy];
    onProgress('지형 타일 로드 중…');
    const dem = new DemSampler(DEM_Z);
    await dem.load(bbox);
    if (!dem.tiles.size) throw new Error('지형 타일을 가져오지 못했습니다 (/data/terrain 확인)');
    const blds = o.bld ? collectBuildings(origin, scale, half) : [];
    const roads = o.road ? collectRoads(origin, scale, half) : [];
    const cover = o.terrain && o.cover ? collectCover(origin, scale, half) : { green: [], water: [] };
    const res = await runSampling({ dem: dem.toJob(), origin: origin, scale: scale, half: half, opts: o, blds: blds, roads: roads, cover: cover }, onProgress);
    res.blds = blds; res.roads = roads; res.missing = dem.missing;
    return res;
  }

  // ── §6 미리보기 (평면도 + 등각 투영) ────────────────────────────────────
  // 점을 깊이 순으로 그려 가까운 것이 덮게 한다(버킷 정렬 — 수십만 점도 수백 ms).
  function drawTop(cv, res, half) {
    const S = cv.width, n = res.n, xyz = res.xyz, rgb = res.rgb;
    const ctx = cv.getContext('2d'); const img = ctx.createImageData(S, S); const d = img.data;
    for (let i = 3; i < d.length; i += 4) d[i] = 255;
    const sc = S / (2 * half);
    let zmin = Infinity, zmax = -Infinity;
    for (let i = 0; i < n; i++) { const z = xyz[i * 3 + 2]; if (z < zmin) zmin = z; if (z > zmax) zmax = z; }
    const NB = 256, cnt = new Int32Array(NB + 1), zb = new Uint8Array(n), dz = (zmax - zmin) || 1;
    for (let i = 0; i < n; i++) { const b = Math.min(NB - 1, Math.floor((xyz[i * 3 + 2] - zmin) / dz * NB)); zb[i] = b; cnt[b + 1]++; }
    for (let b = 0; b < NB; b++) cnt[b + 1] += cnt[b];
    const order = new Int32Array(n), pos = cnt.slice(0, NB);
    for (let i = 0; i < n; i++) order[pos[zb[i]]++] = i;
    for (let k = 0; k < n; k++) {          // 낮은 z 부터 → 지붕이 지형·도로를 덮는다
      const i = order[k], px = Math.floor((xyz[i * 3] + half) * sc), py = Math.floor((half - xyz[i * 3 + 1]) * sc);
      if (px < 0 || py < 0 || px >= S || py >= S) continue;
      const o = (py * S + px) * 4, c = rgb[i];
      d[o] = (c >> 16) & 255; d[o + 1] = (c >> 8) & 255; d[o + 2] = c & 255;
    }
    ctx.putImageData(img, 0, 0);
    return { zmin: zmin, zmax: zmax };
  }
  function drawIso(cv, res, half, zr) {
    const W = cv.width, H = cv.height, n = res.n, xyz = res.xyz, rgb = res.rgb;
    const ctx = cv.getContext('2d'); const img = ctx.createImageData(W, H); const d = img.data;
    for (let i = 3; i < d.length; i += 4) d[i] = 255;
    const r2 = Math.SQRT1_2, span = 2 * half * r2;                // 회전 후 u 범위 = ±half·√2
    const s = Math.min(W / (span * 1.05), H / (span * 0.5 + (zr.zmax - zr.zmin) * 0.85 + 10));
    const cx = W / 2, cy = H * 0.62;
    const NV = 512, cnt = new Int32Array(NV + 1), vb = new Uint16Array(n);
    for (let i = 0; i < n; i++) { const v = (xyz[i * 3] + xyz[i * 3 + 1]) * r2; const b = Math.min(NV - 1, Math.max(0, Math.floor((v + span / 2) / span * NV))); vb[i] = b; cnt[b + 1]++; }
    for (let b = 0; b < NV; b++) cnt[b + 1] += cnt[b];
    const order = new Int32Array(n), pos = cnt.slice(0, NV);
    for (let i = 0; i < n; i++) order[pos[vb[i]]++] = i;
    for (let k = n - 1; k >= 0; k--) {     // 먼 것(v 큰 것)부터 → 가까운 점이 덮는다
      const i = order[k], x = xyz[i * 3], y = xyz[i * 3 + 1], z = xyz[i * 3 + 2];
      const u = (x - y) * r2, v = (x + y) * r2;
      const sx = Math.floor(cx + u * s), sy = Math.floor(cy + v * 0.5 * s - (z - zr.zmin) * 0.85 * s);
      if (sx < 0 || sy < 0 || sx >= W || sy >= H) continue;
      const o = (sy * W + sx) * 4, c = rgb[i];
      d[o] = (c >> 16) & 255; d[o + 1] = (c >> 8) & 255; d[o + 2] = c & 255;
    }
    ctx.putImageData(img, 0, 0);
  }

  // ── §7 UI ──────────────────────────────────────────────────────────────
  const BOX_SRC = 'pcd-export-box', BOX_LAYER = 'pcd-export-box-line';
  const btn = document.createElement('button');
  btn.className = 'ctl';
  btn.textContent = 'PCD 내보내기';
  controls.appendChild(btn);

  // 로딩 스피너(미리보기·내보내기 진행 중) — CSS 1회 주입
  const css = document.createElement('style');
  css.textContent = '@keyframes pcd-spin{to{transform:rotate(360deg)}}' +
    '.pcd-spinner{display:inline-block;width:12px;height:12px;border:2px solid #3a4656;border-top-color:#e8b84a;' +
    'border-radius:50%;animation:pcd-spin .8s linear infinite;vertical-align:-2px;margin-right:6px;flex:none}' +
    '.pcd-spinner.big{width:34px;height:34px;border-width:4px;margin:0 0 10px 0}' +
    '#pcd-pv-busy{position:absolute;inset:0;display:none;flex-direction:column;align-items:center;justify-content:center;' +
    'background:rgba(11,15,20,.72);color:#e8eef7;font-size:12px;border-radius:8px}' +
    '.ctl[disabled]{opacity:.45;cursor:default}';
  document.head.appendChild(css);

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
    '<div style="display:flex;gap:6px;margin-top:8px">' +
    '<button id="pcd-preview" class="ctl" style="flex:1">미리보기</button>' +
    '<button id="pcd-go" class="ctl" style="flex:1">내보내기</button></div>' +
    '<div style="display:flex;align-items:flex-start;margin-top:6px;min-height:1.7em">' +
    '<span id="pcd-spin" class="pcd-spinner" style="display:none;margin-top:4px"></span>' +
    '<div id="pcd-status" style="color:#7d8aa0;flex:1"></div></div>' +
    '<div style="margin-top:6px;color:#5d6a80;font-size:11px">미리보기는 지형 ' + PREVIEW_TSTEP + 'm·건물 ' + PREVIEW_BSTEP +
    'm 로 성기게 만든 것(점 수 ≈ 1/16)이고, 내보낸 뒤엔 실제 점군을 그립니다. DEM 원본 30m · 도로는 중심선뿐이라 ' +
    'class 별 대표 실폭으로 폅니다 · 건물 폴리곤은 타일 경계에서 잘려 내부에 가짜 벽면이 생길 수 있습니다(서버 내보내기에서 해소).</div>';
  document.body.appendChild(panel);

  // 미리보기 창 — 패널 오른쪽. 평면도(정사각) + 등각 투영.
  const pv = document.createElement('div');
  pv.style.cssText = 'position:fixed;top:52px;left:290px;z-index:12;display:none;background:#0b0f14;' +
    'border:1px solid #2c3542;border-radius:8px;padding:8px;color:#cdd6e3;font-size:11px;line-height:1.5;font-family:inherit;overflow:hidden';
  const PV_S = 300, PV_W = 360;
  pv.innerHTML =
    '<div id="pcd-pv-title" style="color:#e8eef7;font-weight:600;margin-bottom:6px"></div>' +
    '<div style="display:flex;gap:8px">' +
    '<div><canvas id="pcd-pv-top" width="' + PV_S + '" height="' + PV_S + '" style="display:block;background:#000"></canvas>' +
    '<div style="color:#7d8aa0;margin-top:3px">평면도(위에서, 북쪽이 위)</div></div>' +
    '<div><canvas id="pcd-pv-iso" width="' + PV_W + '" height="' + PV_S + '" style="display:block;background:#000"></canvas>' +
    '<div style="color:#7d8aa0;margin-top:3px">등각 투영(남서 → 북동)</div></div></div>' +
    '<div style="color:#5d6a80;margin-top:4px">지표=하이프소메트릭 · 수역=청 · 녹지=녹 · 건물=난색(높을수록 밝음) · 도로=주황/노랑/회색 · 철도=보라</div>' +
    '<div id="pcd-pv-busy"><span class="pcd-spinner big"></span><span id="pcd-pv-busy-text">생성 중…</span></div>';
  document.body.appendChild(pv);

  const el = (id) => panel.querySelector('#' + id);
  const opts = () => ({
    side: +el('pcd-side').value, tStep: +el('pcd-tstep').value, bStep: +el('pcd-bstep').value,
    terrain: el('pcd-terrain').checked, bld: el('pcd-bld').checked, walls: el('pcd-walls').checked,
    road: el('pcd-road').checked, cover: el('pcd-cover').checked,
    color: el('pcd-color').checked, ascii: el('pcd-ascii').checked,
  });

  function showPreview(res, half, title) {
    const zr = drawTop(pv.querySelector('#pcd-pv-top'), res, half);
    drawIso(pv.querySelector('#pcd-pv-iso'), res, half, zr);
    pv.querySelector('#pcd-pv-title').textContent = title + ' · z ' + zr.zmin.toFixed(0) + '~' + zr.zmax.toFixed(0) + 'm';
    pv.style.display = 'block';
  }

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
    if (open) refresh(); else { clearBox(); pv.style.display = 'none'; }
  };
  panel.addEventListener('change', refresh);
  // 패널이 열린 동안만 동작 — 건물 재수집이 무겁지 않도록 디바운스.
  let refreshTimer = null;
  map.on('moveend', () => { clearTimeout(refreshTimer); refreshTimer = setTimeout(refresh, 200); });

  let busy = false;
  const setBusy = (v, label) => {
    busy = v; el('pcd-go').disabled = v; el('pcd-preview').disabled = v;
    el('pcd-spin').style.display = v ? 'inline-block' : 'none';
    // 미리보기 창이 이미 열려 있으면 그 위에 큰 스피너로 덮는다(이전 그림이 최신인 줄 오해하지 않게)
    const ov = pv.querySelector('#pcd-pv-busy');
    ov.style.display = v && pv.style.display !== 'none' ? 'flex' : 'none';
    if (label) pv.querySelector('#pcd-pv-busy-text').textContent = label;
  };
  const progress = (status, prefix) => (t) => {
    status.textContent = prefix + t;
    pv.querySelector('#pcd-pv-busy-text').textContent = prefix + t;
  };

  // 미리보기 — 같은 파이프라인을 굵은 간격으로. 색상은 항상 켜서 레이어가 구분되게 한다.
  el('pcd-preview').onclick = async () => {
    if (busy) return;
    const status = el('pcd-status');
    const o = opts();
    if (!o.terrain && !o.bld && !o.road) { status.textContent = '지형·건물·도로 중 하나는 선택해야 합니다.'; return; }
    setBusy(true, '미리보기 생성 중…');
    try {
      const c = map.getCenter(), origin = { lon: c.lng, lat: c.lat };
      const scale = enuScale(origin.lat), half = o.side / 2;
      const po = Object.assign({}, o, { tStep: Math.max(o.tStep, PREVIEW_TSTEP), bStep: Math.max(o.bStep, PREVIEW_BSTEP), color: true });
      const t0 = performance.now();
      const res = await generate(po, origin, scale, half, progress(status, '미리보기 '));
      const sec = ((performance.now() - t0) / 1000).toFixed(1);
      showPreview(res, half, '미리보기(성긴 샘플 ' + po.tStep + 'm/' + po.bStep + 'm) · ' + res.n.toLocaleString() + ' 점 · ' + sec + 's');
      status.textContent = '미리보기 완료 · ' + res.n.toLocaleString() + ' 점 · ' + sec + 's (실제 내보내기는 예상 점 수 기준)';
    } catch (e) {
      console.error(e);
      status.textContent = '미리보기 실패: ' + e.message;
    }
    setBusy(false);
  };

  el('pcd-go').onclick = async () => {
    if (busy) return;
    const status = el('pcd-status');
    const o = opts();
    if (!o.terrain && !o.bld && !o.road) { status.textContent = '지형·건물·도로 중 하나는 선택해야 합니다.'; return; }
    setBusy(true, '내보내기 생성 중…');
    try {
      const c = map.getCenter(), origin = { lon: c.lng, lat: c.lat };
      const scale = enuScale(origin.lat), half = o.side / 2;
      // 예상 점 수가 크면 확인
      const blds0 = o.bld ? collectBuildings(origin, scale, half) : [];
      const px = o.bStep * o.bStep, s = stats(blds0);
      const est = (o.terrain ? Math.pow(2 * Math.floor(half / o.tStep) + 1, 2) : 0) +
        s.area / px + (o.walls ? s.wall / px : 0) + (o.road ? roadLength(collectRoads(origin, scale, half)) / px : 0);
      if (est > WARN_POINTS &&
          !confirm('예상 ' + Math.round(est).toLocaleString() + ' 점입니다. 브라우저가 느려질 수 있습니다. 계속할까요?')) {
        status.textContent = '취소했습니다.'; setBusy(false); return;
      }
      const t0 = performance.now();
      const res = await generate(o, origin, scale, half, progress(status, ''));
      if (!res.n) throw new Error('생성된 점이 없습니다 — 범위·레이어 설정을 확인하세요.');
      const genSec = ((performance.now() - t0) / 1000).toFixed(1);

      progress(status, '')('PCD 인코딩 중… (' + res.n.toLocaleString() + ' 점)');
      await new Promise(r => setTimeout(r, 0));
      const blob = encodePcd(res, o.color, o.ascii, {
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
      status.textContent = '완료: ' + name + ' · ' + res.n.toLocaleString() + ' 점 · ' +
        (blob.size / 1048576).toFixed(1) + ' MB · 생성 ' + genSec + 's' + (res.missing ? ' (일부 DEM 타일 없음)' : '');
      // 내보낸 실제 점군을 그대로 미리보기(색상 껐으면 회색 단색)
      if (!o.color) { const g = rgb(200, 200, 200); res.rgb.fill(g); }
      showPreview(res, half, '내보낸 점군 · ' + res.n.toLocaleString() + ' 점 · 생성 ' + genSec + 's');
    } catch (e) {
      console.error(e);
      status.textContent = '실패: ' + e.message;
    }
    setBusy(false);
  };
})();
