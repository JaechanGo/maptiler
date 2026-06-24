<!-- 생성: geocode-followup-ops-spec 워크플로우 (11 agents, ~911k tok, 라이브 검증 2026-06-24, 적대검토 2렌즈 반영) -->

# 후속처리 운영 스펙 (Geocode/PostGIS) — D1~D3 · C1~C4

> 본 문서는 테크리드 종합 운영 스펙이다. 적대검토에서 지적된 **명령오류·안전갭·롤백누락·의존성**을 모두 반영했고, 핵심 사실은 라이브(2026-06-24)로 재확인했다. 라이브 검증 결과는 본문에 명시한다. 모든 명령은 **읽기전용/설계 산출물 원칙**을 따르며, 실제 TRUNCATE/대량UPDATE/덤프/컨테이너변경은 사람 승인 후 단독 직렬로 수행한다.

---

## 0. 개요

### 0.1 목적
대량 적재(POI)·대량 UPDATE(지번 backfill, geom_pt 보강)·인프라 정리(SQLite 지오코더 은퇴)·API 견고화를 **데이터 손실 없이, 두 번 다시 1시간+ 재적재하지 않도록** 운영한다.

### 0.2 라이브 기준선 (검증됨, 2026-06-24)
| 항목 | 값 | 비고 |
|---|---|---|
| parcel | 39,882,449 | 18 LIST 파티션(parcel_11~52 + default), `geom_pt` 전부 NULL |
| address | 11,281,251 | addr 10,686,547 / poi(OSM) 406,064 / road 165,601 / place 21,668 / station 1,371 |
| poi | **0** | biz/facility 미적재 |
| lawd_dong | 5,046 / lawd_sigungu | 254 |
| admin_boundary | **0** / building | **0** (→ `/reverse` areas 항상 `[]`, 별도 적재 태스크 필요) |
| PostgreSQL 서버 | **17.5** (컨테이너) | `server-postgis-1` |
| 호스트 pg_dump | **18.4** | **서버(17.5)와 메이저 불일치 — D1 안전갭 핵심** |
| 컨테이너 메모리 | 3.30/7.65 GiB, CPU 5% | work_mem 32MB / maintenance_work_mem 512MB / shared_buffers 1GB |
| 컨테이너 디스크 | 453G 중 55G(13%) 사용, 375G 여유 | tuple rewrite 일시 2배 팽창 대비 |
| backups/ | **부재(0건)** | D2/C2/D3 TRUNCATE/대량작업의 무조건 선행 |
| 컨테이너(live) | server-postgis-1, geocode-pg-1(8092), geocode-1(8082, SQLite), gateway-1(18080), martin-1, tileserver-1(8080) | 전부 Up |
| `parcel_jibun_lookup` 실측 indexdef | `CREATE INDEX parcel_jibun_lookup ON ONLY public.parcel USING btree (emd_cd, ji_main, ji_sub)` | **ON ONLY 부모 인덱스** |
| 지번질의 EXPLAIN 실측 | `Index Scan using parcel_41_emd_cd_ji_main_ji_sub_idx on parcel_41` | **부모명은 plan 에 안 나타남** |
| lawd_sigungu.sigungu_nm 실측 | `동구`/`구리시`/`평창군`/`공주시`/`서초구` (단형, 시도명 없음) | address.sigungu(`남구`/`동구`…)와 포맷 상이 |

### 0.3 적대검토 반영 요약(핵심 정정 12건)
1. **(C1)** 인덱스 재생성은 `ON ONLY public.parcel USING btree` 실측본과 일치(또는 검증기준을 "컬럼·순서 동치"로 완화). `DROP INDEX parcel_jibun_lookup`은 부모 인덱스를 떨구므로 ON ONLY/ATTACH 메커니즘을 롤백에 반영.
2. **(D3/C2)** EXPLAIN 기대 인덱스명을 자식 파티션명 `parcel_<sido>_emd_cd_ji_main_ji_sub_idx`(또는 `on parcel_<sido>`)로 정정. `parcel_jibun_lookup` grep은 항상 실패.
3. **(전반)** 모든 SQL 검증을 `.sql 파일 + psql -f`로 통일. `$$dollarquote$$`·`\047`·`<` 리다이렉트는 zsh `_safe_eval`에서 깨짐(라이브 재현). 셸 호출은 `env bash <launcher.sh>` 형태 권장.
4. **(D1)** 백업/복원을 **컨테이너 내부 pg_dump(=17.5)** 로 수행(`docker exec server-postgis-1 pg_dump`). 호스트 18.4 단독 강제는 17.x 복원 비호환 위험.
5. **(D1)** 동시금지를 "확인"에서 **강제 게이트(pg_advisory_lock + 최신덤프 mtime<24h·size>0·pg_restore -l 통과)** 로 승격.
6. **(C2)** `build_dong_dict.sql` `ORDER BY`에 tie-break(`left(bcode,8), emd, sigungu, sido`) 추가로 멱등화. would_rows≈5046 자동 비교 게이트(불일치 시 INSERT 거부).
7. **(C2)** backup STEP을 geocode(TRUNCATE) **앞**에도 배치(또는 -t에 address/poi 포함). 맨끝 단독 backup은 address 소실 시 복원 불가.
8. **(DAG)** `lawd_dong` 재생성(C2-lawd)을 **D2(address 최종 적재) 이후**로 강제. conductor 배정의 "C2적재→D2"(C2를 앞) 모순 해소.
9. **(D3)** 전국 단일 트랜잭션(권장-B)을 **금지로 강등**, 파티션별(권장-A)만 유지. `work_mem 2GB` SET 제거(seq scan이라 무효·OOM 표면만 키움). 수용기준 "전건 NULL=0"을 "대상 파티션 NULL=0"으로 완화.
10. **(D2)** `geocode.sqlite`를 재빌드 직전 타임스탬프 사본으로 보존(atomic rename은 "성공 후 불량"을 못 막음).
11. **(C4)** to_regclass 점검을 `public.` 스키마 한정. except 순서(Operational→Programming→Error) 단위테스트를 수용기준에 추가. `/health` degraded 도입 시 외부 헬스체크 임계 동반 조정 노트.
12. **(전반)** sigungu 토큰 동반 골든셋(`수원시 영통구 매탄동 1-11`) 추가로 sigungu_nm 단형 매칭 회귀 검증. **admin_boundary 적재 태스크 신설**(현재 0행 → /reverse 항구 빈값).

---

## 1. 실행 순서 DAG (표기 X와의 관계 포함)

### 1.1 그래프
```
[게이트]            [대량 DB 직렬 레일 — 동시성 cap=1]                 [실효검증]
                 ┌────────────────────────────────────────────────────┐
 D1(백업)  ──────┤                                                      │
  (advisory      │  X5-적재 → D2(TRUNCATE+재적재) → C2-lawd → C1-backfill → D3(geom_pt 부분)
   lock 게이트)  │      ▲                ▲              ▲          ▲           ▲
                 └──────┘                │              │          │           │
                                         │              │          │           │
  X5(ri 3파일 동기) ─→ D2         (ri 컬럼이 적재 COLS/DDL/INSERT에 선행; 현 스키마엔 ri 없음=현재 안전)
  D2(address 적재) ─→ C2-lawd     (lawd_dong은 address.bcode 기반 → address 최종본 뒤에 생성)  ★DAG 정정
  C2(lawd_dong) ─→ X1 실효검증     (lawd_dong WHERE emd=%s 공집합이면 parcel sido/sigungu/emd 영구 미충족)
  C1(ji_main/ji_sub) ─→ X1·D3 검증 (ji_main NULL이면 parcel SELECT 0건 무음실패 — D3 골든셋도 C1에 암묵 의존)
  C2 + C1 ─→ X4 선결게이트         (lawd_dong COUNT>0 AND parcel.ji_main NOT NULL ≥임계)
```
```
[코드 작성 — 병렬 그룹 A, 파일 disjoint]
  {X1 + X6 + C4}  = server/geocode-api-pg.py   (동일파일 → 한 conductor 내부 순차)
  X2              = scripts/09-gen-geocode.py
  X5(작성)        = schema/10-base.sql + load_geocode.py + 09-gen
  C2(작성)        = build_dong_dict.sql + build_sigungu_dict.sh + load-all.sh + build-studio.py
  C1(배선)        = backfill_parcel_jibun.sql + load-all.sh STEPS
  X3(인터페이스 합의 후) = demo/js/search.js + 문서 ;  X4 = 골든셋·게이트 정의
  X1·X3 안정화 ─→ C3 (SQLite 은퇴, 최후행; 13d parity가 SQLite↔PG name 비교라 X1이 PG name 바꾸면 의미 약화)
```

### 1.2 X 태스크와의 관계 (흡수/선후)
| X | 정의 | 본 D/C와의 관계 |
|---|---|---|
| X1 | API 분해응답 + parcel 버그수정(런타임) | **C4 ⊂ X1**(동일파일 동일 PR), **X6 ⊂ X1**. C2-lawd·C1 실효검증이 X1로 수렴 |
| X2 | 빌드 산출컬럼 정정(biz/facility+ri) | X5→X2. D2의 09-gen 재빌드와 인접(파일 동일 09-gen) |
| X3 | 프론트 + 문서(런타임) | X1 인터페이스 확정 후. C3의 8082 직결 문서 갱신과 겹침 |
| X4 | 검증/회귀 하니스 + 선결게이트 | **선결게이트 = lawd_dong COUNT>0 AND parcel.ji_main NOT NULL** → C2·C1 완료 전제 |
| X5 | ri 스키마·적재 3파일 동기[GO 전제] | **X5→D2**(미동기 시 COLS/DDL/INSERT 컬럼 드리프트). 현재 load_geocode COLS에 ri 없음=현 스키마 정합(현재 안전) |
| X6 | admin_boundary PIP 헬퍼 | **X6 ⊂ X1**. 단 admin_boundary=0이라 별도 **admin 적재 태스크(신설, 본 스펙 §10 리스크)** 가 선행돼야 실효 |

### 1.3 직렬/병렬 근거
- **병렬 그룹 A(코드)**: 파일 충돌 없는 단위로 동시 작성 가능. conductor ≤ 2.
- **직렬 그룹 B(대량 DB)**: 동시성 **cap=1**. 과거 16-에이전트 fan-out 풀스캔 → 호스트 OOM(load 259, RAM ~10MB) 재발 방지. `pg_advisory_lock(정수키)` 단일 게이트(D1에 흡수)로 강제.
- **게이트 근거**: (1) D1이 모든 TRUNCATE/대량UPDATE의 **무조건 선행**. (2) **D2→C2-lawd**(lawd_dong은 address 최종본 기반). (3) **C1→D3/X1**(ji_main 먼저). (4) geom_pt는 런타임 COALESCE 폴백이라 **비차단 후순위**.

---

## 2. 태스크별 상세

> 표기 규칙: 모든 검증 SQL은 `*.sql` 파일로 두고 `psql -f`로 전달. 셸은 `env bash <launcher.sh>`(zsh `_safe_eval` 회피). psql/pg_dump는 변수에 담지 말고 직접 호출. **백업/복원·검증 psql은 컨테이너 내부(`docker exec server-postgis-1 ...`, =17.5)** 사용을 1순위로 한다(버전 정합).

---

### D1 — PostGIS 전체 DB 백업 (pg_dump -Fc -Z6)

**목적**: 대량 적재/UPDATE(특히 load_geocode의 address+poi TRUNCATE) 직전에 cuvia DB 전체를 custom-format(-Fc -Z6)으로 호스트(Docker 볼륨 밖·iCloud 밖)에 백업해 분 단위 복원점 확보. 읽기/덤프만, DB 무수정.

**선행**
- `server-postgis-1` Up + `docker exec server-postgis-1 pg_isready -U cuvia -d cuvia` 통과.
- 백업 디렉터리 `~/geocode-build/backups/` 생성(`mkdir -p`). 호스트 로컬, pgdata 볼륨 밖, iCloud 밖.
- 동시 대량작업 부재 — **확인이 아니라 강제**: `pg_advisory_lock(911001)` 획득 실패 시 대기/중단(아래 단계 1).
- 컨테이너 df 여유 확인(컨테이너 기준).

**단계 + 명령** (실행은 백그라운드 런처 + 사람 승인 후)
1. **동시성 강제 게이트 + 사전점검**
   ```bash
   # launcher: d1_guard.sh
   #!/bin/bash
   set -euo pipefail
   # advisory lock 단일 게이트 (대량 DB작업 cap=1)
   docker exec server-postgis-1 psql -U cuvia -d cuvia -tAc \
     "SELECT pg_try_advisory_lock(911001)" | grep -qx t || { echo "LOCK busy — 다른 대량작업 진행중. 중단"; exit 1; }
   docker exec server-postgis-1 pg_isready -U cuvia -d cuvia
   docker exec server-postgis-1 df -h /var/lib/postgresql/data
   docker exec server-postgis-1 pg_dump --version   # = 17.5 정합 확인(호스트 18.4 아님)
   mkdir -p ~/geocode-build/backups; df -h ~/geocode-build   # 호스트 백업폴더 여유(1차 disk-full 지표)
   ```
   > 정정(적대검토 HIGH): **컨테이너 내부 pg_dump(17.5)** 로 덤프해 서버 버전 정합. 호스트 18.4로 뜬 -Fc 아카이브는 17.x pg_restore 복원 거부 위험.
2. **DB 크기·테이블 크기 측정**(컨테이너 df 기준) — `db_size.sql` 파일로:
   ```sql
   SELECT pg_size_pretty(pg_database_size('cuvia'));
   SELECT relname, pg_size_pretty(pg_total_relation_size(c.oid))
   FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
   WHERE n.nspname='public' AND c.relkind IN ('r','p')
   ORDER BY pg_total_relation_size(c.oid) DESC LIMIT 12;
   ```
   `docker exec -i server-postgis-1 psql -U cuvia -d cuvia -P pager=off -f /dev/stdin`(파일 마운트) 또는 `docker cp` 후 `-f`.
3. **활성 세션 점검** — `state<>'idle'` **AND martin MVT(ClientRead)** 도 관찰(I/O 경합 가드, 적대검토 HIGH):
   ```sql
   SELECT pid,state,wait_event_type,wait_event,left(query,80)
   FROM pg_stat_activity WHERE datname='cuvia' AND pid<>pg_backend_pid();
   ```
4. **백그라운드 덤프 런처**(컨테이너 내부 pg_dump):
   ```bash
   # d1_dump.sh (run_in_background)
   #!/bin/bash
   set -euo pipefail
   TS=$(date +%Y%m%d_%H%M%S)
   OUT=/var/lib/postgresql/data/_dump_${TS}.dump          # 컨테이너 내부 임시 → 호스트로 cp
   LOG=$HOME/geocode-build/backups/dump_${TS}.log
   echo "[start] $(date)" >> "$LOG"
   docker exec server-postgis-1 pg_dump -U cuvia -d cuvia -Fc -Z6 --no-owner --no-privileges -v -f "$OUT" >> "$LOG" 2>&1
   docker cp server-postgis-1:"$OUT" ~/geocode-build/backups/cuvia_${TS}.dump >> "$LOG" 2>&1
   docker exec server-postgis-1 rm -f "$OUT"
   echo "[done] $(date) size=$(du -h ~/geocode-build/backups/cuvia_${TS}.dump | cut -f1)" >> "$LOG"
   ```
   > 컨테이너 내부로 덤프 후 `docker cp`로 호스트(iCloud 밖)로 이관. (대안: 호스트 18.4로 직접 덤프하되 **복원도 18.x 클라이언트 강제** 명시.)
5. **모니터링**: `tail -n20 dump_*.log` + `ls -lh *.dump` 크기 증가 + wait_event(DataFileRead/CPU=정상). foreground sleep 금지 → Monitor/until 루프.
6. **무결성 검증**(복원 없이 TOC만, 컨테이너 17.5 pg_restore):
   ```bash
   docker cp ~/geocode-build/backups/cuvia_<TS>.dump server-postgis-1:/tmp/v.dump
   docker exec server-postgis-1 pg_restore --list /tmp/v.dump | grep -E "TABLE (DATA )?(public )?(parcel|address|poi|lawd_dong|lawd_sigungu)" | head -40
   ```
7. **(선택) 부분 덤프**(parcel + 사전): `-t parcel -t lawd_dong -t lawd_sigungu` — 부모 -t parcel 지정 시 18 파티션 자동 동반(단, **pg_restore -l로 parcel_11~52 TABLE DATA 존재 확인**, PG 버전별 자식 누락 가능 시 자식 개별 -t).

**변경파일**: `~/geocode-build/backups/cuvia_<TS>.dump`, `dump_<TS>.log` (레포 외부 산출물). 런처 스크립트 산출.

**수용기준**
- `cuvia_<TS>.dump` 생성 + 크기>0.
- `pg_restore --list`가 오류 없이 TOC 출력 + parcel/address/poi/lawd_dong/lawd_sigungu 항목 존재.
- 로그 마지막 줄 `[done]` + size 기록, pg_dump 종료코드 0.
- 백업이 호스트 로컬(pgdata 볼륨 밖, iCloud 밖).
- 백업 전후 행수 불변(읽기): parcel 39,882,449 / address 11,281,251 / lawd_dong 5046 / lawd_sigungu 254.
- **호스트 백업폴더 df가 disk-full 미도달**(1차 지표로 승격) + 컨테이너 df 정상.

**검증**
| check | expected |
|---|---|
| `test -s cuvia_<TS>.dump && echo NONEMPTY` | NONEMPTY |
| `docker exec ... pg_restore --list /tmp/v.dump \| grep -Ec "TABLE.*(parcel\|address\|poi\|lawd_dong\|lawd_sigungu)"` | ≥5 |
| `docker exec ... psql -tAc "SELECT count(*) FROM parcel"` | 39882449 |
| address/lawd_dong/lawd_sigungu count | 11281251 / 5046 / 254 |
| `tail -n3 dump_<TS>.log` | `[done] ... size=...` (스택트레이스 없음) |

**롤백**: 읽기 작업이라 DB 롤백 불필요. 실패/중단 시: 부분 .dump 삭제(손상 덤프 오인 방지), 백그라운드 pg_dump 중단 안전(읽기 트랜잭션), 디스크 부족 시 폴더 정리 후 재시도/부분덤프. **advisory lock 해제** `SELECT pg_advisory_unlock(911001)`. **절대 금지**: 공간확보 목적 Docker `diskSizeMiB` 직접편집/재시작(데이터 손실 직접 원인) — GUI 리사이즈 + 사전백업만.

**모니터링**: `tail -f dump_<TS>.log` + `.dump` 크기 증가; wait_event(DataFileRead/CPU=정상); 컨테이너 df + 호스트 백업폴더 df; **martin MVT 트래픽 I/O 경합 관찰**(현재 CPU 5%라 평시 무방). 완료 신호=로그 `[done]`.

**안전**: 컨테이너 pg_dump(17.5) 1순위(버전 정합). 직렬 실행(advisory lock). load_geocode는 D1 완료 후만. 산출물 iCloud 금지. `diskSizeMiB` 직접편집 금지. work_mem 불필요(덤프는 정렬/인덱스 무관). 읽기전용(restore 별도).

**의존**: 없음(D1 자체 무의존). D1은 D2/C2/C1/D3의 무조건 선행 게이트.

**conductor**: 단독(직렬). 후속 대량작업의 depends-on. fan-out 금지. advisory lock 단일 게이트를 D1이 보유→해제는 다음 대량작업 시작 직전.

---

### D2 — POI(biz/facility) 적재 (09-gen 재빌드 `--poi-csv-dir` → load_geocode 재적재)

**목적**: 소상공인 상가/인허가/생활편의 POI를 `poi`(martin 타일)·`address`(상호명·업종 검색)에 적재. 현재 poi=0, address엔 OSM POI(406,064)만. 정식 파이프라인으로 dedup·카테고리 표준화·시설 지오코딩 일관 처리.

**선행**
- **D1(백업) 강제 게이트(정정)**: `~/geocode-build/backups`에 **mtime<24h AND size>0 AND `pg_restore --list` 통과** 덤프 존재. 디렉터리 존재만으로 통과 금지(적대검토). 미충족 시 중단.
- compose postgis 가동(server-postgis-1·geocode-pg).
- 호스트 psql/pg_dump → `/opt/homebrew/opt/libpq/bin` PATH 선두(load_geocode 내부 `psql` 직접 호출). **단, 검증 psql은 컨테이너 17.5 사용**.
- `test -f ~/geocode-build/geocode.sqlite`(파일이어야 함; 디렉터리면 09 rename IsADirectoryError).
- 입력 CSV: `~/geocode-build/poi-all/{localdata_clean.csv, facility_clean.csv, sangga/*.csv}`.
- advisory lock(911001) 획득(직렬 cap=1).
- **X5 미적용 상태 확인**: load_geocode COLS에 `ri` 없음(현 스키마 정합=현재 안전). X5 적용 시엔 X5→D2 순서 강제.

**단계 + 명령**
1. **인자 재확인(--help)**: `09-gen-geocode.py`(--src/--osm/--out/--only/--poi-csv-dir/--dedup/--areas), `load_geocode.py`(--db).
2. **사전 상태/백업 게이트(정정)** — 디렉터리 존재가 아니라 덤프 무결성 검증:
   ```bash
   # d2_pre.sh
   D=$(ls -t ~/geocode-build/backups/cuvia_*.dump 2>/dev/null | head -1)
   [ -n "$D" ] && [ -s "$D" ] || { echo "백업 없음/빈파일 — D1 미완. 중단"; exit 1; }
   AGE=$(( ($(date +%s) - $(stat -f %m "$D")) / 3600 ))
   [ "$AGE" -lt 24 ] || { echo "덤프 stale(${AGE}h>24h). 중단"; exit 1; }
   docker cp "$D" server-postgis-1:/tmp/v.dump
   docker exec server-postgis-1 pg_restore --list /tmp/v.dump >/dev/null || { echo "덤프 손상. 중단"; exit 1; }
   test -f ~/geocode-build/geocode.sqlite || { echo "geocode.sqlite 파일 아님/없음. 중단"; exit 1; }
   ```
   + 기준값 캡처: address kind별 count, poi=0, lawd_dong=5046, lawd_sigungu=254.
3. **geocode.sqlite 스냅샷(신설, 적대검토)**: 재빌드 직전 `cp ~/geocode-build/geocode.sqlite ~/geocode-build/geocode.sqlite.pre-d2-$(date +%Y%m%d_%H%M%S)` — atomic rename은 "실패시 보존"만 보장, "성공 후 불량"엔 무력.
4. **STEP 1 재빌드**(로컬 python, DB 무접촉):
   ```bash
   python3 scripts/09-gen-geocode.py \
     --src ~/geocode-build/staged/navi --osm ~/geocode-build/osm.sqlite \
     --out ~/geocode-build/geocode.sqlite --poi-csv-dir ~/geocode-build/poi-all
   ```
   `--poi-csv-dir`이 biz/facility 포함의 유일 트리거. 로그에 `biz: +N`, `facility 도로 X + 지번폴백 Y` 확인.
5. **STEP 1.5 재빌드 산출물 사전검증**(쓰레기 적재 방지) — sqlite read-only:
   `places`에 `kind='biz','facility'` > 0 이어야 STEP 2 진행. 0이면 적재 금지(TRUNCATE만 일어나 poi 공백).
6. **STEP 2 재적재**(`set -e` + STEP1.5 fail-fast 게이트 후에만):
   ```bash
   export PATH=/opt/homebrew/opt/libpq/bin:$PATH
   export PGHOST=localhost PGPORT=5433 PGUSER=cuvia PGDATABASE=cuvia PGPASSWORD=cuvia
   export GEOCODE_MAINT_MEM=1GB GEOCODE_MAINT_WORKERS=4
   python3 scripts/postgis/load_geocode.py --db ~/geocode-build/geocode.sqlite
   ```
   GEOCODE_MAINT_MEM=1GB 보수(컨테이너 7.6/8GB + **martin 트래픽 동반** OOM 위험). trgm GIN 재생성 최대 병목.
7. **적재 후 검증**(컨테이너 psql): address kind별(biz/facility 신규 + 기존 보존), poi>0(biz·facility만), lawd_* 불변, 인덱스 전부 존재(`\di poi*`, `\di address_*`).
8. **골든셋 + 상호명 회귀**(게이트웨이 :18080): 매탄동 1-11→(127.0581,37.2711), 세종대로 110→서울시청, 강남대로 396→강남역, 상호명(스타벅스)→kind biz/facility, `:8092/health`.

**변경파일**: `geocode.sqlite`(재생성), `geocode.sqlite.pre-d2-<ts>`(스냅샷, 신설), `style/poi-taxonomy.json`, PostGIS `address`(TRUNCATE→재적재), `poi`(0→>0), 로그 `d2_build.log`/`d2_load.log`.

**수용기준**
- address.kind에 biz/facility 신규 + 기존 addr(~10,686,547) 보존.
- poi count>0, kind in (biz, facility)만.
- lawd_dong=5046, lawd_sigungu=254 불변.
- 골든셋 3건 통과 + 상호명 질의 kind biz/facility 반환.
- poi/address 2차 인덱스 전부 재생성.
- **D1 백업 산출물이 적재 시작 전 무결성 검증 통과(롤백 가능 상태)** + **geocode.sqlite 스냅샷 존재**.

**검증**: address kind GROUP BY(biz/facility 신규), `count(*) FROM poi`>0, lawd 불변, `\di`, 골든셋 curl(URL 인코딩), `:8092/health` places=address count.

**롤백**: **D1 백업으로 복원(컨테이너 17.5 pg_restore)**.
```bash
docker cp ~/geocode-build/backups/<D1덤프> server-postgis-1:/tmp/r.dump
docker exec server-postgis-1 pg_restore -U cuvia -d cuvia --clean --if-exists -t address -t poi /tmp/r.dump
```
- address `search_text`는 GENERATED STORED + pg_trgm 의존 → 동일 DB 복원이라 확장 존재(현실 위험 낮음, 명시).
- geocode.sqlite는 atomic rename(직전 실패 보존) + **스냅샷(성공 후 불량 시 복귀)** 둘 다 가용.
- 단일 트랜잭션 중단은 전부 롤백(정상) — 중간 count=0을 실패로 오판 금지. 복원 후 STEP7·8 재검증.

**모니터링**: wait_event(CPU/DataFileRead/Write/temp), 컨테이너 df, `docker stats`(OOM), 로그 tail. trgm GIN 재생성이 maintenance_work_mem 민감 → OOM 징후 시 GEOCODE_MAINT_MEM=512MB 하향 재시도. **재적재 시 martin 부하 동반** 명시.

**안전**: D1 무결성 게이트 없으면 실행 금지. 직렬(advisory lock). GEOCODE_MAINT_MEM 1GB·세션 RESET. bash 런처·psql 직접호출. geocode.sqlite 파일 확인. `diskSizeMiB` 금지. 산출물 iCloud 금지. parcel/backfill 미접촉. **STEP1.5 fail이면 STEP2 강제 중단**(set -e + 명시 게이트).

**의존**: **D1(무결성 검증 통과)**. (X5 적용 환경이면 X5→D2 강제; 현재 미적용=안전.)

**conductor**: 단독(직렬). D1 완료 후만. STEP4(재빌드)→STEP6(재적재) 내부 순차. DB 점유 유일 작업으로 스케줄.

---

### D3 — parcel.geom_pt 대표점 보강 (ST_PointOnSurface, 파티션별 점진)

**목적**: parcel geom_pt(전부 NULL)를 ST_PointOnSurface(geom) 대표점으로 채워 질의시 즉석계산 제거 + 마커가 항상 필지 내부. 런타임 `COALESCE(geom_pt, ST_PointOnSurface(geom))`(geocode-api-pg.py:193) 폴백이라 **비차단**. 읽기전용 설계 산출물(실행 금지).

**선행**
- server-postgis-1 Up. **검증 psql은 컨테이너(17.5)**.
- `geom_pt geometry(Point,4326)` 컬럼 존재(21-parcel-jibun.sql). 부재 시 apply-schema 선행.
- 18 파티션 + default 존재.
- **D1 백업 완료(advisory lock 게이트)**. backups/ 생성.
- **C1(ji_main/ji_sub backfill) 완료(신설 의존)** — geom_pt만 채우고 ji_main NULL이면 지번검색 0건. D3 골든셋(매탄동 1-11) 통과는 ji_main 충족 전제.
- 다른 대량작업 부재(직렬 cap=1).

**단계 + 명령** (실행 금지 — 설계)
1. 백업 디렉터리 + parcel/사전 부분덤프(D1 우산 하 — 컨테이너 pg_dump):
   `docker exec server-postgis-1 pg_dump -U cuvia -d cuvia -Fc -Z6 -t parcel -t lawd_dong -t lawd_sigungu -f /var/lib/postgresql/data/_parcel_$(date +%Y%m%d).dump` 후 `docker cp`.
2. 파티션 목록 + 크기(`pg_inherits` 조인) — `.sql 파일 -f`.
3. NULL 베이스라인: `SELECT count(*) total, count(*) FILTER (WHERE geom_pt IS NULL) FROM parcel;` (시작 ≈ 전부 NULL).
4. EXPLAIN(ANALYZE 아님) 단일 소규모 파티션(parcel_36 세종): `EXPLAIN UPDATE parcel_36 SET geom_pt = ST_PointOnSurface(geom) WHERE geom_pt IS NULL;` → Seq Scan만(self-join/temp 없음, geom_pt 인덱스 없음).
5. **[권장-A 유일 — 파티션별 순차]**(work_mem SET 제거, 정정):
   ```bash
   # d3_partition.sh — work_mem 상향 제거(seq scan이라 무효·OOM 표면만 키움)
   for P in parcel_36 parcel_29 parcel_30 parcel_31 parcel_28 parcel_27 parcel_50 parcel_26 \
            parcel_11 parcel_43 parcel_51 parcel_44 parcel_52 parcel_46 parcel_48 parcel_41 \
            parcel_47 parcel_default; do
     echo "== $P =="
     docker exec server-postgis-1 psql -U cuvia -d cuvia -v ON_ERROR_STOP=1 \
       -c "UPDATE $P SET geom_pt = ST_PointOnSurface(geom) WHERE geom_pt IS NULL;"
   done
   ```
   각 psql 독립 트랜잭션 → 점진 커밋·관찰·재개. `WHERE geom_pt IS NULL`이 멱등 재개 보장. 파티션 이름은 단계2 출력으로 확정.
6. **[권장-B 전국 단일 트랜잭션 — 금지(정정)]**: runbook 안전제약과 정면 충돌(전국 일괄 금지). 정식 옵션에서 제거. 30~70분 단일 트랜잭션은 디스크 2배 팽창 + 중단 시 전체 롤백. backfill_parcel_jibun.sql에 geom_pt 재합치기 금지(89→47분 최적화 핵심).
7. 진행 모니터링(다른 세션): `pg_stat_activity` wait_event(빈값/DataFileRead/Write=정상, Lock=동시충돌 의심) + `now()-query_start` + 컨테이너 df.
8. 완료 후 `ANALYZE parcel;`.
9. (선택) geom_pt GiST 인덱스 — 현 질의는 geom_pt를 좌표추출(ST_X/Y)에만 사용 → 기본 생략.

**변경파일**: 없음(설계). 실행 시 parcel.geom_pt 갱신(파생 컬럼).

**수용기준(정정)**
- **대상(완료) 파티션** `count(*) FILTER (WHERE geom_pt IS NULL) = 0`("전건 NULL=0"에서 완화 — 점진 커밋 허용).
- geom_pt 모두 geometry(Point,4326) + ST_PointOnSurface 정의상 필지 내부/경계.
- 골든셋 매탄동 1-11→(127.0581,37.2711) (geom_pt와 폴백 동일).
- 지번 EXPLAIN에서 **자식 파티션 `parcel_<sido>_emd_cd_ji_main_ji_sub_idx` Index Scan**(정정 — `parcel_jibun_lookup` 아님) + geom_pt 직접 사용.
- 비차단: 보강 전/중/후 모두 지번질의 정상(COALESCE 폴백).
- 동시 UPDATE 단 1건(직렬).

**검증(정정)**
| check | expected |
|---|---|
| `SELECT count(*) FILTER (WHERE geom_pt IS NULL) FROM parcel_36;` | 0 (완료 파티션) |
| `SELECT count(*) FILTER (WHERE NOT ST_Intersects(geom, geom_pt)) FROM parcel WHERE geom_pt IS NOT NULL;` | 0 |
| `curl ".../geocode?q=매탄동%201-11&limit=1"` (18080) | lon≈127.0581 lat≈37.2711 |
| `EXPLAIN ... parcel WHERE sido_cd=ANY('{41}'::char(2)[]) AND emd_cd=ANY('{41117101}'::char(8)[]) AND ji_main=1 AND ji_sub=11` | **`Index Scan using parcel_41_emd_cd_ji_main_ji_sub_idx on parcel_41`** (라이브 실측) |

**롤백(정정)**: geom_pt는 파생 컬럼 + COALESCE 폴백이라 부분 채움도 검색 정상.
- **NULL 복원 금지 권고**: `UPDATE parcel SET geom_pt=NULL`은 또 다른 39.9M rewrite(동일 비용·OOM). 어느 파티션까지 커밋됐는지 NULL 분포 쿼리로 **식별→재개**만 하면 됨.
- 데이터 손상 의심 시에만 D1/부분덤프 복원(컨테이너 pg_restore).
- 선택 GiST 인덱스 `DROP INDEX IF EXISTS parcel_geompt_gix;`.
- 다른 인덱스/제약 무접촉.

**모니터링**: wait_event + 경과시간 + 컨테이너 df(tuple rewrite dead tuple, 80%≈362G 경보선). 파티션별은 커밋 진행을 파티션 단위 NULL 감소로 관찰. 예상 30~70분.

**안전(정정)**: 읽기전용 스펙. zsh 회피. **전국 일괄 금지(권장-B 강등)**. **work_mem SET 제거**(seq scan 무효). 작업 전 백업(D1 게이트). geom_pt를 backfill에 재합치기 금지. parcel 인덱스 임의 삭제 금지. `diskSizeMiB` 금지. 산출물 iCloud 금지.

**의존(정정)**: D1(백업), schema 21-parcel-jibun.sql, **C1(ji_main backfill) 선행(신설)**, D2 후순위(비차단).

**conductor**: 단독(직렬 cap=1). 파티션 18개 fan-out 금지. D1·C1 완료 후. 권장순서 백업→POI→backfill→geom_pt.

---

### C1 — backfill_parcel_jibun.sql 최적화 리팩터 (work_mem + geom_pt 분리 + 인덱스 drop/recreate)

**목적**: backfill을 (a) work_mem 상향으로 self-join temp 스필 제거, (b) geom_pt 연산을 `backfill_geom_pt.sql`로 분리, (c) `parcel_jibun_lookup` DROP→UPDATE→CREATE로 39.9M 증분갱신을 일괄빌드 전환 — 89분→~30분. int4 가드(length≤7) 보존. **스펙/리팩터 산출, 전국 39.9M 재실행 금지**(소규모 파티션 dry-run + EXPLAIN만).

**선행**
- server-postgis-1 Up(컨테이너 7.6/8GB). 컨테이너 psql.
- parcel 컬럼 ji_main/ji_sub/san/geom_pt 존재.
- **실측 indexdef 캡처(추측 금지)**: `CREATE INDEX parcel_jibun_lookup ON ONLY public.parcel USING btree (emd_cd, ji_main, ji_sub)` (라이브 확인).
- geom_pt 인덱스 0건 확인.
- 동시 대량작업 부재(advisory lock).

**단계 + 명령**
1. 현행 backfill_parcel_jibun.sql 재검증(라이브 확인): `SET maintenance_work_mem='512MB'` 만, work_mem 미상향, 단일 UPDATE에 self-join + `geom_pt = ST_PointOnSurface(p.geom)` 포함, int4 가드 `length((s.m)[1]) <= 7` / 부번 가드 보유, 끝 `ANALYZE parcel`.
2. **실측 indexdef 캡처**(재생성과 1:1):
   ```bash
   docker exec server-postgis-1 psql -U cuvia -d cuvia -Atc \
     "SELECT indexdef FROM pg_indexes WHERE indexname='parcel_jibun_lookup'"
   # → CREATE INDEX parcel_jibun_lookup ON ONLY public.parcel USING btree (emd_cd, ji_main, ji_sub)
   ```
3. **산출(1) `backfill_geom_pt.sql` 신규**(geom_pt 전담, 멱등):
   ```sql
   \timing on
   UPDATE parcel SET geom_pt = ST_PointOnSurface(geom) WHERE geom_pt IS NULL;  -- seq scan, 인덱스 없음
   ANALYZE parcel;
   ```
   work_mem SET 불필요(seq scan). `WHERE geom_pt IS NULL` 멱등·재개. (실제 실행은 D3.)
4. **산출(2) `backfill_parcel_jibun.sql` 개정** — 구조:
   ```sql
   \timing on
   SET maintenance_work_mem = '512MB';
   SET work_mem = '2GB';                          -- self-join Hash 인메모리화
   DROP INDEX IF EXISTS parcel_jibun_lookup;      -- ON ONLY 부모 인덱스 제거
   UPDATE parcel p SET
     san     = CASE WHEN left(btrim(p.jibun),1)='산' THEN 1 ELSE 0 END,
     ji_main = (s.m)[1]::int,
     ji_sub  = COALESCE((s.m)[2],'0')::int
     -- geom_pt 라인 제거(분리)
   FROM ( SELECT id, sido_cd, regexp_match(jibun, '(\d+)(?:-(\d+))?') AS m
          FROM parcel WHERE jibun IS NOT NULL ) s
   WHERE p.id=s.id AND p.sido_cd=s.sido_cd AND s.m IS NOT NULL
     AND length((s.m)[1]) <= 7                    -- int4 가드 보존
     AND ((s.m)[2] IS NULL OR length((s.m)[2]) <= 7);
   -- 정정: 실측본과 동치 재생성. byte-match가 필요하면 'ON ONLY public.parcel USING btree' 그대로 복사.
   CREATE INDEX parcel_jibun_lookup ON parcel (emd_cd, ji_main, ji_sub);
   RESET work_mem;
   ANALYZE parcel;
   ```
   > **정정(적대검토 HIGH)**: `DROP INDEX parcel_jibun_lookup`은 ON ONLY **부모**를 떨군다. 재생성 `CREATE INDEX ... ON parcel (...)`(ON ONLY 없음)은 전 파티션 전파형으로 만들어져 **의도 결과는 동일하나 정의 문자열은 ON ONLY와 다름**. 따라서 수용기준을 **"컬럼·순서 동치"** 로 운용하거나, byte-match가 요구되면 실측 `ON ONLY public.parcel USING btree (...)`를 그대로 쓴다.
5. EXPLAIN(work_mem=2GB, 실행 안 함) → Hash Batches: 1(temp 스필 0). 인용 까다로우면 `.sql 파일 -f`.
6. **소규모 dry-run(전국 금지)** — 세종(36) `BEGIN; SET LOCAL work_mem='2GB'; EXPLAIN (ANALYZE,BUFFERS) UPDATE ... WHERE ... sido_cd='36' ...; ROLLBACK;`. **추가(정정)**: 비정상 12자리 jibun 가능 시도(경북 47/충남 44) 소규모도 `BEGIN;...ROLLBACK`으로 1회 — length≤7 가드가 ::int 오버플로를 실제 차단하는지 재현(grep만으론 회귀 미탐).
7. 인덱스 drop/recreate 정확성 dry-run: `BEGIN; DROP INDEX IF EXISTS parcel_jibun_lookup; CREATE INDEX ...; SELECT indexdef ...; ROLLBACK;` → **컬럼·순서 동치** 확인(byte-match 아님, ON ONLY 차이 허용).
8. 정적 검토: 가드/캐스팅/멱등 grep. load-all.sh 통합은 C2 범위.

**변경파일**: `scripts/postgis/backfill_parcel_jibun.sql`, `scripts/postgis/backfill_geom_pt.sql`(신규).

**수용기준(정정)**
- work_mem 상향 추가 + EXPLAIN Hash Batches=1.
- geom_pt 라인 제거 + `backfill_geom_pt.sql` 신규(멱등).
- DROP→UPDATE→CREATE 래핑, 재생성 indexdef가 운영 실측본과 **컬럼·순서 동치**(byte-match는 ON ONLY 차이로 불가 — 기준 완화).
- int4 가드 length≤7 / 부번 가드 보존.
- 전국 39.9M 미실행(EXPLAIN + 세종/경북·충남 소규모 dry-run ROLLBACK만).
- work_mem 세션 한정 + RESET.

**검증**
| check | expected |
|---|---|
| `grep -c "work_mem" backfill_parcel_jibun.sql` | ≥2 (SET + RESET) |
| `grep -c "ST_PointOnSurface" backfill_parcel_jibun.sql` | 0 |
| `grep -c "length((s.m)\[1\])" backfill_parcel_jibun.sql` | ≥1 |
| `grep -E "DROP INDEX IF EXISTS parcel_jibun_lookup\|CREATE INDEX parcel_jibun_lookup"` | DROP+CREATE 둘 다 |
| EXPLAIN(work_mem=2GB) | Hash Batches: 1 |
| 세종 dry-run BEGIN;...ROLLBACK | 갱신 후 영구변형 0 |
| 경북/충남 소규모 dry-run | length≤7 가드로 오버플로 차단(UPDATE 성공·ROLLBACK) |

**롤백(정정)**: 운영 무변형(EXPLAIN+ROLLBACK). 파일: `git checkout -- backfill_parcel_jibun.sql && rm -f backfill_geom_pt.sql`. **(범위 밖) 전국 실행 중 DROP된 상태로 중단 시**: ON ONLY 부모 인덱스를 복구해야 함 — `CREATE INDEX parcel_jibun_lookup ON ONLY parcel (...)` + 파티션별 ATTACH, **또는** 단순 `CREATE INDEX ... ON parcel (...)`(전파형, 자식 재빌드 39.9M, 장시간) 후 ANALYZE. 단순 일원화 권장 시 롤백 문서도 전파형으로 통일. work_mem 누수는 새 세션이 기본값 복귀.

**모니터링**: pg_stat_activity wait_event(temp=스필), `pg_stat_database.temp_files/temp_bytes`, 컨테이너 df, `docker stats`(work_mem×노드 합산 OOM, 7GiB 근접 시 하향), dry-run 롤백 확인(geom_pt/ji_main count 불변).

**안전**: 전국 재실행 금지(EXPLAIN+세종+경북/충남 소규모 dry-run). int4 가드 제거 금지. char(2)/char(8) 캐스팅 질의측 유지. bash 런처·psql 직접호출. work_mem 세션 2GB·RESET. 직렬. **DROP/CREATE는 트랜잭션 또는 dry-run ROLLBACK 내에서만**, UNIQUE(sido_cd,pnu) 미접촉. 단일 트랜잭션 중간 count=0 오판 금지. `diskSizeMiB` 금지.

**의존**: apply-schema(21-parcel-jibun.sql), parcel 적재 완료, geom_pt 분리본 실행은 D3, load-all 통합은 C2.

**conductor**: 단독 권장. 파일 리팩터+grep은 DB 무접촉(비-DB 태스크와 병렬 가능, 단 동일 파일 수정 태스크와는 직렬). EXPLAIN/dry-run만 단독 DB 점유.

---

### C2 — 빌드/복구 오케스트레이터 통합 (load-all.sh STEPS + 끝 백업) + 두 트랙 README

**목적**: load-all.sh(STEPS=`schema admin parcel building geocode facility`, 라이브 확인)에 ①지번 backfill(개선판) ②lawd_dong 재생성 ③lawd_sigungu 재생성을 멱등 STEP으로 통합 + 끝에 pg_dump 자동백업. build-studio 트랙(load_postgis→load-all.sh 호출)과 scripts/postgis 트랙 README. **스펙/스크립트 작성만, 전국 재실행 금지**.

**선행**
- compose postgis 가동. libpq 환경변수. 검증 psql은 컨테이너(17.5).
- apply-schema 선적용.
- 검증 전제 데이터(§0.2).
- backups/ 부재 → backup STEP이 `mkdir -p $BUILD_HOME/backups` 선행.

**단계 + 명령**
1. 현행 갭 확정(grep): STEPS에 backfill·lawd·backup 없음, lawd_dong populate 부재(21-parcel-jibun.sql 빈 CREATE, geocode-api-pg.py SELECT만). int4 가드 18-19행 존재.
2. **backfill 개선판**(C1 산출 재사용): work_mem 2GB + geom_pt 분리 + DROP/CREATE 래핑 + int4 가드. EXPLAIN만(전국 금지).
3. **`build_dong_dict.sql` 신규(멱등·결정성 정정)**:
   ```sql
   TRUNCATE lawd_dong;
   INSERT INTO lawd_dong(emd_cd, sido, sigungu, emd)
   SELECT DISTINCT ON (left(bcode,8)) left(bcode,8)::char(8), sido, sigungu, emd
   FROM address
   WHERE kind='addr' AND bcode IS NOT NULL AND length(btrim(bcode))>=8 AND emd IS NOT NULL
   ORDER BY left(bcode,8), emd, sigungu, sido;   -- tie-break 추가(정정): 멱등 결정성 보장
   ANALYZE lawd_dong;
   ```
   > **정정(적대검토)**: ORDER BY가 DISTINCT ON 키만이면 동명 표기 흔들림에 비결정적 → tie-break(`emd, sigungu, sido`) 추가. 길이 8 필수(`lawd_dong.emd_cd(8)=left(bcode,8)=left(pnu,8)`).
   **사전 게이트(신설)**: `SELECT count(DISTINCT left(bcode,8)) FROM address WHERE kind='addr' AND bcode IS NOT NULL AND length(btrim(bcode))>=8` 결과가 현재 5046과 ±허용오차 내가 **아니면 INSERT 거부**(무음 누락 차단). bcode<8/emd NULL 분포 사전 로깅.
4. **lawd_sigungu 재생성** = 권위 빌더 `build_sigungu_dict.sh`(멱등 DROP/CREATE/\copy/INDEX/ANALYZE). 소스=내비DB 7z(`match_jibun_<시도>.txt`, left(법정동코드,5)=sigungu_cd). **호출 규약 정정**: `build_sigungu_dict.sh`는 `--help` 미지원(set -uo pipefail 후 바로 $2 처리). 7z 경로는 **위치인자 $2**. `--7z <path>` 사용 시 --7z=$1, path=$2로 우연히 동작. 단일인자면 $2 비어 기본값 → 의도와 다름. **권고**: 코드에 `--7z` 분기 먼저 추가하거나 `build_sigungu_dict.sh "" <7z경로>`. **7z 경로 존재를 precondition으로 실측 확인**(부재 시 항상 스킵 → "재생성"이 no-op, 254 보존이면 명시). address.bcode(left,5) 대체경로는 sigungu_nm 포맷 충돌로 금지(아래 의존 참조).
5. **load-all.sh STEPS 확장**(has()/run()/fail 패턴 유지, 라이브 확인): `schema admin parcel backfill building geocode lawd facility backup`.
   - **backup STEP 이중화(정정)**: geocode(TRUNCATE) **앞**에 pre-backup(`-t address -t poi -t parcel -t lawd_dong -t lawd_sigungu` 또는 FULL) + 맨끝 post-backup. 맨끝 단독은 address 소실 시 복원 불가.
   - backfill STEP = parcel 직후, `has backfill`. 전국 UPDATE 무거움 → 기본 STEPS 제외 권장, `STEPS=...backfill...` 명시 실행 + "~30분" 경고 로그.
   - **lawd STEP = geocode(D2 address 최종) 직후(정정)**: `psql -f build_dong_dict.sql` + (7z 있으면) build_sigungu_dict.sh. lawd_dong은 address 최종본 기반이므로 **반드시 geocode 뒤**.
   - 스킵 vs 실패 구분(38-49행 패턴)을 lawd/backfill/backup에도 동일 적용.
   - **D1 백업 강제 게이트 흡수(정정)**: geocode STEP 진입부에 `backups/ 최신 덤프 mtime<24h AND size>0 AND pg_restore -l 통과` 미충족 시 TRUNCATE 거부(exit). `pg_advisory_lock(911001)`로 cap=1.
6. **pg_dump 자동백업 STEP**: `mkdir -p $BUILD_HOME/backups`; 기본 `-Fc -Z6 -t parcel -t lawd_dong -t lawd_sigungu` + **address/poi 포함(또는 FULL_DUMP=1)**. **컨테이너 pg_dump(17.5)** 1순위. 백업 실패는 fail=1 누적·적재 종료코드 분리.
7. **README**: scripts/postgis/README.md 보강 + `docs/빌드-트랙-정리.md` 신규. (A) build-studio=`scripts/build-studio.py`(:8090, CANON 목록) → load_postgis가 load-all.sh 호출. (B) scripts/postgis=PostGIS 적재. 표로 트랙·산출물(호스트 tiles/geocode.sqlite vs Docker pgdata)·기동·백업위치 + 안전제약 박스(zsh 회피, psql 직접호출, 동시금지, diskSizeMiB 금지, work_mem 세션·RESET). "분리가 아니라 상하 호출관계" 명시.
8. **멱등·dry-run 회귀(읽기, 소규모)**: backfill EXPLAIN(sido_cd='36'), lawd_dong would_rows≈5046, lawd_sigungu=254 불변, **자식 인덱스 EXPLAIN(`parcel_<sido>_..._idx`)**, 골든셋 3건 + **sigungu 토큰 동반(`수원시 영통구 매탄동 1-11`) 추가(정정)**.

**변경파일**: `load-all.sh`, `backfill_parcel_jibun.sql`, `build_dong_dict.sql`(신규), `backfill_geom_pt.sql`(신규), `README.md`, `docs/빌드-트랙-정리.md`. 재사용: `build_sigungu_dict.sh`. 참고: `geocode-api-pg.py`(C4/X1 별도).

**수용기준**
- load-all.sh에 backfill·lawd·backup STEP has() 게이트 + 개별 STEPS 선택.
- backfill 개선판(work_mem/geom_pt 분리/DROP-CREATE/int4 가드/RESET) — EXPLAIN temp 0.
- build_dong_dict.sql 멱등 재실행 시 lawd_dong=5046 일치/근사, emd_cd char(8). **사전 would_rows 게이트 통과**.
- lawd_sigungu 254 유지(7z 부재 시 스킵·보존), sigungu_nm LIKE 호환.
- **backup STEP이 geocode 앞(address/poi 포함)에도 존재** + 맨끝.
- README 트랙·산출물·안전제약 표.
- 골든셋 3건 + **sigungu 토큰 동반 1건** 통과, EXPLAIN 자식 파티션 Index Scan.
- 전국 39.9M 미실행.

**검증(정정)**
| check | expected |
|---|---|
| `grep -nE "backfill\|lawd\|backup" load-all.sh` | STEP 분기 + pg_dump 라인 |
| backfill grep(work_mem/geom_pt/DROP-CREATE/length/RESET) | SET work_mem 존재, geom_pt 없음, DROP/CREATE, length≤7, RESET |
| `SELECT count(DISTINCT left(bcode,8)) FROM address WHERE kind='addr' AND bcode IS NOT NULL AND length(btrim(bcode))>=8` (`.sql -f`) | ≈5046 |
| lawd_sigungu / lawd_dong count | 254 / 5046 |
| `EXPLAIN (COSTS OFF) ... parcel WHERE sido_cd=ANY(ARRAY['11']::char(2)[]) AND emd_cd=ANY(ARRAY['11110101']::char(8)[]) AND ji_main=1 AND ji_sub=11` | **`Index Scan using parcel_11_..._idx on parcel_11`**(자식, 정정) |
| 골든셋 3건 + `수원시 영통구 매탄동 1-11` curl(18080) | 매탄동/세종대로/강남대로 + sigungu 동반 정상 |
| `docker exec server-postgis-1 pg_dump --version` | 17.5(컨테이너 정합) |

**롤백**: 파일 git checkout/신규 삭제. lawd_dong 오류 시 백업 복원(컨테이너 pg_restore `-t lawd_dong`) 또는 build_dong_dict.sql 재실행(멱등). backfill ji_main/ji_sub 멱등 재계산. **단일 트랜잭션 중단 자동 롤백**(본 태스크 전국 미실행이라 회피). pg_dump는 비파괴 — 디스크 부족만 주의, 실패 시 부분파일 삭제. **(정정)** /health degraded 등 외부 계약 변경 없음(C2는 스크립트).

**모니터링**: wait_event, 컨테이너 df, 대량 UPDATE 커밋 전 미반영 정상(중간 kill 금지), load-all 종료 요약 행수(+lawd_dong/lawd_sigungu), 골든셋+EXPLAIN+시도별 분포.

**안전**: zsh 회피(bash 런처, psql 직접). 직렬(advisory lock). geocode STEP은 backup 선행 게이트. 전국 UPDATE 금지(EXPLAIN/소규모). int4 가드·char 캐스팅 유지. emd_cd(8)/sigungu_cd(5) 길이 엄수. work_mem 세션·RESET. `diskSizeMiB` 금지. UNIQUE(sido_cd,pnu) DROP 금지. 산출물 iCloud 금지, 게이트웨이 18080. 읽기전용(실제 TRUNCATE/INSERT/UPDATE/pg_dump는 승인 후).

**의존(정정)**: apply-schema. 09-gen(geocode.sqlite). build_sigungu_dict.sh + 7z(존재 확인). **D2(address 최종) → C2-lawd**(DAG 정정). (범위 외) geocode-api-pg.py ProgrammingError(C4).

**conductor**: 단독 권장. 문서/스크립트 작성은 DB 무접촉(파일 충돌 없으면 병렬). 실제 TRUNCATE/INSERT/UPDATE/pg_dump는 승인 후 단독 직렬.

---

### C3 — SQLite 지오코더(geocode:8082) 은퇴

**목적**: SQLite 백엔드 지오코더(server/docker-compose.yml geocode, 127.0.0.1:8082)를 무중단 은퇴. 게이트웨이(:18080) /geocode·/reverse가 이미 `geocode-pg:8082`(PostGIS)로 라우팅됨을 재확인하고, 1단계 stop(보존)→검증→2단계(옵션) 영구제거. **실행 금지(스펙), stop/commit은 승인 후**.

**선행**
- 게이트웨이 upstream=`geocode-pg`(gateway-nginx.conf:58-60).
- geocode-pg 현역(`:8092/health` ok:true).
- 게이트웨이 포트 18080(:80 아님).
- demo/runtime same-origin(8082 하드코딩 직호출 없음).
- 8082 default 진단도구 인지: 13d-geocode-parity.py, 13-qc-check.py(build-studio.py:614 호출), 13e-geocode-bench.py(차단 아님).
- DB 변경 없음 → pg_dump 불필요(PostGIS 데이터 백업은 D1 별도).

**단계 + 명령** (`/bin/bash -c` 런처)
1. upstream 정적 재확인(gateway-nginx.conf 58-60 = `set $up_geo geocode-pg; proxy_pass http://$up_geo:8082$request_uri;`). SQLite면 은퇴 중단.
2. geocode-pg 헬스(8092) ok:true.
3. 게이트웨이 골든셋 baseline(18080, URL 인코딩).
4. 8082 하드코딩 부재 grep(demo + demo-nginx.conf).
5. 컨테이너 상태(geocode=8082, geocode-pg=8092 분리).
6. **[1단계 보존 중지]** `docker compose ... stop geocode`(rm 아님). depends_on은 기동순서만 → 무중단.
7. **[검증]** stop 직후 골든셋 재확인(회귀 0) + `/`·`/demo/guide.html`·`/styles/cuvia/style.json` 200 + geocode-pg health 유지.
8. **[진단도구 영향 고지]** 8082 default 도구 base를 8092/18080로 전환(별도 PR). build-studio.py:614 `--api http://localhost:8082`가 중지된 SQLite 가리켜 qc 거짓실패 가능 → **C3→build-studio 사용 태스크 사이 "진단도구 8092 전환" 선행(의존 정정)**.
9. **[2단계 영구제거 옵션]** docker-compose.yml에서 **geocode 서비스 블록(47행 시작 ~ 다음 서비스 geocode-pg(74행) 직전까지, networks 줄 포함)** 삭제 + gateway.depends_on geocode(183행) 삭제. **정정**: "47-68행"은 networks 줄을 누락 가능 → 경계를 "geocode:(47행)~geocode-pg(74행) 직전"으로 명시.
10. **[검증]** `docker compose config --services`에 geocode 없음·geocode-pg 잔존·에러 0.
11. **[적용 옵션]** `rm -f geocode` + `up -d`(잔여 정합). 직후 단계7 재검증.
12. **[롤백]** 1단계: `start geocode`. 2단계: `git checkout -- server/docker-compose.yml` + `up -d geocode`.

**변경파일**: `server/docker-compose.yml`(2단계). 옵션 후속: build-studio.py:614, 13-qc-check.py, 13d/13e, docs(integration-guide.md·data-patch-runbook.md 8082→18080).

**수용기준**: upstream=geocode-pg 재확인. stop 후 골든셋 회귀 0. `/`·guide·style 200 + geocode-pg health 유지. demo :8082 하드코딩 없음(진단도구만 의존·고지). (영구제거) config에 geocode 부재·geocode-pg 잔존·에러 0. 롤백 즉시 가능.

**검증**: gateway-nginx.conf 58-60, `:8092/health`, 골든셋 curl(18080), `compose ps geocode`(Exited), guide 200, `config --services`.

**롤백**: 1단계 `start geocode`(1줄). 2단계 `git checkout` + `up -d geocode`. 어느 경우든 게이트웨이는 geocode-pg로 프록시(사용자 영향 사실상 없음). **정정**: geocode 이미지는 python:3.12-alpine(원격 pull) — 폐쇄망/오프라인에서 이미지 미보존 시 재기동 불가, alpine pull 의존 명시(이미지 보존 권장).

**모니터링**: stop/제거 직후 5분 — 골든셋 + `/`·guide·style 200 폴링, geocode-pg health/places 불변, `compose ps` gateway/geocode-pg/tileserver Up. PostGIS 부하는 pg_stat_activity(이 태스크 DB 무변경=평시). 컨테이너 df.

**안전**: 읽기전용(stop/rm/edit/commit은 승인 후). bash 런처. 포트 18080. compose stop은 의존 미재시작(무중단). geocode(8082)≠geocode-pg(8092) 혼동 금지(geocode-pg 절대 중지 금지). DB 변경 없음. `diskSizeMiB` 금지·image prune 범위 밖(geocode 이미지 보존). geocode-api.py·../geocode 마운트·geocode.sqlite는 stop/rm으로 미삭제(폐쇄망 폴백 보존).

**의존**: 선행 Phase 5c(upstream=geocode-pg, 완료). geocode-pg 안정성. **후속(정정·필수 선행화)**: build-studio 사용 태스크 전 "진단도구 8092 전환". 문서 8082 갱신.

**conductor**: 단독 권장. 대량 DB작업 아님. **단 D2/C2-lawd/D3 진행 중에는 stop 검증 보류**(적재 중 상태 관측 회피). 평시 수행. fan-out 금지.

---

### C4 — geocode-api-pg.py 에러 핸들링 보강 (ProgrammingError graceful 5xx + 필수테이블 점검/health degraded)

**목적**: do_GET가 `psycopg.OperationalError`만 catch(341행) → ProgrammingError(UndefinedTable/Column) uncaught 시 빈바디 500 + traceback 문제 해소. (1) ProgrammingError·광의 Error까지 확장해 항상 JSON 5xx, (2) 부팅 1회 + /health에서 필수테이블 점검 → 누락 시 degraded(ok=false, 503), (3) SQLi 방어·char 캐스팅·int4 가드·_limit 가드·골든셋 100% 보존. **읽기전용 스펙, 코드수정/배포는 별 단계**.

**선행**
- 대상 `server/geocode-api-pg.py`(do_GET 320-342, except 341, /health 324-327은 address·admin_boundary만 count).
- 현역 컨테이너(geocode-pg-1 8092, geocode-1 8082, postgis-1 5433). 게이트웨이 18080.
- psycopg는 호스트 없음 → 예외검증/실행은 **컨테이너 내부(docker exec server-geocode-pg-1)**.
- 의존 테이블: address, admin_boundary, parcel, lawd_dong, lawd_sigungu. 현 /health는 address·admin만 → parcel/lawd_* 누락 시 health ok지만 지번질의 500(관측 사각).
- 보존 불변식: char(2)/char(8) 캐스팅(195행), parse int4 가드(a,b≤99999, 104행), _limit 가드(49행).
- 골든셋(§0.2).

**단계 + 명령**
1. 현행 예외경로 재확인(grep): except OperationalError 유일.
2. psycopg 예외계층 검증(컨테이너): `issubclass(UndefinedTable, ProgrammingError)`=True, `issubclass(UndefinedColumn, ProgrammingError)`=True, `issubclass(OperationalError, ProgrammingError)`=False(형제 — 분기 분리 정당).
3. **do_GET except 다단(순서 강제)**: (a) `except psycopg.OperationalError` → 503, (b) `except psycopg.ProgrammingError as e` → 500 '스키마/질의 오류: {str(e)[:120]}', (c) `except psycopg.Error as e` → 500. 모두 `_send(obj, code)` JSON. 사용자입력 원문 echo 금지(str(e)[:120] 절단). **좁은→넓은 순서 필수**(Operational 먼저, Error 마지막).
4. **필수테이블 점검 + /health degraded**: `REQUIRED_TABLES=('address','admin_boundary','parcel','lawd_dong','lawd_sigungu')`. `_check_tables(cur)`:
   ```sql
   SELECT t FROM unnest(%s::text[]) t WHERE to_regclass('public.'||t) IS NULL  -- public. 한정(정정), 파라미터 바인딩
   ```
   /health: 부재 시 `{ok:false, degraded:true, missing_tables:[...], places, areas}` 503, 정상 시 기존 `{ok:true,...}` 200 **키 보존**. count는 address/admin 존재 시만.
   > **정정**: to_regclass를 `public.` 스키마 한정(search_path 의존 오탐 회피).
5. **부팅 1회 점검**: POOL.open() 직후 _check_tables → 누락 시 stderr 경고만(프로세스 계속 기동, 부분기능 보존). fatal 금지.
6. **검증 A 골든셋(무손상)**: :8092 + :18080 양쪽 매탄동/세종대로/강남대로 좌표 동일.
7. **검증 B SQLi/캐스팅/가드**: (1) **자식 파티션 Index Scan EXPLAIN(`parcel_<sido>_..._idx`)**(정정 — parcel_jibun_lookup 아님), (2) 따옴표/세미콜론 q → 500 아님(바인딩), (3) 6자리 초과 번지 → 500 아님, (4) limit=abc → 500 아님.
8. **검증 C 결함주입(격리, 운영 불변)**: 운영 parcel/lawd_* **DROP/RENAME 금지**. ProgrammingError→500 매핑은 **예외계층 사실 + 코드 단위테스트**(가짜 예외 주입)로 보증. 실DB 결함재현 필요 시 pg_dump 백업 후 throwaway 컨테이너만(범위 밖).
9. **검증 D health**: 정상 200 ok:true 유지. degraded는 단위/모의.
10. **검증 E parity**: 13d-geocode-parity.py `--a :8082 --b :8092 --limit 5` 수정 전후 불변(옵션 `--help` 확인).

**변경파일**: `server/geocode-api-pg.py`(do_GET except 다단 + REQUIRED_TABLES/_check_tables + /health degraded + 부팅 경고). dev override 마운트라 재빌드 없이 `up -d --force-recreate geocode-pg` 반영.

**수용기준**
- do_GET가 Operational(503)/Programming(500)/Error(500) 모두 JSON graceful(빈바디·소켓끊김 없음).
- ProgrammingError가 UndefinedTable·UndefinedColumn 포괄(계층검증).
- /health 필수테이블 누락 시 ok:false+degraded+missing_tables+503, 정상 시 기존 키 보존.
- 부팅 점검 1회(누락=stderr 경고, 계속 기동).
- **except 순서 단위테스트(가짜 예외 503 vs 500)가 수용기준에 포함(정정)**.
- 회귀 무손상: 골든셋 동일, parity 불변.
- SQLi 바인딩(to_regclass도 바인딩·public. 한정), char 캐스팅·int4·_limit 보존.
- 운영 DB 무변경.

**검증(정정)**
| check | expected |
|---|---|
| `docker exec server-geocode-pg-1 python3 -c "..."` 예외계층 | True True False |
| 골든셋 3건 :8092 | 좌표 동일 |
| SQLi/가드 3종 http_code | 200 / 200 / 200 |
| `EXPLAIN ... parcel WHERE sido_cd=ANY(ARRAY['41']::char(2)[]) AND emd_cd=ANY(...) AND ji_main=1 AND ji_sub=11` | **`Index Scan using parcel_41_..._idx on parcel_41`**(자식, never executed 나머지) |
| `:8092/health` | 200 + {ok:true,places:11281251,areas:N} |
| 13d parity `--a :8082 --b :8092 --limit 5` | exit=0(수정 전 동일) |
| except 순서 단위테스트 | OperationalError→503, ProgrammingError→500 |

**롤백**: 단일파일 git checkout + `up -d --force-recreate geocode-pg`(dev override 마운트라 재빌드 불필요). 데이터 무변경. 재기동 후 `/health` 200 + 골든셋 확인. **정정**: /health degraded(503 가능) 도입 시 **외부 헬스체크가 'HTTP 200만'이면 ok 필드 파싱으로 임계 동반 조정**(false-down 알람 회피) — 마이그레이션 노트.

**모니터링**: 재기동 후 /health 200 ok:true, 골든셋, parity exit=0. `docker logs --tail=100 -f server-geocode-pg-1` 부팅 WARNING + 500 빈도(traceback 사라지고 500이 JSON 바디인지). 게이트웨이 5xx 비율(server-gateway-1). pg_stat_activity(읽기전용=대량부하 없음).

**안전**: 읽기전용. 운영 parcel/address/lawd_* DROP/RENAME/TRUNCATE/대량UPDATE 금지(결함재현은 계층+단위). bash/docker exec. load_geocode 호출 금지. 불변식 무수정(바인딩·char 캐스팅·int4·_limit). 직렬 무관(인덱스질의/EXPLAIN). 산출물 iCloud 금지. psycopg는 컨테이너 내부만.

**의존**: 없음(C4 단독). **C4 ⊂ X1**(동일파일 동일 PR). parity는 :8082(SQLite, server-geocode-1 Up) 필요 → C3(은퇴) 전에 parity 검증 완료.

**conductor**: 단독 권장(동일 파일 server/geocode-api-pg.py 만지는 geocode 태스크와 직렬). 대량 DB 없음. bash 런처+docker exec.

---

## 3. 백업/복원 런북 (pg_dump/pg_restore) — 읽기전용 절차서

> **버전 정합(정정·HIGH)**: 서버 17.5, 호스트 pg_dump 18.4. **백업·복원 모두 컨테이너 내부 pg_dump/pg_restore(17.5)** 를 1순위로 한다(`docker exec server-postgis-1 ...`). 호스트 18.4로 -Fc 덤프 시 17.x 복원 거부 위험 — 부득이 호스트 덤프면 **복원도 18.x 클라이언트 강제**. 모든 명령 bash 런처/직접호출(zsh 회피), SQL은 `-f` 파일.

**[0] 디렉터리 준비**
`mkdir -p ~/geocode-build/backups` (호스트 로컬, iCloud/Docker볼륨 밖)

**[1] 전체 압축 덤프 (컨테이너 17.5)**
```bash
TS=$(date +%Y%m%d_%H%M)
docker exec server-postgis-1 pg_dump -U cuvia -d cuvia -Fc -Z6 -f /var/lib/postgresql/data/_cuvia_${TS}.dump
docker cp server-postgis-1:/var/lib/postgresql/data/_cuvia_${TS}.dump ~/geocode-build/backups/cuvia_${TS}.dump
docker exec server-postgis-1 rm -f /var/lib/postgresql/data/_cuvia_${TS}.dump
```
**[1b] 대용량 가속(directory + 병렬)**: `pg_dump -Fd -j 4 -Z6`(테이블 단위 4잡, max_connections 100 여유, martin I/O 경합 주의 — 현 CPU 5% 무방).
**[2] 핵심 테이블만**: `-t parcel -t lawd_dong -t lawd_sigungu` (+ D2 직전 `-t address -t poi`로 즉시 복원 가능). 부모 -t parcel 지정 시 18 파티션 동반.
**[3] 무결성(복원 없이)**: `docker exec server-postgis-1 pg_restore -l /tmp/v.dump | head -40` + size>0 + 종료코드 0.
**[4] 전체 복원(재해)**: `pg_restore -U cuvia -d cuvia --clean --if-exists -j 4 <dump>` (멱등, 복원 중 DB 쓰기 금지).
**[5] 부분 복원**:
- 5a parcel만: `--clean --if-exists -t parcel <parcel_dicts.dump>` — **⚠ 파티션 -t parcel 선택복원이 자식 누락 가능 → `pg_restore -l`로 parcel_11~52 TABLE DATA 확인 후, 누락 시 자식 -t 개별 지정**.
- 5b lawd_*만: `-t lawd_dong -t lawd_sigungu`.
- 5c address/poi만(D2 TRUNCATE 롤백): `-t address -t poi <전체덤프>`. **address.search_text는 GENERATED+pg_trgm 의존 → 동일 DB 복원이라 확장 존재(현실 위험 낮음, 명시)**.
**[6] 데이터-온리**: `--data-only --disable-triggers -t <table>` (대량이면 `--section=data` 후 REINDEX 고려).
**[7] 격리 검증DB(F10, 운영 비파괴)**:
```bash
docker exec server-postgis-1 createdb -U cuvia cuvia_verify
docker exec server-postgis-1 pg_restore -U cuvia -d cuvia_verify -j4 /tmp/cuvia.dump
# load_geocode --only/TRUNCATE 실험은 전부 cuvia_verify 에서. 끝나면 dropdb.
```
**[8] 복원 후 검증**: parcel 39,882,449 / address 11,281,251 / lawd_dong 5046 / lawd_sigungu 254 재확인. `ANALYZE parcel; ANALYZE address;`(플랜 회귀 방지). 골든셋 라이브. EXPLAIN 자식 파티션 Index Scan(char 캐스팅).
**[geocode.sqlite 백업(정정·신설)]**: pg_dump 대상 아님(파일). D2 재빌드 직전 `cp geocode.sqlite geocode.sqlite.pre-d2-<ts>`(성공 후 불량 대비).
**[대안 볼륨 tar(콜드, DB 정지)]**: `compose stop postgis` → `docker run ... alpine tar czf .../pgdata_${TS}.tgz` → `start postgis`. 핫 백업은 pg_dump 우선.
**[자동화 권고]**: load-all.sh/recover.sh 끝 + **geocode STEP 앞** pg_dump. geocode 진입 가드: 'backups/ 최신 mtime<24h AND size>0 AND pg_restore -l 통과 아니면 자동 pg_dump 선실행, 실패 시 TRUNCATE 거부'. **pg_advisory_lock(911001) cap=1**.

---

## 4. conductor 배정·증설 (레이트리밋 고려)

**[현재 상태(live)]** conductors 2개: surface:17(idle), surface:18(idle). master surface:19(idle), manager running. 레이트리밋: **7d 0.78(78%, allowed_warning)**, 5h 0.24(24%), 7dReset≈2026-06-25 04:00 KST.

**[배정 원칙]** 코드 작성(그룹 A)은 파일 disjoint 단위로 2 conductor 분산. 대량 DB(그룹 B)는 **동시성 cap=1**(advisory lock) — conductor 수 무관, 1개가 순차 점유. DB OOM 안전제약이 conductor 병렬성을 무력화.

**[현재 2 conductor 절약 모드(증설 없이)]**
- **라운드 1(코드, 병렬)**:
  - C-17 ← `{X1+X6+C4}` = server/geocode-api-pg.py(단일파일 내부 순차, 최중량 전담).
  - C-18 ← X5(ri 3파일) → X2(09-gen) → C2(build_dong_dict.sql+build_sigungu_dict.sh+load-all.sh+build-studio.py) → C1(backfill 배선). X1과 비충돌.
  - X3(search.js)/X4(하니스)는 X1 확정 후 여유 슬롯.
- **라운드 2(대량 DB, cap=1 — 1개만 활성)**: C-18 단독 **D1 → X5적재 → D2(TRUNCATE+재적재) → C2-lawd → C1-backfill → D3(geom_pt 부분) → X4실행**. (DAG 정정 반영: C2-lawd를 D2 뒤). C-17 idle. 전부 격리 검증 DB.
- **라운드 3**: C-17 ← C3(SQLite 은퇴, X1·X3 안정화 + 진단도구 8092 전환 후 단독 후행).

**[증설 권고 — 비권장(현 2개 유지)]**
1. **7d 78%(allowed_warning)** — 3개+ 증설 시 7dReset(≈06-25 04:00) 전 한도 도달 위험. 5h는 여유나 본체는 7d 제약.
2. **병목이 토큰이 아니라 DB 직렬 레일(cap=1)** — 적재/백필은 conductor 3+여도 병렬화 불가.
3. **총량(D3+C4+X6 다수 흡수)**이 2 conductor 라운드제로 소화 가능.
- **조건부 증설**: 7dReset 후(78%↓) AND 코드 트랙(X1≠X2≠X5≠C2 동시)을 한 번에 밀 때만 3번째 일시 추가, DB 라운드에선 즉시 idle.

**[동시성 cap 명세]**
- 코드(A): conductor ≤ 2, 파일 disjoint.
- DB(B): cap=1(pg_advisory_lock(911001) 단일 게이트, D1 흡수). conductor 몇이든 동시 적재/백필 1개.
- **토큰 가드**: 7d 80% 도달 시 신규 라운드 착수 보류, 7dReset 후 재개.

---

## 5. 통합 검증 (골든셋 + 태스크별)

### 5.1 골든셋(필수 회귀 — 게이트웨이 :18080, URL 인코딩)
| 질의 | 기대 |
|---|---|
| 매탄동 1-11 | (127.0581, 37.2711) |
| 세종대로 110 | 서울시청 |
| 강남대로 396 | 강남역 |
| **수원시 영통구 매탄동 1-11(신설)** | 매탄동 1-11과 동일 좌표 — **sigungu_nm 단형(`영통구`) vs address 전형(`수원시 영통구`) 매칭 회귀 검증**(기존 3건은 sigungu 토큰 미동반이라 이 결함 미탐) |

### 5.2 태스크별 게이트
- **X4 선결게이트(FAIL-fast)**: `lawd_dong COUNT>0 AND parcel.ji_main NOT NULL 비율(골든셋 시도 한정)≥임계` + **검증 DB가 격리(cuvia_verify)인지 확인**. 미충족 시 무음통과 차단.
- **D1**: dump size>0, `pg_restore -l` TOC + 5개 핵심테이블, 행수 불변, 호스트 백업폴더 df 정상.
- **D2**: address kind biz/facility 신규 + addr 보존, poi>0, lawd_* 불변, 인덱스 전부, 골든셋+상호명, /health places=address count.
- **D3**: 대상 파티션 NULL=0, ST_Intersects 위반 0, **자식 파티션 Index Scan**, 골든셋.
- **C1**: work_mem grep≥2, ST_PointOnSurface grep=0, int4 가드≥1, DROP/CREATE, EXPLAIN Hash Batches=1, 세종+경북/충남 dry-run ROLLBACK.
- **C2**: STEP 분기, build_dong_dict would_rows≈5046 게이트, lawd 254/5046 불변, **자식 파티션 Index Scan**, 골든셋+sigungu 동반, pg_dump(컨테이너) 가용.
- **C3**: upstream=geocode-pg, stop 후 골든셋 회귀 0, 200 응답, config geocode 부재.
- **C4**: 예외계층 True/True/False, 골든셋 동일, SQLi/가드 200×3, **자식 파티션 Index Scan**, /health 200 ok:true, parity exit=0, except 순서 단위테스트.

### 5.3 공통 사후검증
poi>0, kind에 biz/facility, lawd_dong 5046/lawd_sigungu 254 불변, EXPLAIN 1파티션(자식) Index Scan, 골든셋 4종, admin_boundary/building 현황(현재 0 — /reverse areas [] 정상).

---

## 6. 리스크

| # | 리스크 | 심각 | 완화 |
|---|---|---|---|
| R1 | **pg_dump 버전 불일치(18.4 호스트 vs 17.5 서버)** → 17.x 복원 거부 | HIGH | **컨테이너 pg_dump/pg_restore(17.5) 1순위**. 호스트 덤프 시 복원도 18.x 강제 |
| R2 | D1 미백업 상태 TRUNCATE(load_geocode address+poi) → 데이터 소실 | HIGH | **advisory lock + mtime<24h·size>0·pg_restore -l 강제 게이트**(geocode STEP 진입부 흡수) |
| R3 | 대량작업 동시 실행 → 호스트 OOM(load 259 전례) | HIGH | **cap=1 pg_advisory_lock(911001)** 단일 레일. martin MVT I/O 경합 관찰 |
| R4 | C1 인덱스 재생성이 ON ONLY 실측과 byte-불일치 → 검증 실패 | MED | 수용기준 "컬럼·순서 동치" 완화 또는 `ON ONLY public.parcel USING btree` 그대로 복사. 롤백에 ATTACH/전파형 명시 |
| R5 | EXPLAIN 기대 인덱스명(parcel_jibun_lookup) plan 미출현 → grep 항상 실패 | MED | **자식 파티션명 `parcel_<sido>_..._idx`로 정정**(라이브 확인) |
| R6 | lawd_dong DISTINCT ON 비결정 → 멱등성 위반 | MED | **ORDER BY tie-break(emd, sigungu, sido)** + would_rows≈5046 게이트(불일치 INSERT 거부) |
| R7 | sigungu_nm 단형(`영통구`) vs address 전형 매칭 회귀 — 골든셋 3건 미탐 | MED | **권위 빌더(build_sigungu_dict.sh) 우선** + sigungu 토큰 골든셋 추가 |
| R8 | DAG 역전(C2-lawd가 D2 앞) → address 비웠다 채우는 사이 정합성 흔들림 | MED | **C2-lawd를 D2(address 최종) 뒤로 강제** |
| R9 | D3 전국 단일 트랜잭션 → 디스크 2배·중단 시 전체 롤백 | MED | **권장-B 금지(파티션별만)**, work_mem SET 제거, 수용기준 파티션 단위 완화 |
| R10 | int4 가드 회귀(grep만으론 미탐) | MED | **경북/충남 12자리 jibun 포함 시도 dry-run ROLLBACK** 재현 |
| R11 | geocode.sqlite "성공 후 불량"(atomic rename 무력) | MED | **재빌드 직전 cp 스냅샷(.pre-d2-<ts>)** |
| R12 | C2 backup 맨끝 단독 → geocode TRUNCATE 직전 미백업 → address 복원 불가 | MED | **backup STEP을 geocode 앞에도(address/poi 포함) 이중 배치** |
| R13 | **admin_boundary=0·building=0 → /reverse areas 항구 빈값**, /health ok로 관측 사각 | MED | **admin_boundary 적재 태스크 신설(별도 선행)** + C4 REQUIRED_TABLES에 admin_boundary 행수>0까지 degraded 포함 고려 |
| R14 | C3 후 build-studio qc(--api 8082) 거짓실패 | LOW | **C3→build-studio 사용 태스크 전 진단도구 8092 전환 선행** |
| R15 | C3 영구제거 후 alpine 이미지 미보존(폐쇄망) → 재기동 불가 | LOW | geocode 이미지/마운트 보존 명시, image prune 범위 밖 |
| R16 | C4 except 순서 오류(Operational이 500으로) → 503 계약 깨짐 | LOW | 좁은→넓은 순서 강제 + 단위테스트 수용기준화 |
| R17 | C4 /health degraded(503) → 외부 모니터 false-down | LOW | 외부 헬스체크 ok 필드 파싱 임계 동반 조정 노트 |
| R18 | zsh `_safe_eval` 버그(`$$`→PID, `<` 리다이렉트, && 체인 실패) — **본 세션 라이브 재현** | LOW | 모든 SQL `.sql -f`, 셸 `env bash <launcher.sh>`, psql 변수담기 금지 |
| R19 | `diskSizeMiB` 직접편집 → PostGIS 볼륨 전체 리셋(과거 손실 직접원인) | HIGH | GUI 리사이즈+사전백업만. 현재 375G 여유라 불필요 |
