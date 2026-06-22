# 지오코딩 검색 성능·최적화 감사 (지번/도로명/주소)

> 대상: `server/geocode-api-pg.py` (PostGIS 지오코딩 API) · 브랜치 `feature/postgis-refactor`
> 방식: **정적 분석** — 라이브 DB·`EXPLAIN ANALYZE` 없이 코드·스키마·인덱스 정의로 PostgreSQL/PostGIS 플래너 동작을 추론. 7개 차원 분석 → 46건 발견 → 적대적 검증 → **41건 확정 / 5건 기각**.
> 제약(계약): `server/geocode-api.py`(SQLite FTS5+R-tree 판)가 **응답 형태·스코어링의 기준**이며 PG 판이 이를 100% parity 보존해야 함. 응답 형태·스코어링은 전부 Python에 있어 **대부분의 인덱스/설정 변경은 계약을 깨지 않고 적용 가능**.

## 핵심 진단

현 PG 판의 지배적 병목은 **한국 주소 검색의 최빈 패턴(한글 1~2글자 자동완성, 다중토큰 상호+지역)이 `pg_trgm` 인덱스를 구조적으로 못 타는 것**이다.

1. **다중토큰 지역 arm 인덱스 부재** → `OR` 의 BitmapOr 가 붕괴해 토큰 길이와 무관하게 다중토큰 질의가 통째로 Seq Scan.
2. **한글 2글자는 trgm 3-gram 추출이 0개** → 단일/다중 토큰 모두 인덱스 무력화.

두 경우 모두 **1,600만행 Seq Scan(스키마 주석이 인정한 2~11초)** 으로 떨어진다. 그 위에 결과당 `addr_at()` N+1 KNN 쿼리, `max_size=8` 풀 + `statement_timeout` 부재로 인한 풀 고갈, `/health` 의 `count(*)` 풀스캔, 프론트의 1글자 최소길이 게이트가 부하를 가중한다.

> 특기: 이전 SQLite 판은 FTS5 `prefix='2 3'` 로 한글 2~3글자 검색을 1급으로 색인했다. PG 전환 시 `pg_trgm` 으로 대체한 것이 **명백한 성능 회귀**이며 한국 검색 최빈 패턴의 사각지대다.

## Quick Wins (저위험·즉효)

| 항목 | 위치 | 노력 |
|---|---|---|
| 프론트 최소 질의 2글자 + 한글 IME composition 가드 | `demo/js/search.js:54` | small |
| `/health` 의 `count(*)` → `reltuples` 추정치 | `server/geocode-api-pg.py:221` | trivial |
| 지역(sido/sigungu/emd) 부분 trgm GIN 추가 | `scripts/postgis/schema/11-address-search.sql` | medium |
| trgm GIN 2개를 `WHERE kind<>'addr'` 부분 인덱스로 재생성 | `11-address-search.sql:6,10` | small |
| `statement_timeout='3s'` + 풀 `max_size` 상향 | `server/geocode-api-pg.py:24-29` | trivial |
| 프론트 AbortController + seq 가드 | `demo/js/search.js:55-60` | small |
| 죽은 인덱스 `address_region_idx` 제거 | `11-address-search.sql:21` | trivial |

## 우선순위별 발견 (검증 통과 41건 중 상위 15)

| 순위 | 심각도 | 제목 | 위치 | 노력 |
|---|---|---|---|---|
| 1 | high | 프론트 최소 질의 2글자 + IME 가드 — trgm 무력화 구간 트래픽 차단 | `search.js:51-60` | small |
| 2 | **critical** | 다중토큰 지역 arm 인덱스 부재 → BitmapOr 붕괴 → 다중토큰 Seq Scan | `geocode-api-pg.py:143-144` | medium |
| 3 | high | 한글 2~3글자 미만 trgm 무력화 — SQLite FTS5(`prefix='2 3'`) 대비 회귀 | `geocode-api-pg.py:143-147` | large |
| 4 | medium | `/health` `count(*)` 2회 — 1,600만행 풀스캔 헬스체크가 풀 점유 | `geocode-api-pg.py:220-223` | trivial |
| 5 | medium | 결과당 `addr_at()` N+1 — 비-addr 결과마다 별도 KNN(요청당 1+N) | `geocode-api-pg.py:173-175` | medium |
| 6 | medium | trgm GIN 이 `kind<>'addr'` 부분조건 없이 1,600만 addr 행까지 적재 | `11-address-search.sql:6,10` | small |
| 7 | medium | `statement_timeout`/풀 대기 timeout 부재 — head-of-line blocking | `geocode-api-pg.py:24-29` | trivial |
| 8 | medium | 프론트 AbortController 부재 — stale 응답이 최신을 덮는 경쟁조건 | `search.js:55-60` | small |
| 9 | low | `ADDR_CAP=400` 무정렬 오버페치 + `SELECT *` | `geocode-api-pg.py:28,116-151` | medium |
| 10 | low | 랭킹/정렬 전부 Python — DB 가 LIMIT 전 Top-N 을 못 고름 | `geocode-api-pg.py:112-164` | large |
| 11 | low | 단일 대형 address 테이블 — 검색 핫패스 kind 분리/파티셔닝 미적용 | `10-base.sql:41-63` | large |
| 12 | low | `address_region_idx(sigungu,emd)` 죽은 btree (동등비교 미사용) | `11-address-search.sql:20-21` | trivial |
| 13 | low | 역지오코딩 거리필터(geography)와 GiST(geometry) 자료형 불일치 | `geocode-api-pg.py:96-188` | medium |
| 14 | low | 서버측 결과 캐시 + gzip/keep-alive 부재 | `geocode-api-pg.py`, `gateway-nginx.conf` | small |
| 15 | low | 프론트 클라 캐시/좌표 정밀도/정규식 미컴파일 등 잔여 위생 | `search.js`, `geocode-api-pg.py` | small |

### 상세 — Critical / High

**[2위·critical] 다중토큰 지역 arm 인덱스 부재 → BitmapOr 붕괴**
다중토큰 이름경로가 토큰마다 `(search_text ILIKE '%t%' OR bld ILIKE '%t%' OR sido ILIKE '%t%' OR sigungu ILIKE '%t%' OR emd ILIKE '%t%')` 를 `AND` 로 묶는다. `sido/sigungu/emd` 엔 trgm 인덱스가 없고 `address_region_idx` 는 평범한 btree 라 선행 와일드카드 `ILIKE '%t%'` 에 무력하다. PG 는 `OR` 의 **모든** arm 이 인덱스화돼야 BitmapOr 를 만드므로, 지역 3개 arm 때문에 OR 전체가 비인덱스화 → **토큰이 4글자여도(예 '강남 스타벅스'의 '스타벅스')** 다중토큰 질의가 통째로 Seq Scan 으로 추락한다.
- 수정: 지역 컬럼에 부분 trgm GIN 추가, 또는 `region_text` 합성컬럼 1개로 통합.
```sql
CREATE INDEX address_sigungu_trgm ON address USING gin (sigungu gin_trgm_ops) WHERE kind <> 'addr';
CREATE INDEX address_emd_trgm     ON address USING gin (emd     gin_trgm_ops) WHERE kind <> 'addr';
CREATE INDEX address_sido_trgm    ON address USING gin (sido    gin_trgm_ops) WHERE kind <> 'addr';
-- 또는 region_text GENERATED ALWAYS AS (coalesce(sido,'')||' '||coalesce(sigungu,'')||' '||coalesce(emd,'')) STORED + gin trgm 으로 arm 통합
```
> 주의: 2글자 토큰은 이 인덱스로도 무력(3위 참조)이므로 본 항목의 효과는 **3글자+ 다중토큰** 구제다.

**[1위·high] 프론트 최소 질의 2글자 + IME composition 가드**
`q.length<1` 만 차단해 1글자('강','역')부터 매 입력마다 `/geocode` 호출. 한글 IME 는 `input` 이벤트라 **조합 미완성 자모('ㄱ','가')에서도 발사**된다. 가장 비싼 질의(1~2글자 trgm 무력화)가 가장 빈번하게 핫패스를 직격.
```js
let composing = false;
input.addEventListener('compositionstart', () => composing = true);
input.addEventListener('compositionend',   () => { composing = false; schedule(); });
input.addEventListener('input', () => { if (!composing) schedule(); });
function schedule() {
  clearTimeout(timer);
  const q = input.value.trim();
  if ([...q].length < 2) { list.style.display = 'none'; return; }
  timer = setTimeout(() => run(q), DEBOUNCE);
}
// 서버측 방어 병행: norm(q) 최장토큰 길이 < 2 면 빈 results 반환
```

**[3위·high] 한글 2글자 trgm 구조적 무력화 (SQLite FTS5 대비 회귀)**
'강남','역삼','홍대','잠실' 같은 한글 2글자는 완전한 3-gram 이 0개라 GIN qual 생성 불가 → 단일/다중 모두 Seq Scan(`kind<>'addr'` 부등호라 `address_kind_idx` 도 무용).
- (a) 즉효(단일토큰 prefix): `name` 정규화 + `text_pattern_ops` btree 부분인덱스로 좌측앵커 `LIKE 't%'` 회수.
- (b) 근본(2글자 substring 까지): **`pg_bigm`(2-gram GIN)** — SQLite `prefix='2 3'` 의 PG 등가물에 가장 근접. **에어갭 폐쇄망 배포라 확장 반입·빌드 비용 별도 확인 필요.**

### 상세 — Medium

- **[4위]** `/health` `count(*)` → `SELECT reltuples::bigint FROM pg_class WHERE relname='address'` (O(1), 적재 끝 `ANALYZE` 로 갱신). SQLite 판도 `meta` 캐시값 반환이라 근사치가 계약 정신에 부합.
- **[5위]** `addr_at()` N+1 → Top-N 확정 후 좌표를 모아 `unnest`+`LATERAL` 배치로 1+N→2회 상수화. **LATERAL 내부 2500m `ST_DWithin` 반경 컷 유지 필수**(빼면 항상 아무 주소나 반환 → `None` 계약 위반). `ord` 로 되매핑, 누락=`None`.
- **[6위]** trgm GIN 2개를 `WHERE kind<>'addr'` 부분 인덱스로 재생성 → 인덱스 수 GB→수백 MB. 후보집합 불변(계약 안전).
- **[7위]** `statement_timeout='3s'`(`ConnectionPool(configure=...)` 또는 DSN `options`), 풀 `max_size` 상향(PG `max_connections` 내), `POOL.connection(timeout=...)` 로 빠른 실패. **timeout 은 안전망이고 본질은 2·3위 쿼리 최적화.**
- **[8위]** 프론트 `AbortController` + `seq` 가드로 stale 응답 덮어쓰기 제거(순수 프론트, 계약 안전). 동기 stdlib 서버라 서버 부하 경감 효과는 없고 UX/정확성 개선.

### Low (구조 개선·위생)
9위 `SELECT *` 폭 축소(단 `cat1/cat2/jibun` 등 사용 컬럼 누락 금지) · 10위 점수식 SQL 화로 early-LIMIT(단 `rb()` 부분문자열 의미 보존 필요) · 11위 비-addr 테이블 분리/`PARTITION BY LIST(kind)`(6위 부분인덱스로 대부분 달성 가능) · 12위 죽은 `address_region_idx` 제거 · 13위 addr 용 geography expression GiST 추가로 거리필터·정렬 자료형 일치 · 14위 서버 `TTLCache` + 게이트웨이 gzip + HTTP/1.1 keep-alive · 15위 클라 LRU 캐시·좌표 `toFixed(6)`·정규식 전역 컴파일.

## 권장 적용 순서

1. **Quick Win** (저위험·즉효): 1·4·7·12위 — 프론트 게이트, `/health` 교체, `statement_timeout`, 죽은 인덱스 제거
2. **인덱스 보강** (중간, parity 안전): 2·6위 — 지역 trgm, 부분 인덱스화
3. **N+1 / 프론트 정확성**: 5·8위 — LATERAL 배치, AbortController
4. **근본 대책** (대공사): 3위 — 한글 2글자(`pg_bigm` 등). 에어갭 제약 확인 후
5. **신중 적용** (parity 위험): 9·10위 — `13d-geocode-parity.py` 회귀 측정 후에만

## 정적 분석의 한계 — 실측으로 확정 필요

- 모든 플랜 추론은 인덱스 정의·연산자·캐스팅 기반 정적 추정. **`EXPLAIN (ANALYZE, BUFFERS)` 로 실측 확정 필요** (Seq Scan vs BitmapOr vs 병렬 Seq Scan 선택, 비용 임계, posting list 크기).
- '2~11초' 는 스키마 주석 인용치. 실제 wall-clock 은 캐시 워밍·병렬워커(`max_parallel_workers_per_gather=4`)·VM 상태에 따라 변동 → 실측 필요.
- 2·3·6위 인덱스 추가 후 BitmapOr/BitmapAnd 형성, 3글자+ 토큰 인덱스 선택 여부를 `EXPLAIN` 으로 검증.
- 4위 `reltuples` 정확도는 `ANALYZE` 시점 의존.
- 9·10위는 `ORDER BY` 부재 절단 + `rb()` 부분문자열 의미 차이로 SQLite top-N parity 를 깰 수 있음 → `13d-geocode-parity.py` 회귀 측정 후에만 적용.
- 13위 geography expression GiST 전환 시 `reverse`(20km 윈도) KNN 가속 손실 가능성 → 도입 전 플랜 실측.
- 14위 캐시 효과는 트래픽 히트율 가정 의존 → 실트래픽 로그로 접두어 반복률 측정.
- 표본 검증 권장: 적재 데이터에 NFD/라틴 POI 명 혼입 시 `ILIKE` 폴딩·`unaccent` 미적용으로 silent 0건 가능(현재 NFC 전제로 기각).
