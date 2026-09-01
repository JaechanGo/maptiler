// 길찾기 데모 — OSRM(osrm-car·osrm-foot) 게이트웨이 경유 호출. FEAT-007/ADR-009.
// 출발/도착/경유지(geocode 자동완성 + 지도 클릭 지정) → /route/v1/{driving|walking} →
// 경로선(GeoJSON)·총 시간/거리·구간(leg)·턴바이턴 목록 표시.
// 도보 프로필에서 역(kind=station) 지점은 최적 출구로 스냅(data/station-exits.json —
// scripts/08-gen-station-exits.py 산출, OSM subway_entrance+ref). 차량은 도로 대표점 그대로.
// 기본 same-origin(게이트웨이 뒤면 포트 무관). 직접 띄웠으면 ?router=http://host:port 지정.
// 주의: 소요시간은 도로등급 실효속도·신호/회전 지연 반영 추정(scripts/route-profiles/car.lua) — 실시간 교통 미반영.
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
  // 도보 걸음 수 — 보폭 0.64m. 상용 내비 표기(349m=545걸음·502m=785걸음)에서 역산한 값과 일치.
  const STRIDE_M = 0.64;
  const fmtSteps = m => Math.round(m / STRIDE_M).toLocaleString('ko-KR') + '걸음';
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
  let routeSeq = 0;                  // 경로 응답 레이스 가드 — 늦게 도착한 이전 질의(프로필 전환 등)가 최신 화면을 덮는 것 차단
  let routes = [];                   // 이번 응답의 경로들(대안 포함) — 선택 전환 시 재질의 없이 사용
  let routeIdx = 0;                  // 선택된 경로 인덱스
  let routePts = [];                 // 그 응답에 쓴 지점(출구 스냅 반영본) — 마커·출구 고지의 진실원
  const avoid = { toll: false, motorway: false };   // 차량 경로 옵션 → OSRM exclude (그래프에 excludable 로 구움)

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
    '  <button id="rt-swap" class="rt-btn" title="출발↔도착 바꾸기">⇅</button>' +
    '  <button id="rt-add" class="rt-btn" style="flex:1">+ 경유지</button>' +
    '  <button id="rt-go" class="rt-btn" style="flex:2;background:#2b5c8f;color:#e8eef7">길찾기</button>' +
    '  <button id="rt-clear" class="rt-btn" style="flex:1">지우기</button>' +
    '</div>' +
    '<div id="rt-opts" style="display:flex;gap:6px;margin-top:6px"></div>' +
    '<div id="rt-routes" style="margin-top:8px"></div>' +
    '<div id="rt-summary" style="margin-top:8px;display:none"></div>' +
    '<div id="rt-steps" style="margin-top:6px;max-height:240px;overflow-y:auto"></div>' +
    '<div style="margin-top:8px;color:#5b6878;font-size:11px">시간은 도로등급·신호 지연 기반 추정 — 실시간 교통 미반영</div>';
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
    optsEl.style.display = profile === 'driving' ? 'flex' : 'none';   // 회피 옵션은 차량 전용
  }

  // 경로 옵션(차량) — OSRM exclude 플래그. 프로필의 excludable(toll·motorway·ferry)만 가능하고
  // 그래프 빌드 시점에 구워지므로 여기서 켜고 끄는 건 재질의만으로 즉시 반영된다.
  const optsEl = panel.querySelector('#rt-opts');
  [['toll', '무료우선'], ['motorway', '고속도로 회피']].forEach(([key, label]) => {
    const b = document.createElement('button');
    b.className = 'rt-btn'; b.style.flex = '1'; b.textContent = label; b.dataset.opt = key;
    // ★ 배타 선택 — OSRM 프로필의 excludable 이 단일 클래스 집합만 선언해서
    //   exclude=toll,motorway 조합은 400 InvalidValue("Exclude flag combination is not supported").
    //   조합을 쓰려면 car.lua excludable 에 Set{'toll','motorway'} 추가 + 그래프 재빌드가 필요하다.
    b.onclick = () => {
      const on = !avoid[key];
      Object.keys(avoid).forEach(k => { avoid[k] = false; });
      avoid[key] = on;
      paintOpts(); route();
    };
    optsEl.appendChild(b);
  });
  function paintOpts() {
    optsEl.querySelectorAll('button').forEach(b => {
      const on = avoid[b.dataset.opt];
      b.style.background = on ? '#2b5c8f' : '#1a2029';
      b.style.color = on ? '#e8eef7' : '#cdd6e3';
    });
  }
  paintOpts();
  paintModes();

  // ---- 슬롯 행 렌더 ----
  const slotsEl = panel.querySelector('#rt-slots');
  function slotPlaceholder(i) {
    return i === 0 ? '출발지 검색 (또는 📍 후 지도 클릭)' : i === slots.length - 1 ? '도착지 검색' : '경유지 검색';
  }
  function renderSlots() {
    // 재렌더 = 행 인덱스 재배열 — 대기 중이던 📍 지정은 무효(삭제/추가로 밀린 인덱스에 오기록 방지)
    if (pickIdx >= 0) { pickIdx = -1; map.getCanvas().style.cursor = ''; }
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
  // 출발↔도착 스왑 — 왕복 경로가 일방통행·회전제한 때문에 대칭이 아니라(차량) 되짚기 질의가 잦다.
  // 경유지는 순서를 뒤집어야 "역순 방문"이 된다(중간을 그대로 두면 왕복 순서가 어긋남).
  panel.querySelector('#rt-swap').onclick = () => {
    slots.reverse();
    renderSlots();
    if (slots.every(Boolean)) route(); else clearRoute();
  };

  // geocode 자동완성 — search.js 와 동일 계약(display 우선·debounce·이전 요청 취소).
  function wireAutocomplete(input, list, idx) {
    let timer = null;
    input.addEventListener('input', () => {
      clearTimeout(timer);
      const q = input.value.trim();
      // ★ 텍스트가 확정 지점 라벨과 달라지면 슬롯 즉시 무효화 + 경로 제거.
      //   없으면 입력을 지워도 옛 좌표로 계속 경로를 그려 "경유지 뺐는데 시간이 그대로" 착오 발생
      //   (실측: 경유지 텍스트 삭제 후에도 요약이 경유 포함 9분 유지 → 직행 4.5분과 혼동).
      if (slots[idx] && q !== slots[idx].label) { slots[idx] = null; clearRoute(); }
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
                slots[idx] = { lon: r.lon, lat: r.lat, label: (r.display && r.display.main) || r.name, kind: r.kind };
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
    const slot = { lon: lng, lat: lat, label: lng.toFixed(5) + ', ' + lat.toFixed(5) };
    slots[i] = slot;
    renderSlots();
    // 역지오코딩으로 사람이 읽을 라벨 보강(실패해도 좌표 라벨 유지)
    fetch(GEOCODE + '/reverse?lon=' + lng + '&lat=' + lat + '&limit=1')
      .then(r => r.json()).then(d => {
        const n = (d.nearest || [])[0];
        // ★ 인덱스가 아니라 슬롯 객체 동일성으로 검사 — 응답 대기 중 경유지를 지우면
        //   뒤 슬롯이 i 로 밀려와 엉뚱한 지점의 라벨·kind 를 덮어쓴다(kind='station' 이면
        //   다음 도보 질의에서 무관한 역 출구로 스냅). 현재 위치를 다시 찾아 쓴다.
        const at = slots.indexOf(slot);
        if (n && at >= 0) { slot.label = (n.display && n.display.main) || n.name; slot.kind = n.kind; renderSlots(); }
      }).catch(() => {});
    route();
  });

  // ---- 역 출구 스냅(도보 전용) ----
  // scripts/08-gen-station-exits.py 산출물(OSM subway_entrance + ref, 전국 4,500여 개).
  // 도보에서 역을 지점으로 잡으면 역 대표점(승강장 위) 대신 이웃 지점 방향의 최적 출구로
  // 좌표를 바꿔 "N번 출구" 안내를 만든다. 파일이 없으면 조용히 비활성(기존 동작 유지).
  let exitsList = null, exitsReq = null;
  function loadExits() {
    if (exitsList) return Promise.resolve(exitsList);
    if (!exitsReq) exitsReq = fetch('data/station-exits.json')
      .then(r => r.json()).then(d => { exitsList = d.exits || []; return exitsList; })
      .catch(() => { exitsList = []; return exitsList; });
    return exitsReq;
  }
  function havM(lon1, lat1, lon2, lat2) {
    const R = 6371000, dLat = (lat2 - lat1) * Math.PI / 180, dLon = (lon2 - lon1) * Math.PI / 180;
    const a = Math.sin(dLat / 2) * Math.sin(dLat / 2) +
      Math.cos(lat1 * Math.PI / 180) * Math.cos(lat2 * Math.PI / 180) * Math.sin(dLon / 2) * Math.sin(dLon / 2);
    return 2 * R * Math.asin(Math.sqrt(a));
  }
  // pts(전 슬롯 확정 좌표) → 역 슬롯만 출구로 치환한 사본.
  // 소속 판정: 출구의 역 좌표(slon/slat)가 슬롯과 150m 이내(좌표 귀속), 또는 역명 일치 +
  // 출구 500m 이내(부천시청역처럼 OSM 에 역 '노드'가 없어 이름으로만 귀속된 경우).
  function snapExits(pts) {
    return loadExits().then(exits => pts.map((p, i) => {
      if (p.kind !== 'station') return p;
      const cand = exits.filter(e =>
        (e.slon !== undefined && havM(e.slon, e.slat, p.lon, p.lat) < 150) ||
        (e.station === p.label && havM(e.lon, e.lat, p.lon, p.lat) < 500));
      if (!cand.length) return p;
      const nb = [];                                  // 이웃 지점 방향으로 출구 선택
      if (i > 0) nb.push(pts[i - 1]);
      if (i < pts.length - 1) nb.push(pts[i + 1]);
      let best = cand[0], bd = Infinity;
      cand.forEach(e => {
        const d = nb.reduce((s, q) => s + havM(e.lon, e.lat, q.lon, q.lat), 0);
        if (d < bd) { bd = d; best = e; }
      });
      return { lon: best.lon, lat: best.lat, kind: p.kind,
               label: p.label + (best.ref ? ' ' + best.ref + '번 출구' : ' 출구'), exit: true };
    }));
  }

  // ---- 경로 그리기 ----
  const SRC = 'cuvia-route';
  const ALT = 'cuvia-route-alt';     // 선택 안 된 대안 경로(회색) — 별도 소스라야 선택 전환이 즉시
  function clearRoute() {
    // ★ 인플라이트 질의 무효화 — 이게 없으면 응답 대기 중 지운 경로가 되살아난다.
    //   (출발지 텍스트 수정 → 슬롯 무효화+clearRoute → 새 질의는 안 나감(슬롯 미완성) →
    //    먼저 나간 응답이 seq 가드를 통과해 지운 지점으로 경로·마커·요약을 전부 복원)
    ++routeSeq;
    markers.forEach(m => m.remove()); markers = [];
    if (map.getLayer(SRC + '-arrows')) map.removeLayer(SRC + '-arrows');
    if (map.getLayer(SRC + '-line')) map.removeLayer(SRC + '-line');
    if (map.getLayer(SRC + '-casing')) map.removeLayer(SRC + '-casing');
    if (map.getSource(SRC)) map.removeSource(SRC);
    if (map.getLayer(ALT + '-line')) map.removeLayer(ALT + '-line');
    if (map.getSource(ALT)) map.removeSource(ALT);
    routes = []; routeIdx = 0; routePts = [];
    panel.querySelector('#rt-routes').innerHTML = '';
    const sum = panel.querySelector('#rt-summary');
    sum.style.display = 'none'; sum.textContent = '';   // 내용까지 비움 — 남기면 다음 표시 때 낡은 값이 스친다
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
  // 대안 경로(선택 안 된 것) — 선택 경로 아래에 회색으로. 클릭 유도는 패널 카드가 담당.
  function drawAlts(feats) {
    const fc = { type: 'FeatureCollection', features: feats };
    if (map.getSource(ALT)) { map.getSource(ALT).setData(fc); return; }
    map.addSource(ALT, { type: 'geojson', data: fc });
    // beforeId 로 선택 경로 casing 아래에 삽입 — 안 그러면 회색 대안이 파란 선택선과
    // 진행방향 화살표를 덮는다(시종점 공유 구간에서 항상 겹침).
    map.addLayer({ id: ALT + '-line', type: 'line', source: ALT,
      layout: { 'line-cap': 'round', 'line-join': 'round' },
      paint: { 'line-color': '#7d8aa0', 'line-width': 4, 'line-opacity': 0.55 } },
      map.getLayer(SRC + '-casing') ? SRC + '-casing' : undefined);
  }
  function drawRoute(geojson, pts) {
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
    pts.forEach((s, i, arr) => {    // 출구 스냅 시 마커도 출구 위치에 — 어느 출구인지 지도에서 보이게
      const last = i === arr.length - 1;
      const color = i === 0 ? '#4ade80' : last ? '#f87171' : '#e8b84a';
      const label = i === 0 ? '출' : last ? '도' : String(i);   // 출/1/2…/도 — 방문 순서 명시
      markers.push(new maplibregl.Marker({ element: badge(label, color) })
        .setLngLat([s.lon, s.lat]).addTo(map));
    });
  }

  // 경로의 주요 경유 도로 — 이름별 거리 합 상위 3개("길주로 2.5km → 계남로 1.0km").
  // 턴바이턴 20여 줄로는 "어느 길로 가는지"가 안 읽힌다(상용 내비가 경로 카드에 이걸 쓰는 이유).
  function summarizeRoads(r) {
    const agg = new Map();
    (r.legs || []).forEach(leg => (leg.steps || []).forEach(s => {
      if (s.name) agg.set(s.name, (agg.get(s.name) || 0) + s.distance);
    }));
    return [...agg.entries()].sort((a, b) => b[1] - a[1]).slice(0, 3)
      .map(([n, d]) => n + ' ' + fmtDist(d)).join(' → ');
  }

  // 대안 경로 카드 목록 — 클릭하면 재질의 없이 선택만 전환.
  function renderRoutes() {
    const el = panel.querySelector('#rt-routes');
    el.innerHTML = '';
    if (routes.length < 2) return;            // 대안이 없으면 목록 자체를 숨김(요약이 이미 같은 정보)
    routes.forEach((r, i) => {
      const card = document.createElement('div');
      const on = i === routeIdx;
      card.style.cssText = 'padding:6px 8px;margin-top:4px;border-radius:6px;cursor:pointer;font-size:12px;' +
        'border:1px solid ' + (on ? '#2b5c8f' : '#2c3542') + ';background:' + (on ? '#1a2c40' : '#161c25');
      const head = document.createElement('div');
      head.style.cssText = 'color:' + (on ? '#e8eef7' : '#cdd6e3');
      head.textContent = (i === 0 ? '추천 ' : '대안 ' + i + ' ') + fmtDur(r.duration) + ' · ' + fmtDist(r.distance) +
        (profile === 'walking' ? ' · ' + fmtSteps(r.distance) : '');
      const via = document.createElement('div');
      via.style.cssText = 'color:#7d8aa0;font-size:11px;margin-top:2px';
      via.textContent = summarizeRoads(r);
      card.appendChild(head); card.appendChild(via);
      card.onclick = () => { routeIdx = i; renderRoutes(); paintRoute(true); };
      el.appendChild(card);
    });
  }

  // 선택된 경로를 지도·요약·턴바이턴에 반영. fit=true 면 화면도 맞춘다(최초 응답·카드 클릭 시).
  function paintRoute(fit) {
    const r0 = routes[routeIdx];
    if (!r0) return;
    const sum = panel.querySelector('#rt-summary');
    const stepsEl = panel.querySelector('#rt-steps');
    drawRoute({ type: 'Feature', geometry: r0.geometry, properties: {} }, routePts);
    drawAlts(routes.filter((_, i) => i !== routeIdx)
      .map(r => ({ type: 'Feature', geometry: r.geometry, properties: {} })));
    if (fit) {
      const b = r0.geometry.coordinates.reduce((acc, c) => [
        Math.min(acc[0], c[0]), Math.min(acc[1], c[1]),
        Math.max(acc[2], c[0]), Math.max(acc[3], c[1])
      ], [Infinity, Infinity, -Infinity, -Infinity]);
      // 패널 몫 340px 은 좁은 화면(모바일)에서 캔버스 폭을 초과해 fitBounds 가 throw → 폭 40%로 클램프
      const padR = Math.min(340, Math.floor(map.getContainer().clientWidth * 0.4));
      map.fitBounds([[b[0], b[1]], [b[2], b[3]]], { padding: { top: 60, bottom: 60, left: 60, right: padR } });
    }
    // 요약: 총 시간·거리 (+구간·출구)
    sum.style.display = 'block'; sum.style.color = '#e8eef7';
    sum.innerHTML = '';
    const strong = document.createElement('b');
    strong.textContent = fmtDur(r0.duration) + ' · ' + fmtDist(r0.distance) +
      (profile === 'walking' ? ' · ' + fmtSteps(r0.distance) : '');
    sum.appendChild(strong);
    if (r0.legs.length > 1) {
      const legs = document.createElement('div');
      legs.style.cssText = 'color:#7d8aa0;font-size:11px;margin-top:2px';
      legs.textContent = r0.legs.map((l, i) => '구간' + (i + 1) + ' ' + fmtDist(l.distance) + '/' + fmtDur(l.duration)).join(' · ');
      sum.appendChild(legs);
    }
    const exitPts = routePts.filter(p => p.exit);   // 출구 스냅 결과 고지 — 어느 출구 기준인지
    if (exitPts.length) {
      const ex = document.createElement('div');
      ex.style.cssText = 'color:#8fd3a8;font-size:11px;margin-top:2px';
      ex.textContent = '출구 기준: ' + exitPts.map(p => p.label).join(' · ');
      sum.appendChild(ex);
    }
    // 턴바이턴 목록
    stepsEl.innerHTML = '';
    r0.legs.forEach(leg => leg.steps.forEach(s => {
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
  }

  // ---- 경로 질의 ----
  function route() {
    const pts0 = slots.filter(Boolean);
    // 전 슬롯이 채워져야 질의. 못 하는 상태면 옛 결과를 지운다 — 안 지우면 프로필·옵션을
    // 바꿨을 때 버튼만 새 상태로 바뀌고 지도·요약은 옛 프로필 결과가 남아 서로 어긋난다
    // (예: 경유지 추가 후 차량 전환 → 차량 하이라이트인데 도보 경로 + "출구 기준" 표기 잔존).
    if (pts0.length < 2 || slots.some(s => s === null)) { clearRoute(); return; }
    const seq = ++routeSeq;   // 이 질의의 순번 — 응답 처리 시점에 최신인지 검사
    // 도보만 출구 스냅(차량은 역 앞 도로 대표점이 관례). 스냅 대기 중 새 질의가 나가면 seq 가드가 무효화.
    (profile === 'walking' ? snapExits(pts0) : Promise.resolve(pts0)).then(pts => {
    if (seq !== routeSeq) return;
    const coords = pts.map(p => p.lon + ',' + p.lat).join(';');
    // 대안 경로는 경유지가 없을 때만 OSRM 이 낸다(경유지가 있으면 무시). 회피 옵션은 차량 전용.
    const ex = profile === 'driving' ? Object.keys(avoid).filter(k => avoid[k]) : [];
    // alternatives=true — 개수(3)를 박으면 서버의 --max-alternatives 를 낮췄을 때 전 질의가
    // 실패한다("higher than current maximum"). true 는 서버 상한을 그대로 따른다.
    const q = '?steps=true&overview=full&geometries=geojson&alternatives=true' +
      (ex.length ? '&exclude=' + ex.join(',') : '');
    // 계산 중 표시 — exclude(무료우선 등) 장거리 질의는 수 초가 걸린다. 표시가 없으면
    // 옛 결과가 그대로 보여 "옵션이 안 먹는다"고 오해한다(실측: 서울→밀양 exclude=toll 약 5초).
    const busy = panel.querySelector('#rt-summary');
    busy.style.display = 'block'; busy.style.color = '#7d8aa0';
    busy.textContent = '경로 계산 중…';
    fetch(ROUTER + '/route/v1/' + profile + '/' + coords + q)
      .then(r => r.json()).then(d => {
        if (seq !== routeSeq) return;   // 이후 질의가 이미 나감(프로필 전환·지점 변경) — 낡은 응답 폐기
        const sum = panel.querySelector('#rt-summary');
        if (d.code !== 'Ok' || !d.routes || !d.routes.length) {
          clearRoute();
          sum.style.display = 'block';
          sum.textContent = '경로를 찾을 수 없습니다' + (d.code && d.code !== 'Ok' ? ' (' + d.code + ')' : '') +
            (ex.length ? ' — 회피 옵션을 끄고 다시 시도해보세요' : ' — 지점을 도로 근처로 옮겨보세요');
          sum.style.color = '#f87171';
          return;
        }
        routes = d.routes; routeIdx = 0; routePts = pts;
        renderRoutes();
        paintRoute(true);
      })
      .catch(e => {
        if (seq !== routeSeq) return;   // 낡은 질의의 실패 — 최신 화면을 오류로 덮지 않음
        console.warn('route 호출 실패:', e);
        const sum = panel.querySelector('#rt-summary');
        sum.style.display = 'block'; sum.style.color = '#f87171';
        sum.textContent = '길찾기 서비스에 연결할 수 없습니다';
      });
    });   // snapExits/Promise 체인 닫기
  }
  panel.querySelector('#rt-go').onclick = route;

  renderSlots();
})();
