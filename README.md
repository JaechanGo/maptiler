# maptiler — 폐쇄망 3D 지도 서비스

OpenMapTiles 스키마 기반의 **자체 호스팅 3D 지도 서비스**. 폐쇄망(air-gapped)에서
MapTiler Cloud를 대체하여 벡터 타일·지형 타일·스타일·글리프·스프라이트를 서빙한다.
소비측 프론트엔드(MapLibre GL)는 `style.json` URL만 사내 서버로 교체하면 된다.

- 대상 지역: 한국 전체
- 벡터 타일: Planetiler (OpenMapTiles 스키마)
- 지형: SRTM 30m → Terrain-RGB
- 서버: TileServer-GL (Docker, x86_64)
- 3D 건물 + 3D 지형(토글) 지원

## 설계 문서

[docs/superpowers/specs/2026-06-12-maptiler-3d-closed-network-design.md](docs/superpowers/specs/2026-06-12-maptiler-3d-closed-network-design.md)

## 브랜치

- `main` — 토대(타일 생성·서버·스타일·데모·패키징)
- `feature/3d-building` — 3D 건물(`Building 3D` fill-extrusion)
- `feature/3d-terrain` — 3D 지형(DEM 파이프라인 + setTerrain 토글)
