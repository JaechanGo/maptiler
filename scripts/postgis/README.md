# PostGIS 적재 (CUVIA 하이브리드 — 빌드호스트 런북)

동적·운영 레이어를 PostGIS 로 적재한다. 베이스 지도(korea.mbtiles)·지형은 기존 Planetiler 경로 유지.
전체 설계: `~/.claude/plans/mellow-riding-stream.md`. 스키마: `schema/*.sql`.

## 0. 사전 준비 (빌드호스트)
```bash
scripts/setup-build-host.sh                 # psql·osm2pgsql·ogr2ogr·GDAL 등
cd server && docker compose --profile postgis up -d postgis martin   # PostGIS+martin 기동
```
연결은 libpq 환경변수. 기본 `cuvia/cuvia@localhost:5433`(맵 전용 postgis 컨테이너 호스트포트 — 빌드호스트의 기존 host PostgreSQL 5432와 충돌 회피). 다른 포트면 `export PGPORT=...`, 비번 변경 시 `export PGPASSWORD=...`. 호스트 psql 없으면 apply-schema 가 컨테이너 psql 자동 사용.

### PostgreSQL 메모리 튜닝 (대형 빌드호스트)
compose 의 postgis 는 **소형 머신(≈8GB)에서도 안전한 보수적 기본값**(shared_buffers 1GB 등)으로 뜬다. 순정 postgres 기본값(shared_buffers 128MB·maintenance 64MB·work_mem 4MB)보다는 크게 낫지만, **대형 빌드호스트(예: 128GB RAM)에서는 하드웨어를 못 살린다** — 특히 전국 필지(≈39.6M) GiST 인덱스 빌드·공간조인이 메모리 부족으로 디스크 스필. `server/.env` 또는 환경변수로 상향 후 `up -d`(컨테이너 재생성) 하면 적용:
```bash
# server/.env — 128GB RAM 빌드호스트 예시 (RAM 비례 스케일)
PG_SHARED_BUFFERS=16GB     # 캐시·쓰기버퍼 (RAM ~12%; 32GB까지 가능)
PG_EFFECTIVE_CACHE=96GB    # 플래너 힌트 (RAM ~75%, OS page cache 포함 추정)
PG_MAINT_MEM=8GB           # 인덱스 빌드 가속 (GiST/btree)
PG_WORK_MEM=128MB          # 공간조인/정렬 인메모리
PG_MAX_WAL=16GB            # 벌크적재 중 체크포인트 빈도↓
PG_MIN_WAL=2GB
PG_SHM=8g                  # 병렬 워커 공유메모리(shm_size)
PG_PARALLEL=8              # max_parallel_workers_per_gather
PG_PARALLEL_MAINT=6        # 병렬 인덱스 빌드
```
벌크적재 시엔 추가로 `PARCEL_JOBS=16 PARCEL_MAINT_MEM=8GB scripts/postgis/load_parcel.sh --shp <폴더> --fresh`
(코어수만큼 워커↑·인덱스 일괄재생성 시 maintenance_work_mem↑). 적재 세션은 load_parcel.sh 가 `synchronous_commit=off`
로 자동 가속한다(서빙 내구성에 영향 없음 — 세션 한정). `fsync` 는 영속 끄지 말 것(서빙 DB 손상 위험; 재적재 가능한
일회성 벌크에 한해 임시로만).

## 1. 스키마 적용 (멱등)
```bash
scripts/postgis/apply-schema.sh
```
10 테이블 + parcel/building 시도 LIST 파티션(17+default) + GiST + SRID 4326.

## 2. 적재 — 한 번에
```bash
scripts/postgis/load-all.sh                 # 소스가 있는 단계만 자동 실행
STEPS="parcel building" scripts/postgis/load-all.sh   # 특정 단계만
```
`BUILD_HOME`(기본 `~/geocode-build`) 기준 기본 경로:
| 단계 | 소스 경로 | 비고 |
|---|---|---|
| admin (법정동) | `sources/boundary/legal/*.shp` | EMD_NM/EMD_CD, EPSG:5186 |
| admin (행정동) | `sources/boundary/admin/BND_ADM_DONG_PG.shp` | ADM_NM/ADM_CD |
| parcel | `staged/parcel/**/*.shp` | VWorld 연속지적, PNU/JIBUN |
| building | `staged/gis/**/*.shp` | VWorld 건물통합, A16/A26, 파일명 `*_D010_<시도>_*` |
| geocode | `geocode.sqlite` | **09-gen-geocode.py 산출** → address+poi |
| facility | `staged/facility_src/<kind>.csv` | kind=police/fire_station/aed/shelter/hospital |

## 3. 적재 — 개별 (필드명 다르면 override)
```bash
# 행정구역
scripts/postgis/load_admin.sh --shp <폴더|파일> --level emd --srs EPSG:5186 --name-field EMD_NM --code-field EMD_CD
# 연속지적 (시도 파티션, --fresh=전체 재적재)
scripts/postgis/load_parcel.sh --shp ~/geocode-build/staged/parcel --fresh
# 건물 (파일명에서 시도 추출; 안되면 --sido 11)
scripts/postgis/load_building.sh --shp ~/geocode-build/staged/gis --fresh
# 주소+POI (navi 재파싱 없이 geocode.sqlite 재사용)
scripts/postgis/load_geocode.py --db ~/geocode-build/geocode.sqlite
# 공공시설 (휴리스틱 컬럼감지; 실패 시 --name-col/--addr-col/--lat-col/--lon-col)
scripts/postgis/load_facility.py --kind police --csv police.csv --source data.go.kr:15077036
```

> **필드명 확인**: SHP 필드는 출처마다 다르다. 적재 전 `ogrinfo -so <shp>` 로 PNU/JIBUN/A16/A26/EMD_NM 등 실제 컬럼명을 확인하고, 다르면 위 `--*-field` 로 지정.

## 4. 시도별 증분 갱신 (파티션 이점)
VWorld 는 시도/시군구 단위 배포 → 모놀리식 안 건드리고 해당 파티션만:
```bash
psql -c 'TRUNCATE parcel_11;'                                  # 서울 필지만 비우고
scripts/postgis/load_parcel.sh --shp <서울 폴더>               # 다시 적재(--fresh 없이)
```

## 5. 검증
```bash
# 적재량·파티션 분포
psql -c "SELECT sido_cd, count(*) FROM parcel GROUP BY sido_cd ORDER BY 1;"
# 시도필터 partition pruning (parcel_11 만 스캔되어야)
psql -c "EXPLAIN (COSTS OFF) SELECT count(*) FROM parcel WHERE sido_cd='11';"
# martin 자동발견
curl -s http://localhost:3000/catalog | python3 -m json.tool   # martin 컨테이너 내부 포트
# 지오메트리 유효성
psql -c "SELECT count(*) FILTER (WHERE NOT ST_IsValid(geom)) AS invalid FROM building;"
```

다음: Phase 3(martin config 정리 + style /dyn 배선) — `public` 스키마·부모 테이블만 노출하도록 martin 설정.
