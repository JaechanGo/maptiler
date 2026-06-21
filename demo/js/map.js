// 베이스 지도 초기화. 기본은 same-origin(상대경로) — 게이트웨이(reverse proxy) 뒤라면 포트 무관하게
// /styles 가 그대로 동작한다. 게이트웨이 없이 데모를 직접 포트로 띄웠다면 ?server=http://host:8080 지정.
const params = new URLSearchParams(location.search);
// 주의: ?server= 값은 검증 없이 URL로 사용된다. 공개망에서 재사용 시 allowlist 검증 필요.
const TILESERVER = params.get('server') || '';

const map = new maplibregl.Map({
  container: 'map',
  style: `${TILESERVER}/styles/cuvia/style.json`,
  // 소비 프론트 map-config.ts MAP_INITIAL_VIEW 와 동일 (주: 원본 주석은 '서초역'이나 좌표는 부천 일대)
  center: [126.783, 37.5044],   // [경도, 위도]
  zoom: 15,
  minZoom: 5,
  maxZoom: 22,
  pitch: 60,                    // MAP_PITCH.on — 2D 토글 시 0
  bearing: -17.6,
  maxPitch: 75,
  // maplibre 는 타일을 Web Worker 에서 로드하는데, 워커엔 document base URL 이 없어
  // 상대경로(예: /dyn/poi/{z}/{x}/{y})로 new Request() 를 만들지 못한다("Failed to parse URL").
  // tileserver 는 자기 mbtiles 소스만 Host 기준 절대 URL 로 재작성하고, 스타일에 인라인된
  // /dyn/* (martin 동적타일=건물·필지·POI) 는 상대경로로 남는다. → same-origin 상대경로를
  // 페이지 origin(게이트웨이) 기준 절대 URL 로 승격해 동적 레이어가 브라우저에서 로드되게 한다.
  transformRequest: (u) => (
    u && u.charAt(0) === '/' ? { url: (TILESERVER || location.origin) + u } : { url: u }
  ),
});
map.addControl(new maplibregl.NavigationControl({ visualizePitch: true }));
window.cuviaMap = map;   // buildings.js / terrain.js / markers-example.js 가 사용
