// 지형(terrain) 토글. 기본 OFF — 활성 시 원거리 3D 건물이 가려지는
// MapLibre 특성이 있어 소비 프론트도 상황에 따라 켜고 끈다.
(function () {
  const map = window.cuviaMap;
  if (!map) { console.warn('terrain.js: cuviaMap 미초기화 — map.js 로드 순서 확인'); return; }
  const controls = document.getElementById('controls');
  if (!controls) { console.warn('terrain.js: #controls 없음'); return; }
  const btn = document.createElement('button');
  btn.className = 'ctl';
  btn.textContent = '지형 OFF';
  let on = false;
  btn.onclick = () => {
    on = !on;
    map.setTerrain(on ? { source: 'terrain', exaggeration: 1.3 } : null);
    btn.textContent = on ? '지형 ON' : '지형 OFF';
  };
  controls.appendChild(btn);
})();
