<!-- 생성: build-input-readiness 워크플로우 (12 agents, ~630k tok, 읽기전용 인벤토리) · 2026-06-25 -->

# 빌드 입력 준비도 보고서 (Build Input Readiness)

> 작성: 테크리드 | 검증일: 2026-06-25 | 전제: **자동수집(01-download / _collect) 제외**, 모든 입력은 사전 업로드/배치된 상태로 간주하여 "처음~끝 무실패(from-scratch) 빌드" 가능 여부를 평가

---

## 1. 한줄 결론

**조건부 GO** — 도구/환경 및 8개 데이터 단계 중 7개는 무실패 빌드 준비 완료(READY)이나, **`geocode` 단계의 sangga(소상공인 상가정보) 입력 1건이 빌드 차단 결함(손상 ZIP, 로컬 복구 불가)** 이라, 이를 정상 데이터로 교체하기 전에는 처음~끝 무실패 통과가 보장되지 않는다. sangga 1건을 교체하면 GO.

> 핵심 근거: sangga 원본이 staged 사본(`poi-all/sangga/`)뿐 아니라 **store 원본(`store/f6/...`)까지 동일하게 손상**(248,152,064 B, EOCD 없는 절단 ZIP, `is_zipfile=False`)되어 있어 `sources/sangga` 재스테이징 경로도 부재 → **로컬 복구 불가, 외부 재취득 필수**.

---

## 2. 단계별 입력 준비도 표

| Stage | 핵심 입력 | 기대 | 실제 | Status | Verdict |
|---|---|---|---|---|---|
| **prereqs/도구** | java21·docker·python3·GDAL셋·jq·curl·rio·git·tippecanoe·tile-join·7z·psql·planetiler.jar·BUILD_HOME | 00점검 9종 + setup 추가종 전부 PRESENT | java 21.0.10, docker 7컨테이너 Up, 도구 전부 PRESENT, planetiler.jar 89MB | PRESENT | **READY** |
| **osm_vector (02)** | south-korea.osm.pbf, planetiler.jar, --download 보조 3종 | repo/data/osm PBF + 보조 ~1.4GB | PBF 268MB(repo), jar 89MB, 보조 3종(NE 434MB·lake 81MB·water 925MB) | PRESENT (보조경로 BUILD_HOME/data/osm는 MISSING이나 비치명) | **READY** |
| **terrain (03)** | SRTM 30m .hgt 48타일 | N33~N38 × E124~E131 = 48 | 48/48, 전부 25,934,402 B(유효) | PRESENT | **READY** |
| **dong (04/05)** | OSM PBF | repo/data/osm 1개 | 281MB + dong-labels.geojson 11.5MB 잔존 | PRESENT (BUILD_HOME 적재본 PARTIAL, 비치명) | **READY** |
| **areas (06)** | boundary_legal(법정동 17시도 SHP) + boundary_admin(BND_ADM_DONG_PG) + geocode.sqlite + ogr2ogr | 17시도 SHP세트 + 단일 행정동 SHP | legal 17/17(prj완비), admin 135MB 완비, geocode.sqlite 3.9GB, ogr2ogr OK | PRESENT | **READY** |
| **geocode (09)** | staged/navi(build·jibun 17×2), osm.sqlite, localdata_clean, facility_clean, **sangga/\*.csv** | 시도17 navi + 3.9GB DB + POI CSV 3종 | navi 17/17·17/17, osm.sqlite 594k행, localdata 2.19M행, facility 96.7k행 / **sangga 손상 ZIP** | **MISSING (sangga)**, 나머지 PRESENT | **PARTIAL** |
| **buildings (10) / PostGIS building** | staged/gis AL_D010 17시도 SHP, sources/building_db zip | 17시도 SHP + 중첩 zip 17 | SHP 24개(17시도 커버), download(1).zip 2.31GB(중첩17) | PRESENT (중복 (2)/(3) 7건) | **READY** |
| **localdata (11)** | staged/localdata 7대분류 CSV, gdaltransform | 7카테고리 전부·CSV 다수 | 7카테고리 195 CSV(0바이트0), gdaltransform OK | PRESENT | **READY** |
| **facility (11b)** | staged/facility default 4종 CSV | public_restroom·kiosk·bicycle_parking·civil_defense_shelter | 4종 전부 1CSV(비제로) | PRESENT (비기본 10종·aed 미배치=정상) | **READY** |
| **poi (12)** | geocode.sqlite (kind IN biz/facility) | biz/facility 행 존재 | **kind=biz/facility = 0건** (sangga 손상 + 직전 geocode가 POI 미적재) | **PARTIAL** | **PARTIAL** |
| **PostGIS 적재 (parcel/address/admin/facility)** | parcel SHP, building SHP, geocode.sqlite, boundary 추출본, navi, facility_src | 시군구 전수 + 17시도 + 3.9GB DB + 경계 + police/fire | parcel 257세트(17/17), building 24, geocode 3.9GB, 경계 완비, police 1·fire 4 CSV | PRESENT (facility_src/aed PARTIAL, 선택·비치명) | **READY** |

---

## 3. ⚠️ 업로드/배치 필요 (MISSING · PARTIAL)

### 🔴 차단(Blocking) — 1건

| 항목 | 경로 | 현상 | 출처/원인 | 조치 |
|---|---|---|---|---|
| **sangga (소상공인 상가정보)** | `~/geocode-build/poi-all/sangga/f6147232….csv` | 확장자 `.csv`이나 **절단 손상 ZIP**(248,152,064 B, `PK\x03\x04` 헤더, EOCD/중앙디렉터리 없음, `is_zipfile=False`). 09 add_biz가 `**/*.csv` glob로 utf-8-sig 직독 시 **UnicodeDecodeError로 빌드 크래시** | `data-sources.json` key=`sangga`, `dest=poi-all/sangga`, `extract=null`. **store 원본(`store/f6/f6147232…`)도 동일 손상** → 로컬 재스테이징 불가, `sources/sangga` 디렉터리 부재 | **정상 상가정보 CSV(또는 정상 ZIP) 외부 재취득 후 재업로드.** data.go.kr selectFileDataDownload→fileDownload 재실행 권장. store SHA 사본 무결성 동반 폐기/재적재 |

### 🟡 비차단(Non-blocking) — 정보/완전성 관점

| 항목 | 경로 | 현상 | 빌드 영향 |
|---|---|---|---|
| OSM BUILD_HOME 적재본 | `BUILD_HOME/data/osm`, `BUILD_HOME/sources/osm` | 디렉터리 자체 부재(MISSING) | **비치명** — 02/04는 repo `ROOT/data/osm`을 읽음. 단 repo와 BUILD_HOME 분리 호스트로 이관 시 경로 불일치 빌드 실패 위험 |
| facility_src/aed | `~/geocode-build/staged/facility_src/aed/` | 디렉터리 부재(MISSING) | **비치명** — E-Gen serviceKey 미보유로 자동수집 미배선, default=false. AED 라벨 필요 시에만 업로드 |
| facility 비기본 10종 | `staged/facility/<10종>` | 미배치 | **비치명** — default:false 선택수집. 기본 빌드 불필요 |

---

## 4. 완전성 리스크 (커버리지)

| 영역 | 태스크 표기 / 기대 | 실제 | 판정 |
|---|---|---|---|
| **시도 17** | navi build·jibun 각 17, boundary_legal 17, building 17, parcel 17시도 라우팅 | navi 17/17·17/17, legal 17/17, building 24(17커버), parcel 17/17 | ✅ 전 단계 17시도 완비 |
| **시군구 188** | 188 시군구 | parcel **257세트** (연속지적도 구단위 세분화로 정상 과잉, 188 초과) | ✅ 초과 = 정상 |
| **카테고리(localdata)** | 7대분류 | 건강13·기타16·동물18·문화53·생활26·식품32·자원환경37 = 195 CSV | ✅ 완비 (개수는 시기별 변동 가능, 절대기준 아님) |
| **카테고리(facility)** | 14종 | default 4종만 (비기본 10종 미배치) | ⚠️ 기본빌드는 4종으로 성립. 14종 완전성 목표 시 PARTIAL |
| **OSM pbf** | 남한 전역 1개 | repo/data/osm 268MB 1개(OSMHeader 정상) | ✅ — BUILD_HOME엔 부재(경로 함정) |
| **DEM** | 48 .hgt 타일 | 48/48 유효 30m SRTM, BUILD_HOME 외부(repo ROOT) | ✅ — BUILD_HOME만 이관 시 data/dem 누락 위험 |
| **admin_boundary** | 행정동/법정동 경계 적재 | 입력(legal 17·admin 135MB) 완비. **admin_boundary=0은 미적재 상태**(build_state.json에 load_postgis 키 자체 미기록)이지 입력 누락 아님 | ✅ 입력 완비 / 적재 미수행 |
| **POI(biz/facility) DB 반영** | geocode.sqlite kind=biz/facility 존재 | **0건** — addr 10.69M·poi 406k·road 165k·place 21.7k·station 1.37k만 존재 | 🔴 직전 geocode 빌드가 poi-all 미적재. 12-build-poi 즉시 실행 시 0 feature |
| **sangga 커버리지** | 17시도 상가 CSV | 손상 ZIP 1개(추정 멤버 '강원_202603') | 🔴 차단(섹션3 참조) |

> **areas 테이블 주의**: `geocode.sqlite`에는 `areas/area_rtree` 테이블이 **존재**(검증 확인). 비치명 관찰로 보고된 "areas 부재"는 `osm.sqlite` 한정(add_osm가 try/except로 경고 후 진행 → 비치명).

> **store 원본 수**: 프롬프트 171 대비 실제 285 SHA. 단 그중 sangga SHA(f6147232…)는 손상본.

---

## 5. 다음 단계 권고 (dry-run / 순차 빌드 전 보완)

### 필수 (Blocking 해소)
1. **sangga 재취득·재업로드** — data.go.kr 소상공인 상가정보 정상 파일을 `poi-all/sangga/`에 배치(또는 `sources/sangga/`에 업로드 후 prepare_sources 재스테이징). store의 손상 SHA 사본은 폐기. 업로드 후 `python3 -c "import zipfile; print(zipfile.is_zipfile(...))"` 또는 헤더 12컬럼(`상호명` 포함) 검증 통과 확인.
2. **geocode 재빌드(09) 선행** — sangga 교체 후 09-gen-geocode를 재실행하여 `geocode.sqlite`에 kind=biz/facility 적재. 그래야 12-build-poi가 0 feature가 아닌 정상 POI 타일을 생성. (현 DB는 POI 미반영 상태이므로 산출물 freshness를 의도적으로 무효화/재빌드 트리거 필요.)

### 권고 (무실패 안정성)
3. **경로 일원화 검증** — repo `ROOT/data/{osm,dem}`와 `BUILD_HOME`이 동일 호스트·동일 트리인지 확인. 폐쇄망/분리 이관 시 `data/osm/south-korea.osm.pbf`, `data/dem/hgt/*.hgt(48)`, `planetiler.jar`, `data/sources/*(보조 3종)`, `data/tile_weights.tsv.gz`를 동반 반입(이들은 `.gitignore` 제외 벤더/대용량).
4. **staged/gis 중복 사본 정리** — `(2)/(3)` 접미 7건(시도 41/44/46/47/48). PostGIS 경로는 ON CONFLICT로 방어되나 레거시 10-gen-buildings.sh는 dedup 없어 2~3배 중복 타일링. 정리 권장.
5. **staged/parcel 잔여물 정리** — `docProps/_rels/xl` (XLSX 오추출). load_parcel.sh는 `*.shp`만 잡아 무영향이나 위생 차원 정리.
6. **build_state 시그니처 재점검** — `building_db.staged_sig` 공란 → 다음 빌드 시 staged/gis rmtree 후 2.31GB zip 재추출(IO 비용). geocode/areas/load_postgis 키 미기록 → 전 단계 재빌드 트리거됨(의도된 from-scratch라면 정상).
7. **실빌드 시 SRS/필드 재확인** — areas 06의 EPSG:5186·EMD_NM/EMD_CD·ADM_NM/ADM_CD 하드코딩 가정을 `ogrinfo -so`로 .prj/필드 일치 확인(메타검증 범위 외).

### dry-run 진입 게이트(체크리스트)
- [ ] sangga 정상 파일 배치 + zip/CSV 무결성 검증 통과
- [ ] geocode 09 재빌드 → `geocode.sqlite` kind=biz/facility > 0 확인
- [ ] repo↔BUILD_HOME 경로 동일성 또는 data/osm·data/dem·planetiler.jar 동반 반입 확인
- [ ] (선택) staged/gis 중복본 정리, build_state 시그니처 정합
- [ ] 위 충족 시 → 실제 dry-run / 순차 빌드 진행 = **GO**

---

### 부록: 검증 방법 한계
모든 검증은 **읽기 전용 메타데이터·헤더·소표본·가벼운 단발 count** 기반으로 수행(백그라운드 pg_dump 보호를 위해 무거운 DB 질의·du 전체스캔 회피). sangga 손상 및 geocode POI 0건, store 원본 손상은 직접 재현 확인함. 셸 프로파일 `_safe_eval` 훅 노이즈는 python3/find/Read 우회로 무력화.

