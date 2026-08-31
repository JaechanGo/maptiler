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

### (4) 3D 건물 — PostGIS `building` 테이블 (martin 동적타일)  ★ 갱신 잦음
> `buildings.mbtiles` 방식은 T028 에서 폐기 — 현행은 PostGIS 시도 파티션 + martin `/dyn/building`.
- **다운로드**: data.go.kr [15052097 일별](https://www.data.go.kr/data/15052097/fileData.do)(신축 최신) 또는 [15083092 분기](https://www.data.go.kr/data/15083092/fileData.do), 또는 VWorld 공간정보 다운로드(AL_D010).
  - **시도별 SHP**(전체데이터, 최신 기준일). ⚠ 2026 개편 후 광주(29)·전남(46)은 **통합 12 파일 하나**로만 나온다(20260809 실측).
  - 좌표계 **EPSG:5186**. 컬럼 `A0~A28`(generic): **A1=GIS건물통합식별번호(28자리, 고유키)·A2=PNU·A16=높이(m, 결측多)·A26=지상층수**. 버전별 A코드 변동 가능 — ogrinfo 로 컬럼 정의서 대조.
- **적재**: `STEPS=building scripts/postgis/load-all.sh` (또는 `scripts/postgis/load_building.sh --shp <폴더> --fresh`)
  - 처리: 5186→4326 + `render_height = A16>0?A16:A26×3.3(폴백 6)` + ON CONFLICT(sido_cd,bld_mgt_no) 중복 방어.
  - load-all 은 직후 juso 건물도형 패치(아래 4b)와 **타일 캐시 3겹 교체**까지 자동 체인한다.
- **반영**: load-all 경유면 자동. 수동 적재였다면 `scripts/postgis/refresh_tile_cache.sh` 필수
  (martin L1 재시작 → 게이트웨이 L2 퍼지 → 스타일 `/dyn/v<BUILD_ID>/` 버전 범프 — 하나라도 빠지면
  줌 레벨마다 다른 시대의 캐시 타일이 섞여 보인다. 2026-08-31 실측).

### (4b) juso 건물도형 신축 패치 — 월간  ★ AL_D010 공백 보완
AL_D010 은 신개발지구 신축이 수년 늦다(과천지식정보타운 실측 — 최신본에도 전무). 행안부
**건물도형(TL_SGCO_RNADR_MST/DONG)** 으로 증분 보완한다. 전체 흐름은 AL_D010 재적재 **직후**가 원칙
(dedup 이 최신 건물 기준으로 서야 함 — load-all 이 이 순서를 보장).
- **다운로드**(수동, 심사 계정): business.juso.go.kr → 전자지도 제공 → **건물도형** 월간 전체분
  (매월 1일 게시. 기존 신청그룹 재사용: `JsmAddressInfoAplyDetails?reqstGroup=46372`).
  시도별 zip 을 `$BUILD_HOME/sources/juso_building_shp/` 에 배치.
- **적재**: `scripts/postgis/load_building_juso_all.sh` — 시도별 해제→적재→삭제.
  - MST 는 **건물군(단지) 폴리곤** — 동(DONG)이 안에 있거나 기존 건물 3채 이상 품으면 제외,
    최종 dedup 은 기존 건물과 **겹침 총합 15%** 초과 시 제외(신축=빈 땅 논리). 상세는 스크립트 헤더.
  - dedup 의 building 조회는 시도 **리터럴** 필수 — 상관식이면 파티션 프루닝 붕괴(경기 30분+).
- **반영**: 단독 실행 시 `scripts/postgis/refresh_tile_cache.sh` 잊지 말 것(스크립트가 말미에 상기시킴).
- **검증**: 신개발지구 1곳(예: 과천지식정보타운)을 z16/z17/z18 로 돌며 타워 존재·덮개 부재·전 줌 동일 확인.

### (4c) 위성(정사영상) 베이스맵 — 폐쇄망 반입  ★ 레이어 스위처 '위성' 모드
데모/뷰어의 레이어 타입(기본/2D/3D/**위성**) 중 위성 모드의 원천. 외부 XYZ(구글·VWorld 실시간)는
폐쇄망에서 못 쓰므로 **정사영상을 반입해 자체 mbtiles 로 서빙**한다.
- **다운로드**(수동, 회원가입): 국토지리정보원 **정사영상** — 국토정보플랫폼(map.ngii.go.kr) 국토정보맵에서
  도엽 단위 TIFF 다운로드(대용량 전송 SW 자동 설치). 도시 12cm·일반 25cm.
  **라이선스: 무료·"이용허락범위 제한 없음"**(공공데이터포털 15059919) — 재배포·폐쇄망 반입 적합.
  단 국외 반출 금지(공간정보관리법)·보안시설은 마스킹 제공. 좌표계 무관(변환 스크립트가 3857 재투영).
  `$BUILD_HOME/sources/ortho/` 에 배치. (저줌 보조가 필요하면 Copernicus Sentinel-2 원시자료(10m,
  출처표시 재배포 가능)로 전국 배경 합성 가능 — EOx s2maps 완성본은 2018년판부터 비상업(NC)이라 부적합.)
- **변환**: `scripts/gen_satellite_mbtiles.sh` — VRT 모자이크 → 3857 재투영 → MBTiles(JPEG) → 오버뷰.
  용량 감: 전국 z15 급 ≈ 4~5GB, 도심 z17 추가 시 수십 GB(반입 매체 계획에 반영).
- **등록**: mbtiles 를 tiles/ 에 배치한 **후에만** `tileserver-config.json` 의 data 에
  `"satellite": {"mbtiles": "satellite.mbtiles"}` 추가 → tileserver 재시작.
  ⚠ 파일 없이 미리 등록하면 tileserver-gl v5 가 **크래시 루프**(2026-09-01 실측) — 그래서 저장소
  기본 config 에는 satellite 항목이 없다.
- **활성화**: 데모의 '위성' 버튼은 `/data/satellite.json` 프로브로 자동 활성화(코드 수정 불요).
  위성 모드는 하이브리드 — 영상 위에 도로·라벨·필지·경계는 유지, 면(녹지·물·건물)만 숨김.

### (7) 시설/상가 POI — `geocode.sqlite`(biz) + `poi.mbtiles`  ★ 분기 갱신
- **다운로드**: data.go.kr [15083033](https://www.data.go.kr/data/15083033/fileData.do) — **시도별 CSV 17개**(UTF-8). 컬럼: 상호명·상권업종(대/중/소분류)·도로명주소·**경도/위도(WGS84)**.
- **지오코딩 적재**: 17개 CSV 를 한 폴더에 모은다 (kind=biz). 좌표 이미 4326 → 변환 불필요.

  ```
  # (구) 09-gen-geocode.py --poi-csv-dir <폴더>            ← 폐지. 주소 0행 DB 로 정본을 대체함
  # (신) 상가/POI 만 갱신하는 경로는 없다. 전체 빌드로 갱신한다:
  python3 scripts/09-gen-geocode.py \
      --src   <202607 원천 폴더> \
      --poi-csv-dir <폴더> \
      --osm   <osm.sqlite> \
      --out   <산출 경로> --dedup er
  ```

  > **주의(T043) — 위 명령은 지금 그대로는 서지 않는다.** 범위가 전국이거나 `--out` 이
  > `~/geocode-build` 아래를 겨누면 게이트 G0(전국 재빌드 차단)가 `exit 2` 로 멈춘다.
  > 고장이 아니라 T018 리(里) 백필 처분이 끝날 때까지의 **의도된 정지**다.
  > 그러므로 **T018 처분 전에는 정본 `geocode.sqlite` 의 분기 POI 갱신이 불가능하다** —
  > 이것은 우회로가 없는 정지이고, 아래 (a)(b)(c) 가 그 안에서 실제로 할 수 있는 전부다.

  **(a) T018 처분 전 — 검증용 산출물까지만 (정본 교체 불가)**

  ```
  # 정본 밖 경로에 시도 단위로 만든다. G0 는 두 조건(전국 범위 / 정본 조준) 모두 아닐 때만 통과한다
  python3 scripts/09-gen-geocode.py \
      --src <202607 원천 폴더> --only chungbuk \
      --poi-csv-dir <폴더> --out /tmp/geocode-poi-check.sqlite --dedup er \
      --taxonomy-out /tmp/poi-taxonomy.json
  ```
  `--only` 로 범위를 좁히고 `--out` 을 정본 밖으로 두면 G1~G10 은 그대로 돌아 CSV 적재
  결과(kind=biz 행수·좌표 범위·무결성)를 확인할 수 있다. **정본은 건드리지 않는다.**
  `--taxonomy-out` 을 주지 않으면 `style/poi-taxonomy.json` 은 **쓰이지 않는다**(T043 M-1) —
  부분 빌드의 빈약한 분류로 저장소 파일을 덮어쓰지 않기 위해서다.

  **(b) T018 처분 후 — 정본 전국 재빌드**

  T018 처분(리 백필 완료 + 검증)이 끝난 뒤에야 `--t018-disposed` 를 붙일 수 있다.
  해제에 필요한 5개 조건은 G0 실패 메시지가 그대로 출력한다. 그 5개를 모두 마친 다음:

  ```
  python3 scripts/09-gen-geocode.py \
      --src <202607 원천 폴더> --poi-csv-dir <폴더> --osm <osm.sqlite> \
      --out ~/geocode-build/geocode.sqlite --dedup er --t018-disposed
  ```
  이때 `--taxonomy-out` 없이도 `style/poi-taxonomy.json` 이 갱신된다(정본 빌드이므로).

  **(c) 분류 트리(`style/poi-taxonomy.json`)만 다시 뽑기**

  이미 POI 가 든 DB 가 있으면 빌드를 다시 돌리지 않고 그 DB 에서 트리만 뽑아도 된다.
  `write_taxonomy()` 는 `--out` DB 를 `mode=ro` 로 읽어 쓰므로, (a)의 산출물이나 정본을
  읽기전용으로 열어 쓰는 짧은 스크립트로 충분하다. **정본을 읽을 때도 쓰기는 금지**다.
- **지도 라벨 타일**: `ogr2ogr`(경도/위도→점) → `tippecanoe`(z12–16, `--drop-densest-as-needed --cluster-distance`) → `tiles/poi.mbtiles`.
- **반영**: `geocode.sqlite` 교체 → `docker compose restart geocode`; `poi.mbtiles` 등록 → restart tileserver.

### (5)(6) 지오코딩 — `geocode.sqlite`  ★ 월/분기
- **주소(5)**: 내비게이션용DB(business.juso.go.kr, 심사) `match_build_*.txt`(EPSG:5179, CP949). `.7z`는 `bsdtar -xf …7z match_build_*.txt`로 17개 시도 확인.
- **이름(6)**: `python3 ~/geocode-build/osm-from-mbtiles.py` → `osm.sqlite`(korea.mbtiles에서 역/지명/POI 재추출).
- **통합 빌드**: `python3 scripts/09-gen-geocode.py --src <내비DB폴더> --osm ~/geocode-build/osm.sqlite [--poi-csv-dir <상가CSV폴더>] --source-label 2026.07` → `~/geocode-build/geocode.sqlite`.
  `--src` 는 **필수**다(T043). 정본을 겨누는 실행은 게이트 G0 가 막으며, 해제는 `--t018-disposed` 로만 가능하다.
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
- 적재: 상가 CSV들과 함께 한 폴더에 둔다 → kind=biz. **`--poi-csv-dir` 단독 실행은 폐지됐다**
  (주소 0행 DB 로 정본을 대체하던 경로). 위 (7) '지오코딩 적재' 의 (a)(b)(c) 를 쓰라 —
  **T018 처분 전에는 (a) 검증까지만 가능하고 정본 교체는 되지 않는다.**

⚠️ **한글 NFC/NFD 정규화 필수**: 정부 데이터/파일명이 NFD(자모분리)일 수 있어, 키워드 매칭(EXCLUDE)·FTS 검색이 조용히 실패한다. build-localdata·검색 모두 NFC로 통일.
⚠️ **중복**: 상가정보 ↔ LOCALDATA 겹침(같은 가게 중복). 검색은 되지만 중복제거·랭킹은 개선 과제.
