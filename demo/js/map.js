// 베이스 지도 초기화. 타일서버 주소는 데모를 띄운 호스트의 8080 포트로 가정한다.
// 다른 서버를 쓰려면 ?server=http://host:port 쿼리로 재지정.
const params = new URLSearchParams(location.search);
const TILESERVER = params.get('server') || `http://${location.hostname}:8080`;

const map = new maplibregl.Map({
  container: 'map',
  style: `${TILESERVER}/styles/cuvia/style.json`,
  center: [126.978, 37.5665],   // [경도, 위도] — 서울시청
  zoom: 15,
  minZoom: 5,
  maxZoom: 22,
  pitch: 45,
  maxPitch: 75,
});
map.addControl(new maplibregl.NavigationControl({ visualizePitch: true }));
window.cuviaMap = map;   // buildings.js / terrain.js / markers-example.js 가 사용
