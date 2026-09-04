<!-- 생성: geocode-display-parity-spec 워크플로우 (9 agents, ~579k tok, context7 3사 근거, 적대검토 3렌즈 반영) · 2026-06-24 -->

# 지오코딩 표기 오버홀 + 분해응답 구현 스펙 (최종본 v2)

> 대상 브랜치: `feature/geocode-followup` 기반 · 코드 기준일 2026-06-24
> 핵심 코드: `server/geocode-api-pg.py`(런타임 API), `scripts/09-gen-geocode.py`(빌드 산출), `scripts/postgis/*`(스키마·적재), `scripts/build-studio.py`(빌드그래프)
> 본 문서는 적대적 검토 3건을 전수 반영한 최종본이다. 모든 값 주장에 **소스 근거(테이블.컬럼 / 신규컬럼 / runtime / 미소싱)** 를 부기한다.

---

## 0. 코드 사실 기준선 (적대검토로 정정된 전제 — AS-IS)

스펙 본문은 아래 '확인된 코드 사실'을 전제로 한다. 초안의 부정확/모순 진단은 모두 여기에 맞춰 정정되었다.

| # | 확인된 사실(파일:라인) | 초안 정정 |
|---|---|---|
| F1 | `lawd_dong` **테이블은 존재(빈 채)**. `schema/21-parcel-jibun.sql:16-22` 가 `CREATE TABLE IF NOT EXISTS lawd_dong(emd_cd char(8) PK, sido text, sigungu text, emd text)` + `lawd_dong_emd_idx`. `apply-schema.sh` 가 `schema/*.sql` 전체 실행 → 매 빌드 CREATE. **누락된 것은 적재자(populator)**, 테이블이 아님. | 진단 '부재'→**'존재하나 미적재(silent empty)'** |
| F2 | `lawd_dong` 컬럼은 `emd_cd/sido/sigungu/emd` 뿐. **`haeng_dong`/`h_code` 없음.** `21-parcel-jibun.sql:14` 주석: 소스는 **`address.bcode 파생`**(admin_boundary 아님). | parcel트랙 haeng_dong/h_code 는 **소싱 불가 → null 고정** |
| F3 | `admin_boundary`(`10-base.sql:6-16`)는 `level/name(리프)/full_name/sido_cd` 만. **`sido`/`sigungu` 명 컬럼 없음.** → build_lawd_dong 의 1순위 소스로 부적합(`full_name` 파싱 필요). | build_lawd_dong 1순위 소스를 **`address`(DISTINCT)** 로 변경 |
| F4 | `address`(`10-base.sql:41-60`)는 `sido/sigungu/emd/road/road_norm/main_no/sub_no/bld/postal/haeng_dong/bcode/hcode/jibun/cat1/cat2/source` 보유. **`ri`/`bld_main_no`/`bld_sub_no`/`san` 컬럼 없음.** `address_road_addr_idx ON (road_norm,main_no,sub_no) WHERE kind='addr'`(`load_geocode.py:101`). | `ri`/`bld_*` 는 **신규 컬럼**, `san` 은 **parcel트랙 한정** |
| F5 | navi addr 적재(`09-gen-geocode.py:164`): `c[1]=sido, c[2]=sigungu, c[3]=emd(법정동), road=c[5], mno=c[7]/sno=c[8](도로명 건물본/부번), bcode=c[0](10자리), hcode=c[13], haeng_dong=c[14]`. **navi addr 의 emd 는 이미 법정동(정상)·haeng_dong 은 행정동(정상).** | emd 버그는 **biz/facility 한정**(`:214 emd=행정동명`). navi addr 회귀 금지 |
| F6 | `09-gen-geocode.py:135-136`: `ri=c[4]; dong=f"{c[3]} {ri}"` — **ri 는 emd 문자열/jibun 에 합성**, 별도 컬럼 미보존. SCHEMA(:106)·address(F4)·load COLS(`load_geocode.py:18-20`)에 ri 없음. | ri 소싱: **런타임 jibun 파싱(best-effort) + 빌드 분해(신규컬럼) 후 권위** |
| F7 | `parcel` 경로(`geocode-api-pg.py:159-207`): `SELECT emd_cd FROM lawd_dong WHERE emd=%s`(:164) → `WHERE ... emd_cd=ANY AND ji_main=%s AND ji_sub=%s`(:195-196) → 좌표 `COALESCE(geom_pt, ST_PointOnSurface(geom))`(:193). 출력(:207) `structure={"emd":p["dong"], "b_code":r["emd_cd"]}` — **입력토큰 그대로·sido/sigungu 없음·b_code 8자리(패딩 없음)·지목문자 포함**. | parcel SELECT 는 :192-201, 조립 버그는 :202-207 |
| F8 | 지번 경로는 **둘**: parcel 테이블(:159-207) + address 테이블 jibun(:209-234). | 초안의 ':185-207' 단일표기 정정 |
| F9 | `backfill_parcel_jibun.sql` 은 **단일 전테이블 `UPDATE parcel`(rewrite)** (:8-21). `parcel.ji_main/ji_sub/san` 의 **유일 채움 경로**. (운영 실측 ~89분, `docs/handoff-후속작업.md`) | C1='리팩터'→**'옵트인 게이트(자동배선 금지)'** 로 격상·정정 |
| F10 | `load_geocode.py:59-60` `TRUNCATE address; TRUNCATE poi;` 후 **전량 재INSERT**(부분적재 경로 없음). COLS(:18-20)에 ri/bld_* 없음. | '국소 재적재'='전량 TRUNCATE 후 국소만 잔존' → **격리DB 필수** |
| F11 | `build_sigungu_dict.sh` **이미 존재**. `DROP TABLE IF EXISTS lawd_sigungu`(:31) + 7z 없으면 `exit 1`(:13). 결과 `lawd_sigungu(sigungu_cd char(5), sigungu_nm text='수원시 영통구' 결합형)`. | '신규'→**'기존 + 편입 + 소스부재 skip 가드'** |
| F12 | `build-studio.py:656-657` `load_postgis.scripts` = `[load-all.sh, load_parcel.sh, load_building.sh, load_geocode.py]`. **schema/*.sql·apply-schema.sh·build_*.sql 전부 미등재.** `out:[]` → 입력 시그니처로만 stale 판정(:737). | schema/*.sql 일반이 **TFRESH 미추적**(무음 미반영 표면 더 큼) |
| F13 | `demo/js/search.js:9` `TYPE_KO={station,place,dong,road,poi,biz}` — **addr/parcel/facility 키 없음**. `:42` 가 `r.type`(undefined) 읽음. | r.type→r.kind 수정 + TYPE_KO 키 확장 동반 필수 |
| F14 | OSM 유래 행(`09-gen add_osm`)·biz/facility 행은 **places 행에 sido/sigungu/emd/road/main_no 를 자체 컬럼으로 보유**(biz `:237`, facility `:257`). 단 OSM(add_osm)은 지역정보 None 적재. `reverse()` 의 non-addr nearest 는 **structure 없음**(name/kind/subtype/lon/lat/dist_m/category 만), 지역은 `addr_at`(인근) 또는 `admin_boundary ST_Contains`(:`areas`)로만. | station/place/dong 의 secondary 지역소스 = **admin_boundary PIP** 로 명문화 |
| F15 | `category_of`(`geocode-api-pg.py:85-91`)는 `{primary, label, (sub)}` 반환(c2 있을 때 `sub=c2`). | 신스키마에 **기존 `sub` 키 보존** 명문화 |
| F16 | `cat-crosswalk.json` 은 `localdata`/`osm` 매핑만(`09-gen:337`). **구글 types[] 소스·매핑 부재.** | 구글 패리티는 **참고(수기 대조)** 로 강등 |
| F17 | `13d-geocode-parity.py` 는 **top-1 좌표·reverse 행정동만** 비교(name/display 문자열 미비교). parity 는 SQLite(:8082) vs PG(:8092). | 'name 회귀0' 검증을 13d 로 입증 불가 → **name 스냅샷 하니스 신규** |

---

## 1. 개요 / 목표

### 1.1 배경
검색 결과 표기가 3사(카카오/구글/네이버) 관행과 어긋나고, 분해형 응답이 없으며, 일부 경로는 명시 버그를 안고 있다.

- 지번(parcel) 경로: `disp='상동 500-1답'` — 지목문자('답') 노출 + 시도/시군구 누락, structure 에 `emd`(입력토큰)/`b_code`(8자리) 만(`geocode-api-pg.py:202-207`, F7).
- 프론트(`demo/js/search.js`): `r.type`(응답 키는 `r.kind`)을 읽어 `undefined` 출력, TYPE_KO 에 addr/facility 키 없음, `structure` 미사용(F13).
- 비-addr(OSM/biz/facility) 결과: geocode 경로에서 자기 행정정보가 응답 structure 로 노출되지 않고 인근 주소(`addr_at`)로만 대체(F14).
- `structure.main_no`/`sub_no` 가 실제로는 **도로명 건물본/부번**이라(F5) '지번 본번'과 의미 충돌. 이 값은 `address_road_addr_idx`·도로질의의 계약(F4)이므로 **의미 이동 불가**.

### 1.2 목표
1. **3사 동등 표기**: 코드 kind 집합(`addr/road/place/station/dong/poi/biz/facility`, `geocode-api-pg.py:238`)과 1:1 정합하는 '주(굵게)+보조(회색)+전체' 2~3단 표기를 일관 제공(미정의 kind 는 안전 fallback).
2. **분해응답 계약**: `display{main,secondary,full}` + `structure`(소싱 가능 필드만 채움, 불가 필드 null 고정) + 표준화 `category`(기존 `sub` 키 보존)를 kind 무관 일관 스키마로 내려준다.
3. **재빌드 반영**: 빌드 산출 컬럼/스키마/적재 SQL 변경이 `build-studio` 시그니처(`TFRESH`+`_target_sig`)에 **개별 등재**되어 자동 stale→재빌드→재적재로 흐르되, **전국 backfill 은 자동배선에서 제외**한다.

### 1.3 안전 제약 (전 작업 공통 · 위반 시 차단)
- **대량 DB 작업 동시 금지**: parcel(39.9M)·building·address 적재/백필 직렬.
- **POI 적재 TRUNCATE 는 백업 후에만**: `load_geocode.py:59-60` 의 TRUNCATE+전량 재적재는 직전 `geocode.sqlite` 아티팩트 스냅샷(`build-studio.py backup_geocode_artifact`) 확인 후 진행.
- **'국소 재적재'는 운영 DB 금지**: `load_geocode.py` 는 부분적재 경로가 없어 `--only` 단일시도 적재 시 **전국이 TRUNCATE 로 소실**(F10). 골든셋 검증은 **별도 격리 검증DB/스키마**에서만 수행. 운영 DB 에 `--only` 적재 금지.
- **전국 backfill 자동배선 금지**: `backfill_parcel_jibun.sql`(39.6M 단일 UPDATE rewrite, F9)은 `load-all.sh` 정규경로·TFRESH 자동 stale 에 **편입하지 않는다**. 명시 옵트인(`STEPS=backfill`)으로만 실행.
- **전국 parcel materialize 안 함**: 지역명은 런타임 dict 조인으로 해소(§4.4). 단 **ji_main/ji_sub 매칭은 backfill 선행이 필수 전제**(§4.4 선결조건).

---

## 2. 타깃 응답 계약 (AS-IS / TO-BE 분리 · 필드표 · 예시 JSON)

> **읽는 법**: 아래 예시는 **TO-BE(목표)** 이며, 각 값은 구현·선결조건 충족 후에만 채워진다. 현행(AS-IS)은 §0 표대로 다수 필드가 null/누락/오류 상태다. 검토자가 '이미 동작'으로 오인하지 않도록 §2.3 에 AS-IS↔TO-BE 대조와 소스 근거를 명시한다.

### 2.1 응답 예시 (TO-BE · 모든 선결조건 충족 가정)
```json
{
  "query": "강남구 테헤란로 152",
  "contract_version": "geocode/2",
  "results": [
    {
      "name": "서울특별시 강남구 테헤란로 152 (강남파이낸스센터)",
      "kind": "addr", "subtype": "road",
      "lon": 127.036508, "lat": 37.500574,
      "display": {
        "main": "테헤란로 152 (강남파이낸스센터)",
        "secondary": "서울 강남구 역삼동 (06236)",
        "full": "서울특별시 강남구 테헤란로 152 (강남파이낸스센터)"
      },
      "address": {
        "road": "서울특별시 강남구 테헤란로 152 (강남파이낸스센터)",
        "parcel": "서울특별시 강남구 역삼동 737",
        "zipcode": "06236", "bld": "강남파이낸스센터",
        "structure": {
          "sido": "서울특별시", "sigungu": "강남구", "emd": "역삼동",
          "haeng_dong": "역삼1동", "ri": null, "san": null,
          "road_name": "테헤란로", "bld_main_no": 152, "bld_sub_no": 0,
          "main_no": 152, "sub_no": 0,
          "ji_main": null, "ji_sub": null,
          "bld_name": "강남파이낸스센터", "zipcode": "06236",
          "b_code": "1168010100", "h_code": "1168064000"
        }
      },
      "category": null, "phone": null, "source": "navi"
    },
    {
      "name": "경기도 부천시 상동 500-1",
      "kind": "addr", "subtype": "parcel",
      "lon": 126.7607, "lat": 37.5045,
      "display": { "main": "상동 500-1", "secondary": "경기 부천시", "full": "경기도 부천시 상동 500-1" },
      "address": {
        "road": null, "parcel": "경기도 부천시 상동 500-1", "zipcode": null, "bld": null,
        "structure": {
          "sido": "경기도", "sigungu": "부천시", "emd": "상동",
          "haeng_dong": null, "ri": null, "san": false,
          "road_name": null, "bld_main_no": null, "bld_sub_no": null,
          "main_no": null, "sub_no": null,
          "ji_main": 500, "ji_sub": 1,
          "bld_name": null, "zipcode": null,
          "b_code": "4119010300", "h_code": null
        }
      },
      "category": null, "phone": null, "source": "parcel"
    },
    {
      "name": "스타벅스 강남R점", "kind": "biz", "subtype": "커피전문점",
      "lon": 127.0276, "lat": 37.4979,
      "display": { "main": "스타벅스 강남R점", "secondary": "카페 > 커피전문점 · 서울 강남구 역삼동" },
      "address": {
        "road": "서울특별시 강남구 강남대로 390", "parcel": null,
        "zipcode": "06232", "bld": null,
        "structure": {
          "sido": "서울특별시", "sigungu": "강남구", "emd": "역삼동", "haeng_dong": null,
          "ri": null, "san": null,
          "road_name": "강남대로", "bld_main_no": 390, "bld_sub_no": 0,
          "main_no": 390, "sub_no": 0,
          "ji_main": null, "ji_sub": null,
          "bld_name": null, "zipcode": "06232",
          "b_code": null, "h_code": null }
      },
      "category": { "group": "카페", "path": "카페 > 커피전문점", "primary": "카페", "label": "카페 > 커피전문점", "sub": "커피전문점" },
      "phone": "02-555-1234", "source": "localdata"
    }
  ]
}
```

> **예시 주석(소싱 정직성)**:
> - parcel 결과 `structure.haeng_dong/h_code/road_name/bld*/zipcode = null`(F2: lawd_dong·parcel 어디에도 소스 없음 — **null 고정**).
> - parcel `sido/sigungu/emd` 는 **build_lawd_dong 적재 + backfill 선행** 후에만 채워짐(§4.4 선결조건). 미충족 시 빈 결과/ILIKE fallback.
> - biz `structure.main_no(=bld_main_no)=390` 은 도로명 건물본번(F5). **지번 본번(ji_main)은 biz 에 정수소스 없음 → null**(초안의 `main_no:825` 같은 미소싱 값 제거).
> - addr `san=null`(F4: address 에 san 컬럼 없음). parcel 만 `san:bool`.
> - `ri=null`(F6: 빌드 분해 전. 런타임 jibun 파싱은 best-effort 옵션, 본 예시는 미적용).

### 2.2 필드표
범례 — 출처: `build`(빌드 산출 컬럼) / `runtime`(API 조립/파싱/조인) / `runtime-alias`(기존 컬럼 거울) / `미소싱`(현재 채울 소스 없음). null 정책 명시.

| 경로(path) | 타입 | null | 출처(소스 근거) | 의미 / 3사 패리티 |
|---|---|---|---|---|
| `name` | string | non-null | runtime | 레거시 1줄 = `display.full` alias. **단 parcel/건물 트랙은 값 변경됨(§7.2)** |
| `kind` | enum(addr·road·place·station·poi·biz·dong·facility) | non-null | both | Kakao address_type+group / Google types[](참고) |
| `subtype` | string\|null | nullable | both | addr=`'road'`/`'parcel'` 명시(신규). biz/facility=소분류 |
| `lon`/`lat` | number(4326) | non-null | both | 좌표(null 행 제외) |
| `display.main` | string | non-null | runtime | 주표시. kind→§3 규칙. **미정의 kind=`name` fallback** |
| `display.secondary` | string\|null | nullable | runtime | 보조(회색). 지역소스=자체 컬럼 또는 admin_boundary PIP(F14) |
| `display.full` | string | non-null | runtime | 전체 1줄(도로명 우선, 없으면 지번). `name` 동일값 |
| `address.road` | string\|null | nullable | build(`road`) | 도로명 전체. `road_str()` |
| `address.parcel` | string\|null | nullable | both | 지번 전체. address트랙=`jibun`(클린), parcel트랙=dict 조립. **biz 는 jibun_txt 신뢰 낮음→null 허용** |
| `address.zipcode` | string\|null | nullable | build(`postal`) | 우편번호. Kakao zone_no |
| `address.bld` | string\|null | nullable | build(`bld`) | 건물명. `''`→`null` 정규화 |
| `structure.sido` | string\|null | nullable | both | 시도명(정식). navi=`sido`(c[1] 정식). parcel트랙=lawd_dong.sido(정식, **build_lawd_dong 가 정식명 적재**) |
| `structure.sigungu` | string\|null | nullable | both | 시군구명. navi=`sigungu`. parcel트랙=lawd_dong.sigungu(분리·클린) |
| `structure.emd` | string\|null | nullable | both | 법정동/읍/면. navi=`emd`(c[3]). parcel트랙=lawd_dong.emd(권위, 입력토큰 금지) |
| `structure.haeng_dong` | string\|null | nullable | build(`haeng_dong`) | 행정동. navi=c[14]. **parcel트랙=null 고정(F2 소스없음)**. biz=X2 후 |
| `structure.ri` | string\|null | nullable | runtime-parse→build(신규컬럼) | 리(里). 1차 런타임 jibun 파싱(best-effort), 빌드 분해 후 권위. 그 전 null |
| `structure.san` | boolean\|null | nullable | both | **parcel트랙만 non-null**(parcel.san→bool). 그 외 트랙 null(F4: address 무컬럼) |
| `structure.main_no` | integer\|null | nullable | both | **도로명 건물본번**(현 의미 동결). addr=`main_no`. Kakao main_building_no. **의미 불변(인덱스 계약)** |
| `structure.sub_no` | integer\|null | nullable(0허용) | both | 도로명 건물부번(현 의미 동결). Kakao sub_building_no |
| `structure.bld_main_no` | integer\|null | nullable | runtime-alias(=main_no) | 도로명 건물본번 명시 별칭. Kakao main_building_no |
| `structure.bld_sub_no` | integer\|null | nullable | runtime-alias(=sub_no) | 도로명 건물부번 명시 별칭. Kakao sub_building_no |
| `structure.ji_main` | integer\|null | nullable | both | **지번 본번**. parcel트랙=parcel.ji_main(backfill 선행). addr트랙=jibun 정규식 파싱(best-effort) 또는 null. Kakao main_address_no |
| `structure.ji_sub` | integer\|null | nullable(0허용) | both | 지번 부번. parcel트랙=parcel.ji_sub. Kakao sub_address_no |
| `structure.road_name` | string\|null | nullable | build(`road`/파싱) | 도로명. Kakao road_name |
| `structure.bld_name` | string\|null | nullable | build(`bld`) | 건물명 거울. Kakao building_name |
| `structure.zipcode` | string\|null | nullable | build(`postal`) | 우편번호 거울 |
| `structure.b_code` | string(10)\|null | nullable | both | 법정동코드10. navi=`bcode`(c[0], 이미 10자리). parcel트랙=`emd_cd`(8)+`00` 패딩(런타임, 신규) |
| `structure.h_code` | string(10)\|null | nullable | build(`hcode`) | 행정동코드10. navi=c[13]. **parcel트랙=null 고정(F2)** |
| `category.group` | string\|null | nullable | build(`cat1`) | 그룹=cat1. Kakao category_group_name. **addr/road/dong/place/station=null** |
| `category.path` | string\|null | nullable | runtime(`cat1>cat2`) | `cat1 > cat2`. Kakao category_name. 네이버 멀티리프='cat2' 단일화(§2.4) |
| `category.primary` | string\|null | nullable | runtime-alias(=group) | 하위호환 alias |
| `category.label` | string\|null | nullable | runtime-alias(=path/subtype) | 하위호환 alias |
| `category.sub` | string\|null | nullable | build(`cat2`) | **기존 키 보존(F15)**. cat2 존재 시 채움 |
| `phone` | string\|null | nullable | build(`phone`) | 전화번호 |
| `source` | enum(navi·osm·localdata·sangga·facility·parcel) | nullable | both | parcel트랙='parcel' |
| `dist_m` | number\|null | /reverse only | runtime | 질의점 거리(m) |

### 2.3 AS-IS ↔ TO-BE 대조 (오인 차단)
| 필드 | AS-IS(현행) | TO-BE(목표) | 차이 메우는 작업 |
|---|---|---|---|
| parcel `structure.sido/sigungu` | 없음 | 정식명 | C2(build_lawd_dong 정식명 적재) + 선결 backfill |
| parcel `structure.emd` | 입력토큰(`p["dong"]`) | lawd_dong.emd(권위) | X1(b) + C2 |
| parcel `structure.b_code` | 8자리 raw | 10자리(+`00`) | X1(d) 런타임 패딩 |
| parcel `name` | `상동 500-1답` | `경기도 부천시 상동 500-1` | X1(b) 지목제거+지역부가 (**의도적 변경, §7.2**) |
| addr `display.*` | 없음 | main/secondary/full | X1(a) |
| biz `structure` | geocode 경로 미부착 | 자체 컬럼으로 부착 | X1(e) non-addr 조립부 신설 |
| station/place/dong `secondary` 지역 | 없음(OSM None) | admin_boundary PIP | X1(e)+X6 |
| `ri` | jibun 문자열 합성 | 분리 컬럼 | X5(스키마)·X2(파서) / 그 전 런타임 파싱 best-effort |
| `bld_main_no/bld_sub_no` | 없음 | main_no/sub_no alias | X1 (runtime-alias) |
| `ji_main/ji_sub`(addr) | 없음 | jibun 파싱 best-effort | X1 |
| 프론트 라벨 | `undefined`(r.type) | 한글 라벨 | X3(r.kind + TYPE_KO 확장) |

### 2.4 3사 패리티 주석 (소스 한계 명시)
- **카카오**: 본 계약의 1차 정합 대상(필드표 매핑대로).
- **네이버**: category 가 `'한식>육류,고기요리'`처럼 콤마 멀티리프 → 본 계약은 `cat2` 단일 리프로 정규화(콤마 분해는 비범위). 멀티리프 원문은 보존하지 않음.
- **구글**: `types[]`(영문 enum) 소스가 빌드 파이프라인에 **부재**(F16). 구글 표기는 **참고(수기 대조)** 이며 자동 소싱·자동 회귀 대상 아님. 필드표의 'Google …'은 참고용.

---

## 3. 결과유형별 표기규칙 표 (코드 kind 집합 1:1)

> 공통: 보조줄=시도 **약칭**('서울','경기'), 전체표기=**정식**('서울특별시','경기도'). 약칭↔정식 변환은 **`SIDO_FULL`/`SIDO_ABBR` 양방향 매핑 테이블**(신규, §4.2)을 단일출처로 사용(런타임 `SIDO_NM` 약칭-only 의존 제거). 법정동≠행정동이면 보조줄에 `법정동(행정동)` 병기 가능. 동명중복은 항상 보조줄에 `시도 시군구` 노출. **미정의/미지원 kind 는 `main=name`, `secondary=null` 안전 fallback.**

| 결과유형(kind/subtype) | 주(main, 굵게) | 보조(secondary, 회색) | 전체(full) | 지역소스 근거 | 비고 |
|---|---|---|---|---|---|
| **지번** (addr/parcel) | `emd [산]ji_main[-ji_sub]` 예 `상동 500-1` | `시도약칭 시군구` 예 `경기 부천시` | `시도정식 시군구 emd [산]ji_main[-ji_sub]` | parcel.ji_main/ji_sub(backfill) + lawd_dong(emd/sido/sigungu) | 지목문자 제거(정규식 또는 ji_* 재조립). 산이면 본번 앞 '산' |
| **도로명** (addr/road) | `road_name bld_main_no[-bld_sub_no] (bld)` 예 `테헤란로 152 (…)` | `시도약칭 시군구 [emd] (zipcode)` | `road_str()`(정식) | address.road/main_no/sub_no/bld/postal | 지하건물=road 앞 '지하' |
| **상가/POI** (biz/facility) | `name`(상호명) — 주소 치환 금지 | `category.path · 시도약칭 시군구 [emd]` | `name (category.path) — road[, parcel]` | biz/facility 자체 컬럼(sido/sigungu/emd/road). 결측 시 admin_boundary PIP | 네이버 `<b>` 제거 |
| **OSM POI** (poi) | `name` | `[category.path · ]시도약칭 시군구` | `name — 시도약칭 시군구` | **OSM 행 지역 None → admin_boundary PIP 필수**(X6) | category 있으면 보조 선두 |
| **지명/행정동** (dong/place) | `name` 예 `역삼동` | `유형라벨 · 시도약칭 시군구` 예 `동 · 서울 강남구` | `시도약칭 시군구 name`(동)/`name (지명)` | **admin_boundary PIP**(자체 지역 None) | 유형라벨=kind→한글(런타임 합성, category 아님) |
| **도로** (road, kind='road') | `name`(도로명) | `도로 · 시도약칭 시군구` | `name — 시도약칭 시군구` | admin_boundary PIP | base 120 |
| **역** (station) | `name`(`…역`) 예 `강남역` | `지하철역 · 시도약칭 시군구` | `name (노선) — 시도약칭 시군구` | admin_boundary PIP | base 175 최상위. 노선데이터 있으면 활용 |
| **건물** (addr/road, bld 히트) | `bld` 예 `강남파이낸스센터` | 그 건물의 road(우선)/parcel | `bld — 시도정식 시군구 road bld_main_no[-bld_sub_no]` | bld ILIKE 히트 행(addr) | **bld 주 승격 = 신규 분기**(§5 X1-f). name=NULL 행이므로 main=bld·full=도로명 |

---

## 4. 빌드 vs 런타임 분담 · 주입지점 · 재빌드 반영 메커니즘

### 4.1 분담 원칙
- **빌드(build)** = 정적 컬럼/스키마: `09-gen-geocode.py`→`geocode.sqlite`→`load_geocode.py`(TRUNCATE+전량 재적재)→`address`/`poi`. 사전(lawd_dong/lawd_sigungu) 적재, 스키마 ALTER(ri/bld_*). → `build-studio` 그래프 안. 재빌드로만 반영.
- **런타임(runtime)** = 표시/조립/조인/파싱: `geocode-api-pg.py`. display 조립, parcel 트랙 region dict 조인, 8→10 패딩/san bool/jibun 파싱(ji_*·ri best-effort), bld_* alias, 비-addr structure 조립, admin_boundary PIP 지역보강. → 그래프 밖, **재기동 즉시 반영**(단 참조 테이블·신규 컬럼은 빌드로 채워져야 함).

### 4.2 주입지점

| 파일 | 변경 요지 | 재빌드 반영 |
|---|---|---|
| `scripts/postgis/build_lawd_dong.sql` (**신규, populator 전용**) | **CREATE 금지(스키마가 담당, F1)**, INSERT 만. 멱등: `INSERT INTO lawd_dong(emd_cd,sido,sigungu,emd) SELECT DISTINCT left(bcode,8), sido, sigungu, emd FROM address WHERE bcode IS NOT NULL AND kind='addr' ON CONFLICT (emd_cd) DO UPDATE SET sido=EXCLUDED.sido, sigungu=EXCLUDED.sigungu, emd=EXCLUDED.emd`. **소스=address(F3·F4, admin_boundary 아님)**. sido=정식명(navi c[1] 정식). PK=emd_cd char(8) 정합(F1) | load-all.sh 편입 + TFRESH **개별 등재** 필수 |
| `scripts/postgis/build_sigungu_dict.sh` (**기존, 가드 추가**) | 7z 소스 부재 시 `exit 1` 대신 **skip(기존 lawd_sigungu 보존)**: `DROP` 전에 소스 존재 확인, 없으면 메시지 후 정상종료. DROP→빈테이블 회귀(검색 `:177`) 차단(F11) | load-all.sh 편입 + TFRESH 등재 |
| `scripts/postgis/backfill_parcel_jibun.sql` (**기존, 옵트인**) | 내용 변경 없이 **배선만 통제**: `load-all.sh` 정규 parcel 단계에 **자동 호출 금지**, `STEPS=backfill` 옵트인 단계로 분리. **TFRESH 미등재(자동 stale 차단, F9·안전제약)** | **의도적 미자동화** — 명시 실행만 |
| `scripts/postgis/load-all.sh` | (1) geocode 단계 직후 `build_lawd_dong.sql`+`build_sigungu_dict.sh`(가드형) 호출, STEPS `dicts` 키 추가. (2) `backfill` 은 **default STEPS 제외**, 옵트인만 | load-all.sh 해시 변경→stale. 단 SQL 내용만 바뀌고 호출문 불변이면 무음(→개별 등재로 보완) |
| `scripts/build-studio.py:656-657` (TFRESH `load_postgis.scripts`) | 리스트에 **개별 추가**: `scripts/postgis/build_lawd_dong.sql`, `scripts/postgis/build_sigungu_dict.sh`, `scripts/postgis/apply-schema.sh`, `scripts/postgis/schema/10-base.sql`, `scripts/postgis/schema/21-parcel-jibun.sql`, `scripts/postgis/schema/11-address-search.sql`. **`backfill_parcel_jibun.sql` 은 제외(자동 전국 UPDATE 차단)** | `_target_sig(:702-737)` 가 각 `_script_hash` 합성 → SQL/스키마 수정도 재빌드 트리거. **미등재 시 무음 미반영** |
| `scripts/postgis/schema/10-base.sql` (address ALTER) | `ALTER TABLE address ADD COLUMN IF NOT EXISTS ri text;` (bld_main_no/bld_sub_no 는 **추가 안 함** — runtime-alias 로 충분). **san 은 address 에 추가 안 함**(parcel 한정 정책) | apply-schema 경유. TFRESH 등재 후 stale |
| `scripts/postgis/load_geocode.py` (COLS/INSERT) | `COLS`(:18-20)·`INSERT`(:75-83)·DDL(:53-55)에 `ri` 추가(**3곳 동기**, F4·F10). 컬럼 드리프트 방지 | TFRESH 등재됨(:657). 내용 해시 변경→stale |
| `scripts/09-gen-geocode.py` | (1) **biz/facility만**: `emd=법정동`(좌표 PIP 또는 지번파싱), CSV `행정동명`→`haeng_dong`(현 emd 자리, :214), sido `'서울특별시'→'서울'` **금지**(정식 유지, 약칭변환은 런타임 §3) — 정식명으로 적재해 navi 와 일관. (2) ri: `c[4]` 를 **별도 컬럼 `ri`** 로 분해(:106 SCHEMA + 추출). **navi addr 는 변경 금지(F5 정상)** | geocode.scripts(:650) 포함 → 재생성→load_postgis 연쇄 stale |
| `scripts/postgis/schema/21-parcel-jibun.sql` | 변경 없음(lawd_dong CREATE/PK/인덱스 기준 정합 확인용). materialize ALTER 는 **비권장·비범위** | apply-schema 경유. TFRESH 등재(스키마 추적) |

### 4.3 재빌드 반영 메커니즘
타깃 시그니처 = `(src SHA staged_sig) + (scripts _script_hash) + (dep_art 재귀)`. 한 입력/스크립트 변경 → 그 타깃 stale → 재빌드 → dep_art 연쇄.
- `09-gen-geocode.py`/`10-base.sql`/`load_geocode.py` 수정 → geocode 재생성·스키마 ALTER·전량 재적재 → 새 컬럼(ri) parity 보존 반영. dep_art=geocode 인 load_postgis·areas 자동 stale.
- postgis SQL/스키마는 **TFRESH scripts 에 개별 등재돼야** stale 판정. **호출문 불변·내용만 변경**도 개별 등재 시 해시로 잡힘(load-all.sh 해시만으론 누락).
- `backfill_parcel_jibun.sql` 은 **의도적 미등재** — 자동 전국 UPDATE 차단. 실행은 옵트인.
- API(`geocode-api-pg.py`)는 그래프 밖 — 재기동 반영.

### 4.4 parcel 지역명 복원 + 지번매칭 전략 (결정)
**결정**: 지역명(sido/sigungu/emd)은 **런타임 dict 조인**, 지번정수(ji_main/ji_sub/san)는 **backfill 선행 필수**. 둘은 별개 — dict 조인은 지번매칭을 대체하지 못한다(F9).

근거 — (1) parcel 39.9M materialize/지역 UPDATE 는 rewrite·수시간 부담인데 지번 질의는 보통 소수 행 → dict 해시조인 무비용(`:191` 소수행 철학). (2) lawd_sigungu 254행·lawd_dong ~5046행 메모리 상주. (3) 정규화 단일출처(사전 교체).

구체안: parcel SELECT 결과를 `JOIN lawd_dong ld ON ld.emd_cd=parcel.emd_cd`(emd/sido/sigungu 권위) 로 보강. 시군구 결합형 보완이 필요하면 `lawd_sigungu` 참조. **sido 정식명은 lawd_dong.sido(build_lawd_dong 가 address 에서 정식명 적재)에서 취득** — 런타임 `SIDO_NM` 약칭-only 의존 제거(약칭은 §3 표기 시 `SIDO_ABBR` 변환).

**선결조건(차단성)**:
1. `build_lawd_dong.sql` 신규 커밋 + load-all.sh 편입 + TFRESH 등재 → 없으면 `lawd_dong WHERE emd=%s`(:164) 공집합 → 빈 결과/ILIKE fallback(:213) 무음 강등. parcel sido/sigungu/emd 영구 미충족.
2. `backfill_parcel_jibun.sql` **실행 완료**(parcel.ji_main NOT NULL) → 없으면 `WHERE ... ji_main=%s AND ji_sub=%s`(:196) 0건 → G1 무음 실패. (옵트인 실행이므로 검증 전 게이트로 확인, §6 X4)
3. parcel `haeng_dong/h_code/road_name/bld*/zipcode` 는 **소스 없음(F2) → null 고정**. 'parcel 타일 지역명 직접표출' 요구 발생 시에만 materialize 재고.

---

## 5. 작업분해

표기: **변경파일** · **수용기준** · **의존성** · **conductor 병렬성**. 안전제약 준수.

### 표기 오버홀 (X)

#### X1 — API 분해응답 + parcel 버그수정 (런타임)
- 변경파일: `server/geocode-api-pg.py`
- 작업: (a) `display{main,secondary,full}` 빌더 신설(kind별 §3 규칙, 미정의 kind=name fallback). (b) parcel 경로(:159-207)에 lawd_dong 조인 + structure 완성(sido/sigungu 정식·emd 권위) + 지목문자 제거 + ji_main/ji_sub 전파, subtype='parcel', source='parcel'. (c) addr structure 에 `ri`(런타임 jibun 파싱 best-effort)/`ji_main`/`ji_sub`(jibun 파싱)/`bld_main_no`/`bld_sub_no`(=main_no/sub_no alias)/`bld_name`/`zipcode`/`road_name` 추가. **main_no/sub_no 의미 불변(도로명 건물본/부번)**. (d) parcel b_code 8→10 패딩(`emd_cd+'00'`), bld `''`→null, addr san=null. (e) **비-addr(biz/facility/osm/dong/place/station) structure 조립부 신설**: 자체 컬럼 사용 + 지역 결측 시 admin_boundary PIP(X6 헬퍼). (f) **건물명(bld ILIKE) 히트 분기**(이름경로 :257): main=bld·full=도로명 승격. (g) reverse nearest 에 display/structure 부착(비-addr 도 PIP 로 지역보강).
- 수용기준: 골든셋(§6) 전 항목 `display.main/secondary/full` non-empty. parcel structure.sido/sigungu/emd 채워짐(선결 충족 시), '500-1답' 지목문자 미노출. **main_no/sub_no 값·의미 불변**(도로질의 회귀 0). 미소싱 필드(parcel haeng_dong 등) null. **회귀정의: road/addr 트랙 name 불변; parcel·건물 트랙 name 은 의도적 변경(복원/승격) — lon/lat·히트집합 동일로 검증**.
- 의존성: 코드작성은 무관 병렬. **실효 검증은 C2(lawd_dong)+backfill 옵트인 실행 후**.
- conductor 병렬성: 코드작성 병렬 가능. 검증 직렬.

#### X2 — 빌드 산출 컬럼 정정 (biz/facility 한정 + ri 분해)
- 변경파일: `scripts/09-gen-geocode.py`(:106 SCHEMA, :214, :237, :257)
- 작업: **biz/facility 한정** — CSV `행정동명`→`haeng_dong`(현 emd 자리), emd=법정동(좌표 PIP 또는 지번파싱), sido **정식명 유지**(약칭변환은 런타임). ri=`c[4]` 별도 컬럼 분해(navi 파서). **navi addr 행은 변경 금지(F5 정상)** — emd/haeng_dong 동작 회귀 차단.
- 수용기준: biz/facility structure.emd=법정동·haeng_dong=행정동. ri 보유 면지역 결과에 ri 노출(X5 컬럼 선행). **navi addr 히트집합·emd·haeng_dong 불변**(13d-parity).
- 의존성: X5(스키마/적재 ri) 선행. 빌드 재생성 → **격리 검증DB 적재**로 검증.
- conductor 병렬성: X1과 병렬(파일 다름). 빌드 실행은 D1 백업 후 직렬.

#### X3 — 프론트 + 문서
- 변경파일: `demo/js/search.js`, API 계약 문서(`server/` README 또는 docs)
- 작업: `r.type`→`r.kind` 수정. **TYPE_KO 에 `addr/road/parcel/facility` 키 추가**(F13, 영문 폴백 차단) 또는 `display.secondary` 전면 대체. `display.main`(굵게)+`display.secondary`(회색) 2단 렌더, structure 활용. 계약 v2 문서화(additive·backwardCompat·소스한계 명시).
- 수용기준: 'undefined'/영문 kind 미출력(addr/facility 포함), 2단 표기, 동명중복 보조줄 구분.
- 의존성: X1 스키마 확정 후.
- conductor 병렬성: X1과 인터페이스 합의 후 병렬.

#### X4 — 검증/회귀 하니스 (+ 선결 게이트 + name 스냅샷)
- 변경파일: `scripts/13b-golden-extract.py`, `scripts/13d-geocode-parity.py`, `scripts/13e-geocode-bench.py`, **`scripts/13f-name-snapshot.py`(신규)**
- 작업: §6 골든셋 추가. **선결 게이트**: 검증 전 `lawd_dong COUNT>0` + `parcel ji_main NOT NULL 비율>임계`(예 골든셋 시도 한정) 확인, 미충족 시 FAIL-fast(무음 통과 차단). **name 스냅샷 하니스 신규**: 13d 가 좌표·행정동만 비교(F17)하므로 addr/road name 문자열 불변 + parcel/건물 name 의도변경을 별도 스냅샷으로 검증.
- 수용기준: 골든셋 자동 PASS, addr/road name 스냅샷 불변, parcel/건물 name 변경이 의도값과 일치, 기존 좌표 parity 회귀 0.
- 의존성: X1·X2·X5 산출 + C2·backfill 후.
- conductor 병렬성: 골든셋·게이트 정의 선행 병렬, 실행 직렬.

#### X5 — 스키마·적재 컬럼 신설 (ri) **[GO 전제조건]**
- 변경파일: `scripts/postgis/schema/10-base.sql`(address ADD COLUMN ri), `scripts/postgis/load_geocode.py`(COLS/DDL/INSERT 3곳 ri 동기), `scripts/09-gen-geocode.py`(SCHEMA ri 추출)
- 작업: ri 컬럼을 3-파일 동기 신설(F4·F10 컬럼 드리프트 방지). **bld_main_no/bld_sub_no 는 스키마 미추가(runtime-alias)**. **san address 미추가(parcel 한정)**.
- 수용기준: 적재 후 address.ri 존재, ri 보유 행 노출. 기존 컬럼 적재 불변.
- 의존성: apply-schema 경유 → load_postgis 재적재 동반. **격리 검증DB**(안전제약).
- conductor 병렬성: SQL/코드 작성 병렬, 적재 실행 D1 후 직렬.

#### X6 — admin_boundary PIP 지역보강 헬퍼 (런타임)
- 변경파일: `server/geocode-api-pg.py`
- 작업: 자체 지역 결측(OSM None/dong/place/station/road) 행에 `SELECT name, level FROM admin_boundary WHERE ST_Contains(geom, pt)` 로 sido/sigungu 보강 헬퍼(reverse `areas` 패턴 재사용, F14). 소수행만 호출(성능).
- 수용기준: G6/G7(역삼동/강남역) secondary 에 시도·시군구 노출.
- 의존성: X1(e/g)와 동일 파일 → 동일 PR 흡수 권장.
- conductor 병렬성: X1과 같은 파일 → 직렬(또는 X1 흡수).

### 데이터 체인 (D) — 직렬, 안전제약 핵심

#### D1 — 백업 (선행 게이트)
- 변경파일: 없음(운영). `backup_geocode_artifact` 스냅샷 확인 + parcel/address 덤프.
- 수용기준: `geocode.sqlite` 타임스탬프 스냅샷 존재 확인 후에만 후속 적재 허용.
- 의존성: 없음(최선행). conductor: 단독 선행.

#### D2 — POI 적재 (TRUNCATE 백업후, 격리DB)
- 변경파일: `load_geocode.py`(X5 반영본), load-all.sh
- 작업: geocode→address/poi 재적재. **TRUNCATE 는 D1 후에만**. **골든셋 검증은 격리 검증DB**(운영 `--only` 금지, F10).
- 수용기준: address/poi 행수 정상 범위, 골든셋 히트.
- 의존성: D1·X5 필수. conductor: D3·C2·backfill 과 동시 금지.

#### D3 — geom_pt 보강 (선택)
- 변경파일: 없음
- 작업: parcel 대표점은 런타임 `COALESCE(geom_pt, ST_PointOnSurface(geom))`(:193) 충분 — 전국 일괄 백필 금지. 부분 보강 선택.
- 수용기준: parcel 좌표 정상. 의존성: D2 후. conductor: 단독.

### 코드/운영 (C)

#### C1 — backfill 옵트인 배선 (리팩터 아님, **게이트로 격상**)
- 변경파일: `scripts/postgis/backfill_parcel_jibun.sql`(내용 유지), `load-all.sh`(STEPS=backfill 옵트인 분리)
- 작업: ji_main/ji_sub/san 백필은 **단일 전테이블 UPDATE(F9)** 임을 인정 — **자동배선·TFRESH 금지**. default STEPS 제외, 명시 옵트인. 필요 시 '시도 한정 backfill' 변형을 별도 작성해 안전제약(직렬·동시금지·전국회피) 정합.
- 수용기준: 멱등 재실행 안전. **G1 parcel SELECT ≥1건**(ji_main NOT NULL). 자동 stale 로 전국 UPDATE 가 트리거되지 않음(검증).
- 의존성: C2(lawd_dong)와 사전 정합. conductor: 실행은 DB 직렬·단독.

#### C2 — 오케스트레이터 + lawd_* 적재
- 변경파일: `scripts/postgis/build_lawd_dong.sql`(신규 populator), `build_sigungu_dict.sh`(가드), `load-all.sh`, `build-studio.py:656-657`
- 작업: lawd_dong **INSERT-only populator(소스=address, F3)** + lawd_sigungu(가드형) 오케스트레이션 + **TFRESH 개별 등재(build_lawd_dong/build_sigungu_dict/apply-schema/schema 3종)**. CREATE 는 schema(F1) 담당 — 역할 분리.
- 수용기준: lawd_dong ~5046행 적재(sido/sigungu/emd non-null), `WHERE emd=%s`(:164) 비공집합, TFRESH 시그니처 변경 감지. build_sigungu 소스부재 시 기존 테이블 보존(skip).
- 의존성: X1 선결조건. D1 후 실행. conductor: SQL 작성 병렬, 적재 D2 와 동시 금지.

#### C3 — SQLite 은퇴 (parity 상호작용 명시)
- 변경파일: 빌드그래프/운영 문서, `geocode-api.py`(레거시) 정리
- 작업: SQLite 경로 디프리케이트, PG 단일화. **주의(F17)**: parity(13d)는 SQLite vs PG 비교가 본질 → X1 이 PG parcel name/display 를 바꾸면 SQLite 백엔드는 옛 형태 유지(좌표 parity 는 통과). C3 전까지 name parity 의미 약화됨을 명시. 단계적·비파괴.
- 수용기준: PG 단독 골든셋 통과, 레거시 참조 제거 후 좌표 회귀 0.
- 의존성: X1·X3 안정화 후(마지막). conductor: 후행 단독.

#### C4 — 에러핸들링 (X1 동일파일)
- 변경파일: `server/geocode-api-pg.py`
- 작업: lawd_dong/parcel ji_* 공집합 시 fallback 로깅/경보(무음 강등 방지), None 컬럼 'None' 문자열 가드(addr_str/display), addr_at 미스 시 address=null 일관.
- 수용기준: 사전·backfill 미충족 시 명시 경고 로그, 'None' 문자열 미출력.
- 의존성: X1과 동일 파일 — 동일 PR. conductor: X1에 흡수.

### 병렬성 요약
- **병렬 그룹 A**(코드, 파일 충돌 없음): {X1+X6+C4 동일파일 묶음} ∥ X2 ∥ X5-SQL/코드 ∥ C2-SQL작성 ∥ C1-배선작성 ∥ X3(인터페이스 합의 후) ∥ X4-정의.
- **직렬 그룹 B**(대량 DB, 동시 금지): D1 → X5-적재/C2-적재/D2 직렬 → backfill 옵트인 실행(C1) → D3 → X4-실행(게이트 통과) → X1/X2 실효검증. **전부 격리 검증DB**.
- C3 는 전체 안정화 후 단독 후행.

---

## 6. 검증 (3사 비교 골든셋 + 회귀 + 선결 게이트)

### 6.1 선결 게이트 (X4, 무음실패 차단)
검증 시작 전 자동 확인 — 미충족 시 즉시 FAIL:
1. `SELECT count(*) FROM lawd_dong` > 0 (C2 적재 완료).
2. 골든셋 시도 범위에서 `parcel.ji_main NOT NULL` 존재 (backfill 옵트인 실행 완료).
3. 검증 대상 DB 가 **격리 검증DB**(운영 아님)임을 확인(F10 데이터 파괴 방지).

### 6.2 골든셋
| # | 질의 | 기대 kind/subtype | display.main | display.secondary | 검증 포인트 |
|---|---|---|---|---|---|
| G1 | `상동 500-1` | addr/parcel | `상동 500-1` | `경기 부천시` | 지목 '답' 미노출, sido/sigungu 복원(버그수정 핵심), ji_main/ji_sub. **선결 게이트 1·2 필수** |
| G2 | `강남대로 396` | addr/road | `강남대로 396` | `서울 강남구 … (우편)` | road_name/bld_main_no 분리, main_no 의미·값 불변 |
| G3 | `세종대로 110` | addr/road | `세종대로 110` | `서울 중구/종로구 …` | 도로명 표준, full 정식 시도명 |
| G4 | `카카오프렌즈` | biz/poi | `카카오프렌즈 …점` | `카테고리 · 시도 시군구 [emd]` | name 치환 금지, category path, structure 자체조립 |
| G5 | `약국`(또는 `장생당약국`) | biz | 상호명 | `약국 · 도로명주소` | category 보조줄, phone, category.sub 보존 |
| G6 | `역삼동` | dong | `역삼동` | `동 · 서울 강남구` | **admin_boundary PIP 지역(X6)**, 동명중복 보조줄 |
| G7 | `강남역` | station | `강남역` | `지하철역 · 서울 강남구` | base 175, **PIP 지역(X6)** |
| G8 | `강남파이낸스센터` | addr(bld 히트) | `강남파이낸스센터` | 도로명주소 | **bld 주 승격 분기(X1-f)**, name 의도변경(스냅샷 검증) |

### 6.3 3사 비교 + 회귀
- 골든셋을 카카오/네이버 동일질의 표기와 대조(주/보조/전체). **구글은 참고(수기)** — 자동 게이트 아님(F16).
- 회귀: `13d`(좌표·행정동) 스냅샷 불변 + **`13f-name-snapshot`(신규)**: addr/road name 불변, parcel/건물 name 의도값 일치. `13e` 레이턴시 회귀 없음(G1 parcel 인덱스 1파티션 Index Scan 유지, char 캐스팅 확인 :195).
- 실행 가드: **격리 검증DB만**(운영 TRUNCATE 금지, F10).

---

## 7. 리스크 · 하위호환 · 롤백

### 7.1 리스크
| 리스크 | 영향 | 완화 |
|---|---|---|
| 신규 SQL/스키마 TFRESH 미등재 | SQL·schema 수정 무음 미반영(F12, schema 일반 미추적) | C2 에서 build_lawd_dong/build_sigungu/apply-schema/schema 3종 **개별 등재**, 시그니처 변경 게이트 |
| backfill 자동배선 | 39.6M 전국 UPDATE 자동 실행(F9, 안전제약 위반) | **TFRESH·정규경로 미편입**, 옵트인만(C1) |
| lawd_dong 미적재 | parcel 조인 공집합→느린 ILIKE 강등 | C2 선행 + C4 경고 + X4 게이트 |
| ji_main NULL(backfill 미실행) | parcel SELECT 0건 무음 실패 | X4 게이트 2 + C4 경고 |
| '국소 재적재' 전국 TRUNCATE | 운영 데이터 소실(F10) | 격리 검증DB 강제, 운영 `--only` 금지 |
| main_no 의미 이동 | 인덱스/도로질의 breaking(F4) | **의미 이동 철회** — ji_main/ji_sub 별도 키, main_no 동결 |
| build_sigungu DROP 후 빈테이블 | 소스부재 시 검색(:177) 회귀(F11) | skip 가드(소스 없으면 보존) |
| ri 컬럼 미신설 | ri 영구 null(F6) | X5 3-파일 동기 + 런타임 파싱 best-effort |
| navi addr emd 오정정 | addr 회귀(F5) | X2 를 biz/facility 한정 |
| name 변경 미검출 | 외부 소비자 회귀 미발견(F17) | 13f name 스냅샷 신규 |

### 7.2 하위호환 (additive 우선 · 변경필드 명시)
1. `name` = `display.full` alias. **단 parcel·건물 트랙은 값 변경**(parcel: 지목제거+지역부가 '복원'; 건물: bld 승격). 이는 additive 아닌 **의도적 변경** → `contract_version='geocode/2'` 로 신호. road/addr 트랙 name 은 불변.
2. `kind` 정본 유지 — 프론트가 kind 읽도록 수정(F13). type 별칭 추가 금지(단기 필요시 `type=kind` 거울 1개 한시).
3. category: 기존 `{primary,label,sub}` **전부 보존**(F15, sub=cat2) + 신규 `{group,path}` 추가. additive.
4. structure: `bld_main_no`/`bld_sub_no`(=main_no/sub_no alias)·`ji_main`/`ji_sub`·`ri` 신규 추가. **`main_no`/`sub_no` 의미·값 동결**(도로명 건물본/부번, 인덱스 계약 보존) — 의미 이동 없음.
5. parcel 버그수정은 빈값→정상값 '복원'(structure 측) + name 변경(표시 측).
6. `contract_version='geocode/2'`: 모든 신규 필드는 additive 라 v1 호환이나, **name 값 변경 동반 시 버전 게이트 필수**(선택 아님).
7. 스키마 컬럼 드리프트 방지: ri 추가는 09 SCHEMA + 10-base + load_geocode COLS/INSERT **3곳 동기**(F4·F10) 의무.

### 7.3 롤백
- 런타임(X1/X6/C4/X3): 코드 revert + 재기동(즉시).
- 빌드(X2/X5): `geocode.sqlite` 직전 스냅샷(D1) 복원 후 load_geocode 재적재. ri 컬럼은 `DROP COLUMN` 또는 무시(nullable).
- 적재(C2): lawd_dong 은 populator 만 — `DELETE FROM lawd_dong` 또는 재실행 멱등(CREATE 는 schema 유지). build_sigungu 가드로 기존 보존.
- backfill(C1): 옵트인이라 미실행이 기본 — 롤백 표면 최소. 실행 후 되돌림은 재backfill(멱등) 또는 컬럼 NULL 화.
- 전국 자동영향 부재(dict 조인 + backfill 옵트인) → 롤백 표면 최소.
