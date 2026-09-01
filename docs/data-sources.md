# 지도 데이터 출처 (화면 요소별)

"화면에 보이는 이것은 어디서 온 데이터인가"에 대한 추적표.
라이선스·의무사항은 [data-licenses.md](data-licenses.md), 소비측 레이어 id는 [integration-guide.md](integration-guide.md) 참조.

## 1. 요약표

| 화면 요소 | 원천 데이터 | 원천 내 위치 | 타일 (소스/레이어) | 스타일 레이어 |
|---|---|---|---|---|
| 도로망 | OpenStreetMap | `highway=*` | korea / transportation | road-minor·secondary·primary·motorway |
| 도로명 | OpenStreetMap | `name:ko`, `name` | korea / transportation_name | road-label |
| 행정경계 | OpenStreetMap | `boundary=administrative` (admin_level≤4) | korea / boundary | boundary |
| **지역 지명** (시·구·동: 부천시, 도당동 …) | **OpenStreetMap** (기여자 입력 — 공식 행정DB 아님) | `place=city/town/village/suburb/quarter/neighbourhood` | korea / place | place-label |
| **역 이름·위치** (춘의역 …) | **OpenStreetMap** | `railway=station/subway/halt/tram_stop` (poi class=railway) | korea / poi | poi-station-dot, poi-station-label |
| 시설 (병원·학교·관공서 …) | OpenStreetMap | `amenity=*`, `shop=*` 등 | korea / poi | poi-civic-label |
| 공항 | OpenStreetMap | `aeroway=aerodrome` | korea / aerodrome_label | aerodrome-label |
| 산봉우리 (이름·표고) | OpenStreetMap | `natural=peak` + `ele` | korea / mountain_peak | peak-label |
| **건물 2D/3D** (footprint·높이) | **OpenStreetMap** | `building=*` 폴리곤 + `height`/`building:levels` | korea / building (`render_height`/`render_min_height`) | building-2d, **Building 3D** |
| **아파트 동(棟) 라벨** (101동 …) | OpenStreetMap 건물 `name`/`ref` → 자체 추출 | scripts/04 정규식(`\d+동`·아파트 숫자형·문자동) | **dong** / dong | dong-dot, dong-label |
| **역 출구(도보 안내)** | **OpenStreetMap** (scripts/08) | `railway=subway_entrance`·`ref` | **demo/data**/station-exits.json (4,555개) | 데모 도보 출구 스냅 |
| **길찾기 경로(차량·도보)** | **OpenStreetMap** 도로망 → OSRM 그래프(scripts/07) | `highway=*`·`oneway`·`maxspeed`·`traffic_signals`·turn restriction — car·foot 은 한국 도심 보정 프로필(scripts/route-profiles) | **route**/{car,foot} (OSRM MLD — 타일 아님) | 데모 routing.js 경로선 |
| **지형 (3D 터레인)** | **NASA SRTM 30m** (AWS Open Data `elevation-tiles-prod`) | skadi `.hgt` (N33~38, E124~131) | **terrain** / raster-dem (Terrain-RGB) | `map.setTerrain` 소스 |
| 바다·호수·하천 | OSM + osmdata.openstreetmap.de 수역 폴리곤 + Natural Earth(저줌) | `natural=water`, water-polygons-split-3857 | korea / water, waterway | water, waterway |
| 글꼴(글리프) | Google Noto Sans (openmaptiles/fonts 빌드) | `style/glyphs/KlokanTech Noto Sans *` | — | 모든 symbol 레이어 |
| 타일 스키마·레이어 구조 | OpenMapTiles 사양 v3.16.0 (Planetiler 구현) | — | korea 전체 | — |
| 다크 네이비 색상 | 자체 제작 (소비 프론트 MapTiler 'CUDO1' 팔레트 이식) | style/base.json, style/layers/* | — | 전체 |

요점: **지형(NASA)과 폰트(Noto)를 제외한 모든 지도 콘텐츠의 1차 출처는 OpenStreetMap**이며,
Geofabrik 한국 추출본 → Planetiler(OpenMapTiles 스키마) → `korea.mbtiles` 경로로 가공된다.
동 라벨만 별도 파이프라인(scripts/04→05 → `dong.mbtiles`)으로 분리되어 있다(라이선스 구조상 의도 — data-licenses.md §3.2).

## 2. 상세

### 지역 지명 (place)
- OSM `place=*` 노드의 `name:ko`/`name`. **국가 공식 행정구역 DB가 아니라 OSM 기여자 입력값**이므로
  명칭·대표점 위치가 공식 고시와 다를 수 있다. z14 실측: city 85, town 1,412, village 15,117,
  quarter(행정동급) 3,584, neighbourhood 200.
- 공식 행정동 경계·명칭이 필요해지면 행안부 행정구역(주소) 데이터로 보강(별도 소스 원칙).

### 역 (poi class=railway)
- OSM `railway=station`(일반철도)·`subway`(도시철도) 등. 전국 실측 1,383개
  (subway 688, station 674, halt 14, tram_stop 7). 환승역은 노선별 POI가 중복 존재할 수 있다.
- **원본 역명에는 '역' 접미사가 없다**("춘의"). 표시 시 스타일 표현식이 '역'을 부착하며,
  "서울역"처럼 이미 '역'으로 끝나면 그대로 둔다 (style/layers/poi-labels.json).

### 건물 / 3D
- footprint: OSM `building=*` 폴리곤. 높이: OSM `height`·`building:levels` 태그를 Planetiler가
  `render_height`/`render_min_height` 속성으로 변환해 타일에 수록.
- **높이 태그가 없는 건물은 스타일에서 15m 기본값으로 압출**된다
  (style/layers/buildings-3d.json의 coalesce) — 실제 높이가 아닐 수 있음에 유의.

### 아파트 동 라벨 (dong.mbtiles)
- OSM 건물의 `name`/`ref`에서 scripts/04-gen-dong-labels.py 가 **92,004점** 추출(건물 중심점),
  scripts/05-gen-dong-tiles.py 가 벡터타일로 인코딩. 단순 Way 건물뿐 아니라 **멀티폴리곤(관계)로
  매핑된 건물도 포함**(관계 멤버 way 형상으로 중심점 근사). 이름에 행정동 표기가 든 공공시설
  (주민센터·우체국·성당 등)은 오탐 제외.
- **커버리지(2가지 분모로 봐야 함):**
  - 전국 의무관리 공동주택(K-apt 144,706동) 대비 **~64%**.
  - OSM가 이미 매핑한 아파트 건물 중 동 라벨 보유율 **~57%**(지역 편차: 인천 75%·경기 73%·서울 60%,
    대구 19%·제주 16% — building=apartments 기준 근사 스냅샷).
- 전국 균질화 시 행안부 도로명주소 건물도형(`BULD_NM_DC` 상세건물명)으로 교체 예정 (data-licenses.md §4).

### 지형 (terrain.mbtiles)
- NASA SRTM 30m (2000년 2월 셔틀 레이더 관측, 퍼블릭 도메인). AWS Open Data
  `s3://elevation-tiles-prod/skadi` 에서 한국 범위 48타일을 받아 scripts/03 이
  GDAL(VRT) → rio-rgbify Terrain-RGB(z5~12)로 인코딩.
- SRTM은 **DSM 계열**(숲 우듬지·건물 지붕에서 반사)이라 도심·삼림에서 실제 지반고보다
  높게 나타난다. 정밀화 시 국토지리정보원 DEM으로 교체(스크립트는 출처 독립 설계).

### 수역
- 바다: osmdata.openstreetmap.de 의 OSM 해안선 가공 폴리곤(water-polygons-split-3857).
- 호수·하천: OSM `natural=water`·`waterway=*`. 저줌 보조: Natural Earth(퍼블릭 도메인).
- 모두 Planetiler `--download` 가 자동 수급 (data/sources/).

## 3. 데이터 기준 시점 (현재 빌드)

`korea.mbtiles` 메타데이터(planetiler 기록) 기준:

| 항목 | 값 |
|---|---|
| OSM 스냅샷 | **2026-06-10T20:37:15Z** (Geofabrik south-korea 복제 시점) |
| 타일 빌드 | 2026-03-28 빌드 jar / 2026-06-12 실행 |
| 스키마 | OpenMapTiles v3.16.0, Planetiler v0.10.2 |
| SRTM 관측 | 2000-02 (다운로드 2026-06-12) |
| 동 라벨 추출 | 2026-06-12 (위 OSM 스냅샷에서) |

## 4. 갱신 방법

OSM 기반 콘텐츠(도로·건물·지명·역·동 라벨)는 한 묶음으로 갱신된다:

```bash
rm data/osm/south-korea.osm.pbf       # 최신 추출본 강제 재다운로드
./scripts/01-download-data.sh
./scripts/02-gen-vector.sh            # korea.mbtiles 재생성
./scripts/04-gen-dong-labels.py       # 동 라벨 재추출
./scripts/05-gen-dong-tiles.py        # dong.mbtiles 재생성
# 지형(SRTM)은 원천이 정적이므로 재생성 불필요
```

서버 반영 시 주의: 설정/스타일 변경은 `docker compose restart` 가 아니라
`down && up -d` 로 컨테이너를 재생성해야 단일 파일 bind-mount 스테일을 피한다.
