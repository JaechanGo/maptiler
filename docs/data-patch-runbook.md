# CUVIA 지도·지오코딩 서버 — 데이터 출처 & 패치 런북

폐쇄망 자체 지도+지오코딩 서버. **각 산출물(레이어)은 독립적으로 재빌드·교체(hot-swap) 가능**하도록 구성.
이 문서는 "어떤 화면 요소가 어느 데이터에서 오고, 어떻게 갱신(패치)하나"를 정리한다(패치 서비스 설계 기준).

> 운영 위치: **`~/geocode-build/`** (iCloud 밖 로컬 — GB급 산출물은 iCloud 동기 금지).
> 소스 코드(타일 빌드 스크립트 등): iCloud repo `maptiler/`(`/Users/jaechango_cudo/Library/Mobile Documents/com~apple~CloudDocs/maptiler`).
> 배포: `~/geocode-build/deploy/docker-compose.yml` → tileserver(8080)·demo(8081)·geocode(8082).

---

## 1. 산출물 ↔ 데이터 출처 (한눈에)

| # | 화면 요소 | 산출물 | 원천 데이터 | 데이터셋 / 위치 | 좌표계 | 갱신주기 | 심사 | 라이선스 |
|---|---|---|---|---|---|---|---|---|
| 1 | 도로·지명·행정경계·건물2D·수역·기본POI | `tiles/korea.mbtiles` | **OpenStreetMap** (Geofabrik 한국) | geofabrik `south-korea-latest.osm.pbf` | OSM 4326 | OSM 상시(재빌드 시점) | 무 | ODbL(출처표시·동일조건) |
| 2 | 3D 지형(terrain) | `tiles/terrain.mbtiles` | **NASA SRTM 30m** (AWS Open Data) | s3 `elevation-tiles-prod` skadi | (Terrain-RGB) | 정적(SRTM 고정) | 무 | public domain |
| 3 | 아파트 동(棟) 라벨 | `tiles/dong.mbtiles` | OSM 건물 name/ref 파생 | korea.mbtiles에서 추출 | 4326 | OSM 연동 | 무 | ODbL |
| 4 | **3D 건물(footprint+높이)** | `tiles/buildings.mbtiles` | **국토부 GIS건물통합정보** | data.go.kr **15052097**(일별)/**15083092**(분기), VWorld | **EPSG:5186** | 전체=월간·변동=일간 | 무(공개) | 이용허락 제한없음 |
| 5 | 지오코딩 — 도로명주소 | `geocode.sqlite`(kind=addr) | **행안부 내비게이션용DB** | business.juso.go.kr (내비게이션용DB) | **EPSG:5179** | 월간(+일변동) | **신청·심사(전국=행안부)** | 공공누리 제1유형 |
| 6 | 지오코딩 — 역/지명/POI 이름 | `geocode.sqlite`(kind=station/place/road/poi…) | OSM (korea.mbtiles 파생) | korea.mbtiles | 4326 | OSM 연동 | 무 | ODbL |
| 7 | **시설/상가 POI(편의점 등)** | `geocode.sqlite`(kind=biz) + `tiles/poi.mbtiles` | **소상공인시장진흥공단 상가(상권)정보** | data.go.kr **15083033** | **WGS84(4326)** | 분기 | 무(공개) | 이용허락 제한없음 |
| 8 | 글꼴(라벨 폰트) | `style/glyphs/*` | Google Noto Sans (OpenMapTiles fonts) | 사전 빌드 | — | 정적 | 무 | OFL |
| 9 | 지도 스타일(팔레트) | `style/style.json` | 자체 제작(CUDO1) | repo `style/` | — | 수동 | — | 자체 |

> **참고**: 1·2·3·6번(OSM/지형/동/이름)은 무심사. 5번(주소 좌표)만 **심사 필요** — 전국은 행정안전부 승인.
> 도로명주소 데이터 상세·심사 절차는 repo `docs/geocode-juso-plan.md`, 라이선스는 `docs/data-licenses.md`.

---

## 2. 데이터별 패치(갱신) 절차

각 레이어는 **독립 재빌드 → 산출물 파일 교체 → 컨테이너 재시작**으로 무중단에 가깝게 갱신된다.
공통: 온라인 PC에서 빌드 → 산출물을 폐쇄망 서버 `~/geocode-build/`(타일은 `tiles/`)에 반입 → `docker compose restart <서비스>`.

### (4) 3D 건물 — `buildings.mbtiles`  ★ 갱신 잦음
- **다운로드**: data.go.kr [15052097 일별](https://www.data.go.kr/data/15052097/fileData.do)(신축 최신) 또는 [15083092 분기](https://www.data.go.kr/data/15083092/fileData.do), 또는 VWorld 공간정보 다운로드.
  - **시도별 SHP 17개**(전체데이터, 최신 기준일). VWorld는 로그인+라온K 필요 → data.go.kr이 간단.
  - 좌표계 **EPSG:5186**(.prj `AUTHORITY["EPSG","5186"]`). 컬럼은 `A0~A28`(generic): **A16=높이(m, 결측多)·A26=지상층수·A27=지하층수**. 컬럼 의미는 페이지의 "컬럼 정의서"로 확인.
- **빌드**: `bash ~/geocode-build/deploy/build-buildings.sh <SHP폴더>`
  - 처리: 5186→4326 변환 + `render_height = A16>0?A16:A26×3.3` + tippecanoe(z13–16) + tile-join. 디스크 절약형(시도별 처리·즉시 삭제).
- **반영**: `tiles/buildings.mbtiles` 교체 → tileserver-config에 `buildings` 데이터 등록 → `docker compose restart tileserver`.

### (7) 시설/상가 POI — `geocode.sqlite`(biz) + `poi.mbtiles`  ★ 분기 갱신
- **다운로드**: data.go.kr [15083033](https://www.data.go.kr/data/15083033/fileData.do) — **시도별 CSV 17개**(UTF-8). 컬럼: 상호명·상권업종(대/중/소분류)·도로명주소·**경도/위도(WGS84)**.
- **지오코딩 적재**: 17개 CSV 병합 후 `python3 scripts/07-gen-geocode.py --poi-csv 상가_전국.csv` (kind=biz). 좌표 이미 4326 → 변환 불필요.
- **지도 라벨 타일**: `ogr2ogr`(경도/위도→점) → `tippecanoe`(z12–16, `--drop-densest-as-needed --cluster-distance`) → `tiles/poi.mbtiles`.
- **반영**: `geocode.sqlite` 교체 → `docker compose restart geocode`; `poi.mbtiles` 등록 → restart tileserver.

### (5)(6) 지오코딩 — `geocode.sqlite`  ★ 월/분기
- **주소(5)**: 내비게이션용DB(business.juso.go.kr, 심사) `match_build_*.txt`(EPSG:5179, CP949). `.7z`는 `bsdtar -xf …7z match_build_*.txt`로 17개 시도 확인.
- **이름(6)**: `python3 ~/geocode-build/osm-from-mbtiles.py` → `osm.sqlite`(korea.mbtiles에서 역/지명/POI 재추출).
- **통합 빌드**: `python3 ~/geocode-build/09-gen-geocode.py --src <내비DB폴더> --osm ~/geocode-build/osm.sqlite [--poi-csv 상가.csv]` → `~/geocode-build/geocode.sqlite`.
  - 5179→4326 순수파이썬 변환 내장(무의존). 본번/부번 정밀 + 폴백.
- **반영**: `docker compose restart geocode`.

### (1)(3) 벡터 지도·동 라벨 — `korea.mbtiles` / `dong.mbtiles`  ★ OSM 갱신 시
- repo 스크립트: `scripts/01-download-data.sh`(Geofabrik OSM 다운로드) → `scripts/02-gen-vector.sh`(Planetiler→korea.mbtiles) → `scripts/04-gen-dong-labels.py`→`05-gen-dong-tiles.py`(dong.mbtiles).
- **반영**: `tiles/korea.mbtiles`·`dong.mbtiles` 교체 → restart tileserver.

### (2) 지형 — `terrain.mbtiles`  ☆ 정적(거의 불변)
- `scripts/03-gen-terrain.sh`(AWS SRTM → Terrain-RGB). SRTM은 갱신이 거의 없어 1회 빌드 후 고정.

### (8)(9) 폰트·스타일 — `style/`  ☆ 수동
- 글리프는 사전 빌드(불변). 스타일 변경은 `scripts/build-style.sh` → `style/style.json` 교체 → restart tileserver.

---

## 3. "패치 서비스" 설계 메모

독립 갱신이 가능하도록 다음 원칙으로 자동화하면 된다:

1. **데이터셋별 버전 = 기준일**: 산출물을 `buildings_20260609.mbtiles`처럼 기준일로 버저닝 → 심볼릭링크(`buildings.mbtiles`)만 교체해 롤백 용이.
2. **소스별 독립 파이프라인**: 건물/상가/주소/OSM은 서로 의존 없음 → 각자 `다운로드 → 전처리 → 빌드 → 산출물` 잡으로 분리(cron/큐).
3. **갱신 주기 매트릭스**: 건물=월(일변동 가능)·상가=분기·주소=월(심사 갱신)·OSM=수시·지형=정적.
4. **무중단 교체**: 산출물은 read-only 마운트 → 새 파일로 교체 후 `docker compose restart <서비스>`(tileserver/geocode). geocode-api는 요청마다 DB를 열어 교체에 강함.
5. **온라인→폐쇄망 2단계**: 온라인 빌드 PC에서 산출물 생성·체크섬 → 번들(`scripts/package.sh` 패턴: `docker save` 이미지 tar + 산출물 tgz) → 폐쇄망 반입 → `docker load` + 산출물 배치 + restart.
6. **디스크**: GB급 산출물·중간파일은 iCloud 밖 로컬에서만. 빌드는 디스크 절약형(시도별·중간파일 즉시 삭제) 사용.
7. **자동 다운로드 한계**: data.go.kr/juso/VWorld는 로그인(+VWorld 라온K) 필요 → 완전 자동화 어려움. 다운로드만 수동/반자동, 그 이후(전처리·빌드·반영)는 스크립트로 자동화.

---

## 4. 디렉터리 맵

```
~/geocode-build/                      # 배포·패치 홈 (로컬, iCloud 밖)
├─ geocode.sqlite                     # 통합 지오코딩 DB (주소+OSM이름+상가)
├─ geocode-api.py                     # 무의존 지오코딩 API 서버
├─ 09-gen-geocode.py                  # 통합 지오코딩 빌더(주소+OSM[+상가])
├─ osm-from-mbtiles.py                # korea.mbtiles → OSM 이름 소스(osm.sqlite)
├─ tiles/
│  ├─ korea.mbtiles  terrain.mbtiles  dong.mbtiles   # (repo tiles/와 동일 산출물)
│  ├─ buildings.mbtiles               # 3D 건물 (GIS건물통합정보)
│  └─ poi.mbtiles                     # 시설/상가 POI 라벨
├─ deploy/
│  ├─ docker-compose.yml              # tileserver+demo+geocode (프로젝트 cuvia)
│  ├─ nginx.conf  demo-nginx.conf     # 게이트웨이/데모 nginx
│  └─ build-buildings.sh              # GIS건물 SHP → buildings.mbtiles
└─ start.sh / stop.sh                 # (옵션) 바이너리 데몬 기동/중지

maptiler/ (iCloud repo)               # 소스 코드 본체
├─ scripts/01..05, 07, 08, 09         # OSM 타일·지형·동·지오코딩 빌드
├─ scripts/build-style.sh, package.sh # 스타일 조립, 폐쇄망 번들
├─ style/  server/  demo/  vendor/    # 스타일·서버설정·데모·오프라인자산
└─ docs/data-sources.md, data-licenses.md, geocode-juso-plan.md
```

## 5. 출처 링크
- OSM 한국: https://download.geofabrik.de/asia/south-korea.html (ODbL)
- SRTM(AWS): https://registry.opendata.aws/terrain-tiles/
- GIS건물통합정보: https://www.data.go.kr/data/15052097/fileData.do · https://www.data.go.kr/data/15083092/fileData.do
- 내비게이션용DB(주소 좌표, 심사): https://business.juso.go.kr/addrlink/elctrnMapProvd/geoDBDwldList.do
- 상가(상권)정보: https://www.data.go.kr/data/15083033/fileData.do
- (상세) repo `docs/data-sources.md`, `docs/data-licenses.md`, `docs/geocode-juso-plan.md`

---

## 부록 — LOCALDATA 시설 POI (대기업·전체 인허가)

소상공인 상가정보가 누락하는 **대기업 직영(스타벅스·맥도날드 등)**까지 넣으려면 LOCALDATA 사용.
- 출처: **LOCALDATA 지방행정인허가데이터** https://www.localdata.go.kr/ (카테고리별, 무료). data.go.kr 표준데이터도 동일.
- 형식: **CP949**, 39컬럼, 좌표 **EPSG:5174**, 영업상태 혼재.
- 핵심: **휴게음식점**(스벅/카페/맥도/패스트푸드)·일반음식점·제과점·대규모점포·병원·의원·약국·미용·숙박·주유(석유판매)·PC방·노래방 등 **물리 시설**.
- 빌드: `python3 build-localdata.py <인허가정보_DIR> localdata/localdata_clean.csv`
  → 비물리(통신판매·제조·도매·농축·공사·대행) 카테고리 제외 + 영업중 + 5174→4326(gdaltransform) + **NFC 정규화** + 상가포맷 변환.
- 적재: 상가 CSV들과 함께 한 폴더에 두고 `09-gen-geocode.py --poi-csv-dir <폴더>` → kind=biz.

⚠️ **한글 NFC/NFD 정규화 필수**: 정부 데이터/파일명이 NFD(자모분리)일 수 있어, 키워드 매칭(EXCLUDE)·FTS 검색이 조용히 실패한다. build-localdata·검색 모두 NFC로 통일.
⚠️ **중복**: 상가정보 ↔ LOCALDATA 겹침(같은 가게 중복). 검색은 되지만 중복제거·랭킹은 개선 과제.
