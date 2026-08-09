# 리(里) 사전 `lawd_ri` 구축·배포 런북 (task-014)

지오코딩의 **리(里) 축**을 복구하기 위한 운영자 절차서다. 코드 배포 → **게이트 G-1** → 사전 구축 → 지번 경로 배포의
4단계로 나뉘며, **각 단계에 기대 출력과 중단 조건을 병기**한다.

> **이 문서는 「실행 결과 기록」이 아니라 「운영자가 실행할 절차」다.**
> 작성 시점(2026-08-09)에 구현자는 실서버 SSH·DB 접근 권한이 없어 P0·P2 를 실행하지 못했다.
> 아래 SQL 의 실행 결과는 전부 **[미확인]** 이며, 운영자가 실행한 뒤 이 문서 하단 「실행 기록」에 채워 넣는다.

관련 문서: 계획서 `.team/tasks/014-admin-boundary-address-ri-parcel-parse/runs/task-014-1786255078/plan.md` (rev.3),
데이터 패치 관례 `docs/data-patch-runbook.md`.

---

## 0. 배경 — 왜 4단계인가

법정동코드 10자리 = **시도2 + 시군구3 + 읍면동3 + 리2**. 리 정보는

- `address.ri` (컬럼은 **이미 존재**하나 실서버 적재 여부 **[미확인]**),
- `address.bd_mgt_sn` 앞 10자리 (리 2자리 생존),
- `parcel.pnu` 9~10번째 2자리 (리 2자리 생존, `parcel.emd_cd` 는 8자리로 절단)

세 곳에 흩어져 있다. 사전 `lawd_ri(emd_cd, ri_cd, ri)` 는 **`address` 에서 파생**되므로,
`address.ri` 가 비어 있으면 사전을 만들 수 없고 기능도 동작하지 않는다.
그래서 **사전을 만들기 전에 그 사실을 1회의 HTTP 호출로 판정**하는 것이 게이트 G-1 이다.

| 단계 | 내용 | 사전 의존 | 이 단계에서 확인 가능한 완료 기준 |
|---|---|---|---|
| **P1a** | 코드 변경 ①(fail-open 플래그)·②(`addr_obj` ri 통과)·③(`parse` 리 토큰 분리)·④(`area_pip` emd) | 없음 | 3(`structure.ri` 부분)·5·6·8, (4 는 경계 적재 시) |
| **G-1** | `/reverse` 1회로 `address.ri` 채움 판정 | P1a 배포 | — (통과/중단 분기) |
| **P2** | `build_ri_dict.sql` 실행 → `lawd_ri` 구축 | G-1 통과 | 사전 자기검증 5종 |
| **P1b** | 코드 변경 ⑤(parcel 경로 `lawd_ri` 조인 + `pnu` NULL 안전 후필터) | P2 | 1·2·7·9 |

**P1a 가 무해한 이유** — 데이터가 없으면 아무것도 바뀌지 않도록 설계했다.
변경 ②는 `address.ri` 가 비면 `None`(현행과 동일), 변경 ③의 `ri` 키는 additive 이며 `dong` 값은 불변,
변경 ④는 `admin_boundary` 에 `emd` 레벨이 없으면 행 0(현행과 동일), 변경 ①은 조회 실패 시 `False` 로 리 로직 전체 비활성.
그래서 P1a 를 **게이트 판정 도구**로 쓸 수 있다.

---

## P0. 진단 (배포 전, SQL 4종)

**운영자 수행 항목**(SSH 필요). SSH 가 불가능하면 P0-1 은 게이트 G-1 이, P0-3 은 §5.3 HTTP 스윕(아래 부록 A)이 대체한다.

| # | SQL | 기대 출력 | 중단 조건 |
|---|---|---|---|
| P0-1 | `SELECT count(*) FROM address WHERE kind='addr' AND ri IS NOT NULL AND ri<>'';` | **600만 이상**(로컬 실측 6,753,341 기준) | **0 이면 G-1 이 닫힌 것과 동치 → 본 과제 중단** |
| P0-2 | `SELECT count(*) FROM parcel WHERE substr(pnu,9,2)<>'00';` | 0 초과 | 0 이면 `pnu` 에 리 정보 없음 → P1b 무의미, 중단 |
| P0-3 | `SELECT left(pnu,2) AS sido2, count(*) FROM parcel GROUP BY 1 ORDER BY 1;` | `42/45/44/47/12` 가 **없어야** 함(신 체계) | 구 시도접두가 나오면 리매핑 전제 붕괴 → 중단 후 재설계 |
| P0-4 | `SELECT level, count(*) FROM admin_boundary GROUP BY 1;` | `emd` 행 존재 여부 **기록** | 중단 아님. `emd` 없으면 완료 기준 4 는 **데이터 미적재로 미해결** 보고 |

> P0-3 의 `42/45` 는 구 강원/전북 접두다. 신 체계는 각각 `51`/`52`.
> `44`(충남)·`47`(경북)·`12`(구 서울 잔재로 오인되던 코드)는 **현행 유효 코드이므로 리매핑 대상이 아니다**
> (`scripts/postgis/schema/20-parcel.sql:26-32`, `scripts/postgis/load_parcel.sh:88-95`).

---

## P1a. 코드 배포 + **게이트 G-1**

배포 전 로컬에서 **단위 테스트를 반드시 통과**시킨다(DB 불요).

```
python3 -m unittest server.test_geocode_api      # T-1 ~ T-12 포함 전건 통과여야 한다
```
> 이 명령은 `psycopg` 가 설치된 인터프리터에서만 동작한다. 시스템 `python3` 에 없으면
> `python3 -m venv .venv && .venv/bin/pip install 'psycopg[binary]' psycopg_pool` 후 `.venv/bin/python` 으로 실행한다.

```
cp <배포경로>/server/geocode-api-pg.py <배포경로>/server/geocode-api-pg.py.bak   # 롤백 지점 확보(필수)
scp server/geocode-api-pg.py <서버>:<배포경로>/server/
ssh <서버> 'docker compose --profile postgis restart geocode-pg'
curl -s 'http://112.216.247.186:18080/health'
curl -s 'http://112.216.247.186:18080/reverse?lon=127.419845&lat=37.737815'
```

- 기대: `health` 정상, `/reverse` 의 `structure.ri == "청평리"`
- 기동 로그에 `geocode-api-pg: lawd_ri: absent` 가 1줄 찍힌다(정상 — 아직 사전이 없다).
  `absent` 여도 `/health` 는 정상이어야 한다. `degraded` 가 되면 **변경 ①이 잘못 적용된 것**이므로 롤백.
- **중단 조건(G-1 닫힘)**: `structure.ri` 가 `null`

### G-1 판정

```
curl -s 'http://112.216.247.186:18080/reverse?lon=127.419845&lat=37.737815' | /usr/bin/grep -o '"ri":[^,]*'
```

| 결과 | 해석 | 조치 |
|---|---|---|
| `"ri":"청평리"` | `address.ri` **채워짐** | **게이트 통과** → P2 진행 |
| `"ri":null` | `address.ri` **비어 있음** | **게이트 닫힘** → 아래 중단 절차 |

### 게이트가 닫혔을 때의 중단 절차 (필수, 순서대로)

0. **산출물 4개를 모두 커밋한다.** 게이트가 닫혀도 코드·테스트·SQL·런북은 **후속 과제의 입력**이므로 폐기하지 않는다.
   커밋 메시지에 `G-1 미통과 — lawd_ri 미구축 상태로 동결` 을 명시한다.
1. P1a 는 무해하므로 **롤백하지 않는다**(그대로 둔다).
2. **P2 를 시도하지 않는다.** 시도하더라도 `build_ri_dict.sql` 의 하한 가드(`lo=15000`)가 `would_rows=0` 으로
   `RAISE EXCEPTION` 하며 거부한다 — **이중 안전망**이다.
3. **task-014 를 여기서 중단**하고 결과를 보고한다: **완료 기준 1·2·3 미해결.**
4. **"address 16.3M 재적재"를 별도 과제로 승격**한다. 본 과제 안에서 처리하지 않는다 —
   "재적재 0 · 백필 0" 이라는 계획의 전제 자체가 무너지므로 계획을 다시 써야 한다.
   (재적재 소요시간은 측정 기록이 없어 **[미확인]**, `TRUNCATE`+`INSERT` 라 중단 시 빈 테이블이 되므로
   **사전 스냅샷 확보 필수**, 폐쇄망 파일 전송 경로가 별도로 필요하다.)
5. **"계획대로 진행하되 미검증으로 남긴다"는 절충은 채택하지 않는다.** 게이트가 닫히면 코드가 옳아도 효과가 0 이다.

---

## P2. 사전 구축

```
psql -v ON_ERROR_STOP=1 -f scripts/postgis/build_ri_dict.sql
```

> **인라인 `-c` 금지 · 파일 + `-f` 실행.** 스크립트가 dollar-quote(`$build_ri$`)를 쓰므로
> 셸을 거치면 인용이 깨진다. `psql -f` 는 파일을 직접 파싱하므로 안전하다.

- 기대: `NOTICE  build_ri_dict: would_rows=… (허용 [15000, 20000])` 후 정상 종료
- **중단 조건**: `RAISE EXCEPTION` 발생. 트랜잭션이 abort 되어 `TRUNCATE` 도 롤백되므로 **빈 사전이 남지 않는다.**
  (이 서술은 would_rows 가드 경로에 한한다. 자기검증 중단 경로는 §대체축 후보 참조)

### 자기검증 결과 판정

| 검증 | 내용 | 판정 |
|---|---|---|
| **0** | `lawd_dong` 축 정합률 (`pct`) | **`pct < 99` 면 P1b 로 진행하지 말고 중단** |
| **1** | `parcel` 축 정합률 (`pct`) | **`pct < 99` 면 P1b 로 진행하지 말고 중단** |
| 2 | 총 키 수 / 리 이름 종수 | 기록만. 로컬 참고치 16,402 은 **폐기된 5규칙 축의 값**이라 직접 대조 불가 |
| 3 | 충돌 키 덤프 | 기록만. `DISTINCT ON` tie-break 로 버려진 쪽의 목록 |
| 4 | `청평리` 존재 확인 | 행이 없으면 **완료 기준 1 달성 불가** — 기록 후 판단 |

- **자기검증 0(`lawd_dong` 축)이 특히 중요하다.** 실제 조회 경로(`geocode-api-pg.py` 의 지번 경로)는
  `lawd_dong` 으로 얻은 `emd_cd` 배열에 `lawd_ri.emd_cd` 를 맞춰 좁힌다. 이 값이 낮으면
  사전이 아무리 정확해도 조회가 0건이 되어 **기능이 무효**다. `parcel` 축(검증 1)만 보고 통과시키면 이 실패를 놓친다.
- 중단할 때는 **어긋난 키의 시도접두 분포**를 함께 보고한다(스크립트가 자동 출력한다):

  ```sql
  SELECT left(emd_cd,2) AS sido2, count(*)
    FROM lawd_ri r
   WHERE NOT EXISTS (SELECT 1 FROM parcel p
                      WHERE p.sido_cd = left(r.emd_cd,2)::char(2)
                        AND p.emd_cd  = r.emd_cd)
   GROUP BY 1 ORDER BY 2 DESC;
  ```
  특정 시도접두에 몰려 있으면 **리매핑 규칙 누락**, 고르게 퍼져 있으면 **데이터 자체의 불일치**다.

### pct < 99 로 중단했을 때 — 대체축 후보 (운영자 판단 사항)

시도 리매핑 축이 어긋났다고 판단되면, 리매핑을 포기하고 **같은 행에서**
아래 두 값을 취하는 축으로 재구축하는 방안이 있다.

    emd_cd = left(btrim(bcode), 8)
    ri_cd  = substr(btrim(bd_mgt_sn), 9, 2)

**채택 전에 반드시 다음을 먼저 세어 보고한다** — 두 축이 어긋나는 행 수:

    SELECT count(*) FILTER (WHERE left(btrim(bcode),8) <> (CASE ... END || substr(btrim(bd_mgt_sn),3,6))) AS mismatch,
           count(*) AS total
      FROM address WHERE kind='addr' AND ri IS NOT NULL AND ri <> '';

이 대안은 **후보일 뿐이며 자동 적용 대상이 아니다.** 실측 후 결정한다.

**중단 시 정리**: 자기검증 0·1 은 `DO` 블록 밖에서 실행되므로, 이 시점에
`lawd_ri` 는 이미 적재·커밋된 상태다. 재구축 전 `DROP TABLE lawd_ri;`
(§P-R 참조) 로 제거하거나, P1b 를 배포하지 않은 채로 둔다.

### 재정의 기준 2 의 달성 가능성 확인 (P2 이후 1회)

```sql
SELECT p.emd_cd, count(*) FROM parcel p
JOIN lawd_ri r ON r.emd_cd = p.emd_cd
WHERE r.ri = '청평리' AND p.ji_main = 432 AND p.ji_sub = 11
GROUP BY p.emd_cd;
```
- **1행** → 재정의 기준이 원리상 달성 가능(게이팅을 푸는 별도 결정이 필요).
- **2행 이상** → **재정의된 기준도 달성 불가.** 기준을 `q=청평면 청평리 432-11` 로 다시 정의해야 한다.
- 결과를 아래 「실행 기록」에 그대로 기록한다.

---

## P1b. 지번 경로 배포 + 검증

- P1a 와 동일한 배포 절차(변경 ⑤ 포함본).
- 기동 로그가 `geocode-api-pg: lawd_ri: present` 로 바뀌어야 한다. `absent` 면 사전이 안 보이는 것이므로
  같은 DB 를 보는지 확인한다(이 상태에서도 **응답은 현행과 동일**하다 — fail-open).
- 아래 회귀 기준 7건을 **배포 전후 각 1회** 찍어 diff 한다.
- **중단 조건**: 회귀 기준의 `가평군 청평면`·`강남구 테헤란로 152`·`투다리`·`이문동` 중
  **하나라도 결과 건수가 줄면 즉시 롤백**(P-R).

### 회귀 기준 (실서버 베이스라인, 2026-08-09 `http://112.216.247.186:18080`)

```
q=가평군 청평면 청평리 432        → 6건, 전부 "경기도 가평군 청평면 432", ri=null, b_code=4182032500, san=false
q=청평리 432-11                   → 1건, parcel 아님(도로명/addr 경로)
q=청평면 청평리 432-11            → 1건, source=parcel, "경기도 가평군 청평면 432-11"
q=강원 춘천 신북읍 천전리 300     → 3건, b_code=5111025000 (pnu 앞2 = 51, 신)
q=전북 완주 소양면 월상리 1       → 8건, b_code=5271034000 (pnu 앞2 = 52, 신), san=true 3건 혼입
q=투다리                          → 8건, 전부 source=osm (POI 정상 — 오탐 회귀 기준)
q=서울 동대문구 이문동 100        → 8건, 전부 source=localdata (POI)
```

- `투다리`·`이문동` 은 **오탐 회귀 기준**이다. 리 토큰 게이팅(읍면동 토큰이 먼저 잡힌 질의에서만 리를 채택)이
  풀리면 상호명 20,078건이 리 사전을 타면서 POI 경로를 가로챈다. 이 두 질의로 감시한다.
- `가평군 청평면 청평리 432` 는 **6건 → 청평리 필지만으로 축소**되고 `structure.ri="청평리"` 가 채워지는 것이 목표다.
  **단 산번지(`san=true`) 혼입은 이번 범위에서 해소되지 않는다.**
- 골든셋은 코드 변경 **전에 3회 연속** 실행해 flaky 를 분리한 baseline 을 확정하고, P1b 후 동일하게 3회 실행해 대조한다.
  **판정은 종료코드가 아니라 출력 집계로 한다**(러너가 FAIL 에도 exit 0).
  ```
  GEOCODE_API_URL=http://112.216.247.186:18080 python3 server/test_geocode_search_golden.py
  ```

### 성능 확인 (P1b 후 1회)

- 다중토큰 지역검색은 `EXPLAIN` 에 **BitmapOr 유지**를 확인한다(커밋 `caf66f2` 의 27ms 경로).
  본 과제는 재적재를 하지 않으므로 인덱스가 바뀌지 않아 위험은 없으나, **확인에는 운영자 `EXPLAIN` 이 필요**하다.
- `reverse?limit=30` 지연은 중앙값을 병기해 기록한다. `area_pip` 의 `level IN` 에 값 1개(`emd`)가 늘었으므로,
  `admin_boundary` 에 `emd` 경계가 **적재돼 있으면 재측정이 필수**다(미적재면 변화 0).

---

## P-R. 롤백

```
ssh <서버> 'cp <배포경로>/server/geocode-api-pg.py.bak <배포경로>/server/geocode-api-pg.py && \
            docker compose --profile postgis restart geocode-pg'
```

- `geocode-api-pg.py` 는 read-only bind mount 라 **이미지 재빌드 불요**.
- 사전은 **롤백 불필요** — `lawd_ri` 는 읽기 전용 신규 테이블이며 구버전 코드는 참조하지 않는다.
  완전 제거가 필요하면 `DROP TABLE lawd_ri;`.
- 재실행은 멱등이다. `build_ri_dict.sql` 은 `TRUNCATE` + `INSERT` 를 단일 트랜잭션에 묶으므로
  몇 번을 돌려도 같은 결과가 되고, 실패하면 이전 상태가 남는다.

---

## 부록 A. SSH 없이 `parcel.pnu` 체계 확인 (P0-3 대체)

리 보유 읍면 질의를 시도별로 던져 `b_code` 앞 2자리를 확인한다.
**구 접두(42/45)가 나오면 리매핑 전제가 깨진 것**이다.

```
for q in "강원 춘천 신북읍 천전리 300" "전북 완주 소양면 월상리 1" \
         "세종 조치원읍 죽림리 245" "경북 경주 안강읍 노당리 1" "전남 담양 봉산면 유산리 1"; do
  curl -s --get 'http://112.216.247.186:18080/geocode' --data-urlencode "q=$q" \
    | /usr/bin/grep -o '"b_code":"[0-9]*"' | head -3
done
```

기대: 앞 2자리가 `51`(강원)·`52`(전북)·`36`(세종)·`27`(경북)·`46`(전남).
**표본 확인이므로 전수 커버리지는 [미확인]** 이다 — 보고에 표본임을 명시할 것.

## 부록 B. 이번 범위에서 해결되지 않는 것

- **`areas` 의 리 레벨 항목** — `admin_boundary` 에 리 경계가 적재돼 있지 않다. 경계 소스 확보가 별도 과제다.
  (`reverse()` 는 `level` 필터 없이 `ST_Contains` 만 하므로, 행이 들어가면 코드 변경 없이 나온다.)
- **POI structure 의 `sido`/`sigungu`** — 해당 레벨 경계 소스가 서버에 없다. `emd` 만 변경 ④로 채워진다.
- **산번지 혼입** — `san` 은 현행 `null` 유지.
- **세종·군위의 5자리 코드 재편** — 2자리 시도 리매핑으로는 처리 불가. 별도 과제.
- **`q=청평리 432-11` 을 필지로 만드는 것** — 읍면동 토큰이 없는 질의는 게이팅에 의해 사전을 타지 않는다.
  게이팅을 풀면 상호명 오탐 20,078건이 되살아난다. **트레이드오프는 운영자 판단 사항이다.**

---

## 실행 기록 (운영자가 채운다)

| 항목 | 실행일시 | 결과 | 비고 |
|---|---|---|---|
| P0-1 `address.ri` 건수 | | **[미확인]** | |
| P0-2 `pnu` 리코드 보유 | | **[미확인]** | |
| P0-3 `pnu` 시도접두 분포 | | **[미확인]** | |
| P0-4 `admin_boundary` 레벨 | | **[미확인]** | |
| **G-1 판정** | | **[미확인]** | 통과/닫힘 |
| P2 `would_rows` | | **[미확인]** | 허용 [15000, 20000] |
| P2 자기검증 0 `pct` | | **[미확인]** | <99 면 중단 |
| P2 자기검증 1 `pct` | | **[미확인]** | <99 면 중단 |
| P2 자기검증 2 keys/distinct_ri | | **[미확인]** | |
| P2 자기검증 3 충돌 건수 | | **[미확인]** | |
| P2 자기검증 4 `청평리` | | **[미확인]** | 없으면 기준 1 불가 |
| 재정의 기준 2 행수 | | **[미확인]** | 1행/2행+ |
| P1b 회귀 7건 diff | | **[미확인]** | |
| 골든셋 3회 (before/after) | | **[미확인]** | PASS/SLOW/FAIL 집계 |
| BitmapOr EXPLAIN | | **[미확인]** | |
| reverse limit=30 중앙값 | | **[미확인]** | |
