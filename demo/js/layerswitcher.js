// 레이어 타입 스위처 — 기본(스튜디오 적용) / 2D / 3D / 위성.
// 기본·2D·3D 는 같은 스타일에서 카메라(pitch)와 건물 레이어 표시만 전환한다(스타일 추가 없음 —
// 스튜디오 테마·오버라이드가 세 모드 모두에 그대로 적용). 위성은 정사영상 raster 소스
// (/data/satellite.json, 폐쇄망 반입 — scripts/gen_satellite_mbtiles.sh + runbook (4c))를
// 베이스로 깔고 면(녹지·물·건물)을 숨긴 하이브리드(도로·라벨 유지). 원사영상이 미탑재면
// 위성 버튼은 비활성 — 탑재·등록 후 자동 활성화된다(사전 config 등록은 금물:
// tileserver-gl v5 는 mbtiles 부재 시 크래시 루프, 2026-09-01 실측).
(function () {
  const map = window.cuviaMap;
  const box = document.getElementById('controls');
  if (!map || !box) return;

  const SAT_SRC = 'satellite';
  const SAT_LAYER = 'satellite-base';
  // 위성 모드에서 숨길 면 레이어(있는 것만) — 도로·라벨·필지·경계는 하이브리드로 유지
  const SAT_HIDE = ['landcover', 'park', 'water', 'building-2d', 'Building 3D'];
  const BTNS = {};
  let snapshot = null;          // 기본 모드 복원용 {pitch, vis:{layer:visibility}}
  let satHidden = {};           // 위성 진입 시 숨긴 레이어의 원래 visibility
  let satReady = false;

  const vis = id => (map.getLayer(id) ? (map.getLayoutProperty(id, 'visibility') || 'visible') : null);
  const setVis = (id, v) => { if (map.getLayer(id) && v != null) map.setLayoutProperty(id, 'visibility', v); };

  function takeSnapshot() {
    if (snapshot) return;
    const v = {};
    SAT_HIDE.forEach(id => { const x = vis(id); if (x != null) v[id] = x; });
    snapshot = { pitch: map.getPitch(), vis: v };
  }

  function leaveSatellite() {
    setVis(SAT_LAYER, 'none');
    Object.entries(satHidden).forEach(([id, v]) => setVis(id, v));
    satHidden = {};
  }

  function enterSatellite() {
    if (!map.getSource(SAT_SRC)) {
      map.addSource(SAT_SRC, { type: 'raster',
        url: (typeof TILESERVER !== 'undefined' && TILESERVER ? TILESERVER : '') + '/data/satellite.json',
        tileSize: 256 });
    }
    if (!map.getLayer(SAT_LAYER)) {
      // 배경 바로 위(첫 비-background 레이어 앞) — 도로·라벨은 위성 위에 그려진다
      const first = (map.getStyle().layers || []).find(l => l.type !== 'background');
      map.addLayer({ id: SAT_LAYER, type: 'raster', source: SAT_SRC }, first && first.id);
    }
    setVis(SAT_LAYER, 'visible');
    satHidden = {};
    SAT_HIDE.forEach(id => { const x = vis(id); if (x != null) { satHidden[id] = x; setVis(id, 'none'); } });
  }

  function setMode(mode) {
    takeSnapshot();
    leaveSatellite();
    if (mode === 'default') {
      Object.entries(snapshot.vis).forEach(([id, v]) => setVis(id, v));
      map.easeTo({ pitch: snapshot.pitch, duration: 600 });
    } else if (mode === '2d') {
      setVis('Building 3D', 'none'); setVis('building-2d', 'visible');
      map.easeTo({ pitch: 0, duration: 600 });
    } else if (mode === '3d') {
      setVis('Building 3D', 'visible');
      map.easeTo({ pitch: Math.max(55, snapshot.pitch || 55), duration: 600 });
    } else if (mode === 'sat') {
      if (!satReady) return;
      enterSatellite();
    }
    Object.entries(BTNS).forEach(([k, b]) => {
      b.style.background = k === mode ? '#2b62c9' : '#1a2029';
      b.style.color = k === mode ? '#fff' : '#cdd6e3';
    });
  }

  function addBtn(mode, label, title) {
    const b = document.createElement('button');
    b.className = 'ctl'; b.textContent = label; if (title) b.title = title;
    b.onclick = () => setMode(mode);
    box.appendChild(b); BTNS[mode] = b;
    return b;
  }

  function init() {
    addBtn('default', '기본');
    addBtn('2d', '2D');
    addBtn('3d', '3D');
    const sat = addBtn('sat', '위성');
    sat.disabled = true; sat.style.opacity = '0.45';
    sat.title = '정사영상 미탑재 — runbook (4c) 절차로 satellite.mbtiles 반입 시 활성화';
    // 정사영상 탑재 여부 프로브 — 등록돼 있으면 tilejson 200
    fetch((typeof TILESERVER !== 'undefined' && TILESERVER ? TILESERVER : '') + '/data/satellite.json')
      .then(r => { if (r.ok) return r.json(); throw 0; })
      .then(j => { if (!j || !j.tiles) throw 0;
        satReady = true; sat.disabled = false; sat.style.opacity = '1'; sat.title = '정사영상(하이브리드)'; })
      .catch(() => {});
    setMode('default');
  }

  if (map.isStyleLoaded()) init(); else map.once('load', init);
})();
