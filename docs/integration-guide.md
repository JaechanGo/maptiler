# 소비 프론트엔드 연동 가이드

본 지도 서비스는 MapTiler Cloud의 드롭인 대체다. 변경은 **style URL 한 줄**이다.

## 1. URL 교체

```diff
 const map = new maplibregl.Map({
-  style: 'https://api.maptiler.com/maps/<맵ID>/style.json?key=<API키>',
+  style: 'http://<사내서버>:8080/styles/cuvia/style.json',   // API 키 불필요
   center: [126.989, 37.426], zoom: 15, minZoom: 9, maxZoom: 22,
   pitch: 45, bearing: 0,
 });
```

center/zoom/pitch/bearing 등 나머지 옵션은 기존 값을 그대로 쓰면 된다.

## 2. 기존 코드와의 호환성

- `map.setLayerZoomRange('Building 3D', 12, 24)` — 본 스타일에 `Building 3D`
  fill-extrusion 레이어가 동일 이름으로 존재하므로 그대로 동작한다.
- 건물 높이 속성: 벡터 타일 `building` 레이어에 `render_height` / `render_min_height`
  가 들어 있다. (`height`, `building:levels` 원본 태그는 타일에 포함되지 않으므로
  기존 case 식의 `render_height` 분기가 사용된다.)
- `map.setTerrain(null)` — 지형은 기본 비활성이다. 켜려면:
  `map.setTerrain({ source: 'terrain', exaggeration: 1.3 })`
  주의: 지형 활성 시 원거리 3D 건물이 가려지는 MapLibre 특성이 있다(기존 코드가
  지형을 꺼둔 이유). 화면 용도에 따라 토글하라.
- 스타일에 아이콘(sprite) 레이어가 없어 `styleimagemissing` 은 발생하지 않는다.

## 3. 마커 찍기 (소비 프론트 책임)

**좌표 순서는 `[경도, 위도]`다.** Leaflet의 `[lat, lng]` 와 반대이니 주의.

소량(수십~수백): DOM 마커

```js
new maplibregl.Marker()
  .setLngLat([127.0, 37.5])            // [lng, lat]
  .setPopup(new maplibregl.Popup().setHTML('<b>이름</b>'))
  .addTo(map);
```

대량(수천~수만): GeoJSON 소스 + 클러스터링 — `demo/js/markers-example.js` 참고.
DB에서 조회한 좌표 배열을 GeoJSON FeatureCollection 으로 변환해 소스 하나로 넣고,
circle/symbol 레이어로 그린다. 갱신은 `map.getSource('id').setData(newGeojson)`.

## 4. 엔드포인트 요약

| 용도 | URL |
|------|-----|
| 스타일 | `http://<서버>:8080/styles/cuvia/style.json` |
| 벡터 타일 TileJSON | `http://<서버>:8080/data/korea.json` |
| 지형 타일 TileJSON | `http://<서버>:8080/data/terrain.json` |
| 글리프 | `http://<서버>:8080/fonts/{fontstack}/{range}.pbf` |
| 데모 | `http://<서버>:8081/demo/` |
| Maputnik(스타일 편집) | `http://<서버>:8081/vendor/maputnik/dist/` |

## 5. 보안 메모

폐쇄망 전환 후 기존 프론트 소스의 MapTiler API 키 문자열은 더 이상 필요 없다.
공개 저장소에 노출된 키는 MapTiler 콘솔에서 폐기할 것.
