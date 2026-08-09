#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""표기(display) parity — 단일 PostGIS API(:8092)의 display.{main,secondary,full} 스냅샷 검증 + 데이터 선결게이트.

배경: 자매 하니스 13d-geocode-parity.py 는 top-1 좌표·reverse 행정동만 비교한다(스펙 F17). 따라서
  name/display 문자열 회귀를 구조적으로 못 잡는다 — parcel 지목문자 누출('상동 500-1답'), 시도/시군구
  미복원, 비-addr 카테고리 보조줄 누락이 좌표 parity 를 통과한 채 무음으로 새어나간다. 본 하니스는
  그 사각(F17)을 메운다: (1) 골든셋 비교 *이전에* DB 사전조건을 확인하는 선결게이트(fail-fast),
  (2) 5개 핵심질의의 display 기대치를 항목별 매칭전략으로 대조하는 표기 스냅샷.

선결게이트(골든셋 이전, fail-fast — 미적재/미백필 상태의 '거짓 PASS' 차단):
  GATE-1 : SELECT count(*) FROM lawd_dong  > 0           (실측 5046)
  GATE-2 : --gate-sido 범위 parcel ji_main NOT NULL 비율 ≥ --min-jimain-ratio
  GATE-DB: psql 부재/접속실패 → --skip-gate 없으면 exit 2(무음 통과 금지), 있으면 경고 후 진행

골든셋(게이트 통과 후): 상동 500-1 / 강남대로 396 / 세종대로 110 / 카카오프렌즈 / 장생당약국(폴백 약국).
  기대값은 라이브 응답 동결이 아니라 스펙 §6.2 + display_of()(geocode-api-pg.py) 로직에서 도출.

사용:
  python3 13f-display-parity.py --selftest        # 네트워크/DB 없이 순수헬퍼만 검증
  python3 13f-display-parity.py                    # 게이트(psql 5433) → 골든셋(HTTP 8092)
  python3 13f-display-parity.py --skip-gate        # 게이트 생략(개발용. CI 비권장)
  PGHOST=localhost PGPORT=5433 PGUSER=cuvia PGDATABASE=cuvia PGPASSWORD=cuvia \
      python3 13f-display-parity.py --show 40

판정/exit: selftest 성공 0 · 게이트 미충족 2 · 골든 불일치 1 · 전부통과 0.
의존: 표준 라이브러리(urllib) + 게이트 한정 psql subprocess. psycopg 미설치이므로 import 금지.
읽기전용: HTTP GET + psql 단일스칼라 SELECT(읽기전용 트랜잭션 전치)만. write/DDL 경로 코드상 부재.

포트 주의: 5433 = PostgreSQL(게이트 SELECT) · 8092 = HTTP API(골든셋 GET). 양자 분리.
"""
import argparse, json, os, re, shutil, subprocess, sys, unicodedata
import urllib.parse, urllib.request

# psql 읽기전용 강제 프리픽스 — write/DDL 시도 시 DB 가 에러로 즉시 실패(이중안전).
RO_PREFIX = "SET default_transaction_read_only=on; "

# ── 골든셋(스펙 §6.2 + display_of 도출). 5개 핵심질의 필수, 보강(역삼동/강남역)은 주석 상수 ──
# field 항목: (path, strategy, expected). strategy ∈ exact/contains/regex/present/eq_name.
#   eq_name: display.main == result.name(치환금지 불변). expected 는 런타임에 result.name 으로 대체.
GOLDEN = [
    {
        "id": "G1", "q": "상동 500-1", "kind": "addr", "subtype": "parcel",
        # 다건(대구27/부천41) 중 b_code 앞2='41'(경기) 후보. 없으면 secondary contains '부천'.
        "pick": {"bcode_prefix": "41", "secondary_contains": "부천"},
        "fields": [
            ("display.main", "exact", "상동 500-1"),          # 지목 '답' 제거 핵심
            ("display.secondary", "contains", "경기 부천시"),
            ("display.full", "contains", "상동"),
        ],
    },
    {
        "id": "G2", "q": "강남대로 396", "kind": "addr", "subtype": "road",
        "pick": {},  # road 질의는 results[0]
        "fields": [
            ("display.main", "contains", "강남대로 396"),     # 코드가 ' (강남역)' 부착 → contains
            ("display.secondary", "contains", "서울 강남구"),
            ("display.secondary", "regex", r"\(\d{5}\)"),     # 우편번호(가변) — 선택
            ("display.full", "contains", "서울특별시 강남구"),
        ],
    },
    {
        "id": "G3", "q": "세종대로 110", "kind": "addr", "subtype": "road",
        "pick": {},
        "fields": [
            ("display.main", "contains", "세종대로 110"),
            ("display.secondary", "regex", r"서울 (중구|종로구)"),  # 동명중복 흡수
            ("display.full", "contains", "서울특별시"),
        ],
    },
    {
        "id": "G4", "q": "카카오프렌즈", "kind": ("poi", "biz"), "subtype": None,
        "pick": {},
        "fields": [
            ("display.main", "eq_name", None),                # name 치환금지 불변
            ("display.main", "contains", "카카오프렌즈"),
            ("display.secondary", "present", None),           # 카테고리 path 형태 비고정
            ("display.full", "contains", "카카오프렌즈"),
        ],
    },
    {
        "id": "G5", "q": "장생당약국", "kind": ("biz", "poi"), "subtype": None,
        "fallback_q": "약국",                                 # 빈결과 시 일반질의 폴백(스펙 §3.1)
        "pick": {},
        "fields": [
            ("display.main", "present", None),                # 상호명 = result.name
            ("display.secondary", "present", None),           # '약국 …' 보조줄
        ],
    },
    # 보강(선택, 스펙 §6.2 추가질의). 핵심 5건 외 — 필요 시 GOLDEN 에 합류:
    # {"id":"G6","q":"역삼동","kind":"addr","pick":{},"fields":[("display.secondary","present",None)]},
    # {"id":"G7","q":"강남역","kind":("station","poi"),"pick":{},"fields":[("display.main","contains","강남역")]},
]


# ── 순수헬퍼(selftest 대상 — 네트워크/DB 부수효과 없음) ─────────────
def nfc_squash(s):
    """표기 정규화: NFC + 공백 1칸 압축 + 양끝 trim. None/비문자 → ''."""
    if s is None:
        return ""
    return re.sub(r"\s+", " ", unicodedata.normalize("NFC", str(s))).strip()


def valid_sido(code):
    """시도코드 화이트리스트 — 정확히 2자리 숫자만 SQL 결합 허용(인젝션 가드)."""
    return bool(re.fullmatch(r"\d{2}", str(code).strip()))


def sido_array_literal(csv_codes):
    """'11,41' → PG 배열리터럴 '{11,41}'. 각 코드 화이트리스트 검증(미통과 시 ValueError)."""
    codes = [c.strip() for c in str(csv_codes).split(",") if c.strip()]
    for c in codes:
        if not valid_sido(c):
            raise ValueError(f"시도코드 화이트리스트 위반(2자리 숫자 아님): {c!r}")
    if not codes:
        raise ValueError("시도코드 공집합")
    return "{" + ",".join(codes) + "}"


def gate2_sql(csv_codes):
    """GATE-2 비율질의(읽기전용 전치). 시도코드는 화이트리스트 검증 후만 결합."""
    arr = sido_array_literal(csv_codes)
    return (RO_PREFIX +
            "SELECT count(*) FILTER (WHERE ji_main IS NOT NULL)::float "
            f"/ NULLIF(count(*),0) FROM parcel WHERE sido_cd = ANY('{arr}')")


def match_field(strategy, expected, actual):
    """매칭전략 디스패치(순수함수). exact/contains/regex/present/eq_name."""
    a = nfc_squash(actual)
    if strategy == "present":
        return a != ""
    e = nfc_squash(expected)
    if strategy in ("exact", "eq_name"):
        return a == e
    if strategy == "contains":
        return e in a
    if strategy == "regex":
        return re.search(expected, a) is not None   # 패턴은 원형 유지(actual 만 정규화)
    raise ValueError(f"알 수 없는 매칭전략: {strategy!r}")


def get_path(obj, dotted):
    """'display.main' 점경로 탐색 → 값 또는 None(중간 결측 graceful)."""
    cur = obj
    for k in dotted.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(k)
    return cur


def pick_result(results, expect):
    """다건 응답에서 골든 대상 1건 식별. 기대 후보 부재 → None(거짓 PASS 금지)."""
    results = results or []
    if not results:
        return None
    pref = (expect or {}).get("bcode_prefix")
    if pref:
        for it in results:
            bcode = get_path(it, "address.structure.b_code")
            if bcode and str(bcode)[:2] == pref:
                return it
        sc = (expect or {}).get("secondary_contains")
        if sc:
            for it in results:
                if sc in nfc_squash(get_path(it, "display.secondary")):
                    return it
        return None                                  # 기대 후보 부재 → FAIL 유도
    return results[0]


# ── 부수효과 함수(HTTP/DB — selftest 미호출) ────────────────────
def http_json(base, path, params, timeout=10):
    """API GET → (dict, None) | (None, err). 13d 와 동일 시그니처(urllib)."""
    url = base.rstrip("/") + path + "?" + urllib.parse.urlencode(params)
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8")), None
    except Exception as e:  # noqa: BLE001 — 네트워크/JSON 오류 전부 불일치로 집계
        return None, f"{type(e).__name__}: {e}"


def _pg_env(args):
    """libpq 환경변수 + 기본값(localhost:5433 cuvia). 사용자 env 우선, 결측만 보강."""
    env = dict(os.environ)
    env.setdefault("PGHOST", "localhost")
    env.setdefault("PGPORT", "5433")
    env.setdefault("PGDATABASE", "cuvia")
    env.setdefault("PGUSER", "cuvia")
    env.setdefault("PGPASSWORD", "cuvia")
    return env


def pg_count(sql, args):
    """게이트용 단일스칼라 SELECT — psql -tAc subprocess(읽기전용). (value, None) | (None, err)."""
    if not shutil.which("psql"):
        return None, "psql 미존재(shutil.which None)"
    cmd = ["psql"]
    if args.pg_dsn:
        cmd.append(args.pg_dsn)
    cmd += ["-tAc", sql]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True,
                           env=_pg_env(args), timeout=30)
    except Exception as e:  # noqa: BLE001
        return None, f"{type(e).__name__}: {e}"
    if p.returncode != 0:
        return None, (p.stderr or p.stdout or "psql 비정상종료").strip()
    # RO_PREFIX 의 'SET' 명령태그가 앞줄로 출력되므로 마지막 비어있지 않은 줄(SELECT 스칼라)만 취함.
    out_lines = [ln for ln in p.stdout.splitlines() if ln.strip()]
    return (out_lines[-1].strip() if out_lines else ""), None


def gate_check(args):
    """선결게이트(fail-fast). 반환: (status, lines). status ∈ 'PASS'/'FAIL'/'DBERR'."""
    lines = []
    if not shutil.which("psql"):
        return "DBERR", ["psql 미존재 — 게이트 미수행"]

    # GATE-1: lawd_dong COUNT > 0
    v, err = pg_count(RO_PREFIX + "SELECT count(*) FROM lawd_dong", args)
    if err:
        return "DBERR", [f"GATE-1 접속/질의 실패: {err}"]
    try:
        cnt = int(v)
    except (TypeError, ValueError):
        return "FAIL", [f"GATE-1 lawd_dong COUNT 파싱불가: {v!r}"]
    if cnt <= 0:
        return "FAIL", ["GATE-1 lawd_dong 미적재(C2 populator 필요) — 거짓 PASS 차단"]
    lines.append(f"GATE-1 lawd_dong COUNT = {cnt} (> 0 ✓)")

    # GATE-2: 시도범위 parcel ji_main NOT NULL 비율 ≥ 임계
    try:
        sql2 = gate2_sql(args.gate_sido)
    except ValueError as e:
        return "FAIL", [f"GATE-2 시도코드 검증 실패: {e}"]
    v2, err2 = pg_count(sql2, args)
    if err2:
        return "DBERR", [f"GATE-2 질의 실패: {err2}"]
    if v2 is None or v2 == "":
        # NULLIF 분모 0 → 빈/NULL 반환: 시도범위 parcel 공집합
        return "FAIL", [f"GATE-2 parcel 시도범위({args.gate_sido}) 공집합 — 적재/시도코드 확인"]
    try:
        ratio = float(v2)
    except ValueError:
        return "FAIL", [f"GATE-2 비율 파싱불가: {v2!r}"]
    lines.append(f"GATE-2 parcel ji_main 비율(sido {args.gate_sido}) = {ratio:.4f} "
                 f"(임계 {args.min_jimain_ratio:g})")
    if ratio < args.min_jimain_ratio:
        lines.append(f"GATE-2 비율 {ratio*100:.2f}% < {args.min_jimain_ratio*100:g}% "
                     "(backfill 옵트인 미실행) — 차단")
        return "FAIL", lines
    return "PASS", lines


def check_golden(base, golden, show, limit):
    """질의별 display.{main,secondary,full}+kind 대조. 반환: (fail_count, samples)."""
    fail_count = 0
    samples = []

    def add_sample(line):
        if len(samples) < show:
            samples.append(line)

    for g in golden:
        gid, q = g["id"], g["q"]
        data, err = http_json(base, "/geocode", {"q": q, "limit": limit})
        if err:
            fail_count += 1
            add_sample(f"  [{gid} {q}] HTTP 오류: {err}")
            continue
        results = (data or {}).get("results") or []

        # 빈결과 → 폴백질의 재시도(G5)
        if not results and g.get("fallback_q"):
            data, err = http_json(base, "/geocode", {"q": g["fallback_q"], "limit": limit})
            results = (data or {}).get("results") or [] if not err else []

        if not results:
            fail_count += 1
            add_sample(f"  [{gid} {q}] results=[] (빈결과 — 데이터 미적재 또는 구버전 가능성)")
            continue

        item = pick_result(results, g.get("pick"))
        if item is None:
            fail_count += 1
            add_sample(f"  [{gid} {q}] 기대 후보 부재(pick 실패: {g.get('pick')})")
            continue

        q_failed = False

        # kind 대조(구버전에서도 kind 는 존재 → 거짓 FAIL 유발 안 함)
        exp_kind = g.get("kind")
        exp_kinds = exp_kind if isinstance(exp_kind, (list, tuple)) else (exp_kind,)
        if exp_kind is not None and item.get("kind") not in exp_kinds:
            q_failed = True
            add_sample(f"  [{gid} {q}] kind  기대:{exp_kind!r}  실제:{item.get('kind')!r}")

        # display 키 부재 → 구버전 서버 단서(핵심 회귀 검출)
        if "display" not in item:
            q_failed = True
            add_sample(f"  [{gid} {q}] display 키 부재 (구버전 서버 가능성)")
        else:
            for path, strategy, expected in g["fields"]:
                exp = item.get("name") if strategy == "eq_name" else expected
                actual = get_path(item, path)
                if not match_field(strategy, exp, actual):
                    q_failed = True
                    add_sample(f"  [{gid} {q}] {path}  {strategy}\n"
                               f"      기대: {exp!r}\n"
                               f"      실제: {actual!r}")
        if q_failed:
            fail_count += 1

    return fail_count, samples


def selftest():
    """네트워크/DB 없이 순수헬퍼 전수검증 후 'selftest OK'; return 0."""
    # 1) match_field 전략별
    assert match_field("exact", "상동 500-1", "상동 500-1") is True
    assert match_field("exact", "상동 500-1", "상동 500-1답") is False
    assert match_field("contains", "강남대로 396", "강남대로 396 (강남역)") is True
    assert match_field("contains", "강남대로 396", "강남대로") is False
    assert match_field("regex", r"서울 (중구|종로구)", "서울 중구 태평로1가") is True
    assert match_field("regex", r"서울 (중구|종로구)", "부산 해운대구") is False
    assert match_field("regex", r"\(\d{5}\)", "서울 강남구 (06232)") is True
    assert match_field("present", None, "약국 · 도로명주소") is True
    assert match_field("present", None, "") is False
    assert match_field("present", None, None) is False
    # 2) eq_name 불변: main == name → True / 치환 → False
    assert match_field("eq_name", "카카오프렌즈", "카카오프렌즈") is True
    assert match_field("eq_name", "카카오프렌즈", "카카오 프렌즈 매장") is False
    # 3) 정규화(NFC + 공백압축)
    assert nfc_squash("상동  500-1") == "상동 500-1"
    assert nfc_squash("  강남대로 396 ") == "강남대로 396"
    assert nfc_squash(None) == ""
    # 4) 시도코드 화이트리스트(인젝션 가드)
    assert valid_sido("41") is True
    assert valid_sido("11") is True
    assert valid_sido("4a") is False
    assert valid_sido("1;DROP") is False
    assert valid_sido("411") is False
    assert sido_array_literal("11,41") == "{11,41}"
    try:
        sido_array_literal("11,1;DROP TABLE parcel")
        raise AssertionError("인젝션 문자열이 통과됨")
    except ValueError:
        pass
    # 5) gate2_sql — 읽기전용 프리픽스 + 화이트리스트 결합(질의 미실행, 문자열만)
    sql = gate2_sql("11,41")
    assert "default_transaction_read_only=on" in sql
    assert "'{11,41}'" in sql
    assert "ji_main IS NOT NULL" in sql
    # 6) pick_result — 다건(대구27/부천41) 중 b_code 앞2 '41' 후보 선택
    fake = [
        {"address": {"structure": {"b_code": "2726010900"}}, "display": {"secondary": "대구 …"}},
        {"address": {"structure": {"b_code": "4119210900"}}, "display": {"secondary": "경기 부천시"}},
    ]
    chosen = pick_result(fake, {"bcode_prefix": "41", "secondary_contains": "부천"})
    assert chosen is fake[1], "부천(41) 후보 선택 실패"
    # 후보 부재 → None(거짓 PASS 금지)
    only_daegu = [{"address": {"structure": {"b_code": "2726010900"}}, "display": {"secondary": "대구 …"}}]
    assert pick_result(only_daegu, {"bcode_prefix": "41"}) is None
    # secondary contains 폴백
    no_bcode = [{"address": {"structure": {"b_code": None}}, "display": {"secondary": "경기 부천시 상동"}}]
    assert pick_result(no_bcode, {"bcode_prefix": "41", "secondary_contains": "부천"}) is no_bcode[0]
    # pick 미지정 → results[0]
    assert pick_result([{"x": 1}, {"x": 2}], {}) == {"x": 1}
    assert pick_result([], {}) is None
    # 7) get_path 점경로
    assert get_path({"display": {"main": "v"}}, "display.main") == "v"
    assert get_path({"display": {}}, "display.main") is None
    assert get_path({}, "a.b.c") is None

    print("selftest OK")
    return 0


def main():
    ap = argparse.ArgumentParser(description="표기(display) parity 하니스 — 선결게이트 + 골든셋")
    ap.add_argument("--base", default="http://localhost:8092", help="검증 대상 API(읽기전용 GET)")
    ap.add_argument("--limit", type=int, default=5, help="/geocode?limit=(다건 후보 확보)")
    ap.add_argument("--min-jimain-ratio", type=float, default=0.95,
                    help="parcel ji_main NOT NULL 비율 게이트 임계")
    ap.add_argument("--gate-sido", default="11,41", help="ji_main 비율 측정 시도범위(CSV, 2자리)")
    ap.add_argument("--skip-gate", action="store_true",
                    help="게이트 생략(개발용. CI 비권장 — 무음통과 방지)")
    ap.add_argument("--pg-dsn", default=None, help="psql 접속 오버라이드(미지정 시 libpq 환경변수)")
    ap.add_argument("--show", type=int, default=20, help="불일치 샘플 출력 개수")
    ap.add_argument("--selftest", action="store_true", help="네트워크/DB 없이 순수헬퍼만 검증")
    args = ap.parse_args()

    if args.selftest:
        return selftest()

    print(f"대상 API(B) = {args.base}   (게이트 PG = libpq env, 기본 localhost:5433)")

    # ── 선결게이트(골든셋 이전, fail-fast) ──
    if args.skip_gate:
        print("\n[게이트] --skip-gate 지정 → 선결게이트 생략(경고: 거짓 PASS 위험, CI 비권장).")
    else:
        status, lines = gate_check(args)
        print("\n[선결게이트]")
        for ln in lines:
            print(f"  {ln}")
        if status == "DBERR":
            print("\n⛔ 게이트 FAIL: psql 부재/접속실패로 게이트 미수행 — 무음 통과 금지.")
            print("   해결: PGHOST/PGPORT(5433)/PGUSER/PGDATABASE 설정 후 재실행, 또는 개발 시 --skip-gate.")
            return 2
        if status == "FAIL":
            print("\n⛔ 게이트 FAIL: DB 사전조건 미충족 — 골든셋 미실행(거짓 PASS 차단).")
            return 2
        print("  → 게이트 통과 ✓")

    # ── 표기 스냅샷 골든셋 ──
    fail_count, samples = check_golden(args.base, GOLDEN, args.show, args.limit)
    print(f"\n[골든셋] 핵심질의 {len(GOLDEN)}건 대조 (불일치 {fail_count}건)")
    if samples:
        print("  ── 불일치 샘플 ──")
        print("\n".join(samples))

    if fail_count == 0:
        print("\n✅ 표기 parity 통과 — display 회귀 없음.")
        return 0
    print(f"\n⛔ 표기 회귀 {fail_count}건 — display 불일치(지목누출/키부재/빈결과 등) 검출.")
    print("   현 :8092 가 X1(display 계약) 이전 구버전이면 전건 FAIL 이 정상(회귀검출 증거).")
    return 1


if __name__ == "__main__":
    sys.exit(main())
