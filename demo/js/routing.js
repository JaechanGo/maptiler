// 길찾기 데모 — OSRM(osrm-car·osrm-foot) 게이트웨이 경유 호출. FEAT-007/ADR-009.
// 출발/도착/경유지(geocode 자동완성 + 지도 클릭 지정) → /route/v1/{driving|walking} →
// 경로선(GeoJSON)·총 시간/거리·구간(leg)·턴바이턴 목록 표시.
// 기본 same-origin(게이트웨이 뒤면 포트 무관). 직접 띄웠으면 ?router=http://host:port 지정.
// 주의: 소요시간은 도로등급·제한속도 기반 추정 — 실시간 교통 미반영(패널 하단 고지).
(function () {
  const map = window.cuviaMap;
  if (!map) { console.warn('routing.js: cuviaMap 미초기화'); return; }
  const params = new URLSearchParams(location.search);
  const ROUTER = params.get('router') || '';
  const GEOCODE = params.get('geocode') || '';

  // OSRM maneuver → 한글 안내문. modifier 는 회전 방향, type 은 기동 종류.
  const MOD_KO = {
    'uturn': '유턴', 'sharp right': '급우회전', 'right': '우회전', 'slight right': '우측 방향',
    'straight': '직진', 'slight left': '좌측 방향', 'left': '좌회전', 'sharp left': '급좌회전'
  };
  const TYPE_KO = {
    'depart': '출발', 'arrive': '도착', 'merge': '합류', 'on ramp': '진입로',
    'off ramp': '출구', 'fork': '분기', 'roundabout': '회전교차로', 'rotary': '로터리',
    'exit roundabout': '회전교차로 진출', 'exit rotary': '로터리 진출', 'end of road': '도로 끝'
  };
  function stepText(s) {
    const m = s.maneuver || {};
    let t = TYPE_KO[m.type] || MOD_KO[m.modifier] || '직진';
    if ((m.type === 'turn' || m.type === 'continue' || m.type === 'new name') && MOD_KO[m.modifier]) t = MOD_KO[m.modifier];
    if ((m.type === 'roundabout' || m.type === 'rotary') && m.exit) t += ' ' + m.exit + '번 출구';
    return t + (s.name ? ' — ' + s.name : '');
  }
  const fmtDist = m => m >= 1000 ? (m / 1000).toFixed(1) + 'km' : Math.round(m) + 'm';
  const fmtDur = s => {
    const min = Math.round(s / 60);
    return min >= 60 ? Math.floor(min / 60) + '시간 ' + (min % 60) + '분' : min + '분';
  };

  // ---- 상태 ----
  // slots: [출발, …경유지, 도착] — {lon,lat,label} 또는 null. UI 행과 1:1.
  let slots = [null, null];
  let profile = 'driving';           // driving | walking (OSRM 관례 URL)
  let markers = [];                  // 지점 마커
  let pickIdx = -1;                  // 지도 클릭 지정 대기 중인 슬롯(-1=없음)
  let ac = null;                     // 자동완성 fetch 취소용

  // ---- UI 골격 (다크 테마 — search.js 와 동일 팔레트) ----
  const btn = document.createElement('button');
  btn.textContent = '길찾기';
  btn.style.cssText = 'position:fixed;top:10px;right:10px;z-index:11;padding:8px 12px;background:#1a2029;' +
    'color:#cdd6e3;border:1px solid #2c3542;border-radius:6px;cursor:pointer;font-size:13px';
  document.body.appendChild(btn);

  const panel = document.createElement('div');
  panel.style.cssText = 'position:fixed;top:48px;right:10px;z-index:11;width:300px;display:none;' +
    'background:#141a22;border:1px solid #2c3542;border-radius:8px;padding:10px;font-size:13px;color:#cdd6e3;' +
    'max-height:calc(100% - 70px);overflow-y:auto';
  panel.innerHTML =
    '<div id="rt-modes" style="display:flex;gap:6px;margin-bottom:8px"></div>' +
    '<div id="rt-slots"></div>' +
    '<div style="display:flex;gap:6px;margin-top:8px">' +
    '  <button id="rt-add" class="rt-btn" style="flex:1">+ 경유지</button>' +
    '  <button id="rt-go" class="rt-btn" style="flex:2;background:#2b5c8f;color:#e8eef7">길찾기</button>' +
    '  <button id="rt-clear" class="rt-btn" style="flex:1">지우기</button>' +
    '</div>' +
    '<div id="rt-summary" style="margin-top:8px;display:none"></div>' +
    '<div id="rt-steps" style="margin-top:6px;max-height:240px;overflow-y:auto"></div>' +
    '<div style="margin-top:8px;color:#5b6878;font-size:11px">시간은 도로등급 기반 추정 — 실시간 교통 미반영</div>';
  document.body.appendChild(panel);
  const style = document.createElement('style');
  style.textContent = '.rt-btn{padding:7px 8px;background:#1a2029;color:#cdd6e3;border:1px solid #2c3542;' +
    'border-radius:6px;cursor:pointer;font-size:12px}.rt-btn:hover{background:#1c2530}';
  document.head.appendChild(style);
  btn.onclick = () => { panel.style.display = panel.style.display === 'none' ? 'block' : 'none'; };

  // 프로필 토글(차량/도보)
  const modes = panel.querySelector('#rt-modes');
  [['driving', '🚗 차량'], ['walking', '🚶 도보']].forEach(([key, label]) => {
    const b = document.createElement('button');
    b.className = 'rt-btn'; b.style.flex = '1'; b.textContent = label; b.dataset.key = key;
    b.onclick = () => { profile = key; paintModes(); if (slots.filter(Boolean).length >= 2) route(); };
    modes.appendChild(b);
  });
  function paintModes() {
    modes.querySelectorAll('button').forEach(b => {
      b.style.background = b.dataset.key === profile ? '#2b5c8f' : '#1a2029';
      b.style.color = b.dataset.key === profile ? '#e8eef7' : '#cdd6e3';
    });
  }
  paintModes();

  // ---- 슬롯 행 렌더 ----
  const slotsEl = panel.querySelector('#rt-slots');
  function slotPlaceholder(i) {
    return i === 0 ? '출발지 검색 (또는 📍 후 지도 클릭)' : i === slots.length - 1 ? '도착지 검색' : '경유지 검색';
  }
  function renderSlots() {
    slotsEl.innerHTML = '';
    slots.forEach((v, i) => {
      const row = document.createElement('div');
      row.style.cssText = 'display:flex;gap:4px;margin-top:4px;position:relative';
      const input = document.createElement('input');
      input.placeholder = slotPlaceholder(i);
      input.autocomplete = 'off';
      input.value = v ? v.label : '';
      input.style.cssText = 'flex:1;min-width:0;box-sizing:border-box;padding:7px 9px;background:#1a2029;' +
        'color:#e8eef7;border:1px solid #2c3542;border-radius:6px;outline:none;font-size:12px';
      const list = document.createElement('div');   // 자동완성 드롭다운(행 하단 절대배치)
      list.style.cssText = 'position:absolute;top:100%;left:0;right:0;z-index:12;background:#141a22;' +
        'border:1px solid #2c3542;border-radius:6px;overflow:hidden;display:none;max-height:180px;overflow-y:auto';
      wireAutocomplete(input, list, i);
      const pick = document.createElement('button');
      pick.className = 'rt-btn'; pick.textContent = '📍'; pick.title = '지도 클릭으로 지정';
      pick.onclick = () => {
        pickIdx = i;
        map.getCanvas().style.cursor = 'crosshair';
        input.placeholder = '지도를 클릭하세요…';
      };
      row.appendChild(input); row.appendChild(pick);
      if (i > 0 && i < slots.length - 1) {          // 경유지만 삭제 버튼
        const del = document.createElement('button');
        del.className = 'rt-btn'; del.textContent = '✕';
        del.onclick = () => { slots.splice(i, 1); renderSlots(); route(); };
        row.appendChild(del);
      }
      row.appendChild(list);
      slotsEl.appendChild(row);
    });
  }
  panel.querySelector('#rt-add').onclick = () => {
    if (slots.length >= 7) return;                  // 출발+경유5+도착 상한(과도한 URL 방지)
    slots.splice(slots.length - 1, 0, null);
    renderSlots();
  };
  panel.querySelector('#rt-clear').onclick = () => { slots = [null, null]; renderSlots(); clearRoute(); };

  // geocode 자동완성 — search.js 와 동일 계약(display 우선·debounce·이전 요청 취소).
  function wireAutocomplete(input, list, idx) {
    let timer = null;
    input.addEventListener('input', () => {
      clearTimeout(timer);
      const q = input.value.trim();
      if (q.length < 1) { list.style.display = 'none'; return; }
      timer = setTimeout(() => {
        if (ac) ac.abort();
        ac = new AbortController();
        fetch(GEOCODE + '/geocode?q=' + encodeURIComponent(q) + '&limit=6', { signal: ac.signal })
          .then(r => r.json()).then(d => {
            const results = d.results || [];
            list.innerHTML = '';
            if (!results.length) { list.style.display = 'none'; return; }
            results.forEach(r => {
              const row = document.createElement('div');
              row.style.cssText = 'padding:7px 10px;cursor:pointer;border-top:1px solid #222b38;font-size:12px';
              row.textContent = (r.display && r.display.full) || r.name;   // textContent — XSS 차단
              row.onmouseenter = () => { row.style.background = '#1c2530'; };
              row.onmouseleave = () => { row.style.background = ''; };
              row.onclick = () => {
                slots[idx] = { lon: r.lon, lat: r.lat, label: (r.display && r.display.main) || r.name };
                input.value = slots[idx].label;
                list.style.display = 'none';
                route();
              };
              list.appendChild(row);
            });
            list.style.display = 'block';
          }).catch(e => { if (e.name !== 'AbortError') console.warn('geocode 호출 실패:', e); });
      }, 220);
    });
    input.addEventListener('keydown', e => { if (e.key === 'Escape') list.style.display = 'none'; });
  }

  // 지도 클릭 지정 — 📍 대기 중일 때만 소비(다른 클릭 핸들러와 공존).
  map.on('click', e => {
    if (pickIdx < 0) return;
    const { lng, lat } = e.lngLat;
    const i = pickIdx; pickIdx = -1;
    map.getCanvas().style.cursor = '';
    slots[i] = { lon: lng, lat: lat, label: lng.toFixed(5) + ', ' + lat.toFixed(5) };
    renderSlots();
    // 역지오코딩으로 사람이 읽을 라벨 보강(실패해도 좌표 라벨 유지)
    fetch(GEOCODE + '/reverse?lon=' + lng + '&lat=' + lat + '&limit=1')
      .then(r => r.json()).then(d => {
        const n = (d.nearest || [])[0];
        if (n && slots[i]) { slots[i].label = (n.display && n.display.main) || n.name; renderSlots(); }
      }).catch(() => {});
    route();
  });

  // ---- 경로 그리기 ----
  const SRC = 'cuvia-route';
  function clearRoute() {
    markers.forEach(m => m.remove()); markers = [];
    if (map.getLayer(SRC + '-arrows')) map.removeLayer(SRC + '-arrows');
    if (map.getLayer(SRC + '-line')) map.removeLayer(SRC + '-line');
    if (map.getLayer(SRC + '-casing')) map.removeLayer(SRC + '-casing');
    if (map.getSource(SRC)) map.removeSource(SRC);
    panel.querySelector('#rt-summary').style.display = 'none';
    panel.querySelector('#rt-steps').innerHTML = '';
  }
  // 순서 뱃지 마커 — 색만으론 방문 순서가 안 읽힌다(왕복 겹침 경로에서 "도착 먼저 들렀다" 착시,
  // 실측: 춘의역→상동로196(경유)→상동역 — 경유지행이 도착지 코앞을 스쳐 왕복). 출/1/2…/도 명시.
  function badge(text, color) {
    const el = document.createElement('div');
    el.textContent = text;
    el.style.cssText = 'width:22px;height:22px;border-radius:50%;background:' + color +
      ';color:#0d1622;font-weight:700;font-size:12px;display:flex;align-items:center;' +
      'justify-content:center;border:2px solid #0d1622;box-shadow:0 1px 4px rgba(0,0,0,.5)';
    return el;
  }
  function drawRoute(geojson) {
    if (map.getSource(SRC)) {
      map.getSource(SRC).setData(geojson);
    } else {
      map.addSource(SRC, { type: 'geojson', data: geojson });
      map.addLayer({ id: SRC + '-casing', type: 'line', source: SRC,
        layout: { 'line-cap': 'round', 'line-join': 'round' },
        paint: { 'line-color': '#0d1622', 'line-width': 9, 'line-opacity': 0.85 } });
      map.addLayer({ id: SRC + '-line', type: 'line', source: SRC,
        layout: { 'line-cap': 'round', 'line-join': 'round' },
        paint: { 'line-color': '#3d8bfd', 'line-width': 5 } });
      // 진행방향 화살표 — 왕복 겹침 구간에서 순서 판독의 유일한 단서. '>' 는 glyphs 확실 보장 문자.
      map.addLayer({ id: SRC + '-arrows', type: 'symbol', source: SRC,
        layout: { 'symbol-placement': 'line', 'symbol-spacing': 90, 'text-field': '>',
                  'text-font': ['KlokanTech Noto Sans Regular'], 'text-size': 14,
                  'text-keep-upright': false, 'text-allow-overlap': true,
                  'text-rotation-alignment': 'map' },
        paint: { 'text-color': '#e8eef7', 'text-halo-color': '#0d1622', 'text-halo-width': 1.5 } });
    }
    markers.forEach(m => m.remove()); markers = [];
    slots.filter(Boolean).forEach((s, i, arr) => {
      const last = i === arr.length - 1;
      const color = i === 0 ? '#4ade80' : last ? '#f87171' : '#e8b84a';
      const label = i === 0 ? '출' : last ? '도' : String(i);   // 출/1/2…/도 — 방문 순서 명시
      markers.push(new maplibregl.Marker({ element: badge(label, color) })
        .setLngLat([s.lon, s.lat]).addTo(map));
    });
  }

  // ---- 경로 질의 ----
  function route() {
    const pts = slots.filter(Boolean);
    if (pts.length < 2 || slots.some(s => s === null)) return;   // 전 슬롯이 채워져야 질의
    const coords = pts.map(p => p.lon + ',' + p.lat).join(';');
    fetch(ROUTER + '/route/v1/' + profile + '/' + coords +
          '?steps=true&overview=full&geometries=geojson')
      .then(r => r.json()).then(d => {
        const sum = panel.querySelector('#rt-summary');
        const stepsEl = panel.querySelector('#rt-steps');
        if (d.code !== 'Ok' || !d.routes || !d.routes.length) {
          clearRoute();
          sum.style.display = 'block';
          sum.textContent = '경로를 찾을 수 없습니다' + (d.code && d.code !== 'Ok' ? ' (' + d.code + ')' : '') +
            ' — 지점을 도로 근처로 옮겨보세요';
          sum.style.color = '#f87171';
          return;
        }
        const r0 = d.routes[0];
        drawRoute({ type: 'Feature', geometry: r0.geometry, properties: {} });
        // 화면을 경로 전체로 — 경로선 bbox 계산
        const cs = r0.geometry.coordinates;
        const b = cs.reduce((acc, c) => [
          Math.min(acc[0], c[0]), Math.min(acc[1], c[1]),
          Math.max(acc[2], c[0]), Math.max(acc[3], c[1])
        ], [Infinity, Infinity, -Infinity, -Infinity]);
        map.fitBounds([[b[0], b[1]], [b[2], b[3]]], { padding: { top: 60, bottom: 60, left: 60, right: 340 } });
        // 요약: 총 시간·거리 (+구간 수)
        sum.style.display = 'block'; sum.style.color = '#e8eef7';
        sum.innerHTML = '';
        const strong = document.createElement('b');
        strong.textContent = fmtDur(r0.duration) + ' · ' + fmtDist(r0.distance);
        sum.appendChild(strong);
        if (r0.legs.length > 1) {
          const legs = document.createElement('div');
          legs.style.cssText = 'color:#7d8aa0;font-size:11px;margin-top:2px';
          legs.textContent = r0.legs.map((l, i) => '구간' + (i + 1) + ' ' + fmtDist(l.distance) + '/' + fmtDur(l.duration)).join(' · ');
          sum.appendChild(legs);
        }
        // 턴바이턴 목록
        stepsEl.innerHTML = '';
        r0.legs.forEach(leg => leg.steps.forEach(s => {
          if (s.maneuver && s.maneuver.type === 'arrive' && s.distance === 0) { /* 도착도 표시 */ }
          const row = document.createElement('div');
          row.style.cssText = 'padding:5px 2px;border-top:1px solid #222b38;font-size:12px;color:#cdd6e3';
          row.textContent = stepText(s);
          if (s.distance > 0) {
            const d2 = document.createElement('span');
            d2.style.cssText = 'color:#7d8aa0;font-size:11px';
            d2.textContent = ' ' + fmtDist(s.distance);
            row.appendChild(d2);
          }
          stepsEl.appendChild(row);
        }));
      })
      .catch(e => {
        console.warn('route 호출 실패:', e);
        const sum = panel.querySelector('#rt-summary');
        sum.style.display = 'block'; sum.style.color = '#f87171';
        sum.textContent = '길찾기 서비스에 연결할 수 없습니다';
      });
  }
  panel.querySelector('#rt-go').onclick = route;

  renderSlots();
})();
