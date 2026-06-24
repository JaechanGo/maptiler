<!-- 생성: geocode-review-and-improvements 워크플로우 (9 agents, ~678k tok) · 2026-06-24 · 읽기전용 적대검토 -->

# 지오코딩 플랫폼 검토·기능개선 종합 보고서

작성: 테크리드 | 기준일: 2026-06-24 | 현행 브랜치: `feature/geocode-followup`

> 본 보고서는 4개 영역 적대 검토결과(지번/도로명 설계↔코드, 복구 핸드오프↔현실, API↔문서, 빌드 파이프라인 정합)와 4개 차원 기능개선 후보(검색품질·랭킹, 응답확장, 커버리지·데이터, 성능·운영·API)를 종합한다.

---

## (A) 문서 검토 요약 — 영역별 발견·드리프트·구식주장

### A-0. 영역 횡단 최우선(시스템 레벨 HIGH)

여러 검토자가 **독립적으로 같은 근본 결함을 지목**했다. 교차확증된 항목은 신뢰도가 높고 즉시 조치 대상이다.

| # | 횡단 결함 | 교차 확인한 검토영역 | 시스템 영향 |
|---|----------|----------------------|-------------|
| X-1 | **`lawd_dong` 채움 스크립트 부재** (빈 테이블만 CREATE) | 지번설계↔코드, 빌드파이프라인 | 빈 DB 재빌드 시 **지번 검색 전체 0건**. 진입점이 없음 |
| X-2 | **`backfill_parcel_jibun.sql` 고아 스크립트** (어떤 오케스트레이터도 미호출) | 핸드오프, 빌드파이프라인 | 재적재 시 `ji_main/ji_sub/san/geom_pt` 전부 NULL → 지번 정확매칭 0건 |
| X-3 | **`geom_pt` 100% NULL** + 커밋된 backfill은 오히려 geom_pt를 채움(문서 '경량판 생략'과 모순) | 지번설계, 핸드오프, 빌드 | 설계 성능근거 미실현 + 경량판 미커밋(재현 불가) |
| X-4 | **브랜치명 구식** (`feature/postgis-refactor` → 실제 `feature/geocode-followup`) | 4개 영역 전부 | 새 세션 오작업 위험 (LOW지만 만장일치) |

### A-1. 지번/도로명 설계문서 ↔ 코드

| 심각도 | 발견 | 근거(요지) | 권고 |
|--------|------|-----------|------|
| **HIGH** | `lawd_dong` populate 코드 전무 | `21-parcel-jibun.sql:16-22` 빈 CREATE만. 전 레포 INSERT 0건. `geocode-api-pg.py:164`가 0행이면 cds=[] → 지번경로 통째 0건 | `lawd_sigungu`(build_sigungu_dict.sh)와 대칭으로 `build_dong_dict.sh` 추가, `load-all.sh` geocode 단계 뒤 호출. apply-schema 직후 `lawd_dong=0` 검증 쿼리 |
| **MED** | 설계 핵심주장 '지번을 이름보다 앞 승격'이 부분만 반영 | `plan:12,57` ↔ `geocode-api-pg.py:162` 지번은 여전히 `if not results` 폴백 게이트(도로명 뒤) | 문서를 현 구조(도로명→지번→이름)로 정정, road/jibun 동시입력 우선순위 명시 |
| **MED** | '전면 폐기'했다는 `search_text ILIKE` 인픽스 경로 잔존(죽은/중복 경로) | `plan:45-46` ↔ `geocode-api-pg.py:213-234`. lawd_dong 비면 **이 경로가 항상 주경로로 승격** → 2자 동명 Seq Scan 위험 재도입 | 코드 제거 또는 '안전망 잔존' 명시. lawd_dong 미적재 시 위험을 문서/코드 동기화 |
| **MED** | 프론트 자동완성이 `r.type`으로 종류 라벨 읽음 → API는 `kind`만 반환 | `search.js:9,42` ↔ API `kind`(`:156,204,233,259,296`). `TYPE_KO`에 `addr` 키 누락 | `search.js:42`를 `r.kind`로 수정, `TYPE_KO`에 `addr:'주소'` 추가 |
| **LOW** | `09-gen-geocode.py:84` int() 오버플로 가드 없음(백필SQL·API는 가드 보유) | backfill `length<=7`, API `a<=99999` 가드 ↔ 09는 무가드 | 09에도 자릿수 가드/try-except. 3개 파싱지점(빌드09·백필SQL·런타임API) 가드정책 단일 문서화 |
| **LOW** | parcel 행수 3종 불일치 (39.6M / 34.9M·39,536,898 / 39,882,449) | plan·요약(내부모순)·handoff·코드주석 전부 다름 | 행수를 문서에 박지 말고 '검증쿼리 결과 참조'로 대체 |
| **LOW** | `geom_pt` 사전계산 성능근거 미실현(런타임 폴백만 동작) | `plan:31,73` ↔ `handoff:46`, `geocode-api-pg.py:193-194` COALESCE | plan §2.5에 '런타임 폴백으로 동작 중' 단서, handoff §1과 교차참조 |
| **LOW** | 파티션 pruning의 char 캐스팅 의존이 정본(plan)엔 없고 주석/요약에만 | `plan §2.4`엔 캐스팅 없음 ↔ 필수성은 `geocode-api-pg.py:188-189` 주석에만 | plan에 'char(2)/char(8) 컬럼은 text[] ANY 시 명시 캐스팅 필수'를 설계제약으로 승격 |

### A-2. 복구 핸드오프 ↔ 현실

| 심각도 | 발견 | 근거(요지) | 권고 |
|--------|------|-----------|------|
| **HIGH** | §3 backfill 개선(work_mem 2GB/geom_pt분리/인덱스 drop·recreate) **미적용** + 커밋파일이 geom_pt를 채워 §0·§1과 정면충돌. 경량판 미커밋 | `backfill_parcel_jibun.sql:5,8-19,12`(self-join+인라인 geom_pt+maintenance_work_mem 512MB) ↔ handoff §3:120-126 | 커밋판을 §3 개선판으로 리팩터하거나 '원본=89분 경로' 명시. geom_pt를 `backfill_geom_pt.sql`로 분리, 경량판 커밋. work_mem vs maintenance_work_mem 다이얼 명칭 교정 |
| **HIGH** | `admin_boundary=0, building=0` 누락 → **모든 /reverse areas가 항상 []**(실측). MEMORY/프롬프트가 areas를 현역으로 표기 | psql 실측, `geocode-api-pg.py:302` | §0 스냅샷에 두 테이블 0행 추가, 'areas 역지오코딩·필지↔건물 조인 미동작' 한계 명시. §16 검증에 회귀항목 추가 |
| **HIGH** | TRUNCATE 백업가드 없음 + `~/geocode-build/backups` 디렉터리 자체 부재 | `load_geocode.py:59-60`(TRUNCATE), fscheck No such file | load_geocode/load-all 진입부에 '최근 덤프 없으면 TRUNCATE 거부 또는 자동 pg_dump 선실행' 가드를 **권고→강제 승격** |
| **MED** | guide.html /health·/reverse 예시 구식 (places:156234/areas:4382 ↔ 실측 11281251/0, areas[] 채워진 예시 ↔ 실측 []) | `guide.html:399,382-396` | 수치 현행화, areas 빈배열 명시. 표기/응답스펙 워크플로우와 연계 |
| **MED** | '동시 대량작업 금지'가 문서 권고뿐(코드 락 없음) → load 259 OOM 재발 여지 | handoff 주의#3 서술적 권고, 코드 가드 부재 | 대량작업 스크립트에 `pg_advisory_lock`/lockfile 단일 게이트 추가 |
| **LOW** | sangga CSV가 문서의 '시도별'과 달리 해시명 단일 237MB 1개(동작무해) | `poi-all/sangga/f6147...csv` ↔ `handoff §2:94` | §2 설명을 '단일 통합 CSV, basename 분류'로 정정 |
| **LOW** | gateway `depends_on:[geocode]`(SQLite) 잔존 → stop만으론 compose up시 부활 | `docker-compose.yml:180-183` ↔ §5는 stop 위주 | §5에 'compose의 gateway.depends_on에서 geocode 제거가 완전은퇴 필수' 명시 |

### A-3. API 구현 ↔ 문서(guide.html)

| 심각도 | 발견 | 근거(요지) | 권고 |
|--------|------|-----------|------|
| **HIGH** | 예외핸들링 갭 — `OperationalError`만 catch. ProgrammingError/DataError/RuntimeError 등은 uncaught → `_send` 전 예외로 **무응답/소켓 끊김**. 에러응답 스키마 문서 전무 | `geocode-api-pg.py:341` | try를 `except psycopg.Error`/`Exception`으로 확장, 500 JSON 직렬화, finally로 `_send` 보장. guide에 400/404/500/503 에러스키마 표 추가 |
| **HIGH** | 지번(parcel) 경로 address가 문서스키마와 심각 불일치 — `parcel`·`structure.emd`·`b_code`만 채우고 road/zipcode/bld/structure 전체 누락 | `geocode-api-pg.py:204-207` ↔ `guide.html:312-326` '주소 결과는 자기 주소' 단언 | 문서에 parcel 출처 필드 한정 명시 또는 구현에서 키를 null로라도 항상 채움. 표기/분해 패리티 스펙과 연계 |
| **MED** | `areas[].type` 드리프트 — 구현은 영문 enum(sido/sigungu/emd/adm_dong) 패스스루 ↔ 문서는 한글('법정동'/'행정동') | `geocode-api-pg.py:302-304`, `10-base.sql:8` ↔ `guide.html:383-384` | 구현에서 한글 라벨 매핑 또는 문서를 영문 enum으로 정정 |
| **MED** | `kind` enum에 `facility` 누락 — 파이프라인은 생산/노출하나 문서 미열거 | `09:206`, `load_geocode.py:85-89`, `10-base.sql:43` ↔ `guide.html:303` | guide에 facility 추가(데이터 적재 즉시 노출되므로 선반영) |
| **LOW** | /health places 의미 불일치 — count(*) address 전체(11.28M) ↔ 문서 'POI 인덱스 건수 156234' | `geocode-api-pg.py:325-327` ↔ `guide.html:399` | 예시값 현행화 + 'address 전체 행수' 의미 명시 |
| **LOW** | CORS preflight 미지원 — `_send`만 ACAO:*, `do_OPTIONS` 부재 → 비단순요청 501 | `geocode-api-pg.py:311-318`, do_OPTIONS grep 무 | `do_OPTIONS` 추가(204+Methods/Headers) 또는 'GET-only 단순요청 한정' 문서화 |
| **LOW** | 빈쿼리 q='' → 200 `{results:[]}`(400 아님) 미문서화. (긍정: SQLi 안전 — 전 파라미터 바인딩) | `geocode-api-pg.py:329-331`, parse 빈terms | 문서에 '빈/공백 q → results:[] (200)' 명시 |
| **LOW** | limit 비숫자/음수 → 기본값 폴백(crash 회피) 미문서화 | `geocode-api-pg.py:47-50` | '범위외 limit → 클램프/기본값(에러 아님)' 한 줄 추가 |

### A-4. 빌드 파이프라인 문서/스크립트 정합

| 심각도 | 발견 | 근거(요지) | 권고 |
|--------|------|-----------|------|
| **HIGH** | `lawd_dong` 채움 단계 부재(A-1 HIGH의 빌드측 관점) | `21-parcel-jibun.sql:16-22`, grep 0건, `handoff:48,134` 산문만 | 멱등 SQL(`TRUNCATE→INSERT SELECT DISTINCT left(bcode,8)...`) 코드화, geocode 단계 직후 호출. 의존순서(geocode→lawd_dong) 명시 |
| **HIGH** | `backfill_parcel_jibun.sql` 고아(load_parcel/load-all 미호출) | grep 호출처 0건 ↔ `geocode-api-pg.py:196` 정확매칭 의존 | load-all parcel 단계 직후 멱등 호출(`WHERE ji_main IS NULL` 가드로 39.9M rewrite 방지) |
| **HIGH** | `build_sigungu_dict.sh` 오케스트레이터 미연결 + navi `.7z` 직접참조(staged/navi 추출본과 drift) | `build_sigungu_dict.sh:12`, grep 0건 ↔ `build-studio.py:573` | load-all 편입 + 입력을 staged/navi로 통일 또는 `left(bcode,5)` 파생 멱등 SQL로 .7z 의존 제거 |
| **MED** | build-studio freshness 시그니처가 backfill/lawd/build_sigungu 미추적 → 개선해도 'fresh' 스킵 | `build-studio.py:656-657` | `TFRESH['load_postgis'].scripts`에 backfill_parcel_jibun.sql·schema/21·build_sigungu_dict.sh 추가, schema 디렉토리 전체 해시 포함 |
| **MED** | QC(13-qc-check)가 지번 산출물 미검증 → **거짓 PASS** | `13-qc-check.py:434-465`, geom_pt/ji_main/lawd_* 미검증 | QC에 lawd_dong/lawd_sigungu>0, ji_main NOT NULL 비율≥95%, parcel_jibun_lookup indisvalid, 골든셋 라이브 1건 추가 |
| **MED** | backfill 미연결 + 인덱스 생성순서 상충(인덱스 먼저 생성됨) → 수동 backfill 시 89분 경로 | `21-parcel-jibun.sql:26-27` ↔ `load_parcel.sh:46-47,147-148` | 'DROP INDEX→backfill UPDATE→CREATE INDEX'로 래핑, work_mem 상향 포함 |
| **MED** | '사전 불변' 단정이 운영DB 한정 진실 — 빈DB 재빌드 경로는 '계속 비어있음' | `handoff:33,109,216` ↔ 채움 0건 | '신규 빌드 시 별도 생성 필요(스크립트 미존재)' 명시, 통합 후 '멱등 재생성'으로 갱신 |
| **LOW** | load-all/build-studio 라벨이 '한 방 빌드 지번까지 완성' 오해 유발(실제 부분완성 exit0) | `load-all.sh:1-3,12`, `build-studio.py:607-608` | 헤더/README에 미포함 단계와 영향 명시 또는 단계 통합 |

**추가 드리프트(A-4):** `lawd_dong` 동수 5067(요약) vs 5046(handoff) 불일치 / 시군구 narrowing이 설계서 `ILIKE`→구현 `lawd_sigungu 사전조인`으로 진화(설계 구식) / 포트표기 혼재(`geocode:8082` vs `geocode-pg:8082` vs `8092`).

---

## (B) 기능개선 백로그 — 중복제거 후 가치×노력 우선순위

> 중복 통합 원칙: (1) `/suggest` 후보가 검색품질·성능 양 차원에 중복 → **단일 항목**으로 통합. (2) `geom_pt 백필`이 커버리지·성능 양 차원에 중복 → 통합. (3) `transcoord`가 커버리지·성능 중복 → 통합. (4) `same_name` UX(검색품질)와 same_name 메타(응답확장)는 **로직 vs 응답봉투**로 분리하되 단일 트랙으로 묶음. (5) 프론트 자동완성 UX 항목은 검색품질·성능 양쪽 언급 → 통합. (6) 행정/법정동 동시제공(응답확장)과 coord2regioncode(커버리지)는 forward/reverse 측면으로 묶음.

### NOW (즉시 — 가치 높고 노력 낮음 / 또는 다른 작업의 전제)

| 항목 | value | effort | feasibility | build vs runtime | providerRef | dependencies |
|------|:---:|:---:|------|------|------|------|
| **B1. POI 적재(상가·인허가·생활편의)** | H | L | 가능 — 소스CSV·파이프라인·스키마 완비(poi-all). TRUNCATE 전 pg_dump 필수, GIN 재생성 maintenance_work_mem 1GB | 재빌드(09)+적재(load_geocode). 런타임/프론트 무변경 | Kakao 키워드검색, Google Places, Naver Local | 백업 선행, 표기 스펙(category) |
| **B2. 프론트 자동완성 UX(r.kind 버그+키보드+race취소+구조화표기)** | M→**H(버그분)** | L | 순수 클라이언트. r.kind/키보드/AbortController 즉시 독립적용 | 프론트(search.js) | 3사 SDK 위젯 | /suggest, 좌표바이어싱, 표기 스펙 |
| **B3. 좌표 정밀도/매칭등급 메타(location_type)** | M | L | 순수 런타임 — 경로별 등급 태깅. geom_pt 백필 후 자동승급 | 런타임 | Google location_type | geom_pt 백필(가속재), 보간(등급공유) |
| **B4. 결과 안정 식별자(place_id) + place_url** | H | L | address.id 즉시 노출(세션내 안정). 영속키는 bd_mgt_sn/biz원본키 2차 | 런타임 1차/적재 2차/프론트 | Kakao id·place_url, Google place_id | 상세/딥링크 라우트 설계 |
| **B5. 법정동·행정동 동시 + 코드4종 정규화** | H | L | addr행은 bcode/hcode 보유. parcel 경로만 행정동 결핍 → lawd_dong에 hcode 1컬럼 | 런타임+소량 적재 | Kakao region_3depth/h_name, coord2regioncode | 표기 스펙(structure 확정), backfill-admin-codes |
| **B6. 응답캐시 nginx proxy_cache(/geocode·/reverse 확장)** | M | L | martin_cache 패턴 재사용. reverse는 좌표 5자리 양자화 선행 | 배포(gateway-nginx.conf) | 3사 엣지/CDN 캐시 | 관측성(X-Cache-Status), 좌표양자화 |
| **B7. graceful 에러 정규화(전경로 try/except+표준바디+statement_timeout)** | M | L | 무의존. A-3 HIGH 결함 직접 해소 | 런타임 | 3사 표준 에러응답 | 레이트리밋·관측성·batch 에러바디 공유 |

### NEXT (다음 — 가치 높고 노력 중 / 또는 NOW의 직접 확장)

| 항목 | value | effort | feasibility | build vs runtime | providerRef | dependencies |
|------|:---:|:---:|------|------|------|------|
| **B8. /suggest 자동완성 전용 엔드포인트(prefix+경량+캡)** | H | M | trgm GIN 재사용. 한글 prefix는 text_pattern_ops B-tree 1개 추가 권장 | 런타임+적재(인덱스1)+프론트 | Google Autocomplete(New), Kakao | 표기 스펙, 좌표바이어싱, B2 |
| **B9. 좌표 바이어싱(origin 거리가중+선택 radius)** | H | M | address.geom GiST 존재. radius 프리필터 병행권장 | 런타임+프론트(현위치 전달) | Google locationBias/origin, Kakao x/y/radius | /suggest, same_name |
| **B10. 오타·유사도 폴백(pg_trgm similarity, 0건시만)** | H | M | pg_trgm 설치·trgm GIN 존재. 폴백 한정 필수(무한스캔 방지) | 런타임(기존 인덱스 재사용) | 3사 오타보정 내장 | /suggest, 인기도 가중 |
| **B11. 인기도/권위 가중(rank_weight 정적 스코어)** | H | M | 09가 is_primary·source·dedup 보유(클릭로그 불요). POI 적재 후 실효 | 재빌드+적재(컬럼)+런타임 | Google prominence, Kakao accuracy | **B1 POI 적재**, 좌표바이어싱 |
| **B12. /reverse 3소스 parity(지번 ST_Contains+도로명+법정/행정동)** | H | M | parcel/admin/address GiST 존재. 점→sido_cd 선결정으로 파티션 pruning | 런타임 | Kakao coord2address+coord2regioncode | admin_boundary 적재(현재 0!), 표기 스펙 |
| **B13. 배치 지오코딩(/geocode/batch·/reverse/batch POST)** | H | M | geocode/reverse 루프 재사용. do_POST 신설, 청크 커밋 | 런타임 | 3사 미제공(폐쇄망 차별가치) | graceful 에러, 레이트/동시성 cap |
| **B14. 관측성(Prometheus /metrics+요청로그+/health degraded)** | H | M | 무의존. /health 풀카운트→reltuples로 비용제거 | 런타임 | 3사 SLA/상태페이지 | 13e-bench 클래스 재사용, compose healthcheck |
| **B15. 검색품질 회귀 게이트(golden 확장+nDCG/MRR/top1)** | M | M | 13b/13c/13e 하니스 존재. golden 라벨 큐레이션 비용 | 오프라인 평가(CI) | 해당없음(내부) | **모든 검색품질 후보의 검증 토대(횡단)** |

### LATER (나중 — 가치 중하 또는 노력 높음 / 선행의존 무거움)

| 항목 | value | effort | feasibility | build vs runtime | providerRef | dependencies |
|------|:---:|:---:|------|------|------|------|
| **B16. parcel geom_pt 백필(39.9M 대표점)** | M | M | handoff §1 절차존재. 시도파티션 점진커밋 권장(89분 함정주의) | 적재(UPDATE) | 내부 최적화 | backfill 개선(§3), B3·B12 가속 |
| **B17. same_name 동명중복 메타+그룹핑/다양성 정렬** | M | L(로직)/M(봉투) | sido/sigungu/emd 보유. results 평면→meta 객체 봉투 확장 | 런타임 | Kakao same_name, Google structuredFormat | 좌표바이어싱, 표기 스펙, 응답봉투 합의 |
| **B18. 관련도 점수 정밀화(다단계+커버리지+길이비)** | M | M | 후보풀 내 재정렬. similarity 결합 | 런타임 | Google relevance, Kakao accuracy | B11, B10, /suggest |
| **B19. 초성 검색(자모분해 chosung 컬럼)** | M | M | 무의존 자모분해(0xAC00 산식). prefix 인덱스 필요 | 재빌드+적재+런타임 | Kakao/Naver 모바일 초성 | /suggest, 동의어 정규화 |
| **B20. 동의어/별칭 사전(약칭·구지명·영문)** | M | M | 사전 큐레이션이 핵심비용(데이터 부재). 고빈도 수백건 시드 | 적재(synonym)+런타임 | 3사 비공개 사전 | 초성/오타 정규화, 표기 스펙 |
| **B21. 띄어쓰기·표기변형 견고화(런타임 parse 강화)** | M | L | 빌드측 정규식 재사용. 변형 후보 상한 필요 | 런타임 | 3사 표준 동작 | 동의어, 관련도 정밀화 |
| **B22. viewport/bbox(필지 extent+행정구역 extent)** | M | M | parcel envelope 4값 무비용. dong은 admin 조인 필요 | 런타임+프론트(fitBounds) | Google viewport(차별화) | flyTo→fitBounds, dong↔admin 정합 |
| **B23. 카테고리 그룹코드 표준화(MT1/FD6 동등)+필터** | M | M | poi-taxonomy/crosswalk 패턴 존재. group_code 컬럼 권장 | 적재+런타임+재빌드 | Kakao category_group_code | **B1 POI 적재**, schema 동기 |
| **B24. 우편번호 일관성(전경로 충전+5자리 정규화)** | M | L | addr postal 보유. parcel은 addr_at 재사용 | 런타임 | Kakao zone_no, Naver postalCode | 좌표정밀도, 표기 스펙 |
| **B25. coord2regioncode 응답완성(b_code/h_code 동시)** | M | L | 경계 폴리곤 적재시 재사용. 행정동코드 체계 크로스워크 주의 | 런타임 | Kakao coord2regioncode, Naver | **admin_boundary 적재(현재 0)**, 표기 스펙. B12와 통합 |
| **B26. 근접검색 파라미터화(radius/rect/sort=distance)** | M | M | geom GiST. 공간조건을 WHERE 선행 필요 | 런타임+프론트(getBounds) | Kakao x/y/radius/rect, Google | viewport 후보와 대칭 |
| **B27. 레이트리밋+동시성 cap** | M | L | nginx limit_req 설정+BoundedSemaphore 무의존 | 배포+런타임 | Google QPS, Kakao 호출제한 | 관측성, batch, graceful 에러 |
| **B28. 행안부 건물DB(텍스트) 적재 — 건물명 보강** | M | M | 무심사 즉시. 09에 extract_build 경로 추가 필요 | 재빌드+적재 | Kakao building_name, Naver | juso_navi 신선도 사이클, B1과 순서조율 |
| **B29. GIS건물통합정보(building) 적재 — footprint/3D/동라벨** | M | M | 6.66M MultiPolygon. 디스크/메모리 주의, ODbL 회피 별도소스 | 적재+런타임 | Kakao/Naver/Google reverse | parcel 파티션 단위, data-licenses |
| **B30. transcoord(좌표계 변환 유틸+런타임 엔드포인트/입력지원)** | M→L | L→M | PostGIS ST_Transform. 5174는 PROJ 필수(순수파이썬 금지) | 빌드(유틸)+런타임(/transcoord, input_coord/out_srid) | Kakao transcoord | graceful 에러(미지원 SRID 400) |
| **B31. CORS preflight(do_OPTIONS)** | L | L | A-3 LOW 결함 해소 | 런타임 | 3사 표준 | B7과 함께 |
| **B32. 지하철 출구·건물 출입구 좌표 적재** | M | M | OSM subway_entrance 즉시. navi/juso 출입구는 심사/장기 | 재빌드+적재 | Kakao/Naver/Google 출구단위 | navi 출입구 컬럼 확인, B1 인프라 |
| **B33. public_facility(병원·경찰·AED) 적재** | L | L | 스키마/로더 일부 존재. 좌표없는 출처 후채움 | 적재+런타임 | Kakao 키워드, Google Nearby | B1 인프라, B23 카테고리 |
| **B34. 증분/델타 적재(juso 변동분+address 부분갱신)** | M | H | 설계부담. arbiter키·삭제분 식별 필요 | 적재(델타로더)+freshness | 3사 미제공(운영성) | B1·B28 적재 후, 백업 자동화 |
| **B35. 도로구간 보간(미존재 본번)** | L | H | navi 본번↔도로 매핑 부재로 부정확. 결번 빈도 낮음 | 런타임/정석화시 적재 | Google RANGE_INTERPOLATED | B3 등급, road 중심선 정합 |
| **B36. 코드 베이크 이미지(Dockerfile pip 제거+wheelhouse)** | M | L | linux/amd64 wheel vendor. 가치는 재현성/버전핀 | 패키징(Dockerfile+package.sh) | 내부 배포위생 | package.sh/deploy.sh, compose volume 분리 |
| **B37. 영문주소(romanization)** | M | H | 행정구역 영문은 고시매핑(5천행). 도로명 로마자 변환기 정확도 난제 | 적재(en사전)+런타임(변환기)+재빌드 | Google address_components | 표기 스펙(경계합의), 신규 모듈 |

---

## (C) 즉시 반영 권고 — 진행 중 후속작업 태스크 매핑

현재 진행 태스크(D1백업·D2POI·D3geom_pt·C1~C4·표기패리티 X1~X4)에 위 발견·개선을 **흡수/추가**할 매핑이다.

| 진행 태스크 | 흡수할 검토발견 | 추가할 개선후보 | 비고 |
|------------|----------------|----------------|------|
| **D1 백업** | A-2 HIGH(backups 디렉터리 부재, TRUNCATE 가드 없음) | B27(레이트/동시성 cap의 advisory lock 일부) | **권고→강제 승격**: load_geocode/load-all 진입부에 '최근 덤프 없으면 TRUNCATE 거부/자동 pg_dump'. mkdir+pg_dump 자동화. A-2 MED(동시작업 락)도 D1에 lockfile로 흡수 |
| **D2 POI** | A-3 MED(kind=facility 문서누락) | **B1(POI 적재)**, B11(rank_weight, POI 후 실효), B23(카테고리 group_code), B33(public_facility) | D2가 곧 B1. 적재 시 guide.html에 facility kind 선반영(A-3). category 표준화(B23)를 동일 재빌드에 묶음 |
| **D3 geom_pt** | A-1 LOW(성능근거 미실현), X-3(경량판 미커밋) | **B16(geom_pt 백필)**, B3(location_type 자동승급) | D3 실행 시 backfill을 §3 개선판(work_mem↑·인덱스 drop/recreate·시도파티션 점진)으로. 백필 후 B3 등급 PARCEL_CENTROID→PARCEL_POINT 자동상향. **경량판 커밋 누락 해소 필수** |
| **C1~C4** (빌드 자동화 추정) | X-1(lawd_dong 채움), X-2(backfill 고아), A-4 HIGH(build_sigungu 미연결), A-4 MED(freshness 시그니처, QC 거짓PASS) | — | **C1: build_dong_dict 추가**(X-1). **C2: backfill을 load-all 편입**(WHERE ji_main IS NULL 가드, DROP→UPDATE→CREATE 래핑). **C3: build_sigungu_dict load-all 편입+입력 staged/navi 통일**. **C4: TFRESH 시그니처+QC 지번검증 추가**(lawd>0, ji_main≥95%, 골든셋 라이브) |
| **표기패리티 X1~X4** | A-3 HIGH(parcel address 스키마 불일치), A-3 MED(areas[].type 영/한, facility), A-1 MED(r.kind 버그), A-2 MED(guide 예시 구식) | B2(프론트 r.kind+구조화표기), B5(법정/행정동 동시), B17(same_name 봉투), B24(우편번호 일관), B12/B25(reverse structure) | **표기 스펙이 structure 스키마를 확정**하면 B5는 '모든 경로 동일충전+행정동 보강'으로 좁혀짐. r.kind 버그(B2)·areas type 매핑·facility enum은 표기 스펙에 흡수. guide.html /health·/reverse 예시 현행화를 X 트랙에 포함 |

**추가 신설 권고(현 태스크에 없으나 즉시 추가):**
- **C에 'A-3 HIGH 예외핸들링(B7)' 신설** — 무응답/소켓끊김은 운영 치명적, effort L이므로 즉시. guide에 에러스키마 표 동반.
- **D2 직전 D1 강제 게이트화** — POI 적재가 TRUNCATE이므로 D1 가드가 D2의 차단 전제.
- **B14(관측성) NOW 격상 검토** — 어떤 개선이든 회귀 측정 토대. /health 풀카운트 제거는 헬스체크 비용도 즉시 절감.

---

## (D) 리스크·미해결 질문

### D-1. 시스템 리스크

| 리스크 | 영향 | 완화 |
|--------|------|------|
| **빈 DB 재빌드 시 지번검색 0건이 정상종료(exit0)** | 새 세션이 '빌드 성공'으로 오인 → 무지번 배포 | C1~C4 통합 + QC 지번검증(거짓PASS 차단)을 D2/D3와 **같은 사이클에** |
| **backfill 89분 pathological 경로 재현** | 인덱스 살아있는 39.9M UPDATE → 장시간 점유/OOM | D3에서 §3 개선판 강제. 단일 트랜잭션 금지(시도파티션 점진) |
| **TRUNCATE 백업 없이 실행** | POI/주소 데이터 소실 | D1 가드 강제화가 D2 선결조건 |
| **admin_boundary=0로 /reverse·B12·B25 전제 붕괴** | reverse areas 영구 [], B12/B25 구현해도 무데이터 | admin 적재를 별도 태스크로 명시(현 진행 태스크에 admin 적재 부재 — **누락 의심**) |
| **동시 대량작업 OOM(load 259 재발)** | 컨테이너 8GB 풀스캔 충돌 | advisory lock/lockfile 단일 게이트(B27 일부를 D1에) |

### D-2. 미해결 질문

1. **admin_boundary/building 적재 계획?** — 현 진행 태스크(D1~D3·C1~C4·X1~X4)에 admin 적재가 보이지 않는다. B12·B25·reverse areas의 전제인데 별도 태스크 필요 여부 확정 요망. (handoff §4-1 recover.sh에 admin·building 단계 포함할지 결정 미정)
2. **C1~C4의 정확한 범위** — 본 보고서는 C1~C4를 빌드자동화(lawd_dong/backfill/build_sigungu/QC)로 추정 매핑했다. 실제 C1~C4 정의 확인 필요.
3. **place_id 영속성 정책** — 빌드마다 PK 재발번(TRUNCATE)되어 address.id는 세션내만 안정. 자연키(bd_mgt_sn/biz 원본키) 채택 시 biz 원본ID 미보존이 선결과제. 어느 수준(1차 kind:id vs 2차 영속키)으로 갈지?
4. **경량 backfill 경량판 SQL 소재** — 현 geom_pt=NULL을 만든 경량판이 미커밋. 재현/감사 위해 복원 가능한가, 아니면 D3에서 신규 작성으로 대체하는가?
5. **응답 봉투(envelope) 합의** — B12/B17/B25(same_name meta, reverse 3소스)가 results 평면구조를 meta 포함 구조로 확장하려 함. 표기패리티 스펙과 **응답 최상위 구조를 누가 확정**할지(중복/충돌 방지). guide.html·search.js·13d-parity 동기 주체 미정.
6. **포트/컨테이너 정본** — 8082(SQLite)/8092(PG) 표기가 문서 내 혼재. SQLite 은퇴(gateway depends_on 제거 포함) 완료 시점과 정본 포트 확정 필요.
7. **freshness 시그니처에 schema 디렉토리 전체 해시 포함 여부** — apply-schema는 멱등이나 인덱스/컬럼 변경이 어떤 타깃 시그니처에도 무기여. 전체 해시 포함의 재빌드 빈도 트레이드오프 판단 필요.
8. **5174(Bessel) 변환 정책** — B30 transcoord에서 순수파이썬 5174 금지(수십cm~m 오차). 폐쇄망 PROJ datum grid 동봉 여부 확인 필요.
