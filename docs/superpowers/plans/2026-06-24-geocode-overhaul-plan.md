# 지오코딩 표기 패리티 + 후속처리 + 개선 통합 구현계획

> **실행 주체:** cmux-team conductor (스펙·코드 직접 열람 가능). 각 태스크는 아래 스펙 섹션을 **단일 진실원천**으로 참조한다(DRY).
> **스펙(커밋 344a554):**
> - 표기: `docs/superpowers/specs/2026-06-24-geocode-display-parity-design.md` (X1~X6, F1~F17)
> - 후속처리: `docs/superpowers/specs/2026-06-24-geocode-followup-ops-spec.md` (D1~D3·C1~C4, DAG·런북)
> - 검토/개선: `docs/geocode-review-개선백로그.md` (HIGH 결함, B1~B37)

**Goal:** 지오코딩 검색 표기·응답을 네이버/카카오/구글과 동등(분해형)하게 만들고, 모든 개선이 build-studio 재빌드 산출물에 반영되게 하며, 손실 없이 후속처리(백업·POI·geom_pt·정합성)를 완료한다.

**Architecture:** 빌드 산출 컬럼(geocode.sqlite→PostGIS) + 런타임 조립(display/structure) + 프론트 소비. 대량 DB작업은 advisory-lock 단일 게이트로 직렬(cap=1), 코드 태스크는 conductor 병렬.

**Tech Stack:** PostGIS17.5(컨테이너)·psycopg·Python http.server(geocode-api-pg.py)·MapLibre(demo)·bash 빌드 스크립트·build-studio.py.

## Global Constraints (모든 태스크 공통 — 위반 시 차단)

- **대량 DB작업 동시 금지**: parcel/address/building 적재·대량UPDATE는 `pg_advisory_lock(911001)` 단일 게이트로 직렬(cap=1). 과거 16-fan-out 풀스캔 → 호스트 OOM(load 259).
- **TRUNCATE는 백업 후**: `load_geocode.py:59-60`이 address+poi TRUNCATE 후 전량 재적재 → 직전 D1 백업·아티팩트 스냅샷 확인 후에만.
- **'국소 재적재' 운영DB 금지**: load_geocode는 부분적재 경로 없음 → 골든셋 실험은 격리 검증DB(cuvia_verify)에서만.
- **전국 backfill 자동배선 금지**: `backfill_parcel_jibun.sql`(39.9M 단일 UPDATE)은 정규 load-all/TFRESH 자동 stale에 편입 금지. `STEPS=backfill` 옵트인만.
- **전국 parcel materialize 안 함**: 지역명은 런타임 dict(lawd_dong/lawd_sigungu) 조인. ji_main/ji_sub/san만 backfill materialize.
- **백업/덤프는 컨테이너 내부 pg_dump(17.5)**: 호스트 pg_dump 18.4 ↔ 서버 17.5 메이저 불일치(복원 비호환). `docker exec server-postgis-1 pg_dump`.
- **int4 가드(length≤7) 제거 금지** / 지번질의 **char(2)/char(8) 캐스팅 유지**(파티션 pruning).
- **zsh `_safe_eval` 회피**: 셸은 `env bash <launcher.sh>`, SQL은 `*.sql` 파일 + `psql -f`, psql/pg_dump 변수에 담지 말고 직접.
- **Docker 디스크 diskSizeMiB 직접편집 금지** / 산출물 iCloud 금지.
- **하위호환**: 응답에 기존 `name`·`category.sub` 유지하고 `display`/`structure` 추가(가산).

---

## 실행 순서 DAG (웨이브)

```
WAVE A (코드·병렬, DB실행 없음) ── 정합성/빌드 기반
  A1 lawd_dong 적재자+사전배선   A2 backfill 분리·옵트인 배선   A3 build-studio freshness+QC 지번검증

WAVE B (코드·병렬) ── 표기/응답 오버홀
  B1 ri/bld_* 스키마 동기(X5)  B2 09-gen 빌드컬럼 정정(X2)  B3 API 조립+parcel수정+PIP+에러(X1·X6·C4)
  B4 프론트+문서(X3)           B5 표기 검증 하니스(X4)

WAVE C (대량 DB·직렬 cap=1, 사람 승인 게이트) ── 데이터
  C1 D1 백업 ──▶ ├─ C2 D2 POI적재(TRUNCATE) ──▶ C4 lawd_* 재생성실행 ──▶ C5 backfill 실행(옵트인) ──▶ C6 D3 geom_pt
                 └─ C3 admin_boundary 적재(신설)

WAVE D (코드·병렬, 일부 데이터 의존) ── 개선 NOW+NEXT
  NOW: B6 nginx 캐시  B7 관측성/metrics  (place_id·법정/행정동은 B3에 흡수)
  NEXT: B8 /suggest  B9 좌표바이어싱  B10 오타보정  B11 인기도가중(POI후)  B12 reverse 3소스 parity(admin후)  B13 batch  B15 검색품질 회귀게이트

WAVE E (정리) ── E1 SQLite(8082) 은퇴
```

**선결 게이트(X4)**: `lawd_dong COUNT>0 AND parcel.ji_main NOT NULL ≥95%` 충족 전엔 표기/지번 검증 PASS 불가.

---

## Wave A — 정합성/빌드 기반 (CRITICAL, 코드 병렬)

> "재빌드 시 반영"의 전제. 현재 빈 DB 재빌드 시 지번검색 0건이 정상종료(거짓 PASS)되는 결함을 막는다.

### Task A1 — lawd_dong 적재자 + 사전 배선
- **Files:** Create `scripts/postgis/build_dong_dict.sql`; Modify `scripts/postgis/build_sigungu_dict.sh`, `scripts/postgis/load-all.sh`
- **Spec §:** ops C2 / review X-1·A-4(HIGH) / display F1·F2
- **수용기준:** ① `build_dong_dict.sql` 멱등(`TRUNCATE→INSERT … SELECT DISTINCT left(bcode,8) … FROM address`, tie-break `left(bcode,8),emd,sigungu,sido`); would_rows≈5046 불일치 시 거부. ② build_sigungu 입력 staged/navi 통일 또는 `left(bcode,5)` 파생, 7z 부재 시 skip 가드. ③ load-all에 `lawd_dong`·`lawd_sigungu` 단계 편입(**geocode/address 적재 이후** 순서). ④ apply-schema 직후 `lawd_dong=0` 검증쿼리.
- **의존:** 없음(스크립트 작성). 실행 검증은 C4.
- **conductor:** 병렬 가능(파일 disjoint).

### Task A2 — backfill 분리·옵트인 배선
- **Files:** Modify `scripts/postgis/backfill_parcel_jibun.sql`; Create `scripts/postgis/backfill_geom_pt.sql`; Modify `scripts/postgis/load-all.sh`
- **Spec §:** ops C1 / 표기 §4 / review A-2·A-4
- **수용기준:** ① backfill에서 geom_pt 분리(본번/부번/산만), `backfill_geom_pt.sql` 신설. ② `WHERE ji_main IS NULL` 가드(39.9M rewrite 방지). ③ 인덱스 `DROP(parcel_jibun_lookup, ON ONLY 부모)→UPDATE→CREATE` 래핑, work_mem 상향은 self-join 한정(geom_pt seq scan엔 미적용). ④ load-all `STEPS=backfill` **옵트인만**(자동배선 금지). ⑤ int4 가드 보존. EXPLAIN 근거 첨부.
- **의존:** 없음(작성). 실행은 C5.
- **conductor:** 병렬 가능.

### Task A3 — build-studio freshness + QC 지번검증
- **Files:** Modify `scripts/build-studio.py`, `scripts/13-qc-check.py`
- **Spec §:** review A-4(MED, freshness/QC 거짓PASS)
- **수용기준:** ① `TFRESH['load_postgis'].scripts`에 schema/21-parcel-jibun.sql·backfill_parcel_jibun.sql·build_dong_dict.sql·build_sigungu_dict.sh 등록(개선이 stale로 인식되게). ② QC에 `lawd_dong>0`·`lawd_sigungu>0`·`parcel.ji_main NOT NULL ≥95%`·`parcel_jibun_lookup(자식) indisvalid`·골든셋 라이브 1건 추가 → 미달 시 FAIL.
- **의존:** A1·A2(검증 대상 스크립트 참조).
- **conductor:** 병렬 가능(A1/A2 후 권장).

---

## Wave B — 표기/응답 오버홀 (코드 병렬; 계약은 B3가 소유)

### Task B1 — ri/bld_* 스키마 동기 (X5, GO 전제)
- **Files:** Modify `scripts/postgis/schema/10-base.sql`, `scripts/postgis/load_geocode.py`(COLS/DDL/INSERT), `scripts/09-gen-geocode.py`
- **Spec §:** 표기 X5 / F4·F6
- **수용기준:** `ri`·`bld_main_no`·`bld_sub_no` 컬럼 3파일(스키마·로더 COLS·09 산출) **동시** 추가(드리프트 0). 미동기 적재 금지. address_road_addr_idx 계약(main_no/sub_no=도로명 건물본/부번) **의미 이동 금지**(지번 본번은 별개 키).
- **의존:** 없음. (C2 적재 전 머지 필요)
- **conductor:** 단독(스키마·로더 결합).

### Task B2 — 09-gen 빌드 분해컬럼 정정 (X2)
- **Files:** Modify `scripts/09-gen-geocode.py`
- **Spec §:** 표기 X2 / F5(emd 버그=biz/facility 한정)
- **수용기준:** biz/facility `emd`를 법정동/행정동 구분(현재 :214 행정동명만) + 시군구 정규화 + ri 분해 산출. **navi addr 경로(:164) 회귀 금지**(emd=법정동·haeng_dong=행정동 유지).
- **의존:** B1(ri 컬럼).
- **conductor:** 단독(09-gen 단일파일, B1과 순차).

### Task B3 — API 응답 조립 + parcel 수정 + PIP + 에러 (X1·X6·C4)
- **Files:** Modify `server/geocode-api-pg.py`
- **Spec §:** 표기 X1·X6 / ops C4 / review A-3(HIGH)
- **수용기준:** ① `display{main,secondary,full}` + `structure`(소싱 가능 필드 채움, 불가=null) + `category`(기존 `sub` 보존) 전 kind 일관. ② **parcel 경로**: 지목 제거(ji_main/ji_sub/san로 라벨), 지역 복원(lawd_dong/lawd_sigungu dict 조인 → sido/sigungu/emd), b_code 8자리. ③ 비-addr(OSM/biz/place/station) secondary 지역 = `admin_boundary ST_Contains` PIP(X6). ④ 예외: `OperationalError`만→`psycopg.Error`/`Exception` 확장 + 500 JSON + finally `_send` 보장(C4), `/health` degraded. ⑤ 골든셋 회귀(매탄동 1-11/세종대로 110/강남대로 396 동일), SQLi 바인딩·char 캐스팅 보존.
- **의존:** C4(lawd_dong 실값)·C3(admin_boundary)는 **실효검증**에 필요(코드는 선작성 가능). 단위는 cuvia_verify에서.
- **conductor:** 단독(geocode-api-pg.py 단일파일에 X1+X6+C4 결합).

### Task B4 — 프론트 + 문서 (X3)
- **Files:** Modify `demo/js/search.js`, `demo/guide.html`
- **Spec §:** 표기 X3 / review A-1(r.kind)·A-2·A-3
- **수용기준:** ① `r.type`→`r.kind`, `TYPE_KO`에 addr/parcel/facility 키 추가. ② `display.main`(굵게)+`display.secondary`(회색) 렌더, 주소는 지역 보조줄. ③ 키보드 내비+AbortController(race 취소). ④ guide.html: 응답스키마(display/structure)·/health 수치(11.28M)·areas 빈배열·facility kind·에러스키마(400/404/500/503) 갱신.
- **의존:** B3(응답 계약 확정 후).
- **conductor:** 단독.

### Task B5 — 표기 검증 하니스 + 선결게이트 (X4)
- **Files:** Create `scripts/13f-display-parity.py`(또는 유사); Modify 골든셋 데이터
- **Spec §:** 표기 X4 / F17(13d는 좌표/행정동만 비교)
- **수용기준:** name/display 문자열 스냅샷 골든셋(상동 500-1, 강남대로 396, 세종대로 110, 카카오프렌즈, 장생당약국) + 3사 표기 대조 + 선결게이트(`lawd_dong>0 AND ji_main≥95%`). 회귀 0.
- **의존:** B3.
- **conductor:** 병렬 가능(B3 후).

---

## Wave C — 데이터 체인 (대량 DB, 직렬 cap=1, ★사람 승인 게이트)

> 각 단계는 advisory-lock 획득 후 단독 실행. 백그라운드 런처 + 모니터링(`pg_stat_activity.wait_event` + 컨테이너 df).

### Task C1 — D1 전체 백업
- **Spec §:** ops D1. **Files:** Create `scripts/postgis/backup_cuvia.sh`(런처)
- **수용기준:** `docker exec server-postgis-1 pg_dump -Fc -Z6`(=17.5) → `~/geocode-build/backups/cuvia_YYYYMMDD_HHMM.dump`; advisory_lock(911001) 게이트; `pg_restore -l` 통과·size>0. mkdir backups.
- **의존:** 없음. **모든 TRUNCATE/대량작업의 선행 게이트.**

### Task C2 — D2 POI 적재 (=B1 백로그)
- **Spec §:** ops D2 / review B1. 
- **수용기준:** 09 재빌드(`--poi-csv-dir ~/geocode-build/poi-all`, geocode.sqlite 타임스탬프 사본 보존) → `load_geocode.py`(TRUNCATE 재적재). 검증: kind에 biz/facility, poi>0, lawd_* 불변, 골든셋+상호명. 
- **의존:** C1, B1(ri 스키마 머지), B2(빌드컬럼).

### Task C3 — admin_boundary 적재 (신설)
- **Spec §:** review D-2.1 / 표기 X6 전제. **Files:** 확인 후 적재 스크립트(기존 admin 로더 탐색)
- **수용기준:** `admin_boundary>0`(시도/시군구/법정동/행정동 레벨), `/reverse` areas 비어있지 않음. building은 후순위(별도 태스크 보류).
- **의존:** C1. (C2와 병렬 가능하나 직렬 레일 cap=1 준수)

### Task C4 — lawd_dong/lawd_sigungu 재생성 실행
- **Spec §:** ops C2-lawd. 수용기준: A1 스크립트로 재생성, `lawd_dong≈5046`·`lawd_sigungu≈254`. **의존:** C2(address 최종본), A1.

### Task C5 — backfill 실행 (옵트인, 필요시)
- **Spec §:** ops C1. 수용기준: 현재 ji_main 채워져 있으므로 **재실행 불필요가 기본**; 신규/부분 적재 시에만 `STEPS=backfill` 소규모 파티션 검증. EXPLAIN. **의존:** C4, A2.

### Task C6 — D3 geom_pt 보강
- **Spec §:** ops D3. 수용기준: 시도 파티션별 UPDATE(단일 트랜잭션 금지), 대상 파티션 `geom_pt NULL=0`, 골든셋 회귀. **의존:** C2(비동시), A2(backfill_geom_pt.sql). 비차단 후순위.

---

## Wave D — 개선 NOW + NEXT (코드 병렬, 일부 데이터 의존)

> 각 항목 상세·근거는 `geocode-review-개선백로그.md` 해당 B번호 참조.

| Task | 백로그 | Files(요지) | 의존 |
|---|---|---|---|
| D-B6 | B6 nginx 캐시(/geocode·/reverse proxy_cache, reverse 좌표 양자화) | `server/gateway-nginx.conf` | B3(헤더), 좌표양자화 |
| D-B7 | B14 관측성(/metrics·요청로그·/health degraded·reltuples) | `server/geocode-api-pg.py` | B3 |
| D-B8 | B8 `/suggest` 자동완성(prefix+경량+캡, text_pattern_ops 인덱스) | geocode-api-pg.py, schema, search.js | B3, B4 |
| D-B9 | B9 좌표 바이어싱(origin 거리가중+radius) | geocode-api-pg.py, search.js | D-B8 |
| D-B10 | B10 오타 폴백(pg_trgm similarity, 0건시만) | geocode-api-pg.py | D-B8 |
| D-B11 | B11 인기도 가중(rank_weight 정적) | 09-gen, schema, geocode-api-pg.py | **C2(POI)** |
| D-B12 | B12 /reverse 3소스 parity(지번 ST_Contains+도로명+행정/법정동) | geocode-api-pg.py | **C3(admin)** |
| D-B13 | B13 batch(/geocode/batch·/reverse/batch POST) | geocode-api-pg.py | B3, D-B7 |
| D-B15 | B15 검색품질 회귀 게이트(golden 확장+nDCG/MRR) | scripts/13*-bench | B5 |

---

## Wave E — 정리

### Task E1 — SQLite 지오코더(8082) 은퇴
- **Spec §:** ops C3 / review A-2. **Files:** Modify `server/docker-compose.yml`(geocode 서비스+gateway depends_on 제거), 문서.
- **수용기준:** 8082 직접호출 클라이언트 grep 0건 확인 → `docker compose stop geocode` → 골든셋/데모/타일 정상(게이트웨이 18080) → compose에서 서비스+depends_on 제거 커밋. **의존:** B3·B4 안정 후(13d parity가 SQLite↔PG 비교라 X1이 PG name 바꾸면 의미 약화).

---

## conductor 배정 (3개: [17][18][19])

- **코드 레일(병렬, 최대 3)**: Wave A(A1·A2·A3) → Wave B(B1→B2 순차, B3, B4, B5) → Wave D 코드 항목 → E1. 파일 disjoint 단위로 분배.
- **데이터 레일(직렬 cap=1)**: Wave C(C1→C2/C3→C4→C5→C6). conductor 1개가 advisory-lock 잡고 순차. ★각 파괴적 단계는 사람 승인.
- **레이트리밋(7d 78%)**: Wave C 대량 DB작업은 리셋(내일 17:00) 전후로 페이싱. Wave D NEXT는 핵심(A·B·C) 안정 후 착수.

---

## Self-Review (스펙 커버리지)

- 표기 X1~X6 → B3(X1·X6·C4)·B2(X2)·B4(X3)·B5(X4)·B1(X5) ✓
- 후속처리 D1~D3·C1~C4 → C1~C6·A1·A2·A3 ✓
- review HIGH(X-1 lawd_dong/X-2 backfill고아/A-4 freshness·QC/A-2 admin0·TRUNCATE가드/A-3 예외·parcel) → A1·A2·A3·C1·C3·B3 ✓
- 개선 NOW+NEXT → Wave D + B3(place_id/법정행정동 흡수) ✓
- admin 신설 → C3 ✓
