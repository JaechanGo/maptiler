# 리(里) 백필 — 운영 적용 절차서 (태스크 018 Phase 1)

> ## ⚠ 이 문서의 성격 — 먼저 읽을 것
>
> **본 절차는 별도 승인 후에 실행한다.**
> **태스크 018 시점에는 실행하지 않았다.** 태스크 018 은 Master 결정(검증 3-ㄴ)에 따라
> **로컬 검증까지만** 수행했고, 운영 서버 `192.168.102.245` 는 **조회를 포함해 일절 건드리지 않았다.**
>
> 이 문서는 "승인이 떨어진 뒤 다른 사람이 이 문서 하나만 보고 실행할 수 있게" 만든 실행본이다.
> 계획서(`plan.md`)를 다시 뒤질 필요가 없도록 검증 쿼리와 기대값을 각 단계에 인라인으로 넣었다.

| 항목 | 값 |
|---|---|
| 대상 | 운영 PostGIS `192.168.102.245` |
| 적용 범위 | **Phase 1 만** — 법정동코드 사전(`lawd_code`/`lawd_ri`) 재구축 + `address.bcode` 리 2자리 백필 |
| 범위 밖 | Phase 2(API 코드 정합), Phase 3(전남·광주 통합) — 각각 별도 배포·별도 승인 |
| 변경되는 것 | `address.bcode` 의 **뒤 2자리만**. 앞 8자리는 불변 |
| 서비스 중단 | **원칙 무중단** (예외 2곳 — §14) |
| 선행 승인 | Master(또는 운영 책임자) 명시 승인 |
| 작성 근거 | `.team/tasks/018-b-code/runs/task-018-1786289274/plan.md` §B-4 / §S1–S5 / §B-2 |

**표기 규칙**: **[측정]** 로컬에서 실제로 측정한 값 · **[추정]** 계산·경험 기반 추정 · **[미확인]** 확인되지 않음 · **[미확정]** 실측 대기 중

---

## 0. 이 절차가 하는 일 / 하지 않는 일

### 하는 일

1. 행정안전부 **법정동코드 원본**(53,387행)을 `lawd_code` 로 적재한다 — 신설 테이블.
2. 리 사전 `lawd_ri` 를 원본 기반으로 **재구축·교체**한다. 현행 사전은 `address` 에서 유도돼 오염돼 있다.
3. `address.bcode` 가 `…00` 으로 끝나 리 자리가 비어 있는 행에 **리 2자리를 채운다**. 로컬 기준 약 674만 행 **[측정]**.

### 하지 않는 일

- **앞 8자리(시도·시군구·읍면동)를 바꾸지 않는다.** 전남(46)·광주(29) 코드는 그대로 남는다.
  → 전남·광주 통합(12 체계) 이관은 Phase 3 이며 이 절차의 범위가 아니다. `docs/jeonnam-gwangju-planB-handover.md` 참조.
- **API 코드를 배포하지 않는다.** 컨테이너 재시작은 마지막에 1회(캐시·커넥션 정리 목적)만 한다.
- **`parcel`·`building`·`admin_boundary` 를 건드리지 않는다.**

### 실행자가 갖춰야 할 것

- 운영 서버 SSH 접속 권한, `docker` 실행 권한
- 운영 PostGIS 의 DB 슈퍼유저 또는 대상 테이블 소유자 권한
- 디스크 여유 **최소 40 GB** (dump 약 5.2 GB **[추정]** + 백필 중 heap·인덱스 팽창 약 8 GB **[추정]** + 여유)
- 롤백 판단 권한 — 게이트 미달 시 **중단**을 스스로 결정할 수 있어야 한다

---

## 1. 준비 — 전송 대상과 SHA256 대조

### 1-1. 전송 대상

| # | 대상 | 위치(로컬) | 비고 |
|---|---|---|---|
| 1 | **법정동코드 원본 zip** | 태스크 018 조사 산출물 `lawd_full.bin` (413,346 bytes) **[측정]** | **재다운로드하지 말 것** — §1-3 |
| 2 | `scripts/postgis/fetch_lawd_code.sh` | repo | 다운로드 재현용(참고). 이번 적용에서는 **쓰지 않는다** |
| 3 | `scripts/postgis/load_lawd_code.py` | repo | CP949→UTF-8 변환 + `COPY` 적재 |
| 4 | `scripts/postgis/build_ri_dict_from_lawd_code.sql` | repo | **신** 사전 생성(S1+S2) |
| 5 | `scripts/postgis/build_ri_dict.sql` | repo | **폐기 가드가 들어간 개정본**. 반드시 함께 전송해 구 파일을 덮어쓴다 (§8) |
| 6 | `scripts/postgis/backfill_ri_bcode.py` | repo | 배치 UPDATE(재개 가능) |
| 7 | `docs/ri-dict-runbook.md` | repo | 갱신본 |
| 8 | **이 문서** | repo | |

전송 방법은 자유(`scp`/`rsync`). 배포 경로 주의사항은 **§16 을 반드시 먼저 읽을 것.**

### 1-2. SHA256 기록·대조

전송 **전** 로컬에서, 전송 **후** 운영에서 각각 측정해 **문자열이 완전히 같은지** 확인한다.

```bash
# 로컬 / 운영 양쪽에서 동일하게
sha256sum lawd_full.bin
sha256sum scripts/postgis/*.py scripts/postgis/*.sql
wc -c lawd_full.bin        # 기대 413346  [측정]
```

원본의 기준 SHA256 **[로컬 실측 — phase1-log.md §1]**:

| 대상 | 값 |
|---|---|
| `법정동코드 전체자료.txt` (= 전송물 `lawd_full.bin`) | `7b4b544a6302d26c4f4c89d2c1355beae82e958c786bad8cc8572db0d2e2eb33` |
| 크기 | **413,346 bytes** (위 `wc -c` 기대값과 같다) |
| 인코딩 / 헤더 | CP949 / `['법정동코드','법정동명','폐지여부']` |
| 데이터 행수 | **53,387** |
| 감싼 zip (`법정동코드 전체자료.zip`) | **[미측정]** — 압축 파일은 리포지토리에 커밋하지 않았고 전송 대상도 아니다. 압축 재생성 시 타임스탬프 때문에 해시가 달라져 대조 기준으로 쓸 수 없다. **대조는 위 txt 해시로만 한다.** |

> 이 값은 태스크 018 Phase 1 실행 Agent 가 로컬에서 측정해 기록한 것이다(`phase1-log.md` §1).
> Phase 1 은 **재취득하지 않고** 조사 단계에서 내려받은 파일을 그대로 썼다 — 로컬 검증 결과의
> 유효성이 이 파일에 묶여 있기 때문이다(§B-4-3). 운영에도 **이 파일을 그대로 전송**한다.
> 값이 비어 있거나 대조에 실패하면 **이 절차를 시작하지 말 것.**
> 대조 기준이 없는 상태로 "받아서 쓰기"를 하면 §1-3 의 위험이 그대로 실현된다.

### 1-3. SHA256 불일치 시 절차 (계획 §B-4-3 / 권고 16)

**원칙: 로컬 파일을 그대로 사용한다.** 로컬 검증 결과(모든 게이트 기대값)의 유효성이 그 파일에 묶여 있기 때문이다.
원본이 갱신되면 53,387 / 20,560 / 15,209 / 17,896 / 6,743,655 가 **전부 동시에** 흔들린다.

불일치가 관측되면:

1. **먼저 전송 오류를 의심한다.** 재전송 후 재측정. 여기서 일치하면 그대로 진행.
2. 로컬 파일 자체가 소실돼 **재다운로드가 불가피한 경우에만** 다음을 **전부** 수행한 뒤 진행한다.
   - `plan.md` §A 절 수치 **전량 재측정**
   - **S1 / S2 / S3 게이트 기대값 재산정**
   - **로컬에서 Phase 1 재검증**(S1–S5 + 검증 하니스)
3. **재측정 없이 운영에 적용하지 않는다.** 게이트는 기대값이 맞을 때만 게이트로 기능한다.

---

## 2. 실행 순서 — 개요

| STEP | 내용 | 게이트 | `address` 변경 | 되돌리기 |
|---|---|---|---|---|
| **0** | 운영/로컬 동일성 대조 | **★ 최우선** | 없음 | — |
| **1** | 파일 전송 + SHA256 대조 | ★ | 없음 | — |
| **2** | `pg_dump` 논리 백업 | ★ | 없음 | — |
| **3** | S1 — 원본 적재 → `lawd_code` | ★ | 없음 | `DROP TABLE` |
| **4** | S2 — 사전 재구축 → `lawd_ri` 교체 | ★ | 없음 | 대칭 롤백 <5초 |
| **5** | S2-b — 구 생성 스크립트 무력화 | — | 없음 | 파일 되돌림 |
| **6** | S3 — 백필 대상 산출 `_ri_backfill_todo` | **★ 진행 게이트** | **없음** | `DROP TABLE` |
| **7** | S4 — 스냅샷 백업 | ★ | 없음 | — |
| **8** | S5 — 배치 UPDATE | ★ | **있음** | 역UPDATE |
| **9** | 최종 검증 | ★ | 없음 | — |
| **10** | API 재시작 | — | 없음 | — |

> **STEP 6 까지는 `address` 가 전혀 바뀌지 않는다.** 여기까지는 언제든 저비용으로 물러설 수 있다.
> 되돌리기 비용이 급증하는 지점은 **STEP 8** 이다.

접속 (운영 서버에서):

```bash
# 컨테이너 이름은 운영에서 다를 수 있다 — 먼저 확인할 것  [미확인]
command docker compose -f /home/gjc/maptiler/server/docker-compose.yml ps
# 이하 PSQL 은 확인된 컨테이너 이름으로 치환해 사용
command docker exec -i <postgis컨테이너> psql -U cuvia -d cuvia -v ON_ERROR_STOP=1
```

---

## 3. STEP 0 — 운영/로컬 동일성 게이트 ★최우선

**이 대조가 1단계인 이유**: 이후 모든 게이트 기대값은 **로컬 DB 를 기준으로 산출**됐다.
운영 행수가 로컬과 다르면 **모든 기대값이 무효**가 되고, 게이트는 통과 여부를 판정할 수 없는 장식이 된다.

```sql
-- ① 전체 주소 행수
SELECT count(*) FROM address WHERE kind='addr';

-- ② 리 이름은 있는데 리 코드가 비어 있는 행 (= 백필 후보 모수)
SELECT count(*) FROM address
 WHERE kind='addr' AND ri <> '' AND ri IS NOT NULL AND right(bcode,2)='00';

-- ③ 현행 리 사전 행수
SELECT count(*) FROM lawd_ri;

-- ④ 읍면동 사전 행수
SELECT count(*) FROM lawd_dong;

-- ⑤ 전남·광주 분포
SELECT left(bcode,2), count(*) FROM address
 WHERE kind='addr' AND left(bcode,2) IN ('46','29') GROUP BY 1;

-- ⑥ 리 자리가 이미 채워진 행 (백필 전에는 0 이어야 한다)
SELECT count(*) FROM address WHERE kind='addr' AND right(bcode,2) <> '00';
```

| # | 항목 | 로컬 기대값 **[측정]** | 운영 실측 | 판정 |
|---|---|---:|---:|---|
| ① | `address` addr 행수 | **10,686,547** | | |
| ② | 백필 후보 모수 | **6,892,473** | | |
| ③ | `lawd_ri` | **16,113** | | |
| ④ | `lawd_dong` | **5,046** | | |
| ⑤ | `bcode` 46 / 29 | **1,159,723 / 163,664** | | |
| ⑥ | 리 자리 기채움 | **0** | | |

**판정 기준**

| 관측 | 행동 |
|---|---|
| 6개 항목 전부 일치 | **진행.** 이후 기대값을 그대로 사용한다 |
| ⑥ 이 0 이 아님 | **즉시 중단.** 운영에 이미 백필이 일부 적용됐다는 뜻이다. 이력을 먼저 규명한다 |
| ①②⑤ 가 다름 | 데이터 시점이 다르다. **모든 게이트 기대값을 운영 기준으로 재산정한 뒤에만** 진행 (§3-1) |
| ③④ 가 다름 | 사전 상태가 다르다. **중단.** 운영 사전이 어떤 스크립트로 만들어졌는지 먼저 확인 |
| `lawd_code` 가 이미 존재 | **중단.** 이 절차가 이미 부분 적용됐을 가능성 |

### 3-1. 기대값 재산정 방법 (①②⑤ 불일치 시)

운영 행수를 기준으로 다음을 다시 계산한다. 비율은 로컬 실측을 그대로 쓴다 **[측정]**.

| 게이트 | 재산정식 |
|---|---|
| S3 `_ri_backfill_todo` 총건수 | 운영 ② × **0.9784** (±0.5%p) |
| §B-3 매칭 실패 행수 | 운영 ② × **0.0216** |
| S4 `address_bcode_bak` 행수 | 운영 ① 과 동일 |
| S5 `right(bcode,2)<>'00'` | S3 게이트 통과값과 동일 |

`lawd_code`·`lawd_ri_new` 기대값(53,387 / 20,560 / 17,896 / 2,687 / 15,209 / 1,411)은 **원본 파일에서만 결정되므로 재산정 대상이 아니다.** 이 값들이 어긋나면 그것은 원본이 바뀌었다는 뜻이며 §1-3 로 간다.

> **운영 서버는 이 대조를 마치고 승인이 확인되기 전까지 읽기 전용으로 취급한다.**

---

## 4. STEP 1 — 파일 전송 + SHA256 대조

§1 을 수행한다. 대조 실패 시 §1-3.

전송 위치: `/home/gjc/maptiler/` 이하 원 구조 그대로.
**§16 의 덮어쓰기 금지 파일 3종을 반드시 먼저 확인할 것.**

---

## 5. STEP 2 — `pg_dump` 논리 백업 ★

**이 백업이 최종 방어선이다.** STEP 8 이후의 광범위한 사고에서 되돌릴 수 있는 유일한 수단이다.

```bash
# 운영 서버에서. 컨테이너 밖으로 파일을 뽑는 형태
command docker exec <postgis컨테이너> \
  pg_dump -U cuvia -d cuvia \
    -t address -t lawd_ri -t lawd_dong -t lawd_sigungu -t lawd_code \
    -Fc -f /tmp/t018_pre.dump

command docker cp <postgis컨테이너>:/tmp/t018_pre.dump /home/gjc/backup/t018_pre.dump
sha256sum /home/gjc/backup/t018_pre.dump | tee /home/gjc/backup/t018_pre.dump.sha256
ls -l /home/gjc/backup/t018_pre.dump
```

**`lawd_ri` 를 반드시 포함해야 하는 이유**: S2 에서 `lawd_ri` 는 **테이블째 교체**된다.
`address` 만 백업하면 사전을 되돌릴 수 없다. `lawd_dong`·`lawd_sigungu` 는 조인 상대이고,
`lawd_code` 는 신설이지만 **적재 결과의 재현성 확인용**으로 함께 담는다.

| 항목 | 값 |
|---|---|
| 예상 용량 | **[추정] 약 5.2 GB** — 실측: **[미측정]** (아래 사유 참조) |
| 예상 소요 | **[추정] 약 10–20분** — 실측: **[미측정]** (아래 사유 참조) |
| 디스크 여유 | 로컬 기준 344 GB **[측정]**. 운영은 **실행 전 `df -h` 로 직접 확인할 것** **[미확인]** |

> **왜 [미측정] 인가.** Phase 1 로컬 실행은 이 STEP 을 수행하지 않았다. 로컬에는 되돌릴 운영 데이터가
> 없고, 롤백 경로로는 STEP 7 의 `address_bcode_bak_20260810` 스냅샷(§10)을 썼기 때문이다.
> `phase1-log.md` 전문에 `pg_dump` 실행 기록이 없다 — **없는 값을 실측치인 양 옮겨 적지 않는다.**
> 운영에서 이 STEP 을 처음 수행하는 사람이 **최초 실측자**다. 아래 표에 값을 기록해 두면
> 다음 회차의 추정이 정확해진다. 기록란: 용량 `______ GB` / 소요 `______` / 측정일 `______`
>
> **[로컬 실측 → 운영 환산] 참고 근거.** 크기 감각만 옮겨 적으면 다음과 같다 —
> 로컬 `address` 테이블 실크기는 백필 전 **8,464 MB**, 백필 후 **10,222 MB** **[측정]** 이다.
> `pg_dump -Fc` 는 힙만 덤프하고(인덱스는 정의만) 압축하므로 위 5.2 GB 추정은 같은 자릿수다.
> 다만 **운영 `address` 행수·크기가 로컬과 같다는 보장이 없으므로** 실행 전 반드시
> `SELECT pg_size_pretty(pg_total_relation_size('address'));` 로 운영 실측치를 먼저 확인한다.

**게이트**: `pg_dump` 가 **exit 0** 이고 파일 크기가 0 이 아니며 SHA256 이 기록됐을 것.
하나라도 아니면 **진행 금지**.

> 무결성을 더 확실히 하려면 별도 임시 DB 에 `pg_restore --schema-only` 로 복원 시연을 해 둔다(선택, **[추정]** 수 분).

---

## 6. STEP 3 — S1 원본 적재 → `lawd_code` ★

```sql
DROP TABLE IF EXISTS lawd_code;
CREATE TABLE lawd_code (
  bcode  char(10) PRIMARY KEY,
  name   text     NOT NULL,
  exist  boolean  NOT NULL          -- 폐지여부='존재'
);
CREATE INDEX lawd_code_emd8_idx ON lawd_code (left(bcode,8));
CREATE INDEX lawd_code_name_idx ON lawd_code (name);
```

적재는 `scripts/postgis/load_lawd_code.py` (psycopg3 `copy()`).
원본은 **CP949 · TAB 구분 · 3컬럼**, 헤더 1행 스킵, `폐지여부='존재'` → `exist=true`.

### 검증 쿼리 / 기대값 **[측정 기준]**

```sql
SELECT count(*) FROM lawd_code;                                     -- 기대 53,387
SELECT exist, count(*) FROM lawd_code GROUP BY 1;                   -- 기대 t=20,560 / f=32,827
SELECT count(*) FROM lawd_code WHERE bcode !~ '^\d{10}$';           -- 기대 0
SELECT count(*) FROM lawd_code WHERE left(bcode,2)='12' AND exist;  -- 기대 3,204
```

**미달 시 행동**: 하나라도 어긋나면 **즉시 중단**. 원본이 갱신됐다는 뜻이다 → §1-3 의 재측정 경로로 간다.
(이 기대값들은 §3-1 의 재산정 대상이 **아니다.** 운영 행수와 무관하게 원본 파일만으로 결정된다.)

**롤백**: `DROP TABLE lawd_code;` — 다른 무엇에도 영향 없음.

---

## 7. STEP 4 — S2 사전 재구축 → `lawd_ri` 교체 ★

### 7-1. 백업과 생성

```sql
-- 백업 (이름에 날짜 고정)
CREATE TABLE lawd_ri_bak_20260810 AS SELECT * FROM lawd_ri;
SELECT count(*) FROM lawd_ri_bak_20260810;           -- 기대 16,113 (STEP 0 ③ 과 같아야 한다)

DROP TABLE IF EXISTS lawd_ri_new;
CREATE TABLE lawd_ri_new (
  emd_cd char(8) NOT NULL,
  ri     text    NOT NULL,
  ri_cd  char(2) NOT NULL,
  exist  boolean NOT NULL,          -- true=존재코드, false=폐지코드 폴백(전남·광주 한정)
  PRIMARY KEY (emd_cd, ri)
);
COMMENT ON COLUMN lawd_ri_new.exist IS
  'false = 46/29(전남·광주) 폐지코드 폴백. Phase 3 안 B 채택 시에만 제거 가능 — 안 A 에서는 영구 잔존.';

WITH cand AS (
  SELECT left(bcode,8) AS emd_cd,
         split_part(name, ' ', array_length(string_to_array(name,' '),1)) AS ri,
         right(bcode,2) AS ri_cd,
         exist
    FROM lawd_code
   WHERE right(bcode,2) <> '00'
     -- 폐지 폴백은 전남·광주 통합 미반영 보상 전용이며, DB 가 실제로 쓰는 읍면동으로 한정한다.
     AND (exist OR (left(bcode,2) IN ('46','29')
                    AND left(bcode,8) IN (SELECT emd_cd FROM lawd_dong)))
), ranked AS (
  SELECT emd_cd, ri, ri_cd, exist,
         count(*)     OVER (PARTITION BY emd_cd, ri, exist) AS n_same_tier,
         row_number() OVER (PARTITION BY emd_cd, ri ORDER BY exist DESC, ri_cd) AS rn
    FROM cand
)
INSERT INTO lawd_ri_new (emd_cd, ri, ri_cd, exist)
SELECT emd_cd, ri, ri_cd, exist FROM ranked
 WHERE rn = 1 AND n_same_tier = 1;   -- 같은 티어에서 후보가 유일할 때만 채택
```

> **설계 판단 — 왜 사전을 46/29 체계로 만드는가.**
> 사전의 코드 체계는 **DB 의 코드 체계와 일치해야** `parcel.emd_cd`·`lawd_dong` 과 조인된다.
> Phase 1 시점의 DB 는 전남·광주가 46/29 이므로 사전도 46/29 여야 한다.
> 존재 행만으로 만들면 전남 항목이 전부 12 로 바뀌어 **지금 동작하는 전남 리 지오코딩까지 깨진다.**

### 7-2. 검증 쿼리 / 기대값 — **가장 위험한 지점**

```sql
-- ⓪ 총량과 폴백 범위
SELECT count(*) FROM lawd_ri_new;                                   -- 기대 17,896
SELECT count(*) FROM lawd_ri_new WHERE NOT exist;                   -- 기대  2,687
SELECT left(emd_cd,2), count(*) FROM lawd_ri_new WHERE NOT exist GROUP BY 1;
--    기대: '46' 2,687 **단 한 행**. '29' 를 포함해 그 외 시도가 하나라도 나오면 중단

-- ① 존재 티어 유일성
SELECT count(*) FROM lawd_ri_new WHERE exist;                       -- 기대 15,209
SELECT count(DISTINCT emd_cd) FROM lawd_ri_new WHERE exist;         -- 기대  1,411

-- ② 한 이름에 복수 코드 / 한 코드에 복수 이름 — 둘 다 0
SELECT count(*) FROM (SELECT emd_cd,ri FROM lawd_ri_new WHERE exist
                      GROUP BY 1,2 HAVING count(*)>1) x;            -- 기대 0
SELECT count(*) FROM (SELECT emd_cd,ri_cd FROM lawd_ri_new WHERE exist
                      GROUP BY 1,2 HAVING count(*)>1) x;            -- 기대 0

-- ③ 만들어진 10자리가 원본에 실재하는지 (존재 티어 한정)
SELECT count(*) FROM lawd_ri_new r WHERE r.exist
  AND NOT EXISTS (SELECT 1 FROM lawd_code c
                   WHERE c.bcode = r.emd_cd||r.ri_cd AND c.exist);  -- 기대 0

-- ④ 유가읍 육안 검증
SELECT ri_cd, ri FROM lawd_ri_new WHERE emd_cd='27710259' ORDER BY ri_cd;
--    기대 13행: 21음리 22양리 23용리 24봉리 25쌍계리 26초곡리 27상리
--               28금리 29유곡리 30도의리 31가태리 32한정리 33본말리
```

**게이트 미달 시 행동**

| 관측 | 판단 | 행동 |
|---|---|---|
| ⓪ 총량이 17,896 ± 50 밖 | 원본 갱신 또는 CTE 오작성 | **중단.** S1 검증부터 재확인. 원인 규명 전 진행 금지 |
| ⓪ 폴백에 46 외 시도 등장 | 폴백 필터가 새는 중 — **초판이 만든 사고 형태** | **즉시 중단.** `DROP TABLE lawd_ri_new` 후 CTE 수정 |
| ⓪ 폴백 < 2,600 | `lawd_dong` 46분이 예상보다 적음 | **중단.** `lawd_dong` 의 46/29 = 622행을 먼저 확인 |
| ①–③ 중 하나라도 0 아님 | 원본 무결성 붕괴 | **중단** |
| ④ 불일치 | 사전 논리 결함 | **중단** |

### 7-3. 교체 (원자적)

**사전 점검 — 실제 인덱스명을 먼저 확인한다.**

```sql
SELECT indexname FROM pg_indexes WHERE tablename IN ('lawd_ri','lawd_ri_new') ORDER BY 1;
--   기대(교체 전): lawd_ri_new_pkey, lawd_ri_pkey, lawd_ri_ri_emd_idx
```

운영 인덱스명이 위와 다르면 **아래 `ALTER INDEX` 대상을 실측값으로 치환**한 뒤 실행한다. **[미확인]**

```sql
BEGIN;
  -- 1) 구 테이블의 인덱스를 먼저 개명해 이름을 비운다
  ALTER INDEX lawd_ri_pkey       RENAME TO lawd_ri_old_20260810_pkey;
  ALTER INDEX lawd_ri_ri_emd_idx RENAME TO lawd_ri_old_20260810_ri_emd_idx;
  -- 2) 테이블 교체
  ALTER TABLE lawd_ri            RENAME TO lawd_ri_old_20260810;
  ALTER TABLE lawd_ri_new        RENAME TO lawd_ri;
  -- 3) 신 테이블의 자동생성 PK 인덱스명을 표준명으로
  ALTER INDEX lawd_ri_new_pkey   RENAME TO lawd_ri_pkey;
  -- 4) 조회 경로 인덱스 재생성
  CREATE INDEX lawd_ri_ri_emd_idx ON lawd_ri (ri, emd_cd);
  CREATE INDEX lawd_ri_emd_cd_idx ON lawd_ri (emd_cd, ri_cd);
COMMIT;
```

> **`ALTER TABLE ... RENAME` 은 인덱스 이름을 바꾸지 않는다.** 테이블과 인덱스가 `pg_class` 네임스페이스를
> 공유하므로, 구 테이블이 `lawd_ri_ri_emd_idx` 를 계속 점유한 채 동명 인덱스를 만들려 하면 `already exists` 로
> **확정 실패**한다. 위 1) 단계가 그것을 막는다.

**교체 직후 확인**

```sql
SELECT count(*) FROM lawd_ri;                                    -- 기대 17,896
SELECT count(*) FROM lawd_ri_old_20260810;                       -- 기대 16,113
SELECT indexname FROM pg_indexes WHERE tablename='lawd_ri' ORDER BY 1;
--   기대: lawd_ri_emd_cd_idx, lawd_ri_pkey, lawd_ri_ri_emd_idx
```

**스키마 호환성**: 신 `lawd_ri` 는 PK 가 `(emd_cd, ri)` 로 바뀌고 `exist` 컬럼이 늘어난다.
`server/geocode-api-pg.py` 의 `lawd_ri` 사용처는 `:316`(존재 확인)·`:468`(명시적 컬럼 SELECT) 2곳뿐이고
`SELECT *` 가 없다 **[리뷰실측]**. 따라서 **API 코드 변경 없이 호환된다.** 단 `ri_cd` 컬럼명은 반드시 유지한다.

**락**: 이 트랜잭션 동안 `lawd_ri` 에 `ACCESS EXCLUSIVE` 락이 걸린다. **[추정] 1초 미만** (§14 예외 1).

---

## 8. STEP 5 — S2-b 구 생성 스크립트 무력화

**누락하면 조용히 되돌아간다.** 구 `scripts/postgis/build_ri_dict.sql` 이 그대로 남아 있으면,
누군가 T014 런북을 따라 재실행하는 순간 신 사전이 구 방식으로 덮어써진다.
더 위험한 것은 그 스크립트의 양방 가드가 `lo=15000, hi=20000` 이라
**신 사전 17,896행이 그 범위 안에 들어가 아무 경고 없이 "성공"한다**는 점이다 **[측정]**.

1. `scripts/postgis/build_ri_dict.sql` 선두에 폐기 가드가 들어간 개정본이 전송됐는지 확인한다.

   ```sql
   -- ⚠ 폐기 (T018 Phase 1, 2026-08-10). 이 스크립트는 address 유도 방식이라 사전을 오염시킨다.
   --    대체: scripts/postgis/build_ri_dict_from_lawd_code.sql (법정동코드 원본 기반)
   DO $dead$ BEGIN
     RAISE EXCEPTION 'build_ri_dict.sql 은 T018 에서 폐기됐다. build_ri_dict_from_lawd_code.sql 을 사용하라.';
   END $dead$;
   ```

2. `scripts/postgis/build_ri_dict_from_lawd_code.sql` 이 존재하는지 확인한다.
3. `docs/ri-dict-runbook.md` 갱신본이 반영됐는지 확인한다 — 폐기 사실, 대체 절차,
   신 가드 기대값(**17,896 / 폴백 2,687 / 폴백 시도 46 단일**).

**확인 방법**

```bash
command head -8 /home/gjc/maptiler/scripts/postgis/build_ri_dict.sql   # 폐기 가드가 보여야 한다
command ls -l /home/gjc/maptiler/scripts/postgis/build_ri_dict_from_lawd_code.sql
```

---

## 9. STEP 6 — S3 백필 대상 산출 `_ri_backfill_todo` ★진행 게이트

**여기까지 `address` 는 전혀 바뀌지 않는다.** `_ri_backfill_todo` 는 작업 목록이자 **롤백 원본**이므로
반드시 `LOGGED`(기본)로 만든다. `UNLOGGED` 로 만들면 크래시 시 롤백 원본이 사라진다.

### 9-1. 재실행 가드 (반드시 먼저 실행)

```sql
DO $guard$
DECLARE n bigint := 0;
BEGIN
  IF to_regclass('public._ri_backfill_progress') IS NOT NULL THEN
    EXECUTE $q$SELECT count(*) FROM _ri_backfill_progress WHERE k='last_id'$q$ INTO n;
    IF n > 0 THEN
      RAISE EXCEPTION 'S3 재실행 금지: 백필이 이미 진행됐다. 롤백을 완료하고 _ri_backfill_progress 를 비운 뒤 재시도하라.';
    END IF;
  END IF;
END $guard$;
```

> **이 가드가 막는 사고**: S5 를 부분 실행한 뒤 S3 을 재실행하면 (a) `done` 플래그가 소실되고,
> (b) 이미 갱신된 행은 `right(bcode,2)='00'` 조건에 걸리지 않아 todo 에서 빠지면서 **`old_bcode` 가 영구 소실**되며,
> (c) 살아남은 `_ri_backfill_progress.last_id` 와 새 todo 가 어긋나 **재개가 앞 구간을 통째로 건너뛴다.**
> 셋 다 조용히 일어난다.
>
> **주의**: `to_regclass` + 동적 `EXECUTE` 형태를 그대로 쓸 것. `IF EXISTS(...) AND EXISTS(...)` 형태는
> PL/pgSQL 이 `IF` 표현식 전체를 하나의 SQL 로 파스하므로, 아직 없는 `_ri_backfill_progress` 를 정적 참조하면
> **최초 실행에서 파스 단계에 실패**한다(로컬 재현 확인 **[측정]**).
> 3케이스 검증 완료 **[측정]** — ①테이블 부재(정상 최초 실행) 통과 ②테이블 존재·`last_id` 없음(롤백 후 재시도) 통과 ③`last_id` 존재 시 예외 발생.

### 9-2. todo 생성

```sql
DROP TABLE IF EXISTS _ri_backfill_todo;
CREATE TABLE _ri_backfill_todo (
  id        bigint   PRIMARY KEY,
  old_bcode char(10) NOT NULL,     -- 롤백용 원본값
  new_bcode char(10) NOT NULL,
  src       text     NOT NULL,     -- 'ri' | 'jibun'
  done      boolean  NOT NULL DEFAULT false
);

-- (1) address.ri 로 매칭 (우선)
INSERT INTO _ri_backfill_todo (id, old_bcode, new_bcode, src)
SELECT a.id, a.bcode, left(a.bcode,8)||l.ri_cd, 'ri'
  FROM address a
  JOIN lawd_ri l ON l.emd_cd = left(a.bcode,8) AND l.ri = a.ri
 WHERE a.kind='addr'
   AND a.ri IS NOT NULL AND a.ri <> ''
   AND a.bcode IS NOT NULL AND length(btrim(a.bcode)) = 10
   AND right(a.bcode,2) = '00';

-- (2) 지번 리 토큰으로 폴백 (1에서 못 잡은 것만)
INSERT INTO _ri_backfill_todo (id, old_bcode, new_bcode, src)
SELECT a.id, a.bcode, left(a.bcode,8)||l.ri_cd, 'jibun'
  FROM address a
  CROSS JOIN LATERAL (
     SELECT t FROM unnest(string_to_array(btrim(a.jibun),' ')) AS t
      WHERE t LIKE '%리' AND char_length(t) >= 2 LIMIT 1
  ) j
  JOIN lawd_ri l ON l.emd_cd = left(a.bcode,8) AND l.ri = j.t
 WHERE a.kind='addr'
   AND a.ri IS NOT NULL AND a.ri <> ''
   AND a.bcode IS NOT NULL AND length(btrim(a.bcode)) = 10
   AND right(a.bcode,2) = '00'
   AND NOT EXISTS (SELECT 1 FROM _ri_backfill_todo t2 WHERE t2.id = a.id);
```

### 9-3. 검증 쿼리 / 기대값 — **이 단계가 진행 게이트다**

```sql
SELECT count(*) FROM _ri_backfill_todo;                  -- 기대 ≈ 6,743,655
SELECT src, count(*) FROM _ri_backfill_todo GROUP BY 1;  -- 'ri' 가 절대다수
SELECT min(id), max(id) FROM _ri_backfill_todo;          -- 로컬 기준 27,563,378 ~ 38,430,733 범위 내
SELECT count(*) FROM _ri_backfill_todo WHERE new_bcode !~ '^\d{10}$';                 -- 기대 0
SELECT count(*) FROM _ri_backfill_todo WHERE right(new_bcode,2)='00';                 -- 기대 0
SELECT count(*) FROM _ri_backfill_todo WHERE left(new_bcode,8) <> left(old_bcode,8);  -- 기대 0
```

**게이트 조건**: 총건수가 **6,743,655 ± 34,000 (±0.5%p)** 범위. (운영 행수가 로컬과 다르면 §3-1 로 재산정한 값 사용)

마지막 쿼리(`left(new_bcode,8) <> left(old_bcode,8)` = 0)는 **"앞 8자리 불변"을 강제하는 안전장치**다.
이것이 0 이 아니면 이 절차가 시도 코드를 건드리고 있다는 뜻이므로 **무조건 중단**한다.

**미달·초과 시 행동**: 진행하지 말고 지번 토큰 추출식 차이를 먼저 규명한다.
규명 후 계획 수치를 실측값으로 갱신하고 매칭 실패 기대값(148,818)도 함께 재산정한 뒤 진행한다.

**교차검증(권장)**: `src='ri'` 와 `src='jibun'` 이 모두 성립하는 행에서 두 결과가 일치하는 비율이
**99.25% [측정]** 근처인지 확인. 크게 낮으면 사전이나 추출식에 문제가 있다는 신호다.

**롤백**: `DROP TABLE _ri_backfill_todo;` — `address` 무변경 상태이므로 부수효과 없음.

---

## 10. STEP 7 — S4 스냅샷 백업 ★

`_ri_backfill_todo.old_bcode` 가 이미 **변경 대상 전 행의 완전한 롤백 원본**이다. 추가로 전 행 스냅샷을 만든다.

```sql
-- 약 10,686,547행 × ~30B ≈ 320 MB [추정]
CREATE TABLE address_bcode_bak_20260810 AS
  SELECT id, bcode FROM address WHERE kind='addr';
CREATE UNIQUE INDEX ON address_bcode_bak_20260810 (id);
SELECT count(*) FROM address_bcode_bak_20260810;   -- 기대 10,686,547 (STEP 0 ① 과 같아야 한다)
```

**게이트**: 행수가 STEP 0 ①과 일치할 것. 다르면 **중단** (그 사이 데이터가 변했다는 뜻).

소요 — **[로컬 실측] 총 27초** (03:01:21 → 03:01:48, `phase1-log.md` §2 S4). 사전 추정 20–60분은 **크게 과대**했다.

| 세부 단계 | 로컬 실측 |
|---|---|
| `CREATE TABLE AS` (10,686,547행) | **9.251s** |
| `CREATE UNIQUE INDEX` | **2.038s** |
| `ANALYZE` | **0.181s** |
| 검증 5문 | 약 **3.8s** |
| 스냅샷 크기 | **761 MB** (사전 추정 320 MB 의 약 2.4배) |

> **[로컬 실측 → 운영 환산]** 이 STEP 은 `address` 전 행을 1회 순차 읽는 비용이 지배한다.
> 로컬 `address` 는 백필 전 **8,464 MB / kind='addr' 10,686,547행** **[측정]** 이었다.
> 운영 규모가 같다면 **1분 이내**로 보면 되고, 운영 `address` 가 N배면 대략 N배로 늘어난다고
> 읽으면 된다(디스크 대역폭이 로컬과 비슷하다는 가정 — **[추정]**).
> 다만 **20–60분 추정은 지우지 말고 상한선으로 남겨 둔다**: 운영은 동시 트래픽이 있어
> I/O 경합으로 로컬보다 느려질 수 있다.
> 스냅샷 크기는 **320 MB 가 아니라 761 MB 를 기준으로 디스크를 확보**할 것.

---

## 11. STEP 8 — S5 배치 UPDATE ★되돌리기 비용이 급증하는 지점

실행: `scripts/postgis/backfill_ri_bcode.py` (psycopg3)

| 항목 | 값 |
|---|---|
| 청크 기준 | **`_ri_backfill_todo` 행 수** |
| 청크 크기 | **200,000행** → 약 **34청크** (6,743,655 / 200,000 = 33.7) |
| `last_id` 초기값 | `min(id) - 1` |
| 커밋 단위 | 청크 1개 = 트랜잭션 1개 → 중단 시 손실 최대 1청크 |
| 재개 | `_ri_backfill_progress(k,v)` 의 `last_id` 기준 |
| VACUUM | `n_dead_tup > 1,500,000` **조건부** |
| 종료 후 | `VACUUM (ANALYZE) address` 1회 |

### 11-1. 진행 상태 테이블

```sql
CREATE TABLE IF NOT EXISTS _ri_backfill_progress (k text PRIMARY KEY, v text);
-- 최초 1회
INSERT INTO _ri_backfill_progress(k,v)
SELECT 'last_id', (min(id)-1)::text FROM _ri_backfill_todo
ON CONFLICT (k) DO NOTHING;
```

### 11-2. 청크 1개

```sql
BEGIN;
  UPDATE address a
     SET bcode = t.new_bcode
    FROM _ri_backfill_todo t
   WHERE a.id = t.id
     AND t.id >  %(lo)s
     AND t.id <= %(hi)s;
  UPDATE _ri_backfill_todo SET done = true WHERE id > %(lo)s AND id <= %(hi)s;
  INSERT INTO _ri_backfill_progress(k,v) VALUES ('last_id', %(hi)s)
    ON CONFLICT (k) DO UPDATE SET v = EXCLUDED.v;
COMMIT;
```

진행 로그 형식:

```
[ri-backfill] chunk 012/034  id 29,842,117..30,116,880  updated 200,000  12.7s
              누계 2,400,000/6,743,655 (35.6%)  ETA 00:04:31
```

### 11-3. VACUUM 정책 — 실행상 주의

- 매 청크 후 `pg_stat_user_tables.n_dead_tup` 을 읽어 **1,500,000 초과 시에만** `VACUUM address` (ANALYZE 없이).
- `ANALYZE` 는 **종료 시 1회**만. `bcode` 에 인덱스가 없어 중간 통계 갱신의 이득이 없다 **[측정]**.
- **`VACUUM` 은 트랜잭션 블록 안에서 실행할 수 없다.** 스크립트는 **autocommit 연결** 또는 별도 연결에서 호출해야 한다.
  청크 트랜잭션과 같은 커넥션에서 `BEGIN` 이 열린 채 호출하면 `VACUUM cannot run inside a transaction block` 으로 실패한다.
- **`n_dead_tup` 은 통계 수집기 반영에 지연이 있다.** 임계 판정이 한 박자 늦을 수 있으며 이는 정상이다.
  판정 시각의 값과 VACUUM 실행 여부를 로그에 남겨 사후 검증 가능하게 한다.

**VACUUM 발동 이력 [로컬 실측 — `phase1-log.md` §3-4]** (임계 `DEAD_TUP_THRESHOLD = 1,500,000`)

| 발동 시점 | 판정 시각 `n_dead_tup` | VACUUM 소요 |
|---|---|---|
| chunk 08 직후 | 1,600,000 | 15.4s |
| chunk 16 직후 | 1,600,000 | 12.7s |
| chunk 25 직후 | **1,610,431** ← 최대치 | 14.9s |
| chunk 33 직후 | 1,584,513 | 14.4s |
| **합계** | **4회** | **57.4s** (총 소요의 **5.3%**) |
| 마무리 `VACUUM (ANALYZE)` 1회 | — | 13.7s |

- **관측된 `n_dead_tup` 최대치는 1,610,431** 로, 임계(1,500,000)를 **7.4% 넘긴 선에서 잡혔다.**
  통계 반영 지연이 있어도 폭주하지 않았다 — 200,000행 청크에서는 임계를 그대로 써도 안전하다.
- 발동 간격이 8·8·9·8 청크로 거의 일정하다. 청크 크기를 100,000행으로 낮추면 발동 간격은
  대략 2배(16~18 청크)로 늘어난다고 보면 된다 **[추정]**.
- 백필 종료 후 잔여 `n_dead_tup` 은 **137,635** 였다 **[측정]** — 마무리 VACUUM 이 정상 동작했다는 뜻이다.

### 11-4. 부하·팽창 예상

- `search_text` 생성식에 `bcode` 가 없다 **[측정]** → **인덱스 키는 재계산되지 않는다.**
  그러나 `fillfactor=100` **[측정]** 이라 HOT 갱신이 대부분 실패하고 새 힙 튜플이 생기므로
  **13개 인덱스(GIN 5 + GiST 2 + btree 6) 전부에 새 TID 엔트리가 추가된다** **[추정]**.
- 팽창 — 사전 **[추정]**: heap 5,209 MB → 최대 약 10 GB, 인덱스 3,255 MB → 최대 약 6 GB. 총 8.5 GB → 최대 약 16 GB.
  **[로컬 실측 — `phase1-log.md` §4]**: 실제 팽창은 추정 상한의 **약 2/3 에 그쳤다.**

  | 구분 | S5 전 | S5 후 | 증가 |
  |---|---|---|---|
  | 힙 | 5,209 MB | **5,722 MB** | +513 MB (+9.8%) |
  | 인덱스 | 3,255 MB | **4,499 MB** | +1,244 MB (**+38.2%**) |
  | **총계** | **8,464 MB** | **10,222 MB** | **+1,758 MB (+20.8%)** |

  → 팽창의 **71%가 인덱스 쪽**이다. 위 §11-4 첫 항목의 "13개 인덱스 전부에 새 TID 엔트리가 추가된다"는
  추정이 실측으로 확인됐다. **디스크는 힙이 아니라 인덱스 기준으로 확보할 것.**
  운영 적용 시 **디스크 여유는 최소 2 GB** 를 확보한다(로컬 규모 기준 **[로컬 실측 → 운영 환산]**;
  운영 `address` 가 N배면 대략 N배).

- **소요** — 사전 **[추정] 20–60분** / **[로컬 실측] 00:18:13 (1,093초)**. 추정 범위 안에 들어왔다.

  | 항목 | 로컬 실측 |
  |---|---|
  | 대상 | **6,743,655행 / 34청크** (청크 200,000행) |
  | 총 소요 | **00:18:13 (1,093s)** |
  | 평균 처리율 | **6,166 rows/s** |
  | 청크 UPDATE 34회 합 | **1,033.6s (94.6%)** |
  | 중간 VACUUM 4회 합 | 57.4s (5.3%) |
  | 마무리 `VACUUM (ANALYZE)` | 13.7s |
  | 청크 소요 최소 / 최대 | **24.4s / 42.9s** |
  | 청크 소요 평균 / 중앙값 | **30.74s / 30.4s** |
  | 청크 처리율 범위 | 8,197 ~ 4,662 rows/s |

  → **첫 3청크(60만 행) 실측으로 확정한다** 는 방침은 유효하다. 다만 **첫 청크는 캐시 미적재로
  가장 느리다**(42.9s, 최대치). 로컬에서 첫 3청크 기준 ETA 는 00:16:39 였고 실제는 00:18:13 —
  **오차 +00:01:34 (+9.4%)** 로 낙관 편향이 있었다. 운영에서도 **첫 3청크 ETA에 약 10%를 더해**
  잡을 것 **[로컬 실측 → 운영 환산]**. 마지막 청크는 잔여분(143,655행)이라 19.1s 로 짧다.
- 응답 지연 상승이 예상되므로 **트래픽이 적은 시간대에 시작**하고,
  첫 3청크 실측 후 지연이 허용치를 넘으면 **청크 크기를 100,000행으로 낮춘다.**

### 11-5. 검증 쿼리 / 기대값

```sql
SELECT count(*) FROM _ri_backfill_todo WHERE NOT done;                  -- 기대 0
SELECT count(*) FROM address WHERE kind='addr' AND right(bcode,2)<>'00';
--   기대 ≈ 6,743,655   (STEP 0 ⑥ 이 0 이었으므로 전량이 이번 변경분)
SELECT count(*) FROM address a JOIN _ri_backfill_todo t ON t.id=a.id
 WHERE a.bcode <> t.new_bcode;                                          -- 기대 0
-- 리 자리가 원본에 실재하는 코드인지 (존재+폐지 모두 허용 — Phase 1 이므로)
SELECT count(*) FROM address a
 WHERE a.kind='addr' AND right(a.bcode,2)<>'00'
   AND NOT EXISTS (SELECT 1 FROM lawd_code c WHERE c.bcode=a.bcode);    -- 기대 0
```

**게이트 미달 시 행동**

| 관측 | 행동 |
|---|---|
| `NOT done` > 0 | 중단된 청크가 있다. 스크립트를 **재개**(`last_id` 기준)하고 재검증. **S3 재실행 금지**(가드가 막는다) |
| 3번째 쿼리 ≠ 0 | 갱신값이 todo 와 불일치 — **즉시 롤백**하고 원인 규명 |
| 4번째 쿼리 ≠ 0 | 원본에 없는 코드를 만들었다 — **즉시 롤백**. 사전(S2)부터 재검증 |
| 2번째 쿼리가 게이트 범위 밖 | S3 게이트 값과 대조해 어느 단계에서 어긋났는지 특정 후 판단 |

---

## 12. STEP 9 — 최종 검증

```sql
-- ① 앞 8자리가 하나도 바뀌지 않았음을 사후 확인 (가장 중요)
SELECT count(*) FROM address a JOIN address_bcode_bak_20260810 b ON b.id=a.id
 WHERE left(a.bcode,8) <> left(b.bcode,8);                              -- 기대 0

-- ② 변경 행수가 todo 와 정확히 일치
SELECT count(*) FROM address a JOIN address_bcode_bak_20260810 b ON b.id=a.id
 WHERE a.bcode <> b.bcode;                                              -- 기대 ≈ 6,743,655

-- ③ 매칭 실패(리 이름은 있으나 코드 못 채운) 잔여
SELECT count(*) FROM address
 WHERE kind='addr' AND ri IS NOT NULL AND ri <> '' AND right(bcode,2)='00';
--   기대 ≈ 148,818 (2.16%)  — 크게 넘으면 사전/매칭식 문제 신호

-- ④ 시도 분포가 그대로인지 (Phase 1 은 시도를 바꾸지 않는다)
SELECT left(bcode,2), count(*) FROM address
 WHERE kind='addr' AND left(bcode,2) IN ('46','29') GROUP BY 1;
--   기대: STEP 0 ⑤ 와 완전히 동일

-- ⑤ 폐지 티어(exist=false) 코드로 채워진 행수 — ★ 아래 경고를 먼저 읽을 것
SELECT count(*) AS n_rows, count(DISTINCT a.bcode) AS n_codes
  FROM address a JOIN lawd_code c ON c.bcode = a.bcode
 WHERE a.kind='addr' AND c.exist = false;
--   기대 ≈ 1,000,000 행 / 약 2,500 코드 — **정상값이다. 롤백 사유가 아니다.**
```

### ★ 경고 — 폐지 티어 코드 약 100만 행은 **기대된 정상값이다**

STEP 9 를 처음 수행하는 운영자가 **가장 오판하기 쉬운 지점**이다.
백필 후 `lawd_code.exist = false` 인 코드로 채워진 행이 **약 100만 건** 나온다.
**이것은 백필 실패도, 사전 오염도 아니다. 롤백하지 말라.**

**근거 한 줄**: **전남이 통합(`12`) 체계를 현행으로 쓰기 때문에**, `lawd_code` 상의 구
전라남도(`46`) 계열 리 코드가 `exist = false` 로 내려간 것이다. 코드 자체는 실재하며
행정 이력상 유효하다 — 우리는 그 코드를 **정확히** 채웠고, 다만 그 티어가 폐지 상태일 뿐이다.

| 확인 항목 | Phase 1 로컬 실측 |
|---|---|
| 이번 백필분 중 폐지 티어 | **1,001,319행 / 2,503코드** |
| `address` 전체 기준 폐지 티어 | 1,003,490행 / 2,511코드 (백필 이전부터 있던 2,171행 포함) |
| 시도 분포 | 시도 `46`(전남) **단일 100.00%** |
| 광주(`29`) 폐지 티어 | **0건** |

> **`lawd_code.exist = false` 를 실패 신호로 해석하지 말 것.** `exist` 는 "현재 행정구역으로
> 살아 있는가"이지 "코드가 옳은가"가 아니다. 두 물음은 다르다.
> 폐지 티어가 **전남에만, 그리고 전량이** 몰려 있다는 사실 자체가 정상 동작의 증거다 —
> 무작위 오염이라면 시도가 흩어진다.
>
> **이상 징후로 볼 조건은 따로 있다**: ⑤ 결과가 광주(`29`)를 포함하거나, 시도가 `46` 외로
> 흩어지거나, 행수가 100만 선에서 **크게** 벗어나는 경우다. 그때만 §15 롤백을 검토한다.
>
> 근거: `phase1-log.md` §5-5 (태스크 018 Phase 1 로컬 실행 로그).

**실패 사유 집계(권장)** — 후속 태스크 입력이 된다.

```sql
CREATE TABLE _ri_backfill_miss AS
SELECT left(a.bcode,8) AS emd8, a.ri, count(*) AS n,
       (SELECT name FROM lawd_code c WHERE c.bcode = left(a.bcode,8)||'00') AS emd_name
  FROM address a
 WHERE a.kind='addr' AND a.ri IS NOT NULL AND a.ri <> ''
   AND right(a.bcode,2) = '00'
 GROUP BY 1,2 ORDER BY n DESC;
```

**매칭 실패 처리 정책**: **코드를 추측해서 채우지 않는다.** 실패 행은 `…00` 으로 **그대로 둔다.**
잘못된 리 코드는 빈 리 코드보다 나쁘다 — 현행 오염이 바로 그 결과다.

### API 레벨 확인 (권장)

리 주소 몇 건을 실제 API 로 조회해 `b_code` 10자리가 리 자리까지 채워져 나오는지 확인한다.
검증 하니스(`vworld_compare.py` 등)를 운영에서 돌릴 경우 **VWorld 키는 파일에 하드코딩하지 않는다.**

---

## 13. STEP 10 — API 재시작

```bash
command docker compose -f /home/gjc/maptiler/server/docker-compose.yml restart geocode
```

**목적**: 커넥션 풀·프로세스 내 캐시 정리. **코드 배포가 아니다.**
Phase 1 은 API 코드를 바꾸지 않으므로 이 재시작은 **선택 사항**이며, 무중단을 우선한다면 생략할 수 있다.
단 `lawd_ri` 스키마가 바뀌었으므로(§7-3) **재시작 후 리 검색 스모크 테스트 1회를 권장**한다.

---

## 14. 서비스 중단 여부 — 판단과 근거

### 판단: **무중단으로 진행한다.**

STEP 8(S5 배치 UPDATE)은 **API 를 정지하지 않고** 진행한다.

**근거 (모두 [측정] 기반)**

1. **각 행의 변화가 단방향이다.** `bcode` 는 `XXXXXXXX00` → `XXXXXXXXnn` 으로만 바뀐다.
   앞 8자리는 불변이고(S3 게이트가 강제), `nn` 은 원본에 실재하는 코드다(S5 검증이 강제).
   **중간 상태에 노출되는 값은 "덜 정확한 값(현행과 동일)"이지 "틀린 값"이 아니다.**
2. **중간 상태의 품질 = 현행 품질.** 아직 갱신되지 않은 행은 지금 운영이 반환하는 값 그대로다.
   즉 진행 중 사용자가 보는 최악의 상태가 **현재 상태**다.
3. **읽기 경로가 `bcode` 로 필터하지 않는다.** `bcode` 에는 인덱스가 없고 검색 질의는
   `search_text`/`road_norm`/`geom` 을 탄다. 부분 갱신이 검색 결과 집합을 바꾸지 않는다.
4. **청크 단위 트랜잭션이므로 어떤 시점에도 "절반만 쓰인 행"이 없다** (MVCC).

### 예외 — 짧은 정지·단절이 있는 지점 2곳

| # | 지점 | 성격 | 길이 | 대응 |
|---|---|---|---|---|
| 1 | **STEP 4 의 `lawd_ri` 교체 트랜잭션** | `ACCESS EXCLUSIVE` 락. 그동안 리 조인 질의(`:468`)가 **대기** | **[추정] 1초 미만** | 정지하지 않되 **트래픽이 적은 시간대에 수행** |
| 2 | **Phase 2 배포 시 컨테이너 재시작** | 수 초의 단절 | 수 초 | 기존 배포 절차와 동일. **Phase 1 범위 밖** |

> STEP 10 의 재시작을 수행하는 경우에도 예외 2 와 같은 수 초 단절이 발생한다. 생략 가능(§13).

### 부하 영향

STEP 8 은 heap·인덱스에 쓰기 부하를 준다. **트래픽이 적은 시간대에 시작**하고,
첫 3청크 실측 후 응답 지연이 허용치를 넘으면 **청크 크기를 100,000행으로 낮춘다.**

### Phase 3(전남·광주 통합)은 별개다

파티션 이동을 동반하므로 **무중단으로 하지 않는다. 중단 창이 필요**하다.
상세는 `docs/jeonnam-gwangju-planB-handover.md`.

---

## 15. 롤백

### 15-1. 단계별 롤백표

| 되돌릴 지점 | 방법 | 예상 소요 | 적용 조건 |
|---|---|---|---|
| STEP 3 (`lawd_code`) | `DROP TABLE lawd_code;` | **[추정]** < 5초 | 언제든. 부수효과 없음 |
| STEP 4 (`lawd_ri` 교체) | §15-2 대칭 롤백 SQL | **[추정]** < 5초 | 교체 후 언제든 |
| STEP 4 (최후 수단) | `lawd_ri_bak_20260810` 에서 재적재 | **[추정]** < 1분 | 대칭 롤백이 실패했을 때 |
| STEP 6 (`_ri_backfill_todo`) | `DROP TABLE _ri_backfill_todo;` | **[추정]** < 10초 | **STEP 8 미착수 시에만** |
| **STEP 8 (배치 UPDATE)** | §15-3 `old_bcode` 기준 역UPDATE | **[추정] 20–60분** | **1순위.** todo 가 온전할 때 |
| STEP 8 (대안) | §15-4 `address_bcode_bak_20260810` 복원 | **[추정] 20–60분** | todo 가 손상됐을 때 |
| 전체 (최후 수단) | §15-5 `pg_restore` | **[추정] 30–60분** | 위 경로가 모두 막혔을 때 |

> **부분 실행 중 중단**은 롤백이 아니다. 청크 = 트랜잭션이므로 중단 시점까지의 갱신은 정합하며,
> 재개(`last_id` 기준)하거나 §15-3 으로 되돌리면 된다. **S3 재실행은 절대 금지** — 가드가 막지만,
> 가드를 지우고 강행하면 `old_bcode` 가 영구 소실된다.

### 15-2. STEP 4 롤백 — `lawd_ri` 대칭 복원

```sql
BEGIN;
  DROP INDEX IF EXISTS lawd_ri_ri_emd_idx;
  DROP INDEX IF EXISTS lawd_ri_emd_cd_idx;
  ALTER INDEX lawd_ri_pkey RENAME TO lawd_ri_new_pkey;
  ALTER TABLE lawd_ri      RENAME TO lawd_ri_new;
  ALTER TABLE lawd_ri_old_20260810 RENAME TO lawd_ri;
  ALTER INDEX lawd_ri_old_20260810_pkey       RENAME TO lawd_ri_pkey;
  ALTER INDEX lawd_ri_old_20260810_ri_emd_idx RENAME TO lawd_ri_ri_emd_idx;
COMMIT;
```

확인: `SELECT count(*) FROM lawd_ri;` → 기대 **16,113**

> **주의**: STEP 8 을 이미 실행한 뒤 `lawd_ri` 만 되돌리면, `address.bcode` 는 신 사전 기준으로
> 채워져 있는데 사전은 구 상태가 된다. **STEP 8 이후에는 §15-3 을 먼저 수행하고 그다음 이 롤백을 한다.**

### 15-3. STEP 8 롤백 — `old_bcode` 기준 역UPDATE (1순위)

```sql
BEGIN;
  UPDATE address a SET bcode = t.old_bcode
    FROM _ri_backfill_todo t
   WHERE a.id = t.id AND t.done;
COMMIT;

-- 트랜잭션 블록 밖에서 (autocommit 연결)
VACUUM (ANALYZE) address;

-- 재실행 가드 해제 — 이것을 잊으면 S3 를 다시 만들 수 없다
DELETE FROM _ri_backfill_progress WHERE k='last_id';
```

검증:

```sql
SELECT count(*) FROM address WHERE kind='addr' AND right(bcode,2)<>'00';   -- 기대 0
SELECT count(*) FROM address a JOIN address_bcode_bak_20260810 b ON b.id=a.id
 WHERE a.bcode <> b.bcode;                                                 -- 기대 0
```

**소요 [추정] 20–60분** (UPDATE 규모가 정방향과 같다) — 실측: **[미측정]**

> **왜 [미측정] 인가.** Phase 1 로컬 실행에서 롤백은 **SQL 로 문서화만 하고 실제로 돌리지 않았다.**
> 백필 결과 자체가 검증 대상이라 되돌릴 이유가 없었기 때문이다(`phase1-log.md` §8-2 — 롤백 SQL 은
> 기재돼 있으나 실행 기록이 없다). **없는 값을 실측치인 양 옮겨 적지 않는다.**
>
> **[로컬 실측 → 운영 환산] 대신 쓸 수 있는 근거.** 이 역UPDATE 는 정방향 S5 와 **행수·조인·SET 폭이
> 사실상 같다**(6,743,655행, `id` 조인, `bcode` 단일 컬럼). 정방향 로컬 실측이 **00:18:13 / 6,166 rows/s**
> (§11-4) 이므로 **같은 자릿수, 20분 안팎**으로 보는 것이 타당하다 **[추정]**. 단 두 가지가 정방향보다 불리하다 —
> ① 이 SQL 은 **청크 분할 없이 단일 트랜잭션**이라 중간 VACUUM 이 끼어들 수 없고 dead tuple 이 끝까지 누적된다,
> ② 롤백 시점의 `address` 는 이미 **+20.8% 팽창**한 상태(§11-4)라 스캔 대상이 더 크다.
> 따라서 **상한 60분을 그대로 유지**하고, 장애 대응 창구도 그 기준으로 잡을 것.
> 운영에서 실제로 롤백을 수행했다면 여기에 기록한다: 소요 `______` / 대상 행수 `______` / 측정일 `______`

**적용 조건**: `_ri_backfill_todo` 가 온전할 것. `done` 이 false 인 행은 애초에 갱신되지 않았으므로 제외된다.

### 15-4. STEP 8 롤백 — 스냅샷 복원 (todo 손상 시)

```sql
BEGIN;
  UPDATE address a SET bcode = b.bcode
    FROM address_bcode_bak_20260810 b
   WHERE a.id = b.id AND a.bcode <> b.bcode;
COMMIT;

VACUUM (ANALYZE) address;   -- 트랜잭션 블록 밖
DELETE FROM _ri_backfill_progress WHERE k='last_id';
```

**소요 [추정] 20–60분.** todo 와 달리 **전 행을 훑으므로** 비교 비용이 조금 더 크다 **[추정]**.

### 15-5. 최후 수단 — `pg_dump` 복원

```bash
command docker cp /home/gjc/backup/t018_pre.dump <postgis컨테이너>:/tmp/t018_pre.dump
command docker exec <postgis컨테이너> \
  pg_restore -U cuvia -d cuvia --clean --if-exists \
    -t address -t lawd_ri -t lawd_dong -t lawd_sigungu -t lawd_code \
    /tmp/t018_pre.dump
```

**소요 [추정] 30–60분.** `--clean` 이 대상 테이블을 드롭·재생성하므로 **이 동안 서비스는 정상 동작하지 않는다.**
반드시 **중단 창 안에서** 수행한다.

**적용 조건**: §15-3·§15-4 가 모두 불가능할 때만. 복원 전 SHA256 을 §5 기록값과 대조한다.

### 15-6. 롤백 후 정리

1. `_ri_backfill_progress` 의 `last_id` 삭제 확인 (미삭제 시 S3 가드가 재시도를 막는다)
2. `_ri_backfill_todo` 는 원인 규명이 끝날 때까지 **삭제하지 않는다** — 사고 분석의 1차 자료다
3. 무엇이 왜 실패했는지 기록한 뒤에만 재시도한다. 같은 조건으로 그냥 다시 돌리지 않는다

---

## 16. 배포 경로 주의 ★

### 경로

운영 배포 소스는 **`/home/gjc/maptiler/`** 이다. (`/root/cuvia` 가 아니다)
**bind mount** 이므로 **파일을 교체한 뒤 컨테이너를 재시작**하면 반영된다.

```bash
command ls -l /home/gjc/maptiler/
command docker compose -f /home/gjc/maptiler/server/docker-compose.yml ps
```

### ⚠ git 으로 덮어쓰면 안 되는 파일 — 서버 고유 설정

| 파일 | 이유 |
|---|---|
| `server/docker-compose.yml` | 포트·볼륨·DB 접속이 서버마다 다르다 |
| `gateway-nginx.conf` | 게이트웨이 라우팅이 서버 고유 |
| `tileserver-config.json` | 타일 소스 경로가 서버 고유 |

**`git pull`/`git checkout` 으로 배포 디렉터리 전체를 갱신하지 말 것.**
이번 절차에 필요한 파일은 **§1-1 목록에 열거된 것뿐**이며, 개별 파일 전송으로 충분하다.

부득이하게 git 을 쓴다면 위 3파일을 먼저 백업하고, 갱신 후 되돌려 놓은 뒤 재시작한다.

---

## 17. 백업 테이블 정리 시점

**기준: "Phase 1 완료 + 운영 안정 확인 후 최소 2주 경과"**

| 정리 대상 | 시점 |
|---|---|
| `lawd_ri_bak_20260810`, `lawd_ri_old_20260810` | 위 기준 충족 시 `DROP` |
| `address_bcode_bak_20260810` | 위 기준 충족 시 `DROP` |
| `_ri_backfill_todo`, `_ri_backfill_progress` | 위 기준 충족 시 `DROP` |
| `_ri_backfill_miss` | 후속 태스크 입력이므로 **보존** (또는 결과를 옮긴 뒤 삭제) |
| `t018_pre.dump` | 위 기준 충족 시 보관 정책에 따라 이관·삭제 |

**왜 Phase 3 을 기준으로 삼지 않는가**: Phase 3(전남·광주 통합)은 결정권자 대기로 **무기한 보류될 수 있다.**
그것을 기준으로 삼으면 Phase 1 백업물이 목적 없이 영구 잔존한다.
**Phase 1 백업물의 수명은 Phase 1 자체에 걸어야 한다.** Phase 3 백업물은 Phase 3 실행 시 별도로 판단한다.

---

## 부록 A. 실행 체크리스트

실행자는 이 표를 인쇄·복사해 채우면서 진행한다. **★ 표시 단계는 통과 전 진행 금지.**

```
[ ] 승인 확인 — 승인자:                    일시:
[ ] STEP 0 ★ 동일성 게이트 6항목 일치 (또는 §3-1 재산정 완료)
[ ] STEP 1 ★ 전송 완료 + SHA256 일치
[ ] STEP 2 ★ pg_dump 완료 (크기 ____ GB, SHA256 기록)
[ ] STEP 3 ★ S1 lawd_code 4개 기대값 일치
[ ] STEP 4 ★ S2 게이트 ⓪①②③④ 전부 통과
[ ] STEP 4 ★ 교체 후 lawd_ri=17,896 / 인덱스 3개 확인
[ ] STEP 5   S2-b 폐기 가드·신 스크립트·런북 반영 확인
[ ] STEP 6 ★ S3 재실행 가드 통과 + todo 6개 검증 통과 (총건수 ______)
[ ] STEP 7 ★ S4 스냅샷 행수 = STEP 0 ①
[ ] STEP 8 ★ 첫 3청크 실측 (____초/청크) → 지연 허용치 확인
[ ] STEP 8 ★ 완주 + 4개 검증 쿼리 통과
[ ] STEP 9 ★ 최종 검증 ①②③④⑤ 통과 (특히 ① 앞8자리 불변 = 0)
[ ] STEP 9   ⑤ 폐지 티어 약 100만 행 = **정상값** 확인 (전남 단일 / 광주 0건 → 롤백 금지)
[ ] STEP 10  API 재시작 + 리 검색 스모크
[ ] 실측치를 이 문서와 phase1-log.md 에 기록
[ ] 2주 후 백업 정리 예정일:
```

## 부록 B. 실측 대기 항목 → **기입 완료** (2026-08-10, 태스크 018 Phase 1 로컬 실행 반영)

**7개 항목 중 4개(1·4·5·6)를 로컬 실측치로 채웠고, 3개(2·3·7)는 [미측정] 로 남겼다.**
남긴 3개는 **Phase 1 이 해당 STEP 을 수행하지 않았기 때문**이며, 각 절에 사유와
"운영에서 무엇을 기준으로 잡을지"를 함께 적었다. **추정치를 확정치처럼 옮겨 적지 않는다** 는
원칙을 그대로 지켰다 — 없는 측정을 지어내는 대신 **왜 없는지**를 남겼다.

| # | 항목 | 위치 | 상태 | 값 |
|---|---|---|---|---|
| 1 | 원본 SHA256 | §1-2 | **[로컬 실측] 기입 완료** | txt `7b4b544a…2eb33` / 413,346 B / 53,387행. **zip 해시는 [미측정]** — 압축본은 커밋·전송 대상이 아니고 재압축 시 해시가 달라져 대조 기준이 될 수 없다 |
| 2 | `pg_dump` 실제 용량 | §5 | **[미측정]** | Phase 1 은 이 STEP 미수행(로컬엔 되돌릴 운영 데이터가 없고 롤백은 STEP 7 스냅샷을 썼다). 사전 **[추정] 5.2 GB** 유지. 환산 근거로 `address` 실크기 8,464 MB(전)/10,222 MB(후) 병기 |
| 3 | `pg_dump` 실제 소요 | §5 | **[미측정]** | 위와 같은 사유. 사전 **[추정] 10–20분** 유지. 운영 최초 수행자가 최초 실측자이며 §5 에 기록란을 뒀다 |
| 4 | S4 스냅샷 생성 소요 | §10 | **[로컬 실측] 기입 완료** | **총 27초** (CTAS 9.251s + UNIQUE INDEX 2.038s + ANALYZE 0.181s + 검증 3.8s), 10,686,547행, **761 MB**. 사전 추정 20–60분은 크게 과대, 크기 추정 320 MB 는 과소(실제 2.4배) |
| 5 | S5 청크당 소요 / 총 소요 | §11-4 | **[로컬 실측] 기입 완료** | 총 **00:18:13 (1,093s)** / 34청크 / 6,743,655행 / **6,166 rows/s**. 청크 min 24.4s · max 42.9s · 평균 30.74s · 중앙 30.4s. 첫 3청크 ETA 대비 **+9.4%** 낙관 편향 |
| 6 | S5 중 `n_dead_tup` 최대치·VACUUM 발동 횟수 | §11-3 | **[로컬 실측] 기입 완료** | 최대 **1,610,431** (임계 1,500,000 대비 +7.4%), 발동 **4회 / 합 57.4s (5.3%)**, 마무리 VACUUM(ANALYZE) 13.7s, 종료 후 잔여 dead 137,635 |
| 7 | 롤백 역UPDATE 실제 소요 | §15-3 | **[미측정]** | Phase 1 은 롤백 SQL 을 **문서화만 하고 실행하지 않았다**(백필 결과 자체가 검증 대상이라 되돌릴 이유가 없었다 — `phase1-log.md` §8-2). 정방향 S5 와 규모가 같아 **20분 안팎 [추정]** 이나, 단일 트랜잭션·팽창분 스캔이라 **상한 60분을 유지**한다 |

**추가로 확정된 값** (부록 B 원표에는 없던 항목):

| 항목 | 값 |
|---|---|
| S5 팽창 | 8,464 MB → **10,222 MB (+1,758 MB, +20.8%)**. 힙 +513 MB / **인덱스 +1,244 MB** — 팽창의 71%가 인덱스 (§11-4) |
| 운영 디스크 최소 여유 | **2 GB** (로컬 규모 기준 **[로컬 실측 → 운영 환산]**) |

> **표기 규칙 확장.** 이번 기입부터 **[로컬 실측]** 과 **[로컬 실측 → 운영 환산]** 을 구분해 쓴다.
> 앞은 로컬 컨테이너에서 실제로 측정한 값이고, 뒤는 그 값에서 운영 규모로 **미루어 짐작한** 값이다.
> 운영 `address` 의 행수·크기가 로컬과 같다는 보장은 없으므로, 환산치는 **실행 전 운영에서
> 반드시 재확인**한다.

출처: `phase1-log.md` (태스크 018 실행 로그 —
`.team/tasks/018-b-code/runs/task-018-1786289274/` 이하)
§1(SHA256·행수) / §2(S4) / §3-1·§3-2·§3-3(S5 소요) / §3-4(VACUUM 이력) / §4(팽창) / §8-2(롤백 미실행)

---

## 부록 C. 관련 문서

| 문서 | 내용 |
|---|---|
| `plan.md` (태스크 018) | 전체 설계·근거·측정치 원본 |
| `docs/ri-dict-runbook.md` | 리 사전 생성 절차(신 방식으로 갱신됨) |
| `docs/jeonnam-gwangju-planB-handover.md` | Phase 3 전남·광주 통합(안 B) 인계 자료 |
| `docs/data-patch-runbook.md` | 레이어별 데이터 갱신 일반 절차 |

---

*작성: 태스크 018 / 2026-08-10. 운영 미적용 상태로 작성됨.*
