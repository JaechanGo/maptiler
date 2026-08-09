# 지번(parcel) 검색 1급화 — 성능·정확도 최적화 계획

> 브랜치: `feature/postgis-refactor` · 작성: architect(Lead) · 조정 채널: walkie-talkie `geocode`
> 결정: **parcel 테이블(전국 39.6M 필지)을 권위 지번 소스로 승격** (사용자 승인)

## 1. 문제 진단

도로명 검색(`server/geocode-api-pg.py:120-143`)은 `address(road_norm,main_no,sub_no)` B-tree 정확매칭으로 정상 동작. 지번은:

| 문제 | 위치 | 영향 |
|------|------|------|
| Fallback 전용 (`if not results`) | `geocode-api-pg.py:149` | 의도적 지번검색 우선순위 없음 |
| `search_text ILIKE '%동%' + '% 번지'` | `:162-166` | 선행 와일드카드 + 짧은 번지 → trigram GIN 미적용, 부정확·저속 |
| `address.jibun` 커버리지 ~60% (구 navi) | `10-base.sql:51` | 다수 필지 누락 |
| **`parcel` 39.6M행 미연동** | `20-parcel.sql` | 권위 지번 소스가 검색에 미사용 |

근본 원인: 권위 지번 소스(`parcel`)가 검색경로에 미연결 + 정규화 컬럼/대표점/인덱스 부재로 빠른 lookup 불가.

## 2. 목표 아키텍처

### 2.1 parcel 스키마 확장 (정규화)
```sql
ALTER TABLE parcel
  ADD COLUMN ji_main int,            -- 본번 (jibun 파싱). road main_no 와 혼동방지 위해 ji_ 접두
  ADD COLUMN ji_sub  int,            -- 부번 (없으면 0)
  ADD COLUMN san     smallint,       -- 산(임야) 여부 0/1
  ADD COLUMN geom_pt geometry(Point,4326);  -- ST_PointOnSurface(geom) 대표점(검색결과 좌표)
```
> 컬럼명 확정: `ji_main`/`ji_sub`/`san` (data-eng 제안 수용, road `main_no` 와 구분).
- `emd_cd char(8)` 기존 활용(법정동코드 앞8). `sido_cd char(2)` = 파티션키.
- `geom_pt` materialize → 질의시 39.6M 폴리곤 centroid 연산 회피.

### 2.2 법정동명 → emd_cd 해소 사전
사용자 질의는 법정동 **이름**("역삼동"), parcel은 **코드**(emd_cd). 사전 필요:
```sql
CREATE TABLE lawd_dong (
  emd_cd char(8) PRIMARY KEY,
  sido text, sigungu text, emd text
);  -- address distinct(bcode/sido/sigungu/emd) 또는 행정표준 법정동코드로 구축
CREATE INDEX lawd_dong_emd_idx ON lawd_dong (emd);
```
법정동명 전국중복(역삼동·중앙동…) → 지역토큰(시군구/시도)으로 좁힘.

> ⚠️ **확정(reviewer HIGH 리스크 반영):** 동명은 **정확 등가(`lawd_dong.emd = %s`)** 매칭만 사용.
> `ILIKE '%동%'` 인픽스 전면 폐기 (2자 동명 → 15.7M Seq Scan 유발). `parse()`가 법정동명 전체토큰을
> 추출하므로 정확매칭 가능. parcel 본질의는 해소된 `emd_cd`(인덱스)로만 수행.

### 2.3 인덱스
```sql
-- 정확 lookup (파티션 로컬, 부모에 생성→전파)
CREATE INDEX parcel_jibun_lookup ON parcel (emd_cd, ji_main, ji_sub);
-- 기존 parcel_geom_gix(GiST), parcel_pnu_idx 유지
```
파티션 pruning: 질의에서 `sido_cd = left(emd_cd,2)` 동반 → 17파티션 중 1개만 스캔.

### 2.4 질의 경로 (geocode-api-pg.py)
지번 경로를 **이름 경로보다 앞**으로 승격(도로명 경로는 무변경):
```
if p["dong"] and p["house"]:
  1) emd_cd 후보 = SELECT emd_cd,sido,sigungu FROM lawd_dong
                   WHERE emd = %dong%        -- 정확등가(=). ILIKE 인픽스 금지
                     [AND (sigungu ILIKE 지역토큰 OR sido ILIKE 지역토큰)]  -- 동명중복 좁힘
  2) SELECT jibun, ST_X(geom_pt) lon, ST_Y(geom_pt) lat, emd_cd FROM parcel
     WHERE sido_cd = ANY(left(cds,2)) AND emd_cd = ANY(cds)
       AND ji_main=%a AND ji_sub=%b [AND san=%]
  3) 스코어(정확본번-부번 우선) → 결과
도로명(road) 경로: 변경 없음
address.jibun fallback: 안전망으로 유지(parcel 0건시)
```

### 2.5 성능 근거
- ILIKE seq/trgm(39.6M) → int B-tree 정확매칭(파티션 로컬). 예상 ms 단위.
- `geom_pt` 사전계산 → 폴리곤 ST_PointOnSurface 런타임 비용 제거.

## 3. 역할 분배 (walkie-talkie geocode 채널)

| 세션 | 역할 | 담당 |
|------|------|------|
| **architect**(나) | Lead | 스키마·동명해소·API경로 설계, 전체 조정, 통합, 충돌 중재 |
| **data-eng** | 데이터 | parcel.jibun→main_no/sub_no/san 백필, geom_pt materialize, lawd_dong 사전 구축, 커버리지 검증 (Docker/DB 기동) |
| **query-opt** | 쿼리/인덱스 | 인덱스 생성, 지번 질의 SQL 구현(geocode-api-pg.py), EXPLAIN ANALYZE·파티션 pruning 검증 |
| **reviewer** | 리뷰 | 스키마·쿼리 변경 리뷰(정규화 안전성, dedup, 도로명 회귀) |
| **qa-eval** | QA | 지번 골든셋 구축, 도로명 parity 회귀, 성능 벤치(전/후) |

## 4. 단계 (의존성)

- **P0 스키마 마이그레이션** — architect 설계 → query-opt 적용 (`scripts/postgis/schema/21-parcel-jibun.sql` 신규)
- **P1 백필·사전** — data-eng (P0 후). 커버리지 리포트(jibun 파싱 성공률, geom_pt 채움률)
- **P2 인덱스·질의경로** — query-opt (P1 후). EXPLAIN 증빙
- **P3 리뷰** — reviewer (P2 후)
- **P4 골든셋·벤치** — qa-eval (P2 후, P3 병행). 지번 정확도 + 도로명 무회귀 + 지연(p50/p95)

## 5. 합의 필요/리스크
- jibun 표기 변형(산, 본번만, 구분자) 파싱 규칙 → data-eng 가 샘플 1000건 추출해 architect 와 확정
- 법정동명 중복 해소 정확도 → qa-eval 골든셋에 동명중복 케이스 포함
- parcel 미적재 가능성(Docker down) → data-eng 가 P1 착수시 적재상태 우선 확인
