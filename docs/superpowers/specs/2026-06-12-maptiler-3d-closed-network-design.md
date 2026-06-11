# 폐쇄망 3D 지도 서비스 — 설계 문서

- 작성일: 2026-06-12
- 상태: 승인됨 (구현 계획 작성 대기)
- 저장소: `git@github.com:JaechanGo/maptiler.git`

## 1. 목표 (What & Why)

OpenMapTiles 스키마 기반의 **자체 호스팅 3D 지도 서비스**를 폐쇄망(air-gapped) 환경에
구축한다. 현재 소비측 프론트엔드(React + MapLibre GL, `gosujin921-wq/cuvia260113`)는
**MapTiler Cloud에 호스팅된 커스텀 스타일**을 사용하고 있다:

```js
new maplibregl.Map({
  style: 'https://api.maptiler.com/maps/<id>/style.json?key=<KEY>',
  center: [126.989, 37.426], zoom: 15, minZoom: 9, maxZoom: 22,
  pitch: 45, bearing: 0,
});
```

본 프로젝트의 목표는 이 **MapTiler Cloud 의존을 폐쇄망 내부 자체 서버로 드롭인(drop-in)
교체**하는 것이다. 프론트엔드 변경은 원칙적으로 **style URL 한 줄**로 한정한다.

```
api.maptiler.com/maps/<id>/style.json?key=<KEY>
        ↓ 교체
http://<사내서버>:8080/styles/cuvia/style.json   (API 키 불필요)
```

### 책임 경계 (Scope)

- **본 프로젝트(IN scope)**: 지도(베이스맵) 그 자체 — 벡터 타일, 지형 타일, 스타일,
  글리프, 스프라이트를 생성·서빙하는 서비스. 3D 건물·3D 지형 렌더링 포함.
- **소비측 프론트엔드(OUT of scope)**: 마커·DB 연동·비즈니스 로직. 마커 데이터는 DB에서
  조회하여 소비 프론트가 우리 지도 위에 직접 표시한다. 우리는 "마커가 얹힐 수 있는 지도"와
  마커 추가 예제/문서만 제공한다.

## 2. 확정 결정사항 (Decisions)

| 항목 | 결정 |
|------|------|
| 결과물 범위 | OSM→벡터타일, DEM→지형타일, 서버, 데모/연동가이드까지 end-to-end 전체 |
| 대상 지역 | 한국 전체 (OSM `south-korea.osm.pbf`) |
| 벡터 타일 생성 | **Planetiler** (OpenMapTiles 스키마 호환, 단일 jar, 한국 전체 수 분 생성) |
| DEM 출처 | **SRTM 30m**로 시작, 출처 독립 설계(추후 국토지리정보원 DEM 교체 가능) |
| 지형 인코딩 | Terrain-RGB (gdal 병합/투영 → rio-rgbify) |
| 빌드 위치 | 인터넷 가능한 Mac에서 전부 빌드 → 산출물만 폐쇄망 반입 |
| 폐쇄망 런타임 | Docker (x86_64 리눅스), TileServer-GL |
| 렌더러 | MapLibre GL JS (소비 프론트가 이미 사용 중, v5.16) |
| 스타일 편집 | Maputnik(오프라인) 산출물에 포함 |
| 연동 방식 | 표준 style.json URL (MapTiler Cloud 드롭인 교체) |

## 3. 아키텍처 & 데이터 흐름

```
┌─ 이 Mac (인터넷 O) — 빌드 단계 ────────────────────────────────┐
│  ① 데이터 수집   south-korea.osm.pbf (Geofabrik)               │
│                  SRTM 30m DEM GeoTIFF                          │
│  ② 벡터타일      Planetiler(jar) → tiles/korea.mbtiles         │
│                  (OpenMapTiles 스키마, render_height 포함)      │
│  ③ 지형타일      gdal 병합/투영 → rio-rgbify                   │
│     [3d-terrain] → tiles/terrain.mbtiles (Terrain-RGB)         │
│  ④ 에셋          glyphs(폰트PBF) · sprites · style.json        │
│                  maplibre-gl(로컬) · Maputnik(오프라인)         │
│  ⑤ 패키징        docker save --platform linux/amd64            │
│                  + mbtiles + 에셋 → 단일 tar 번들              │
└──────────┼──────────────────────────────────────────────────────┘
           │  USB / 승인 채널 반입
┌──────────▼─ 폐쇄망 (Docker x86_64) — 실행 단계 ───────────────┐
│  ⑥ docker load + docker compose up                            │
│     TileServer-GL : korea.mbtiles + terrain.mbtiles + 에셋    │
│                     /styles/cuvia/style.json 서빙             │
│     → http://<서버>:8080  (외부 인터넷 호출 0건)              │
│                                                               │
│  소비 프론트(별도) : style URL만 이쪽으로 변경 후 마커 표시   │
└───────────────────────────────────────────────────────────────┘
```

핵심 원칙: **빌드/실행 경계 = 폐쇄망 경계.** ①~⑤는 인터넷 Mac에서, ⑥만 폐쇄망에서.
⑥ 단계에 외부 호출이 단 한 건이라도 남으면 air-gap에서 실패하므로, 검증 단계에서
브라우저 DevTools Network "외부 요청 0건"을 확인한다.

## 4. 브랜치 전략

```
main ────●────────────────────────────●──────●──── (둘 다 사용 가능)
          \                          ↗      ↗
           \  feature/3d-building ──●      /   (Building 3D fill-extrusion)
            \                             /
             feature/3d-terrain ────────●     (DEM 파이프라인 + setTerrain 토글)
```

- **main**: 토대 — 데이터 수집 스크립트, Planetiler 벡터타일 생성, TileServer-GL/
  docker-compose, 기본 스타일(2D + 글리프/스프라이트), 데모 페이지, 연동 가이드,
  패키징/배포 스크립트, Maputnik.
- **feature/3d-building**: `Building 3D` fill-extrusion 레이어를 스타일에 정의.
  (소비 프론트가 `setLayerZoomRange('Building 3D', ...)`로 의존하는 바로 그 레이어)
- **feature/3d-terrain**: DEM→Terrain-RGB 생성 스크립트 + raster-dem 소스 +
  `setTerrain` **토글 옵션** + sky 레이어.

### 충돌 없는 병합을 위한 모듈 분리 원칙

두 기능이 같은 파일의 같은 줄을 고치면 병합 충돌이 발생한다. 스타일과 데모 JS를 조각
단위로 분리하여 각 브랜치가 자기 파일만 *추가*하고 공유 파일은 "조립 지점" 한 줄만
건드리게 한다.

- 건물: `style/layers/buildings-3d.json` + `demo/js/buildings.js`
- 지형: `style/layers/terrain.json` + `demo/js/terrain.js`
- 최종 `style/style.json`은 조각을 합치는 빌드 스텝(`scripts/build-style.*`)으로 생성.

## 5. 소비측 호환성 필수 조건 (코드 분석 결과)

소비 프론트(`HOME-v4/MapView.tsx`, `HOME/MapView.tsx`)가 스타일 내부를 런타임 후처리하므로,
우리 style.json은 아래를 만족해야 **코드 수정 없이** 동작한다.

| 소비 프론트 코드 | 우리 style.json 요구사항 |
|---|---|
| `setLayerZoomRange('Building 3D', 12, 24)` | **`"Building 3D"` 이름의 fill-extrusion 레이어 필수** |
| 높이를 `height` / `render_height` / `building:levels`에서 읽음 | 벡터 타일 `building` 레이어에 해당 속성 포함 |
| `pitch: 45~60`, `maxZoom: 22` | 3D 틸트 전제, z14 생성 후 오버줌 허용 |
| `map.setTerrain(null)` (의도적 비활성) | **터레인은 토글 가능**해야 함 (원거리 3D 건물 충돌 회피) |
| `styleimagemissing` 핸들러 | 누락 스프라이트가 치명적이지 않게 — 스프라이트 완비 |

> **알려진 트레이드오프**: MapLibre에서 터레인을 켜면 깊이 버퍼가 원거리 fill-extrusion을
> 가려 먼 거리 3D 건물이 사라진다. 소비 프론트는 이 때문에 터레인을 꺼두었다. 따라서
> `feature/3d-terrain`은 터레인을 "항상 켜짐"이 아니라 **사용자/옵션 토글**로 구현한다.

## 6. 디렉토리 구조

```
maptiler/
├── docs/superpowers/specs/          # 설계 문서
├── data/                            # 원본 (gitignore, 대용량)
│   ├── osm/south-korea.osm.pbf
│   └── dem/*.tif
├── tiles/                           # 생성 결과 (gitignore)
│   ├── korea.mbtiles
│   └── terrain.mbtiles              # [3d-terrain]
├── planetiler/                      # planetiler.jar
├── scripts/
│   ├── 01-download-data.sh          # OSM + SRTM (online, Mac)
│   ├── 02-gen-vector.sh             # Planetiler → korea.mbtiles
│   ├── 03-gen-terrain.sh            # [3d-terrain] DEM → terrain.mbtiles
│   ├── build-style.sh               # style/layers/* 조각 → style.json 조립
│   ├── package.sh                   # docker save + tar 번들
│   └── deploy.sh                    # [폐쇄망] docker load + compose up
├── server/
│   ├── docker-compose.yml           # tileserver-gl (+ 정적 서빙)
│   └── tileserver-config.json
├── style/
│   ├── style.json                   # 조립된 최종 스타일 (커밋 or 빌드 산출)
│   ├── layers/
│   │   ├── base.json
│   │   ├── buildings-3d.json        # [3d-building] "Building 3D" 레이어
│   │   └── terrain.json             # [3d-terrain]
│   ├── glyphs/                      # 폰트 PBF
│   └── sprites/
├── demo/                            # 서비스 동작 확인용 데모 (앱 아님)
│   ├── index.html
│   └── js/{map,buildings,terrain,markers-example}.js
├── vendor/                          # maplibre-gl, maputnik (로컬 번들)
├── docs/integration-guide.md        # 소비 프론트 연동 가이드 (URL 교체 + 마커 예제)
├── .gitignore
└── README.md
```

## 7. 폐쇄망 패키징 / 반입 절차

1. Mac에서 `01~03` + `build-style` 실행 → `tiles/*.mbtiles` + 조립된 에셋 생성.
2. `docker pull --platform linux/amd64 maptiler/tileserver-gl` → `docker save`로 tar.
   - **주의**: Apple Silicon에서 그냥 save하면 arm64 이미지 → 폐쇄망 x86_64에서 실행 불가.
     반드시 `--platform linux/amd64` 강제.
3. `package.sh`가 [이미지 tar + mbtiles + style/glyphs/sprites + demo + vendor + compose]를
   단일 tarball로 묶음.
4. 승인 채널(USB 등)로 반입.
5. 폐쇄망에서 `deploy.sh`: `docker load` → `docker compose up -d` → `http://<서버>:8080`.

## 8. 소비측 연동 (Integration)

- 소비 프론트는 `maplibregl.Map`의 `style`만 사내 URL로 교체. 나머지(center/zoom/pitch/
  bearing/min·maxZoom)는 기존 값 유지.
- 마커: 소비 프론트가 DB 조회 후 직접 표시. 대량이면 GeoJSON 소스 + 심볼/서클 레이어 +
  클러스터링 권장, 소량이면 `maplibregl.Marker`. **좌표는 `[경도, 위도]` 순서**.
- `docs/integration-guide.md`에 URL 교체 방법 + 마커 추가 예제(소량/대량 양쪽) 수록.

## 9. 검증 (Testing)

- **벡터**: TileServer-GL가 `korea.mbtiles` 렌더, `building` 레이어에 `render_height` 존재.
- **건물**: `Building 3D` fill-extrusion이 높이대로 솟음 + `setLayerZoomRange` 정상.
- **지형**: raster-dem 소스 로드 + `setTerrain`으로 기복 표시 + 토글 on/off + 터레인 ON
  상태에서 원거리 건물 동작 확인(트레이드오프 검증).
- **드롭인 호환**: 소비 프론트의 style URL만 바꿔도 기존 화면이 동일하게 렌더.
- **폐쇄망 적합성**: 데모를 오프라인에서 열고 DevTools Network "외부 요청 0건" 확인.
- **아키텍처**: amd64 이미지가 폐쇄망(x86_64)에서 정상 구동.

## 10. 보안 메모

- 현재 소비 프론트 공개 저장소에 MapTiler API 키가 노출되어 있다. 폐쇄망 전환 후에는
  해당 키가 불필요해지므로 프론트에서 제거하고, 노출된 키는 MapTiler 콘솔에서 폐기 권장.

## 11. 범위에서 제외 (YAGNI)

- 마커/DB 연동 로직 (소비 프론트 책임)
- OpenMapTiles 클래식 PostGIS/imposm 툴체인 (Planetiler로 대체)
- 레이어 SQL 수준의 스키마 커스터마이징 (디자인은 style.json으로 충분)
- 국토지리정보원 고정밀 DEM (SRTM로 시작, 추후 교체 가능하게만 설계)
