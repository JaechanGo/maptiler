# maptiler — 폐쇄망 3D 지도 서비스

OpenMapTiles 스키마 기반의 **자체 호스팅 3D 지도 서비스**. 폐쇄망(air-gapped)에서
MapTiler Cloud를 대체하여 벡터 타일·지형 타일·스타일·글리프·스프라이트를 서빙한다.
소비측 프론트엔드(MapLibre GL)는 `style.json` URL만 사내 서버로 교체하면 된다.

- 대상 지역: 한국 전체
- 벡터 타일: Planetiler (OpenMapTiles 스키마)
- 지형: SRTM 30m → Terrain-RGB
- 서버: TileServer-GL (Docker, x86_64)
- 3D 건물 + 3D 지형(토글) 지원
- 아파트 동(棟) 라벨: OSM 추출 → 별도 벡터타일(dong.mbtiles, 토글)
- 역·공항·산봉우리·주요시설 라벨 (OpenMapTiles poi/place 레이어 활용)
- 통합 지오코더: 전국 도로명주소(내비DB) + OSM 이름 + 소상공인 상가 + LOCALDATA 인허가
  → FTS5+R-tree 단일 인덱스. 결과에 전체주소·지번·우편번호·전화·업종대분류(cat1) 동반
- 상가·시설 라벨: 지오코더 시설(biz) → 별도 벡터타일(poi.mbtiles, 업종 색상 도트+상호 라벨)

## 설계 문서

- [docs/superpowers/specs/2026-06-12-maptiler-3d-closed-network-design.md](docs/superpowers/specs/2026-06-12-maptiler-3d-closed-network-design.md)
- [docs/data-sources.md](docs/data-sources.md) — 화면 요소별 데이터 출처(지명·역·건물·지형 등)와 기준 시점, 갱신 방법
- [docs/data-licenses.md](docs/data-licenses.md) — 상용 배포 라이선스 매트릭스와 의무사항
- [docs/integration-guide.md](docs/integration-guide.md) — 소비 프론트 연동(드롭인 교체)

## 빌드/실행 순서 (인터넷 가능한 Mac)

```bash
./scripts/00-check-prereqs.sh    # 도구 점검
./scripts/01-download-data.sh    # OSM/글리프/MapLibre/Maputnik
./scripts/02-gen-vector.sh       # 벡터 타일 (korea.mbtiles)
./scripts/03-gen-terrain.sh      # 지형 타일 (terrain.mbtiles)
./scripts/04-gen-dong-labels.py  # 동 라벨 추출 (OSM → data/dong/*.geojson)
./scripts/05-gen-dong-tiles.py   # 동 라벨 타일 (dong.mbtiles)
./scripts/10-gen-buildings.sh    # 3D 건물 타일 (buildings.mbtiles, GIS건물통합 SHP)
./scripts/11-build-localdata.py  # LOCALDATA 인허가 → 상가포맷 CSV (영업중·비물리제외·NFC)
./scripts/09-gen-geocode.py \    # 통합 지오코딩 인덱스 (geocode.sqlite)
  --src <내비DB> --osm osm.sqlite --poi-csv-dir <상가CSV폴더> --out geocode.sqlite
./scripts/12-build-poi.sh        # 시설 라벨 타일 (poi.mbtiles, geocode.sqlite의 biz 추출)
./scripts/13-qc-check.py         # 빌드 QC 검증 (NFC·좌표·시도커버리지·인덱스·골든질의·스타일↔타일)
./scripts/build-style.sh         # style.json 조립 ★ 서버 기동 전 필수 (gitignore 산출물)
./scripts/package.sh             # 폐쇄망 반입 번들 (QC 게이트 통과 시 dist/, 대용량은 BUILD_HOME)
```

> 선택: `python3 scripts/build-studio.py` → http://localhost:8090 (무의존 빌드 콘솔).
> 업로드(드래그앤드롭·다중·⌘V)·체크박스 정제·종류별 진행률(SSE)·QC·패키징을 웹에서.
> `/style` 스타일 디자인: 객체별(배경·물·녹지·도로·건물·라벨…) 색을 팔레트/색상값으로 설정,
> MapLibre 라이브 미리보기, 저장 시 `style/theme.json` 기록 → `build_style.py` 적용. 기본 로컬 전용(외부노출은 `HOST=0.0.0.0`).

폐쇄망 서버: 번들 해제 후 `./scripts/deploy.sh /path/to/images.tar`

> `style/style.json` 은 생성물이라 git에 없다. `git clone` 직후 바로 `docker compose up` 하면
> 스타일 404가 나므로 반드시 `build-style.sh` 를 먼저 실행할 것 (`package.sh` 는 자동 수행).

## 배포 구조 (빌드 / 운영 2환경)

- **인터넷 서버 = 빌드 + 모델하우스(쇼룸)**: `build-studio.py`(또는 위 CLI)로 빌드 → `package.sh` 번들.
  같은 호스트에 운영 스택(tileserver·demo·geocode·Style Studio)을 띄워 미리보기·스타일 디자인.
- **번들 물리 반입** → **폐쇄망 서버 = 운영**: `deploy.sh` 로 지도 스택 기동.
- **Style Studio**(`scripts/style-studio.py`, 경량·무의존) — 빌드 없이 **스타일만** 담당하며 번들에 포함되어
  쇼룸·폐쇄망 어디서나 배포. 색·POI·글꼴 테마를 팔레트/색상값으로 지정 + MapLibre 라이브 미리보기,
  저장 시 `style/theme.json` → `build_style.py` → tileserver 재시작(영구 반영). import/export 로 환경 간 스타일 이동.

```bash
# 폐쇄망/쇼룸 서버에서 지도 스택 옆에 Style Studio 기동(호스트 실행 → docker로 tileserver 재시작)
STUDIO_TOKEN=$(openssl rand -hex 12) ./scripts/start-style-studio.sh   # → http://<서버IP>:8091/?token=…
```

> 보안: Style Studio 기본 바인드 `127.0.0.1`. LAN 노출(`HOST=0.0.0.0`) 시 `STUDIO_TOKEN` 설정 권장
> (변경 API는 `X-Studio-Token` 요구). `build-studio.py`(빌드 콘솔)는 인터넷 서버 전용.

### 단일 도메인 게이트웨이 (공개망 권장)

`server/docker-compose.yml` 의 `gateway`(nginx) 가 모든 서비스를 한 포트로 통합한다:
`/`→demo · `/styles /data /fonts`→tileserver · `/geocode /reverse`→geocode. 데모 JS는 게이트웨이
경유(80/443/8088 포트)면 자동으로 same-origin(상대경로)으로 호출하므로 CORS·다중포트 노출이 불필요하다.

```bash
cd server && GATEWAY_PORT=80 docker compose up -d   # → http://<서버>/demo/ (단일 도메인)
# 게이트웨이만 노출하려면 tileserver/demo/geocode 의 ports 매핑 제거
```

> ★ tileserver-gl은 프록시 뒤에서 스타일 내부 URL을 **Host 호스트명 + 포트 80** 으로 만든다.
> **포트 80(http) 게이트웨이는 그대로 동작**하지만, **https(443)나 비표준 포트**로 띄우면
> `server/tileserver-config.json` 의 `options` 에 `"publicUrl": "https://maps.example.com/"` 를 명시해야
> glyphs/sprite/sources URL이 맞는다(미설정 시 라벨·타일이 안 보임 — 예전 게이트웨이 실패 원인).
> Style Studio는 관리툴이라 게이트웨이 밖 `:8091`(토큰) 유지.

## 브랜치

- `main` — 토대(타일 생성·서버·스타일·데모·패키징) + 3D 건물/지형 병합 완료
- `feature/3d-building` — 3D 건물(`Building 3D` fill-extrusion)
- `feature/3d-terrain` — 3D 지형(DEM 파이프라인 + setTerrain 토글)
