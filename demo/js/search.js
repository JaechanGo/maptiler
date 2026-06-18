// 지오코딩/역지오코딩 데모 — server/geocode-api.py(:8082) 호출.
// 검색창에 입력 → /geocode → 결과 클릭 시 지도 이동. 지도 우클릭 → /reverse 팝업.
(function () {
  const map = window.cuviaMap;
  if (!map) { console.warn('search.js: cuviaMap 미초기화'); return; }
  // 지오코드 API 주소 — 게이트웨이 경유면 same-origin(/geocode), 직접 포트(:8081)면 같은 호스트의 :8082.
  const params = new URLSearchParams(location.search);
  const VIA_GATEWAY = ['', '80', '443', '8088'].includes(location.port);
  const GEOCODE = params.get('geocode') || (VIA_GATEWAY ? '' : `http://${location.hostname}:8082`);
  const TYPE_KO = { station: '역', place: '지명', dong: '동', road: '도로', poi: 'POI', biz: '상가' };

  // ---- UI ----
  const box = document.createElement('div');
  box.style.cssText = 'position:fixed;top:10px;left:50%;transform:translateX(-50%);z-index:11;width:340px;font-size:13px';
  box.innerHTML =
    '<input id="gc-q" placeholder="지오코딩: 증미역 · 101동 · 테헤란로 …" autocomplete="off" ' +
    'style="width:100%;box-sizing:border-box;padding:9px 12px;background:#1a2029;color:#e8eef7;' +
    'border:1px solid #2c3542;border-radius:6px;outline:none">' +
    '<div id="gc-list" style="margin-top:4px;background:#141a22;border:1px solid #2c3542;border-radius:6px;' +
    'overflow:hidden;display:none"></div>';
  document.body.appendChild(box);
  const input = box.querySelector('#gc-q');
  const list = box.querySelector('#gc-list');
  let marker = null;

  function flyTo(lon, lat, label) {
    if (marker) marker.remove();
    marker = new maplibregl.Marker({ color: '#e8b84a' }).setLngLat([lon, lat])
      .setPopup(new maplibregl.Popup({ offset: 24 }).setText(label)).addTo(map);
    marker.togglePopup();
    map.flyTo({ center: [lon, lat], zoom: 16, speed: 1.4 });
  }

  function render(results) {
    if (!results.length) { list.style.display = 'none'; return; }
    list.innerHTML = '';
    results.forEach(r => {
      const row = document.createElement('div');
      row.style.cssText = 'padding:8px 12px;cursor:pointer;border-top:1px solid #222b38;color:#cdd6e3';
      row.onmouseenter = () => row.style.background = '#1c2530';
      row.onmouseleave = () => row.style.background = '';
      row.innerHTML = '<b style="color:#e8eef7">' + r.name + '</b> ' +
        '<span style="color:#7d8aa0">· ' + (TYPE_KO[r.type] || r.type) +
        (r.subtype ? '/' + r.subtype : '') + '</span>';
      row.onclick = () => { flyTo(r.lon, r.lat, r.name); list.style.display = 'none'; input.value = r.name; };
      list.appendChild(row);
    });
    list.style.display = 'block';
  }

  let timer = null;
  input.addEventListener('input', () => {
    clearTimeout(timer);
    const q = input.value.trim();
    if (q.length < 1) { list.style.display = 'none'; return; }
    timer = setTimeout(() => {
      fetch(GEOCODE + '/geocode?q=' + encodeURIComponent(q) + '&limit=8')
        .then(r => r.json()).then(d => render(d.results || []))
        .catch(e => { console.warn('geocode 호출 실패:', e); list.style.display = 'none'; });
    }, 220);
  });
  input.addEventListener('keydown', e => { if (e.key === 'Enter') { const f = list.querySelector('div'); if (f) f.click(); } });
  document.addEventListener('click', e => { if (!box.contains(e.target)) list.style.display = 'none'; });

  // ---- 역지오코딩: 지도 우클릭 ----
  map.on('contextmenu', e => {
    const { lng, lat } = e.lngLat;
    fetch(GEOCODE + '/reverse?lon=' + lng + '&lat=' + lat + '&limit=4')
      .then(r => r.json()).then(d => {
        const near = (d.nearest || []).map(n => n.name + ' (' + Math.round(n.dist_m) + 'm)').join('<br>') || '주변 결과 없음';
        const areas = (d.areas || []).map(a => a.name).join(', ');
        const html = '<div style="font-size:12px;line-height:1.5">' +
          (areas ? '<b>' + areas + '</b><br>' : '') + near + '</div>';
        new maplibregl.Popup({ offset: 8 }).setLngLat([lng, lat]).setHTML(html).addTo(map);
      }).catch(e2 => console.warn('reverse 호출 실패:', e2));
  });
})();
