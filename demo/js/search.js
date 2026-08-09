// 지오코딩/역지오코딩 데모 — server/geocode-api.py(:8082) 호출.
// 검색창에 입력 → /geocode → 결과 클릭 시 지도 이동. 지도 우클릭 → /reverse 팝업.
// 응답계약 geocode/2: 각 result 에 display{main,secondary,full}·address.structure·category 부착.
// 표기는 display 우선, 누락(구버전/결측) 시에만 kind 라벨+지역 fallback (영문 kind 원문 노출 차단).
(function () {
  const map = window.cuviaMap;
  if (!map) { console.warn('search.js: cuviaMap 미초기화'); return; }
  // 지오코드 API 주소 — 기본 same-origin(/geocode). 게이트웨이 뒤면 포트 무관. 직접 띄웠으면 ?geocode= 지정.
  const params = new URLSearchParams(location.search);
  const GEOCODE = params.get('geocode') || '';
  // kind → 한글 라벨 (display.secondary 누락 시 fallback 용도. 평상시엔 display 우선이라 미사용).
  const TYPE_KO = {
    addr: '주소', road: '도로', place: '지명', station: '역',
    dong: '동', poi: 'POI', biz: '상가', facility: '시설'
  };
  const SUBTYPE_KO = { parcel: '지번' };   // subtype 폴백 라벨
  // setHTML 주입용 HTML 이스케이프 — 데이터 유래 값(주소·장소명)의 XSS 차단.
  const esc = s => String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');

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
  let rows = [];    // 렌더된 행 [{ el, data }] — 키보드 내비 상태 보관
  let sel = -1;     // 현재 하이라이트 인덱스(-1=없음)
  let ac = null;    // 진행 중 fetch 취소용 AbortController

  function flyTo(lon, lat, label) {
    if (marker) marker.remove();
    marker = new maplibregl.Marker({ color: '#e8b84a' }).setLngLat([lon, lat])
      .setPopup(new maplibregl.Popup({ offset: 24 }).setText(label)).addTo(map);
    marker.togglePopup();
    map.flyTo({ center: [lon, lat], zoom: 16, speed: 1.4 });
  }

  // display.secondary 누락 시 조립: 라벨[/subtype] + ' · ' + 지역(structure sido/sigungu/emd).
  function fallbackSecondary(r) {
    let label = TYPE_KO[r.kind] || '장소';   // 영문 kind 원문 대신 항상 한글 라벨
    if (r.subtype) label += '/' + (SUBTYPE_KO[r.subtype] || r.subtype);
    const st = (r.address && r.address.structure) || {};
    const region = [st.sido, st.sigungu, st.emd].filter(Boolean).join(' ');
    return region ? label + ' · ' + region : label;
  }

  // 행 선택(클릭/Enter 공통) — flyTo + 리스트 닫기 + 입력창 채움.
  function selectRow(r) {
    const label = (r.display && r.display.full) || r.name;
    flyTo(r.lon, r.lat, label);
    list.style.display = 'none';
    input.value = label;
  }

  // 하이라이트 이동 — 클램프 + 배경 갱신 + 스크롤. (hover/키보드 공용)
  function setSel(i) {
    if (!rows.length) { sel = -1; return; }
    sel = Math.max(0, Math.min(i, rows.length - 1));
    rows.forEach((row, idx) => { row.el.style.background = idx === sel ? '#1c2530' : ''; });
    rows[sel].el.scrollIntoView({ block: 'nearest' });
  }

  function render(results) {
    rows = []; sel = -1;
    if (!results.length) { list.style.display = 'none'; return; }
    list.innerHTML = '';
    results.forEach((r, idx) => {
      const row = document.createElement('div');
      row.style.cssText = 'padding:8px 12px;cursor:pointer;border-top:1px solid #222b38;color:#cdd6e3';
      // 1줄: display.main(굵게) — 누락 시 name fallback. textContent 로 데이터 유래 XSS 차단.
      const main = (r.display && r.display.main) || r.name;
      const bel = document.createElement('b');
      bel.style.color = '#e8eef7';
      bel.textContent = main;
      row.appendChild(bel);
      // 2줄: display.secondary(회색) — 있을 때만. 누락 시 라벨+지역 조립.
      const secondary = (r.display && r.display.secondary) || fallbackSecondary(r);
      if (secondary) {
        const sub = document.createElement('div');
        sub.style.cssText = 'color:#7d8aa0;font-size:12px;margin-top:1px';
        sub.textContent = secondary;
        row.appendChild(sub);
      }
      row.onmouseenter = () => setSel(idx);   // hover 와 sel 동기화
      row.onclick = () => selectRow(r);
      rows.push({ el: row, data: r });
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
      if (ac) ac.abort();              // 이전 요청 취소(빠른 연타 시 늦은 응답이 최신 결과 덮는 race 차단)
      ac = new AbortController();
      fetch(GEOCODE + '/geocode?q=' + encodeURIComponent(q) + '&limit=8', { signal: ac.signal })
        .then(r => r.json()).then(d => render(d.results || []))
        .catch(e => {
          if (e.name === 'AbortError') return;   // 정상 취소 — 무시
          console.warn('geocode 호출 실패:', e); list.style.display = 'none';
        });
    }, 220);
  });

  // 키보드 내비 — ↓/↑ 이동, Enter 선택, Escape 닫기.
  input.addEventListener('keydown', e => {
    const open = list.style.display !== 'none' && rows.length;
    if (e.key === 'ArrowDown') {
      if (!open) return;
      e.preventDefault(); setSel(sel + 1);
    } else if (e.key === 'ArrowUp') {
      if (!open) return;
      e.preventDefault(); setSel(sel - 1);
    } else if (e.key === 'Enter') {
      if (!open) return;
      e.preventDefault();
      const r = sel >= 0 ? rows[sel].data : rows[0].data;   // 미선택이면 첫 행(현행 호환)
      selectRow(r);
    } else if (e.key === 'Escape') {
      list.style.display = 'none'; sel = -1;   // 입력 포커스는 유지
    }
  });
  document.addEventListener('click', e => { if (!box.contains(e.target)) list.style.display = 'none'; });

  // ---- 역지오코딩: 지도 우클릭 ----
  map.on('contextmenu', e => {
    const { lng, lat } = e.lngLat;
    fetch(GEOCODE + '/reverse?lon=' + lng + '&lat=' + lat + '&limit=4')
      .then(r => r.json()).then(d => {
        // 근접 라벨은 display.main 우선(없으면 name). nearest[*] 에도 display 부착됨.
        const near = (d.nearest || [])
          .map(n => esc((n.display && n.display.main) || n.name) + ' (' + Math.round(n.dist_m) + 'm)')
          .join('<br>') || '주변 결과 없음';
        const areas = (d.areas || []).map(a => esc(a.name)).join(', ');   // 빈 배열이면 헤더 생략
        const html = '<div style="font-size:12px;line-height:1.5">' +
          (areas ? '<b>' + areas + '</b><br>' : '') + near + '</div>';
        new maplibregl.Popup({ offset: 8 }).setLngLat([lng, lat]).setHTML(html).addTo(map);
      }).catch(e2 => console.warn('reverse 호출 실패:', e2));
  });
})();
