# 후속작업 핸드오프 (geocode 복구 이후)

> 작성: 2026-06-24, 데이터 손실 복구 직후. 새 세션이 이 문서만 읽고 바로 작업을 이어갈 수 있도록 상세히 기록.
> 대상 작업: ① geom_pt 보강 ② POI 적재 ③ backfill 스크립트 개선 ④ 빌드 간편화 추천 ⑤ SQLite 폐기 가능 여부 ⑥ 데이터 보존(백업)
> 레포: `/Users/jaechango_cudo/Desktop_mac/maptiler` · 브랜치: `feature/postgis-refactor`

---

## ⚠️ 주의사항 (작업 전 반드시 읽기)

> 이번 복구에서 실제로 데이터를 잃거나 시간을 크게 버린 항목들. 새 세션은 작업 시작 전 이 섹션을 먼저 숙지할 것.

### 🔴 치명적 — 데이터 손실/서비스 중단 위험
1. **Docker Desktop 디스크 리사이즈 금지(직접편집).** `settings(-store).json`의 `diskSizeMiB`를 편집+재시작하면 가상화가 반복 크래시 → **Docker가 디스크 이미지를 리셋해 PostGIS 볼륨 전체가 날아간다(이번 손실의 직접 원인).** 공간이 필요하면 **반드시 백업 후 Docker Desktop GUI**로만. 새 디스크는 ~450GB라 당분간 리사이즈 불필요.
2. **작업 전 pg_dump 백업 필수(§6).** parcel 재적재(~1시간)+backfill(47분)을 두 번 다시 하지 않도록, 어떤 적재/대량 UPDATE 전에도 먼저 백업. 특히 **`load_geocode.py`는 `address`·`poi`를 TRUNCATE 후 전체 재적재**하므로(§2 POI 적재가 이걸 호출) 실행 전 백업 없으면 기존 address가 일시적으로 사라진다. 백업은 **호스트(Docker 볼륨 밖, iCloud 밖)**에 둘 것.
3. **대량 작업 동시 실행 금지.** backfill·load_geocode·geom_pt 보강을 **동시에 돌리지 말 것(순차 실행).** 과거 16-에이전트 워크플로우가 35M+ 행 풀스캔을 병렬로 돌려 **호스트 OOM(load 259, RAM ~10MB)**을 냈다. 마찬가지로 Workflow/fan-out으로 대형 테이블 풀스캔·집계 금지 — 인덱스 질의·동시성 cap만.
4. **work_mem/maintenance_work_mem 과다 설정 시 컨테이너 OOM.** postgis 컨테이너 메모리는 ~7.6GB/8GB. `work_mem`은 (커넥션 × 쿼리 노드)당 할당이라, 빌드 세션 1개에서만 2GB 정도로 상향하고 **끝나면 RESET**. 여러 무거운 쿼리 동시엔 합산되어 OOM.

### 🟡 빌드/적재 실수 방지
5. **zsh `_safe_eval` 래퍼 버그.** `&&` 체인·일부 글로브·복합 리다이렉트에서 `command not found: _safe_eval`로 명령이 통째 실패한다. → **`#!/bin/bash` 런처 스크립트 파일**로 실행하거나 python3로 우회. **psql은 변수에 담지 말고 직접 호출**(변수에 담으면 래퍼가 깨뜨림).
6. **psql은 기본 PATH에 없음.** `/opt/homebrew/opt/libpq/bin/psql` 절대경로 사용. docker는 `/opt/homebrew/bin`, cred helper는 `/Applications/Docker.app/Contents/Resources/bin` — 런처 PATH에 모두 추가.
7. **단일 트랜잭션 대량 UPDATE는 커밋 전 결과가 안 보인다.** UPDATE 도중 `count(*) filter(... is not null)`이 0으로 보이는 건 정상(미커밋). 중간에 죽이면 **전부 롤백**되어 처음부터. 진행 판단은 `pg_stat_activity.wait_event`(CPU/DataFileRead/Write) + 컨테이너 `df`로.
8. **EXPLAIN으로 plan 먼저 확인.** backfill 원본이 self-join+기본 work_mem으로 **Hash Join 12GB temp 스필(50배치)** → 89분 pathological이었다. 대량 쿼리는 실행 전 `EXPLAIN`으로 Hash/Sort temp 스필·Seq Scan 여부 확인.
9. **디스크는 컨테이너 내부 기준으로 본다.** `docker exec server-postgis-1 df -h /var/lib/postgresql/data`가 정확(호스트 `df`는 Docker VM 공유라 다름). 과거 이 차이로 disk-full을 못 알아챘다.
10. **geocode.sqlite 빌드 전 파일 상태 확인.** 09는 `.tmp`→`rename`(atomic). `geocode.sqlite`가 **빈 디렉터리**면 rename이 `IsADirectoryError`로 실패(이번에 겪음). 빌드 전 `geocode.sqlite`가 디렉터리가 아닌지 확인.
11. **int4 가드/캐스팅 제거 금지.** backfill의 `length((s.m)[1])<=7` 가드 빼면 비정상 12자리 jibun에서 `::int` 오버플로 → **UPDATE 전체 롤백**(과거 경북/충남 100% NULL 버그). 지번 질의의 `sido_cd=ANY(%s::char(2)[])`·`emd_cd=ANY(%s::char(8)[])` 캐스팅 빼면 파티션 pruning 깨져 전 파티션 Seq Scan.
12. **emd_cd 매칭 길이 주의.** `lawd_dong.emd_cd(8) = left(address.bcode,8) = left(parcel.pnu,8)`. substring 길이 틀리면 동명→emd_cd→parcel 조인이 0건. lawd_sigungu는 `left(bcode,5)`.
13. **parcel 인덱스 임의 삭제 금지.** `parcel_XX_sido_cd_pnu_key`(UNIQUE)는 증분 적재 ON CONFLICT의 arbiter라 중복제거 정확성에 직결. 공간 회수 목적으로 함부로 DROP 말 것.

### 🟢 SQLite/게이트웨이/검증
14. **SQLite 지오코더(geocode:8082)는 이미 미사용.** 게이트웨이 `/geocode`·`/reverse`는 **PostGIS(geocode-pg)로 전환 완료**(gateway-nginx.conf:58-60, Phase 5c). SQLite는 레거시/shadow → §5 따라 은퇴 가능. 단, **8082를 직접 호출하는 클라이언트가 없는지**(게이트웨이 경유면 안전) 확인 후 중지. SQLite 동시성 제약은 **쓰기**에만 있고 읽기 다중조회는 OK지만, 풀/병렬/복잡 공간질의·관측성은 PostGIS 우위 → PostGIS 단일화 권장.
15. **게이트웨이 포트는 80이 아니라 18080.** `.env`의 `GATEWAY_PORT=18080`. 데모 접속/헬스체크는 `http://127.0.0.1:18080/`(LAN은 `http://<서버IP>:18080/`). `:80` 가정 금지.
16. **재적재/대량작업 후 검증 필수.** 골든셋으로 회귀 확인 — 예: `매탄동 1-11 → (127.0581, 37.2711)`, `세종대로 110 → 서울시청`, `강남대로 396 → 강남역`. + `EXPLAIN`으로 인덱스 사용 확인 + 시도별 행 분포. POI 적재 후엔 `select count(*) from poi > 0`, lawd_dong/lawd_sigungu 수치 불변 확인.
17. **산출물/소스를 iCloud 폴더에 두지 말 것.** GB급 파일은 eviction·느린 I/O 유발. 현재 `~/geocode-build`·레포는 전부 로컬(확인됨). 백업도 로컬에.

---

## 0. 지금 상태 (복구 완료 스냅샷)

**전제 사건**: Docker Desktop 디스크 리사이즈(diskSizeMiB 직접편집+재시작) → 가상화 반복 크래시 → **디스크 이미지 리셋 = PostGIS 볼륨 전체 초기화(데이터 손실)**. 호스트의 재구축 소스(staged/*, geocode.sqlite)는 무사해서 전량 재적재로 복원함. **교훈: Docker 디스크 리사이즈는 GUI로만, 반드시 백업 후. (아래 §6 백업 필수)**

### 데이터 레이어 (PostGIS `cuvia` DB, localhost:5433 / 컨테이너 5432)
| 테이블 | 행수 | 비고 |
|------|------|------|
| `parcel` (연속지적, 지번) | 39,882,449 | 시도 LIST 파티션(parcel_11~52+default). GiST·pnu·`parcel_jibun_lookup(emd_cd,ji_main,ji_sub)` 인덱스 완비 |
| `parcel.ji_main/ji_sub/san` | 39,882,339 채움 | 지번 검색키. **`geom_pt`는 전부 NULL**(경량 backfill이 생략 → §1 보강 대상) |
| `address` (도로명+이름) | 11,281,251 | addr 10,686,547 + poi(OSM) 406,064 + road 165,601 + place 21,668 + station 1,371. geom 100% 채움. trgm GIN(search_text,bld)·road_addr·geom GiST 인덱스 완비 |
| `lawd_dong` | 5,046 | 법정동명→emd_cd(8자리). address.bcode에서 `left(bcode,8)`로 재생성 |
| `lawd_sigungu` | 254 | 시군구코드(5)→명. address에서 `left(bcode,5)`로 재생성 |
| `poi` (martin POI 타일용) | **0** | biz/facility 미적재 → §2 적재 대상 |

### 컨테이너 (compose: `server/docker-compose.yml`, `--profile postgis`)
모두 Up: `postgis`(5433) · `geocode-pg`(8092, **PostGIS 지오코더 = 현역**) · `martin`(MVT동적타일) · `tileserver`(8080, 베이스타일) · `gateway`(**:18080**) · `demo` · `geocode`(8082, **SQLite 지오코더 = 레거시/미사용** → §5).

### 데모서버 (가동 중)
- 게이트웨이 통합: **http://127.0.0.1:18080/** (홈/가이드/타일/`/geocode`·`/reverse` 프록시). `.env`에 `GATEWAY_PORT=18080`.
- 타일서버 직접: http://127.0.0.1:8080/ (frontPage=demo/guide.html)
- 검증됨: `/`200, `/demo/guide.html`200, `/styles/cuvia/style.json`200, `/geocode?q=매탄동 1-11`→정상.
- **게이트웨이 `/geocode`·`/reverse` 업스트림 = `geocode-pg:8082`(PostGIS)** (gateway-nginx.conf:58-60, Phase 5c 전환 완료).

### 접속/경로 상수
- PG: `PGHOST=localhost PGPORT=5433 PGUSER=cuvia PGDATABASE=cuvia PGPASSWORD=cuvia`
- 호스트 psql: `/opt/homebrew/opt/libpq/bin/psql` (기본 PATH에 없음 → 명시 필요)
- ogr2ogr/docker: `/opt/homebrew/bin` · docker cred helper: `/Applications/Docker.app/Contents/Resources/bin`
- 빌드홈: `~/geocode-build` (= `/Users/jaechango_cudo/geocode-build`). 산출물 `geocode.sqlite`(3.9GB, addr+OSM), staged/(parcel·navi·gis·localdata), poi-all/(정제 CSV).
- ⚠️ **zsh 래퍼 버그**: `&&` 체인·일부 글로브·복합 리다이렉트에서 `_safe_eval: command not found` 발생 → **bash 런처 스크립트 파일**로 실행하거나 python3로 우회. psql은 변수에 담지 말고 직접 호출.

---

## 1. geom_pt 대표점 보강 (비차단)

**무엇**: `parcel.geom_pt = ST_PointOnSurface(geom)` 39,882,449건 전부 NULL → 채우기. 지번 검색 결과 좌표를 미리 굳혀 질의 속도↑, 지도 마커가 항상 필지 내부.
**왜 비차단**: geocode-api-pg.py가 `COALESCE(geom_pt, ST_PointOnSurface(geom))`로 **질의 시점 폴백**하므로, 안 채워도 검색은 동작(단건은 충분히 빠름). 그래서 후순위.

**권장 실행 (인덱스 영향 없음 — geom_pt엔 인덱스 없음, self-join 불필요)**:
```sql
SET work_mem='2GB';            -- 정렬/해시 여유 (이 쿼리는 단순 seq scan이라 큰 의미는 적음)
UPDATE parcel SET geom_pt = ST_PointOnSurface(geom) WHERE geom_pt IS NULL;
ANALYZE parcel;
```
- 비용: 39.9M 폴리곤 ST_PointOnSurface(단일코어, 복잡 폴리곤은 ~100µs/건) + **각 행 tuple rewrite(큰 geom 포함)** → macOS Docker I/O에서 **30~70분** 예상. CPU/디스크 바운드, 정체 아님.
- ⚠️ 단일 트랜잭션이라 커밋 전 geom_pt NULL로 보임(정상). 백그라운드 bash 런처로 돌리고 5분 모니터링 권장.
- **더 빠르게**(선택): 시도 파티션별로 쪼개 17개 UPDATE를 순차/소수 병렬 → 커밋이 점진적이라 관찰·재개 쉬움. 예: `UPDATE parcel_46 SET geom_pt=ST_PointOnSurface(geom) WHERE geom_pt IS NULL;` …
- 보강 후 점-기반 근접질의 원하면 `CREATE INDEX parcel_geompt_gix ON parcel USING gist(geom_pt);`(선택).

---

## 2. POI 적재 (biz/facility) — 현재 `poi`=0, address에 OSM POI만

**무엇**: 소상공인 상가/인허가(localdata)/생활편의(facility)를 `address`(검색)·`poi`(martin 타일)에 적재 → 상호명·업종 검색 + POI 타일.
**소스 (정제 CSV, 호스트에 있음)**: `~/geocode-build/poi-all/`
- `localdata_clean.csv` (564MB) → biz(인허가)
- `facility_clean.csv` (19MB) → facility(생활편의)
- `sangga/*.csv` (248MB) → biz(상가)
- (09-gen-geocode.py `add_biz`가 basename으로 구분: `facility_clean.csv`→facility, `localdata_clean.csv`→localdata(biz), 그 외→sangga(biz))

**권장 경로 = 정식 파이프라인 재실행 (geocode.sqlite 재빌드 → 재적재)**:
```bash
# 1) geocode.sqlite를 addr+OSM+biz+facility로 재빌드 (~5-10분, 순수 로컬 python)
python3 scripts/09-gen-geocode.py \
  --src ~/geocode-build/staged/navi \
  --osm ~/geocode-build/osm.sqlite \
  --out ~/geocode-build/geocode.sqlite \
  --poi-csv-dir ~/geocode-build/poi-all          # ★ 이 옵션이 biz/facility 포함
# 2) PostGIS 재적재 (address+poi TRUNCATE 후 벌크 — ~15-25분, trgm GIN 포함)
PATH=/opt/homebrew/opt/libpq/bin:$PATH PGPASSWORD=cuvia GEOCODE_MAINT_MEM=1GB \
  python3 scripts/postgis/load_geocode.py --db ~/geocode-build/geocode.sqlite
```
- ⚠️ `load_geocode.py`는 **address·poi를 TRUNCATE 후 전체 재적재**한다. addr/도로명도 같이 다시 들어가므로(동일 소스라 결과 동일) **lawd_dong/lawd_sigungu는 그대로 유효**(address bcode 기반이라 변동 없음). 재적재 후 §0의 데이터 수치 + biz/facility만 증가.
- 재적재 후 검증: `select kind,count(*) from address group by kind` 에 biz/facility 등장, `select count(*) from poi` > 0.
- ⚠️ 빌드 전 **반드시 백업**(§6) — load_geocode가 TRUNCATE하므로.
- 대안(부분 적재): biz만 별도 테이블/INSERT로 넣을 수도 있으나, 정식 파이프라인이 dedup(is_primary)·카테고리 표준화(cat-crosswalk)까지 처리하므로 위 경로 권장.

---

## 3. backfill 스크립트 개선 (`scripts/postgis/backfill_parcel_jibun.sql`)

**문제 (이번에 89분 걸려 취소함)**: 원본 쿼리가 ① `UPDATE parcel p FROM (SELECT … FROM parcel) s` **self-join** + 기본 work_mem(32MB) → Hash Join이 **12GB temp 스필 + 50배치 디스크 스래싱**, ② `geom_pt=ST_PointOnSurface` **무거운 기하연산** 동시 수행, ③ ji_main/ji_sub가 `parcel_jibun_lookup` 인덱스에 포함 → **39.9M 인덱스 갱신**까지. → 합쳐서 pathological.

**개선안 (다음 빌드부터 적용 권장)**:
1. `SET work_mem='2GB';` 추가 → self-join 해시 인메모리화(temp 스필 0, 단일 패스). EXPLAIN으로 Hash Cond·temp 확인.
2. **geom_pt를 backfill에서 분리** → ji_main/ji_sub/san만 먼저(검색키, 빠름), geom_pt는 §1로 후순위. (이번 복구의 경량판이 이 방식, 47분에 완료)
3. **인덱스 DROP→UPDATE→CREATE INDEX 재생성**: `DROP INDEX parcel_jibun_lookup;` → UPDATE → `CREATE INDEX … ON parcel(emd_cd,ji_main,ji_sub);`. 39.9M 인덱스 증분 갱신보다 일괄 빌드가 빠름.
4. (선택) 시도 파티션별 UPDATE로 점진 커밋.

→ 결과적으로 `backfill_parcel_jibun.sql`을 (a)work_mem 상향 (b)geom_pt 제거(또는 별도 파일 `backfill_geom_pt.sql`) (c)인덱스 drop/recreate 래핑으로 리팩터. **이걸 PR로 정리하면 향후 전국 빌드가 89분→~30분.**

---

## 4. 빌드 간편화 + 개선 추천 (사용자 검토 요청 항목)

### 4-1. "한 방 빌드/복구" 오케스트레이터
지금 복구는 수동으로 단계(스택→스키마→parcel→backfill→address→사전→검증)를 이어붙였다. 이미 `scripts/postgis/load-all.sh`(schema·admin·parcel·building·geocode·facility)가 있으나 **지번 backfill·lawd_dong·lawd_sigungu가 빠져 있음**. 추천:
- `load-all.sh`에 단계 추가: parcel 직후 `backfill`(개선판), geocode 직후 `lawd_dong`/`lawd_sigungu` 채우기를 멱등 SQL로 통합. `STEPS=` 로 선택 가능하게.
- 또는 새 `scripts/postgis/recover.sh` — "빈 PostGIS → 풀 서비스"를 한 번에(이번 복구 절차를 코드화). 각 단계 멱등+로그+검증 쿼리.

### 4-2. build-studio와의 경계 정리
- `build-studio`(build_state.json: osm_vector/osm_sqlite/dong/terrain/localdata/facility)는 **타일·geocode 소스 빌드** 담당. **PostGIS 적재(parcel/address)는 별개 트랙**(scripts/postgis/). 두 트랙의 역할과 산출물(타일=호스트 tiles/, geocode.sqlite=호스트, PostGIS=Docker볼륨)을 README에 한 장으로 정리하면 다음 사람이 안 헤맴.

### 4-3. 빌드 가속/안정 팁 (이번에 체득)
- `work_mem`/`maintenance_work_mem`는 대량 UPDATE/인덱스의 핵심 다이얼. compose의 postgis 설정값(shared_buffers 1GB 등)은 8GB 컨테이너 기준 보수적 — 빌드 시에만 세션단위로 상향.
- 대량 UPDATE는 **인덱스 DROP→작업→재생성**, geom 같은 큰 컬럼 파생은 **분리**.
- 진행 모니터링은 `pg_stat_activity`의 `wait_event`(CPU/DataFileRead/Write/temp 스필) + 컨테이너 `df`로 판단. EXPLAIN으로 plan 먼저 확인(Hash Join temp 스필 조기 발견).
- bash 런처 파일로 백그라운드 실행(zsh 래퍼 버그 회피) + append 로그.

### 4-4. ⚠️ 절대 하지 말 것
- **Docker Desktop diskSizeMiB 직접편집+재시작 금지**(이번 데이터 손실 원인). 공간 필요 시 GUI 리사이즈 + 사전 백업. 새 디스크는 현재 ~450GB라 한동안 공간 걱정 없음.

---

## 5. SQLite 지오코더 폐기 가능 여부 + 동시호출 이슈 (사용자 질문)

### 결론: **지금 폐기(은퇴) 가능.** 게이트웨이는 이미 PostGIS로 전환됨.
- **현재 트래픽 경로**: gateway `/geocode`·`/reverse` → **`geocode-pg:8082`(PostGIS)** 로 프록시(gateway-nginx.conf:58-60, Phase 5c 완료). SQLite 컨테이너(`geocode`:8082→호스트8082, geocode/geocode.sqlite 670k POI)는 **게이트웨이가 더 이상 호출하지 않음**(레거시/shadow).
- **PostGIS가 기능 우위**: 지번(parcel 39.9M)·도로명(address 10.7M)·이름검색(road/place/station/poi 594k)·역지오코딩 모두 보유. SQLite(670k)는 POI 위주 구버전.

### "SQLite는 동시호출 문제 많다" — 정확한 사실관계
- SQLite의 동시성 제약은 **쓰기(write)**에 있다: 한 번에 한 writer(파일 락). 지오코딩은 **읽기 전용**이라 **다중 reader 동시 조회는 문제 없음**(특히 WAL/read-only 모드). 그래서 "검색 서버"로서 SQLite 자체가 치명적이진 않다.
- 다만 한계: 커넥션 풀/병렬성·복잡 공간질의(GiST KNN, ST_DWithin 등)·대용량 인덱스(trgm GIN)·운영 관측성은 **PostGIS가 압도**. 동시 트래픽이 늘수록(프로세스/스레드 모델, 파일락 경합, mmap 한계) PostGIS가 안전.
- **권장**: PostGIS 단일화. SQLite 지오코더는 (a) 폐기하거나 (b) 폐쇄망/단일파일 폴백용으로만 유지. 폐기 시:
  ```bash
  # 게이트웨이는 이미 PostGIS라 무중단. compose에서 geocode(sqlite) 서비스 비활성/중지.
  cd server && docker compose stop geocode    # 컨테이너만 중지(이미지/데이터 보존)
  # 영구 제거하려면 docker-compose.yml의 geocode 서비스 + gateway depends_on에서 제거 후 commit.
  ```
  - ⚠️ 단, demo/guide.html이나 다른 클라이언트가 8082를 직접 호출하지 않는지 확인 후 중지(게이트웨이 경유면 안전).

---

## 6. 데이터 보존 = 백업 (★최우선 추천 — "데이터 버리지 말고 잘 쓰자")

이번 손실의 핵심 교훈: **PostGIS 적재 결과(parcel 1시간+ + backfill 47분 + address)를 매번 재적재하면 너무 비싸다.** 한 번 만든 걸 **pg_dump로 호스트에 백업**해두면 다음엔 분 단위 복원.

### 권장: 적재 완료 시점마다 압축 덤프
```bash
# 전체 DB 덤프(압축) — 호스트(Docker볼륨 밖, iCloud 밖)에 저장
PGPASSWORD=cuvia /opt/homebrew/opt/libpq/bin/pg_dump -h localhost -p 5433 -U cuvia -d cuvia \
  -Fc -Z6 -f ~/geocode-build/backups/cuvia_$(YYYYMMDD).dump      # 날짜는 셸에서 치환
# 특정 큰 테이블만(예: parcel) 따로 떠두면 부분 복원 빠름
#   pg_dump … -Fc -t parcel -f ~/geocode-build/backups/parcel.dump
# 복원
#   pg_restore -h localhost -p 5433 -U cuvia -d cuvia --clean --if-exists ~/.../cuvia_YYYYMMDD.dump
```
- parcel(geom 포함)은 덤프가 크다(수~십 GB) → 압축(-Z6)·NVMe 권장. 시간 없으면 최소 **parcel + lawd_* 만이라도** 덤프(backfill 결과까지 보존).
- 호스트 디스크는 현재 ~450GB 여유라 백업 보관 여력 충분.
- (대안) Docker 볼륨 자체를 tar로: `docker run --rm -v server_pgdata:/v -v ~/geocode-build/backups:/b alpine tar czf /b/pgdata.tgz -C /v .` — 단 DB 정지 후가 안전.
- **자동화 추천**: load-all/recover 스크립트 끝에 pg_dump 한 줄 추가 → 빌드할 때마다 자동 백업.

### 재사용 가능한 산출물(버리지 말 것)
- `~/geocode-build/geocode.sqlite` (addr+OSM, 3.9GB) — address 재적재 즉시 가능(이번에 이걸로 복원).
- `~/geocode-build/staged/*` (parcel SHP 18.7G, navi 4G, gis 28.7G 등) — 모든 PostGIS 적재의 원천.
- `~/geocode-build/poi-all/*` — POI 적재 소스(§2).
- `~/geocode-build/osm.sqlite`, `tiles/*.mbtiles` — 타일/이름검색 소스.

---

## 7. 빠른 시작 (새 세션 체크리스트)

```bash
# 0) 헬스 확인
curl -s http://127.0.0.1:8092/health           # {"ok":true,"places":11281251}
curl -s "http://127.0.0.1:18080/geocode?q=매탄동%201-11&limit=1"

# 1) 백업부터! (§6) — 작업 전 현재 복구분 보존
mkdir -p ~/geocode-build/backups
PGPASSWORD=cuvia /opt/homebrew/opt/libpq/bin/pg_dump -h localhost -p 5433 -U cuvia -d cuvia -Fc -Z6 -t parcel -t lawd_dong -t lawd_sigungu -f ~/geocode-build/backups/parcel_dicts.dump

# 2) geom_pt 보강 (§1) — 백그라운드 bash 런처 권장
# 3) POI 적재 (§2) — 09 재빌드 --poi-csv-dir → load_geocode (TRUNCATE 주의, 백업 후)
# 4) backfill 스크립트 개선 커밋 (§3)
# 5) SQLite 지오코더 은퇴 검토 (§5)
```

### 작업 순서 추천
1. **백업**(§6) — 최우선, 두 번 다시 1시간+ 재적재 안 하도록.
2. **POI 적재**(§2) — 검색 기능 확장(상호명). load_geocode가 TRUNCATE하므로 백업 직후 실행, 끝나면 lawd_dong/lawd_sigungu 재확인(변동 없음).
3. **geom_pt 보강**(§1) — 비차단, 길어도 검색 영향 없음. POI 적재와 동시 실행은 DB 부하/메모리 경합 주의(순차 권장).
4. **backfill 스크립트 개선**(§3) + **load-all 오케스트레이터 통합**(§4-1) 코드화 → PR.
5. **SQLite 은퇴**(§5) — 트래픽 영향 없음 확인 후.

---

## 8. 핵심 메모리(이미 저장됨, 참고)
- `docker-desktop-disk-resize-danger` — 디스크 리사이즈가 데이터 손실 유발. GUI+백업만.
- `parcel-backfill-update-optimization` — backfill 89→47분: work_mem 상향, geom_pt 분리, 인덱스 drop/recreate.
- `workflow-concurrency-large-db` — 대형 DB에 fan-out 워크플로우 풀스캔 OOM. 인덱스 질의·동시성 제한.
