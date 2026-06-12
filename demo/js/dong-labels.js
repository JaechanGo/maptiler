// 동 라벨(dong-dot/dong-label) 표시 토글.
// 라벨은 스타일에 내장된 벡터타일 소스(dong.mbtiles — scripts/04·05 생성물)에서 나온다.
// 데모는 buildings.js 와 동일하게 visibility 만 토글한다.
(function () {
  const map = window.cuviaMap;
  if (!map) { console.warn('dong-labels.js: cuviaMap 미초기화 — map.js 로드 순서 확인'); return; }
  const controls = document.getElementById('controls');
  if (!controls) { console.warn('dong-labels.js: #controls 없음'); return; }
  const btn = document.createElement('button');
  btn.className = 'ctl';
  btn.textContent = '동 라벨 ON';
  let visible = true; // 초기값 = 레이어 기본(visible)
  btn.onclick = () => {
    if (!map.getLayer('dong-label')) return; // 스타일 파싱 전 클릭 무시 (buildings.js와 동일 패턴)
    visible = !visible;
    const v = visible ? 'visible' : 'none';
    map.setLayoutProperty('dong-label', 'visibility', v);
    map.setLayoutProperty('dong-dot', 'visibility', v);
    btn.textContent = visible ? '동 라벨 ON' : '동 라벨 OFF';
  };
  controls.appendChild(btn);
})();
