# 데이터·소프트웨어 라이선스 (상용 배포 기준)

본 서비스는 자가 생성 타일(Planetiler) + 자가 호스팅(TileServer-GL) 구조로,
**MapTiler Cloud 등 외부 유료 서비스와 계약 관계가 없다.** 아래 의무만 지키면
상업적 사용·사내外 서비스 탑재가 가능하다.

> 법률 자문이 아닌 기술적 라이선스 검토 문서다. 계약·분쟁 리스크가 큰 사안은 법무 검토를 거칠 것.
> 화면 요소별 데이터 출처 추적은 [data-sources.md](data-sources.md) 참조.

## 1. 사용 중인 구성요소

### 데이터

| 구성요소 | 출처 | 라이선스 | 상업 이용 | 의무 |
|---|---|---|---|---|
| 지도 데이터(도로·건물·지명·POI·역) | OpenStreetMap (Geofabrik 추출본) | **ODbL 1.0** | ✅ | 출처표시 필수, §3 참조 |
| 동(棟) 라벨 (dong.mbtiles) | OSM에서 자체 추출(scripts/04·05) | **ODbL 1.0** (파생DB) | ✅ | §3.2 참조 |
| 길찾기 도로망 그래프 (route/) | OSM에서 OSRM 추출(scripts/07) | **ODbL 1.0** (파생DB) | ✅ | §3.2 참조 — 기존 OSM 표기에 포함, 신규 의무 없음 |
| 수역 폴리곤 | osmdata.openstreetmap.de | ODbL 1.0 | ✅ | OSM 표기에 포함 |
| 국경·주기(보조) | Natural Earth | **퍼블릭 도메인** | ✅ | 없음 |
| 호수 중심선 | openmaptiles/lake_centerline | OSM 파생(ODbL) | ✅ | OSM 표기에 포함 |
| 지형 DEM | NASA SRTM 30m (AWS elevation-tiles) | **퍼블릭 도메인** | ✅ | 없음 |
| 타일 스키마·카토그래피 | OpenMapTiles | 코드 **BSD-3** / 디자인·스키마 결정 **CC-BY 4.0** | ✅ | **"© OpenMapTiles" 표기 필수**(§3.1) — 예외는 MapTiler 서면 허가(유료)뿐 |

### 소프트웨어·폰트

| 구성요소 | 라이선스 | 의무 |
|---|---|---|
| Planetiler (+ planetiler-openmaptiles 프로파일) | Apache-2.0 / BSD | 고지 보존 |
| TileServer-GL (-light) | BSD-2-Clause | 고지 보존 |
| OSRM (osrm-backend) | BSD-2-Clause | 고지 보존 |
| MapLibre GL JS | BSD-3-Clause | 고지 보존 |
| Maputnik (내부 편집 도구) | MIT | 고지 보존 |
| KlokanTech/Noto 글리프 폰트 | **SIL OFL 1.1** | 고지 보존, 폰트 단독 재판매 금지 |
| nginx, Docker 베이스 이미지 | 각 OSS 라이선스 | 번들 내 고지 보존 |

## 2. 결론 요약

- **무료로 상용 가능.** 사용량·뷰·사용자 수 과금 없음.
- **법적 필수 의무는 attribution 하나**: 화면에 `© OpenMapTiles © OpenStreetMap contributors` 노출.
- 소비 프론트가 `attributionControl`을 끄거나 CSS로 가리면 **위반**이 된다. 지도가 작으면 ⓘ 접힘 허용.
- 서비스/타일 소스코드 공개 의무 없음(§3.2의 좁은 예외만 주의).

## 3. 의무사항 상세

### 3.1 Attribution (필수)

`style/base.json`의 openmaptiles 소스에 내장되어 있어 MapLibre가 자동 표시한다:

```
© OpenMapTiles © OpenStreetMap contributors
```

- OpenMapTiles LICENSE.md 원문: 전자 지도는 *"[© OpenMapTiles](http://openmaptiles.org/)
  [© OpenStreetMap contributors](http://www.openstreetmap.org/copyright)"* 를 보이게 표기.
  인쇄물은 이미지 인근 텍스트로. 예외는 MapTiler(info@maptiler.com) 서면 허가로만 가능(상업적).
- OSM(ODbL §4.3): 데이터 사용 사실과 라이선스를 이용자가 인지할 수 있게 표기.

### 3.2 ODbL — Produced Work vs Derivative Database

- 타일을 **화면에 렌더링해 보여주는 것**(마커·팝업 포함)은 *Produced Work* → **share-alike 없음**.
  소비 서비스(예: Metis) 소스코드·DB 공개 의무 없음.
- `korea.mbtiles`, `dong.mbtiles` 자체는 OSM의 *Derivative Database* 다. 이를 **공개적으로 사용**
  (타일 서빙)하므로, ODbL §4.6에 따라 **요청 시 파생DB(또는 재생성 알고리즘)를 ODbL로 제공할 의무**가 있다.
  → 본 저장소의 scripts/01~05 가 전체 재생성 절차이므로, 요청 시 스크립트+원본 출처를 안내하면 충족된다.
  (사내 비공개 데이터를 이 mbtiles 안에 섞지 말 것 — 섞으면 그 데이터도 제공 대상이 된다.)
- **비-OSM 데이터(국가 건물DB 등)를 도입할 때는 반드시 별도 소스(별도 mbtiles)로 둘 것.**
  별도 소스/레이어로 나란히 서빙하면 *Collective Database* 로 간주되어 share-alike가
  비-OSM 데이터에 전파되지 않는다. 반대로 OSM 지오메트리에 비-OSM 속성을 병합(머지)하면
  그 결과 전체가 ODbL 파생DB가 된다. 현 구조(dong.mbtiles 분리)는 이 원칙에 맞게 설계됨.

### 3.3 고지 보존

폐쇄망 반입 번들(`dist/`)에 서드파티 LICENSE 사본(또는 THIRD-PARTY-NOTICES 통합 파일)을 동봉할 것:
MapLibre, Planetiler, TileServer-GL, Maputnik, Noto/OFL 폰트.

## 4. 도입 후보 국가 데이터 (2026-06-12 웹 검증 완료)

| 데이터셋 | 입수경로 | 계정/신청 | 라이선스 | 상업 | 타일가공·재배포 |
|---|---|---|---|---|---|
| 도로명주소 전자지도·건물도형 | business.juso.go.kr | 가입+신청서+기관 승인 | **공공누리 1유형**, 무료 | ✅ | ✅ (출처표시) |
| GIS건물통합정보 (SHP) | [data.go.kr 15083092](https://www.data.go.kr/data/15083092/fileData.do) | **비로그인** | "이용허락범위 제한 없음" | ✅ | ✅ |
| NGII DEM / 연속수치지형도 | 국토정보플랫폼(map.ngii.go.kr) | 로그인 | 공공누리 1유형/제한 없음 | ✅ | ✅ (출처표시) |
| 소상공인 상가(상권)정보 — POI 보강 | [data.go.kr 15083033](https://www.data.go.kr/data/15083033/fileData.do) | **비로그인**, CSV(위경도 포함), 분기 갱신 | "이용허락범위 제한 없음" | ✅ | ✅ |
| ⚠️ **V-World 다운로드** | vworld.kr | 비로그인 | 일부 데이터셋 **CC BY-NC-ND 표기** | **❌ (비영리·변경금지)** | **❌** |

핵심 주의·확인 사항:

- **같은 국가 데이터라도 배포 채널에 따라 라이선스가 다르다.** V-World에 게재된 "도로명주소 건물"·"DEM 90m"은
  CC BY-NC-ND(비영리·변경금지)로 표기되어 상용 타일 가공이 막힌다. **상용 입수는 반드시 원 기관 채널**
  (juso.go.kr=공공누리1, data.go.kr=제한 없음, 국토정보플랫폼=공공누리1)로 받을 것.
- **아파트 동(棟) 명칭 필드 확인됨**: 행안부 도로명주소 건물도형(TL_SPBD_BULD)의
  **`BULD_NM_DC`(상세건물명)** — "101동" 류 수록. `BULD_NM`=건물명(단지명),
  `BD_MGT_SN`(건물관리번호 25자리)으로 건축물대장 연계 가능.
- 공공누리 1유형 원문(kogl.or.kr): "상업적 활용 여부에 관계없이 무료로 자유롭게 이용",
  "2차적 저작물 작성 등 변형하여 이용" 허용, **출처표시 의무** (온라인은 출처 링크 제공).
- 도로명주소 데이터의 신청서에는 이용 목적 심사가 있으므로 "상용 지도 서비스 타일 제공" 목적을 명시해 승인받을 것.
- 고해상도 NGII DEM(5m/1m)은 국가공간정보 보안관리규정상 공개제한 여부를 신청 시 확인할 것(90m는 공개 확인).
- OSMF 공식 가이드라인(§3.2의 근거): [Collective Database Guideline](https://osmfoundation.org/wiki/Licence/Community_Guidelines/Collective_Database_Guideline_Guideline),
  [Horizontal Map Layers Guideline](https://osmfoundation.org/wiki/Licence/Community_Guidelines/Horizontal_Map_Layers_-_Guideline)
  — 한 피처타입(예: 건물/동명칭)을 같은 지역에서 **전량 비-OSM으로 대체**하고 레이어를 분리하면
  Collective Database로서 share-alike가 국가 데이터에 전파되지 않음. OSM·비OSM을 같은
  피처타입에 **혼재**시키면 Derivative가 되어 share-alike 적용.

규모 참고(분모): K-apt 기준 전국 공동주택(의무관리+100세대 이상) **21,625단지 / 144,706동**.
OSM 추출 동 라벨 **92,004개 = 그 대비 ~64%** (100세대 미만 포함 전체 공동주택 분모로는 더 낮음).

## 5. 운영 체크리스트

- [ ] 소비 프론트에서 attribution 표시 확인 (숨김 금지)
- [ ] 기존 코드에 남은 MapTiler Cloud API 키 폐기 (integration-guide §5)
- [ ] 번들에 서드파티 고지 동봉
- [ ] 새 데이터 출처 추가 시 본 문서에 행 추가 + 별도 소스 원칙 준수
- [ ] DEM을 국토지리정보원 데이터로 교체 시 해당 약관 검토 후 §1 표 갱신
