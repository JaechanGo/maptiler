// 베이스 지도 초기화. 게이트웨이(단일 도메인) 경유면 same-origin(상대경로), 직접 포트 접속(:8081)이면
// 같은 호스트의 :8080 사용. ?server=http://host:port 로 재지정 가능.
const params = new URLSearchParams(location.search);
// 게이트웨이(reverse proxy) 경유 판별 — 80/443/8088 이면 /styles 등을 same-origin 으로 호출.
const VIA_GATEWAY = ['', '80', '443', '8088'].includes(location.port);
// 주의: ?server= 값은 검증 없이 URL로 사용된다. 공개망에서 재사용 시 allowlist 검증 필요.
const TILESERVER = params.get('server') || (VIA_GATEWAY ? '' : `http://${location.hostname}:8080`);

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
});
map.addControl(new maplibregl.NavigationControl({ visualizePitch: true }));
window.cuviaMap = map;   // buildings.js / terrain.js / markers-example.js 가 사용
