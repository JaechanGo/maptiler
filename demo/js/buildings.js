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
