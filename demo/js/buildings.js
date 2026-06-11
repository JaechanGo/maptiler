// 'Building 3D' fill-extrusion 레이어 표시 토글.
(function () {
  const map = window.cuviaMap;
  if (!map) { console.warn('buildings.js: cuviaMap 미초기화 — map.js 로드 순서 확인'); return; }
  const controls = document.getElementById('controls');
  if (!controls) { console.warn('buildings.js: #controls 없음'); return; }
  const btn = document.createElement('button');
  btn.className = 'ctl';
  btn.textContent = '3D 건물 ON';
  let visible = true; // 초기값 = 레이어 기본(visible). 외부 코드가 직접 토글하면 desync 가능(데모 한정 허용).
  btn.onclick = () => {
    if (!map.getLayer('Building 3D')) return; // 스타일 파싱 전 클릭 무시 (terrain.js와 동일 패턴)
    visible = !visible;
    map.setLayoutProperty('Building 3D', 'visibility', visible ? 'visible' : 'none');
    btn.textContent = visible ? '3D 건물 ON' : '3D 건물 OFF';
  };
  controls.appendChild(btn);
})();
