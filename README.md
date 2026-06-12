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

## 설계 문서

[docs/superpowers/specs/2026-06-12-maptiler-3d-closed-network-design.md](docs/superpowers/specs/2026-06-12-maptiler-3d-closed-network-design.md)

## 빌드/실행 순서 (인터넷 가능한 Mac)

```bash
./scripts/00-check-prereqs.sh    # 도구 점검
./scripts/01-download-data.sh    # OSM/글리프/MapLibre/Maputnik
./scripts/02-gen-vector.sh       # 벡터 타일 (korea.mbtiles)
./scripts/03-gen-terrain.sh      # 지형 타일 (terrain.mbtiles)
./scripts/04-gen-dong-labels.py  # 동 라벨 추출 (OSM → data/dong/*.geojson)
./scripts/05-gen-dong-tiles.py   # 동 라벨 타일 (dong.mbtiles)
./scripts/build-style.sh         # style.json 조립 ★ 서버 기동 전 필수 (gitignore 산출물)
./scripts/package.sh             # 폐쇄망 반입 번들 (dist/)
```

폐쇄망 서버: 번들 해제 후 `./scripts/deploy.sh /path/to/images.tar`

> `style/style.json` 은 생성물이라 git에 없다. `git clone` 직후 바로 `docker compose up` 하면
> 스타일 404가 나므로 반드시 `build-style.sh` 를 먼저 실행할 것 (`package.sh` 는 자동 수행).

## 브랜치

- `main` — 토대(타일 생성·서버·스타일·데모·패키징) + 3D 건물/지형 병합 완료
- `feature/3d-building` — 3D 건물(`Building 3D` fill-extrusion)
- `feature/3d-terrain` — 3D 지형(DEM 파이프라인 + setTerrain 토글)
