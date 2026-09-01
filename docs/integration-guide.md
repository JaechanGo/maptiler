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
| 동 라벨 TileJSON | `http://<서버>:8080/data/dong.json` |
| 글리프 | `http://<서버>:8080/fonts/{fontstack}/{range}.pbf` |
| **지오코딩** | `http://<서버>:8082/geocode?q=증미역` |
| **역지오코딩** | `http://<서버>:8082/reverse?lon=126.86&lat=37.55` |
| **길찾기(차량)** | `http://<서버>/route/v1/driving/{lon},{lat};{lon},{lat}` (게이트웨이 경유) |
| **길찾기(도보)** | `http://<서버>/route/v1/walking/{lon},{lat};{lon},{lat}` (게이트웨이 경유) |
| 데모 | `http://<서버>:8081/demo/` |
| Maputnik(스타일 편집) | `http://<서버>:8081/vendor/maputnik/dist/` |

## 4.2 지오코딩 / 역지오코딩 (검색)

별도 무의존 서비스(`server/geocode-api.py`, 기본 :8082)가 `geocode/geocode.sqlite`(scripts/07 생성, FTS5+R-tree)를 서빙한다.

```js
// 지오코딩: 이름/주소 → 좌표
const r = await fetch(`http://<서버>:8082/geocode?q=${encodeURIComponent('증미역')}`).then(r=>r.json());
// r.results[0] = { name:'증미역', kind:'station', lon:126.8618, lat:37.5575, address:{…} }
map.flyTo({ center: [r.results[0].lon, r.results[0].lat], zoom: 16 });

// 역지오코딩: 좌표 → 최근접 장소 + 포함 영역(행정동/지번; areas 데이터 적재 시)
const rev = await fetch(`http://<서버>:8082/reverse?lon=126.8618&lat=37.5575`).then(r=>r.json());
// rev.nearest = [{name,kind,dist_m,...}], rev.areas = [{name,type,code}]
```

- 검색 대상: 역(전국 ~1,400) · 지명 · 아파트 동 · 도로명 · POI 등 약 67만 건(OSM 기반). 국가 상가정보 CSV(`--poi-csv`)·행정경계 GeoJSON(`--areas-geojson`)로 확장.
- 데모 상단 검색창(`demo/js/search.js`) 참고. CORS 허용(`Access-Control-Allow-Origin: *`).

## 4.3 길찾기 (차량·도보)

OSRM 자체 호스팅(`osrm-car`·`osrm-foot`)을 게이트웨이가 프로필로 분기한다
(`/route|/table|/trip|/nearest` 의 `v1/driving`→차량, `v1/walking`→도보 — OSRM/Mapbox Directions 표준 URL).
도보 길찾기에서 역을 지점으로 지정하면 데모가 최적 출구로 좌표를 스냅한다
(`demo/data/station-exits.json` — `scripts/08-gen-station-exits.py` 가 OSM `railway=subway_entrance` +
출구번호 `ref` 를 추출, 전국 4,555개). 이웃 지점에 가장 가까운 출구를 골라 "상동역 8번 출구"로 안내한다.
차량은 스냅하지 않는다(역 앞 도로 대표점이 관례).

폐쇄망 전용 — 실시간 교통은 미반영. 소요시간은 한국 도심 보정 프로필(`scripts/route-profiles/{car,foot}.lua` — 차량: 등급별 실효속도·신호등 40s·회전/유턴 지연, 도보: 4.5km/h·신호 횡단 30s·계단 감속)로 상용 내비의 "평상시" 추정에 근사시킨 값이다.

```js
// 경로: 출발;경유지;도착 (lon,lat 순서 주의) — steps=턴바이턴, geometries=geojson 권장
const r = await fetch(`/route/v1/driving/126.9877,37.4292;126.9769,37.4009;126.9169,37.4017` +
                      `?steps=true&overview=full&geometries=geojson`).then(r=>r.json());
// r.code === 'Ok'
// r.routes[0].distance(m) · duration(s) · geometry(GeoJSON LineString — 지도에 그대로 addSource)
// r.routes[0].legs[i]     — 경유지 구간별 distance/duration/steps
// legs[i].steps[j]        — { maneuver:{type,modifier}, name:'과천대로', distance, duration } 턴바이턴
// r.waypoints             — 입력좌표의 도로 스냅 결과 { name, location, distance(스냅거리 m) }

// 매트릭스(N×N 소요시간/거리): 가까운 지점 찾기 등
const t = await fetch(`/table/v1/driving/${coords}?annotations=duration,distance`).then(r=>r.json());
// 다중지점 방문 순서 최적화: /trip/v1/driving/{coords}?roundtrip=true
```

- 데모 우상단 '길찾기' 패널(`demo/js/routing.js`) 참고 — geocode 검색 연동·지도 클릭 지정·경유지 포함 예제.
- 그래프는 `scripts/07-gen-route-graph.sh` 산출물(`route/{car,foot}`) — OSM 갱신 시 재생성(프로필당 약 3분).

## 4.1 스타일 내장 라벨 레이어 (토글용 id)

| 레이어 id | 내용 | 표시 줌 |
|---|---|---|
| `dong-dot` / `dong-label` | 아파트 동(棟) 점/번호 — OSM 추출 92,004개 (K-apt 144,706동 대비 ~64%) | z14+ / z16+ |
| `poi-station-dot` / `poi-station-label` | 철도·지하철역 (전국 1,383개) | z12+ (단, 데이터가 z12·z13에 부분 존재 — 완전 표시는 z14+) |
| `poi-civic-label` | 병원·학교·관공서·박물관 등 주요 시설 | z15+ |
| `peak-label` | 산봉우리 (전국 2만, 이름 보유) | z12+ |
| `aerodrome-label` | 공항 | z9+ |

숨기기 예: `map.setLayoutProperty('dong-label', 'visibility', 'none')`
(동 라벨은 데모 우상단 토글과 동일하게 `dong-dot`도 함께 토글할 것)

## 5. 보안 메모

폐쇄망 전환 후 기존 프론트 소스의 MapTiler API 키 문자열은 더 이상 필요 없다.
공개 저장소에 노출된 키는 MapTiler 콘솔에서 폐기할 것.
