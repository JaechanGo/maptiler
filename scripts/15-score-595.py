#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""행안부 595건 채점 하네스 — 정규화 규칙 적용 전/후를 같은 코드로 재채점한다 (T024).

배경: 채점 자산의 기대 지번에 표기 결함이 섞여 있어 정상 출력이 오답으로 집계됐다.
      기존 채점은 스크립트마다 제각각인 `norm()` 안에 정규화가 박혀 있어, 규칙을
      바꾸면 무엇이 달라졌는지 분리해 볼 수 없었다. 이 하네스는 정규화를
      `scripts/addr_norm.py` 한 곳에서만 가져오고(이 파일은 정규화를 정의하지
      않는다), 규칙 조합을 인자로 받아 **전/후를 같은 경로로** 측정한다.

두 축을 절대 섞지 말 것:
  왕복(roundtrip)  우리 순방향 → 우리 역방향. 기준선 516/595 = 86.7%.
                   자기참조 지표이므로 합격 판정용이 아니다.
  역지오(reverse)  xlsx 좌표 → 우리 역방향. 기준선 520/595 = 87.4%.
                   xlsx 좌표의 594/595 가 우리 순방향 산출물이라 이 축 역시 자기참조다.

사용:
      python3 scripts/15-score-595.py --selftest
      python3 scripts/15-score-595.py rescore --axis roundtrip --dump DUMP.json \
              [--rules none|C-1,A,B] [--corrections scripts/595-corrections.csv]
      python3 scripts/15-score-595.py invariant   --xlsx XLSX
      python3 scripts/15-score-595.py corrections-stats --corrections CSV
      python3 scripts/15-score-595.py measure --server http://HOST:PORT --xlsx XLSX
      python3 scripts/15-score-595.py check-independence --sample CSV --server http://HOST:PORT --out JSON
      python3 scripts/15-score-595.py measure-ground --sample CSV --independence-report JSON --server http://HOST:PORT

판정: `rescore` 는 통과 건수와 비율을 출력한다. 규칙 적용 전/후를 **둘 다** 출력해야
      비교가 성립하므로 `--rules none` 실행을 반드시 함께 남긴다.

주의: `--server` 에 **기본값이 없다**. 미지정이면 오류 종료한다. 운영 서버는
      `--allow-production` 없이는 거부한다. 네트워크를 쓰는 경로는 `measure`·
      `check-independence`·`measure-ground` 셋뿐이고 **모두 같은 가드를 통과한다**.
      T024 측정에서는 `measure` 를 쓰지 않았다(저장 덤프 재채점만 수행).

축:   S(자기정합) 와 G(실측 정확도) 는 서로 다른 축이며 더하지 않는다(A003 §6.0 P1).
      `rescore` 는 S 축 전용이라 `--axis ground` 를 거부한다. G 축은 `measure-ground`
      로만 재고, 그 앞에 `check-independence` 게이트를 반드시 통과해야 한다.

의존: 표준 라이브러리(argparse·csv·hashlib·json·math·urllib) + addr_norm. 선택적으로 openpyxl
      (원본 xlsx 를 읽을 때만, 항상 read_only=True).
"""
import argparse
import csv
import hashlib
import json
import math
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from addr_norm import RULES, base_norm, norm_jibun, post_norm, rule_b  # noqa: E402

PRODUCTION_HOSTS = ("192.168.102.245",)

# 축 이름. `AXES` 는 이 하네스가 아는 전체 축이고, `RESCORE_AXES` 는 그중
# **저장 덤프로 재채점이 가능한** 축만이다. G 축(ground)은 역지오(reverse)와 같은
# 이유로 재채점이 불가하다 — 서버를 다시 불러야만 값이 나온다.
AXES = ("roundtrip", "reverse", "ground")
RESCORE_AXES = ("roundtrip", "reverse")

# ------------------------------------------------------------------ G 축 상수
# A003 §6.0 P2: G 수치는 좌표 등급 없이는 무효다.
GROUND_GRADES = ("L4", "L2")
GROUND_CRS = "EPSG:4326"
KR_LAT_MIN, KR_LAT_MAX = 33.0, 39.0
KR_LON_MIN, KR_LON_MAX = 124.0, 132.0

# 자기참조 판정 임계. 표본 좌표가 우리 순방향 출력과 0.5 m 미만으로 붙어 있으면
# 그 행은 "독립 좌표원"이 아니다(T019 D-4 는 20/20 이 정확히 0.0 m 였다).
# 엄격 부등호 `<` 를 쓴다 — 정확히 0.50 m 는 세지 않는다.
GROUND_SELF_REF_M = 0.5

# 임계 비교는 전부 정수산술로 한다 (부동소수 반올림으로 경계가 흔들리지 않게).
GROUND_CONTAM_NUM, GROUND_CONTAM_DEN = 1, 20      # 오염 5%
GROUND_MIN_EFF_NUM, GROUND_MIN_EFF_DEN = 1, 2     # 유효건수 하한 50%

# 계통 편차 임계. 표본 전건이 이 거리를 넘어서 어긋나 있으면 "독립"이 아니라
# **데이텀 오선언이거나 순방향이 통째로 망가진 것**으로 본다. 상술은
# `judge_independence` 독스트링에 있다.
GROUND_SYSTEMATIC_M = 50.0

# IUGG 평균반경 R1. 0.5 m 눈금에서 구면-측지선 차이는 mm 급이라 P4 예산(167 mm)
# 안에 들어온다. pyproj·geopy 를 끌어오지 않는다(의존 계약 유지).
EARTH_R_M = 6371008.8


# ------------------------------------------------------------------ 정규화 조합
def make_normalizer(rule_names):
    """선택된 규칙만 얹은 정규화기를 만든다. 정규화 자체는 addr_norm 소유다."""
    if rule_names is None:
        return norm_jibun
    chosen = [fn for name, fn in RULES if name in rule_names]
    unknown = set(rule_names) - {name for name, _ in RULES}
    if unknown:
        raise SystemExit(f"알 수 없는 규칙: {sorted(unknown)} (가능: {[n for n, _ in RULES]})")

    def run(s):
        s = base_norm(s)
        for fn in chosen:
            s = fn(s)
        return s
    return run


def parse_rules(spec):
    if spec is None:
        return None
    spec = spec.strip()
    if spec.lower() in ("none", "off", ""):
        return []
    return [t.strip() for t in spec.split(",") if t.strip()]


# ------------------------------------------------------------------ 입력 적재
def load_expected(xlsx):
    """원본 xlsx 의 기대 지번. 원본 보호를 위해 read_only 로만 연다."""
    import openpyxl
    ws = openpyxl.load_workbook(xlsx, data_only=True, read_only=True)["Sheet1"]
    out = []
    for r in ws.iter_rows(min_row=4, values_only=True):
        no = "" if r[1] is None else str(r[1]).strip()
        if not no:
            continue
        out.append((no, "" if r[5] is None else str(r[5]).strip()))
    return out


def load_corrections(path):
    """applied_to_scoring=1 인 행만 채점 치환에 쓴다. 나머지는 기록용이다."""
    if not path:
        return {}, []
    with open(path, encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    applied = {r["no"]: r["expected_fixed"] for r in rows if r["applied_to_scoring"] == "1"}
    return applied, rows


def load_dump(path, axis):
    d = json.load(open(path))
    if axis == "roundtrip":
        if "rt_bad" not in d:
            raise SystemExit(f"{path}: 왕복 덤프가 아니다 (rt_bad 없음)")
        base_ok = d["metrics"]["rt_ok"]
        pairs = [(b["no"], b["in"], b["out"] or "") for b in d["rt_bad"]]
        return base_ok, pairs
    # 역지오 축: 우리 출력 595건이 저장돼 있지 않다.
    keys = sorted(d.keys())
    raise SystemExit(
        f"{path}: 역지오 축은 저장 덤프로 재채점할 수 없다.\n"
        f"  덤프 최상위 키 = {keys}\n"
        f"  'mismatch' 는 우리 출력과 VWorld 출력이 다른 행의 목록이지 채점 불일치 목록이 아니며,\n"
        f"  우리 /reverse 응답 595건은 저장돼 있지 않다. 이 축은 서버 재호출로만 재측정 가능하다.")


# ------------------------------------------------------------------ 채점
def rescore(base_ok, pairs, normalize, applied):
    """저장 덤프 재채점.

    기준선 통과분(base_ok)에 규칙을 다시 적용하지 않는 근거:
      정규화는 g∘f 합성이고 g 는 f 의 출력만 받는 순수 함수다. 기준선에서
      f(기대) == f(출력) 이었으므로 g(f(기대)) == g(f(출력)) 가 함수 정의상
      성립한다. 즉 규칙이 기존 통과분을 깨는 것은 구조적으로 불가능하다.
      덤프에 통과분 원문이 없어도 이 보존은 증명된다.
    """
    gained, still = [], []
    for no, expected, ours in pairs:
        e = applied.get(no, expected)
        (gained if normalize(e) == normalize(ours) else still).append(no)
    return base_ok + len(gained), gained, still


def fmt(total, n=595):
    return f"{total}/{n} = {total / n * 100:.1f}%"


# ------------------------------------------------------------------ 서버 가드
def resolve_server(value, allow_production):
    """서버 주소에 기본값을 두지 않는다 — 사고로 운영을 때리는 경로를 없앤다."""
    if not value:
        raise SystemExit(
            "--server 가 지정되지 않았다. 이 하네스는 기본 서버 주소를 두지 않는다.\n"
            "  측정 대상을 명시하라 (예: --server http://127.0.0.1:18080).")
    for host in PRODUCTION_HOSTS:
        if host in value and not allow_production:
            raise SystemExit(
                f"운영 서버({host}) 는 거부한다. 정말 필요하면 --allow-production 을 붙여라.")
    return value.rstrip("/")


# ------------------------------------------------------------------ 하위 명령
def cmd_rescore(args):
    normalize = make_normalizer(parse_rules(args.rules))
    applied, rows = load_corrections(args.corrections)
    base_ok, pairs = load_dump(args.dump, args.axis)

    total, gained, still = rescore(base_ok, pairs, normalize, applied)
    label = args.rules if args.rules is not None else "전체(C-1,A,B)"
    print(f"축      : {args.axis}")
    print(f"덤프    : {args.dump}")
    print(f"규칙    : {label}")
    print(f"정정표  : {args.corrections or '미적용'}"
          + (f" (채점 적용 {len(applied)}행 / 전체 {len(rows)}행)" if rows else ""))
    print(f"기준선  : {fmt(base_ok)}  (덤프의 통과분)")
    print(f"추가통과: {len(gained)}건  {','.join(sorted(gained, key=int))}")
    print(f"최종    : {fmt(total)}")
    print(f"잔여    : {len(still)}건  {','.join(sorted(still, key=int))}")
    return 0


def _corpus(xlsx, dump):
    """검사에 쓸 문자열 모음 — 기대값 595 + 덤프에 남아 있는 우리 출력."""
    strings = [e for _, e in load_expected(xlsx)]
    if dump:
        _, pairs = load_dump(dump, "roundtrip")
        strings += [o for _, _, o in pairs if o]
    return strings


def cmd_check_monotonic(args):
    """합격선 #1 — 규칙이 기존 통과분을 깨지 않음(broken=0).

    측정이 아니라 **구조 보장**이다. 덤프에는 불일치 행만 있어 통과분 원문을
    대조할 수 없으므로, 합성 g∘f 에서 나오는 함의를 증명하고 가용 문자열
    전수에서 반례가 0건임을 확인한다.
    """
    import inspect
    src = inspect.getsource(post_norm)
    reenters = "base_norm" in src
    print("[구조] norm_jibun = post_norm ∘ base_norm")
    print(f"[구조] post_norm 이 base_norm 을 재호출하는가 : {'예 — 증명 붕괴' if reenters else '아니오'}")
    print("[함의] base_norm(a)==base_norm(b) ⟹ norm_jibun(a)==norm_jibun(b)  (함수 정의상)")

    strings = _corpus(args.xlsx, args.dump)
    buckets = {}
    for s in strings:
        buckets.setdefault(base_norm(s), set()).add(norm_jibun(s))
    broken = sum(1 for v in buckets.values() if len(v) > 1)
    print(f"[실측] 검사 문자열 {len(strings)}개 / 기저 정규형 그룹 {len(buckets)}개")
    print(f"broken={broken}")
    print("✓ 기존 통과분 무파괴" if broken == 0 and not reenters else "✗ 단조성 위반")
    return 0 if (broken == 0 and not reenters) else 1


def cmd_check_corrections_scope(args):
    """합격선 #2 — 정정표가 '이미 통과하던 건'에 걸리지 않음(applied_to_passing=0)."""
    normalize = make_normalizer(parse_rules(args.rules))
    applied, _ = load_corrections(args.corrections)
    base_ok, pairs = load_dump(args.dump, "roundtrip")
    mismatched = {no for no, e, o in pairs if normalize(e) != normalize(o)}
    passing_all = set(applied) - {no for no, _, _ in pairs}   # 덤프에 없는 = 기준선 통과분
    still_mismatch = {no for no in applied if no in mismatched}
    on_passing = sorted(set(applied) - still_mismatch, key=int)
    print(f"정정표 채점 적용 행 : {sorted(applied, key=int)}")
    print(f"규칙 적용 후에도 불일치 : {sorted(still_mismatch, key=int)}")
    print(f"기준선에서 이미 통과하던 행에 적용 : {sorted(passing_all, key=int)}")
    print(f"applied_to_passing={len(on_passing)}")
    print("✓ 정정표는 잔여 불일치에만 적용된다" if not on_passing else "✗ 통과분에 정정이 걸렸다")
    return 0 if not on_passing else 1


_RE_SAN_LOT = re.compile(r"(?:^|\s)산\s*\d")


def _san_lot(s):
    """이 주소가 '산번지' 인가 — 공백 유무와 무관하게 판정한다."""
    return bool(_RE_SAN_LOT.search(s))


def _digits(s):
    """번지의 숫자 내용. 규칙 B(말미 -0 제거) 적용 후로 본다."""
    return re.findall(r"\d+", rule_b(base_norm(s)))


def cmd_check_collision(args):
    """합격선 #3(a) — 규칙이 **서로 다른 필지**를 같게 만들지 않음.

    규칙 A·B·C-1 은 같은 필지의 다른 표기를 합치라고 만든 것이므로,
    "정규화 후 같아진 쌍"의 존재 자체는 위반이 아니다(그게 목적이다).
    위반은 병합이 **필지 동일성의 경계를 넘을 때**만 성립한다.

      (1) 산 경계   — 한쪽은 산번지, 한쪽은 일반번지인데 같아짐
      (2) 숫자 내용 — 말미 `-0` 제거 외에 번지 숫자가 달라졌는데 같아짐

    parcel 전수 (emd_cd, jibun) 그룹 수 대조는 DB 가 필요하다 — 재현 불가.
    """
    strings = _corpus(args.xlsx, args.dump)
    buckets = {}
    for s in strings:
        buckets.setdefault(norm_jibun(s), set()).add(s)

    merged = {k: sorted(v) for k, v in buckets.items() if len(v) > 1}
    san_x, digit_x = [], []
    for key, members in merged.items():
        if len({_san_lot(base_norm(m)) for m in members}) > 1:
            san_x.append(members)
        if len({tuple(_digits(m)) for m in members}) > 1:
            digit_x.append(members)

    by_design = {"A": 0, "B": 0, "C-1": 0, "기타": 0}
    for members in merged.values():
        forms = {base_norm(m) for m in members}
        if any(f.endswith("-0") for f in forms):
            by_design["B"] += 1
        elif len({f.replace(" ", "") for f in forms}) == 1 and any(_san_lot(f) for f in forms):
            by_design["C-1"] += 1
        elif len({f.replace(" ", "") for f in forms}) == 1:
            by_design["A"] += 1
        else:
            by_design["기타"] += 1

    print(f"검사 문자열 {len(strings)}개 → 정규형 {len(buckets)}개 / 병합된 정규형 {len(merged)}개")
    print(f"의도된 병합(같은 필지의 다른 표기) : {by_design}")
    print(f"산 경계를 넘은 병합   : {len(san_x)}건 {san_x[:3]}")
    print(f"숫자 내용이 달라진 병합: {len(digit_x)}건 {digit_x[:3]}")
    total = len(san_x) + len(digit_x)
    print(f"collisions={total}")
    print("[미확인] parcel 전수 (emd_cd, jibun) 그룹 수 대조는 DB 가 필요하다 — Docker 미가동으로 재현 불가")
    print("✓ 필지 경계를 넘은 병합 없음" if total == 0 else "✗ 병합 발생")
    return 0 if total == 0 else 1


def cmd_invariant(args):
    """최상위 불변식 전수 검사 — norm(산X) != norm(X)."""
    rows = load_expected(args.xlsx)
    checked, viol = 0, []
    for no, e in rows:
        if "산" not in e:
            continue
        checked += 1
        if norm_jibun(e) == norm_jibun(e.replace("산", "", 1)):
            viol.append(no)
    print(f"기대값 {len(rows)}건 중 '산' 포함 {checked}건 검사")
    print(f"norm(산X) == norm(X) 위반 : {len(viol)}건 {viol}")
    print("✓ 산번지와 일반번지가 분리돼 있다" if not viol else "✗ 최상위 불변식 붕괴")
    return 0 if not viol else 1


def cmd_corrections_stats(args):
    _, rows = load_corrections(args.corrections)
    from collections import Counter
    print(f"총 {len(rows)}행")
    for key in ("category", "handler", "evidence_kind"):
        c = Counter(r[key] for r in rows)
        print(f"  {key:14s} " + "  ".join(f"{k}={v}" for k, v in sorted(c.items())))
    applied = [r["no"] for r in rows if r["applied_to_scoring"] == "1"]
    print(f"  applied_to_scoring=1  {len(applied)}행  {applied}")
    coord = [r["no"] for r in rows if r["applied_to_scoring"] == "1"
             and any(t in r["evidence"].lower() for t in ("좌표", "pip", "거리", "부존재"))]
    print(f"  채점 적용 행의 좌표·PIP·부존재 근거  {len(coord)}행  {coord}")
    return 0 if not coord else 1


def cmd_measure(args):
    """서버를 호출해 왕복 채점을 새로 만든다. T024 측정에서는 사용하지 않았다."""
    import urllib.parse
    import urllib.request
    server = resolve_server(args.server, args.allow_production)
    normalize = make_normalizer(parse_rules(args.rules))
    applied, _ = load_corrections(args.corrections)

    def get(path, **params):
        url = f"{server}{path}?{urllib.parse.urlencode(params)}"
        with urllib.request.urlopen(url, timeout=args.timeout) as r:
            return json.loads(r.read().decode())

    ok, bad = 0, []
    for no, expected in load_expected(args.xlsx):
        try:
            fwd = get("/geocode", q=expected)
            lat, lon = fwd["lat"], fwd["lon"]
            ours = get("/reverse", lat=lat, lon=lon).get("jibun", "")
        except Exception as exc:                       # noqa: BLE001
            bad.append((no, expected, f"<오류: {exc}>"))
            continue
        if normalize(applied.get(no, expected)) == normalize(ours):
            ok += 1
        else:
            bad.append((no, expected, ours))
    print(f"서버    : {server}")
    print(f"최종    : {fmt(ok)}")
    print(f"불일치  : {len(bad)}건")
    if args.out:
        json.dump({"server": server, "ok": ok,
                   "bad": [{"no": n, "in": e, "out": o} for n, e, o in bad]},
                  open(args.out, "w"), ensure_ascii=False, indent=1)
        print(f"저장    : {args.out}")
    return 0


# ------------------------------------------------------------------ selftest
def _selftest():
    fails = []

    def check(label, cond):
        if not cond:
            fails.append(label)

    # 서버 가드 — 기본값 없음, 운영 거부
    for label, kwargs in (("미지정 시 종료", dict(value=None, allow_production=False)),
                          ("빈 문자열 시 종료", dict(value="", allow_production=False)),
                          ("운영 거부", dict(value="http://192.168.102.245:18080",
                                             allow_production=False))):
        try:
            resolve_server(kwargs["value"], kwargs["allow_production"])
            check(label, False)
        except SystemExit:
            pass
    check("명시 주소 통과", resolve_server("http://127.0.0.1:18080/", False)
          == "http://127.0.0.1:18080")
    check("운영 명시 허용", resolve_server("http://192.168.102.245:18080", True)
          == "http://192.168.102.245:18080")

    # 규칙 조합
    off = make_normalizer([])
    allr = make_normalizer(None)
    check("규칙 없음은 기저만", off("경기도 김포시 월곶면 성동리263-8")
          == "경기도 김포시 월곶면 성동리263-8")
    check("전체 규칙", allr("경기도 김포시 월곶면 성동리263-8")
          == "경기도 김포시 월곶면 성동리 263-8")
    check("부분 규칙 B", make_normalizer(["B"])("구계리 617-0") == "구계리 617")
    check("부분 규칙 B 는 A 를 하지 않음", make_normalizer(["B"])("성동리263-8") == "성동리263-8")

    # 재채점 산술
    total, gained, still = rescore(
        500, [("1", "가리 1-0", "가리 1"), ("2", "가리 2", "가리 3")], allr, {})
    check("재채점 산술", (total, gained, still) == (501, ["1"], ["2"]))
    total, _, _ = rescore(500, [("9", "나리 5  산", "나리 5")], allr, {"9": "나리 5"})
    check("정정표 치환", total == 501)

    # 이 파일이 정규화를 재정의하지 않음 (합격선 #7).
    # 행 머리 기준으로 본다 — 아래 검사식 자체의 리터럴에 걸리지 않게 하기 위함이다.
    import re
    src = Path(__file__).read_text(encoding="utf-8")
    check("norm 재정의 없음", not re.search(r"(?m)^\s*def\s+norm", src))
    check("합성 구조 유지", norm_jibun("가 1-0") == post_norm(base_norm("가 1-0")))

    for f in fails:
        print(f"✗ {f}")
    print("✓ 15-score-595 selftest 통과" if not fails else f"✗ {len(fails)}건 실패")
    return 0 if not fails else 1


# ================================================================== G 축 (실측 정확도)
# A003 §6.0 의 네 원칙이 이 절의 설계 근거다.
#   P1 S 와 G 를 더하거나 섞지 않는다.
#   P2 모든 G 수치는 좌표 등급을 달고 다닌다.
#   P3 합격선은 같은 표본 동시 측정에서만 나온다.
#   P4 기준 좌표원은 재는 대상 오차의 1/3 이하여야 한다 (TUR 3:1).


def haversine_m(lat1, lon1, lat2, lon2):
    """두 위경도 사이 대권거리(m). 구면 근사이며 0.5 m 눈금에서 오차는 mm 급이다."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_R_M * math.asin(math.sqrt(a))


def sha256_of(path):
    """표본 파일의 지문. 독립성 보고서와 측정 대상 표본이 같은 파일인지 묶는 데 쓴다."""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def is_self_ref(d_m):
    """이 행이 자기참조인가. **엄격 부등호** — 정확히 0.50 m 는 세지 않는다.

    전제: `d_m` 은 유효 행(순방향이 좌표를 낸 행)의 거리다. 무응답 행은 애초에
    이 함수에 들어오지 않는다.
    """
    return d_m < GROUND_SELF_REF_M


def is_contaminated(hits, n):
    """오염률이 5% 이상인가.  20*hits >= n  ⟺  hits/n >= 5%  (부동소수 오차 없이)

    전제: `n > 0`. 판정 절차(`judge_independence`)의 0단계가 `n == 0` 을 미리 막으므로
    여기서 다시 막지 않는다. 방어를 두 겹으로 두면 어느 쪽이 진짜 관문인지 흐려진다.
    """
    return GROUND_CONTAM_DEN * hits >= GROUND_CONTAM_NUM * n


def has_enough_effective(n_effective, n):
    """유효 행이 절반 이상인가.  2*n_eff >= n  ⟺  n_eff/n >= 50%

    전제: `n > 0`. `is_contaminated` 와 같은 이유로 여기서 `n == 0` 을 막지 않는다.
    n == 0 이면 이 식은 무의미하게 True 를 낸다 — 그래서 0단계가 먼저 있어야 한다.
    """
    return GROUND_MIN_EFF_DEN * n_effective >= GROUND_MIN_EFF_NUM * n


def judge_independence(hits, n, n_effective, d_m_min):
    """독립성 3값 판정: "pass" · "fail" · "inconclusive".

    순서가 곧 의미다.
      0) n == 0        → 즉시 중단. 빈 표본은 판정 대상이 아니다.
      1) 오염          → "fail".    유효건수보다 **먼저** 본다. 표본이 오염됐다는 사실은
                                    유효 행이 몇 건이든 달라지지 않는다.
      2) 유효건수 부족 → "inconclusive". 전건 무응답이 "자기참조 0건 = 통과" 로
                                    읽히는 열린 실패를 막는다.
      3) 계통 편차     → "inconclusive". 아래 참조.
      4) 그 외         → "pass".

    3단계(닫힌 실패)를 두는 이유 — **범위 검사는 데이텀 수준 오선언을 잡지 못한다.**
    구 측지계(동경측지계) 위경도는 `crs=EPSG:4326` 선언 검사도, 한반도 범위 검사도
    그대로 통과한다. 데이텀 차이가 한반도에서 ~350 m 라 상자 안에 얌전히 들어오기
    때문이다. 그러면 전 행이 d ≈ 350 m 로 나오고, 자기참조는 0건이 되어 "pass" 가
    떨어지고 게이트가 열린다. P4 예산(167 mm)의 2,000 배쯤 되는 오차를 달고서.

    그래서 `hits == 0` 인데 표본 **최소** 거리마저 GROUND_SYSTEMATIC_M(50 m)을 넘으면
    "독립"이 아니라 "판정 불가"로 닫는다. 이론 하한은 ~1.5 m 인데(연속지적도 실측편차
    1.245~1.701 m; 송용현 2025, DOI 10.7848/ksgpc.2025.43.5.603 — n=21·대상지 2곳이라
    전국 대표성은 미검증) 표본 전체가 그 30배 넘게 벌어져 있다면, 정직한 결론은
    "우리 지오코더가 정확히 그만큼 나쁘다"가 아니라 **"이 표본으로는 판정할 수 없다"**다.
    최소값을 보는 이유는, 한 행이라도 정상 범위에 있으면 계통 오류가 아니기 때문이다.
    """
    if n <= 0:
        raise SystemExit("표본이 비어 있다. 독립성은 판정 대상이 없으면 판정할 수 없다.")
    if is_contaminated(hits, n):
        return "fail"
    if not has_enough_effective(n_effective, n):
        return "inconclusive"
    if hits == 0 and d_m_min is not None and d_m_min > GROUND_SYSTEMATIC_M:
        return "inconclusive"
    return "pass"


def _quantile_m(vals, q):
    """선형보간 분위수. 표본이 비면 None (없는 값을 0.0 으로 위장하지 않는다)."""
    if not vals:
        return None
    s = sorted(vals)
    if len(s) == 1:
        return round(s[0], 3)
    pos = q * (len(s) - 1)
    lo, hi = int(math.floor(pos)), int(math.ceil(pos))
    if lo == hi:
        return round(s[lo], 3)
    return round(s[lo] + (s[hi] - s[lo]) * (pos - lo), 3)


def load_ground_sample(path):
    """G 표본 CSV(7열)를 읽어 **전건 검증을 통과한** 행만 돌려준다.

        no,jibun,lat,lon,grade,source,crs

    좌표계는 선언(`crs` 열)과 관측(한반도 범위) 두 겹으로 검사한다. 다만 이 2중
    방어가 막는 것은 **축 수준** 오류뿐이다 — 평면좌표(TM/UTM-K)가 들어오거나
    위경도가 뒤바뀐 경우. **데이텀 수준 오선언은 여기서 잡히지 않는다.** 동경측지계
    위경도는 값의 생김새가 WGS84 와 같고 차이가 ~350 m 라 범위 상자를 그대로
    통과한다. 그쪽은 `judge_independence` 3단계가 거리 분포로 잡는다.

    한 행이라도 어긋나면 **부분 진행 없이** 즉시 중단한다. 절반만 검증된 표본으로
    낸 수치는 그 자체가 오염원이다.
    """
    rows = []
    with open(path, newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        for i, rec in enumerate(reader):
            ln = i + 2                                   # 헤더가 1행
            raw_crs = (rec.get("crs") or "").strip()
            if not raw_crs:
                raise SystemExit(
                    f"{path}:{ln}행 — `crs` 열이 없거나 비어 있다. G 표본은 좌표계를 "
                    f"반드시 선언해야 한다 (허용: {GROUND_CRS}).")
            crs = re.sub(r"\s+", "", raw_crs).upper()
            if crs != GROUND_CRS:
                raise SystemExit(
                    f"{path}:{ln}행 — `crs` 가 {GROUND_CRS} 가 아니다: {raw_crs!r}")
            for col in ("lat", "lon"):
                if rec.get(col) in (None, ""):
                    raise SystemExit(f"{path}:{ln}행 — `{col}` 이 비어 있다.")
            try:
                lat, lon = float(rec["lat"]), float(rec["lon"])
            except ValueError:
                raise SystemExit(
                    f"{path}:{ln}행 — lat/lon 이 수가 아니다: "
                    f"lat={rec['lat']!r} lon={rec['lon']!r}")
            if not (KR_LAT_MIN <= lat <= KR_LAT_MAX):
                raise SystemExit(
                    f"{path}:{ln}행 — `lat` 이 한반도 범위 밖이다: {lat!r} "
                    f"(허용 {KR_LAT_MIN}~{KR_LAT_MAX}). 선언은 {GROUND_CRS} 인데 값이 "
                    f"평면좌표이거나 위경도가 뒤바뀐 것으로 보인다.")
            if not (KR_LON_MIN <= lon <= KR_LON_MAX):
                raise SystemExit(
                    f"{path}:{ln}행 — `lon` 이 한반도 범위 밖이다: {lon!r} "
                    f"(허용 {KR_LON_MIN}~{KR_LON_MAX}).")
            grade = (rec.get("grade") or "").strip().upper()
            if grade not in GROUND_GRADES:
                raise SystemExit(
                    f"{path}:{ln}행 — `grade` 는 {list(GROUND_GRADES)} 중 하나여야 한다: "
                    f"{rec.get('grade')!r} (A003 §6.0 P2)")
            jibun = (rec.get("jibun") or "").strip()
            if not jibun:
                raise SystemExit(f"{path}:{ln}행 — `jibun` 이 비어 있다.")
            rows.append({
                "no": (rec.get("no") or str(ln)).strip(),
                "jibun": jibun, "lat": lat, "lon": lon, "grade": grade,
                "source": (rec.get("source") or "").strip(),
                # 보고서에는 **정규화된 정본**을 싣는다. 소비자(measure-ground)는
                # 정확 일치로 검사하므로, 여기서 정본을 못 박지 않으면 소문자 표본이
                # 게이트에서 통째로 거부된다.
                "crs": GROUND_CRS,
            })
    return rows


class AxisScore:
    """축 하나의 점수. **더할 수 없다** — 그게 이 타입이 존재하는 이유다.

    A003 §6.0 P1 은 S 와 G 를 섞지 말라고 한다. 규칙을 문서에만 두면 언젠가 누가
    합산한다. 그래서 `__add__` 를 막아 타입 수준에서 실패하게 만든다.

    막지 **못하는** 것도 분명히 해 둔다: `s.hit + g.hit`, `(s.rate()+g.rate())/2`,
    `statistics.mean([...])`, 사람이 표를 눈으로 더하기. 타입 방어는 세 겹 중 첫 겹일
    뿐이고, 나머지 둘은 보고 양식(`render_report`)과 문서 규칙(docs §5.3·§6)이다.
    """

    __slots__ = ("family", "axis", "grade", "hit", "n")

    def __init__(self, family, axis, hit, n, grade=None):
        if family not in ("S", "G"):
            raise ValueError(f"축 계열은 'S' 또는 'G' 여야 한다: {family!r}")
        if axis not in AXES:
            raise ValueError(f"알 수 없는 축: {axis!r} (가능: {list(AXES)})")
        if family == "G" and grade not in GROUND_GRADES:
            raise ValueError(
                f"G 수치는 좌표 등급 없이 존재할 수 없다 (A003 §6.0 P2). "
                f"grade={grade!r}, 가능: {list(GROUND_GRADES)}")
        if family == "S" and grade is not None:
            raise ValueError("S 축에는 좌표 등급이 없다. grade 를 넘기지 마라.")
        self.family, self.axis, self.grade = family, axis, grade
        self.hit, self.n = hit, n

    def rate(self):
        """이 축 자신의 분모로만 계산한다. `fmt()` 의 기본 분모 595 를 상속하지 않는다."""
        return 0.0 if self.n == 0 else 100.0 * self.hit / self.n

    def __add__(self, other):
        raise TypeError("축 점수는 합산할 수 없다 (A003 §6.0 P1)")

    __radd__ = __iadd__ = __add__

    def __repr__(self):
        g = f" [{self.grade}]" if self.grade else ""
        return f"<AxisScore {self.family}:{self.axis}{g} {self.hit}/{self.n}>"


def _score_cell(sc):
    """n == 0 은 '정확도 0%' 가 아니라 '측정 없음'이다. 둘을 같은 글자로 쓰지 않는다."""
    return "n/a" if sc.n == 0 else f"= {sc.rate():.1f}%"


def render_report(s_scores, g_scores, independence=None):
    """A003 §6.7 보고 양식. S 절과 G 절을 나누고, **둘을 잇는 칸을 만들지 않는다.**

    `assert` 를 쓰지 않고 `ValueError` 를 던진다. `python3 -O` 는 assert 문을 통째로
    지우기 때문에, 검증을 assert 로 쓰면 최적화 모드에서 P1·P2 위반이 그대로 통과한다.
    """
    for sc in s_scores:
        if sc.family != "S":
            raise ValueError(
                f"S 절에 {sc.family} 계열 점수가 들어왔다: {sc.axis} (A003 §6.0 P1)")
    for sc in g_scores:
        if sc.family != "G":
            raise ValueError(
                f"G 절에 {sc.family} 계열 점수가 들어왔다: {sc.axis} (A003 §6.0 P1)")

    out = ["[S] 자기정합도 — 내부 회귀 전용. 대외 정확도 주장에 쓰지 않는다."]
    if not s_scores:
        out.append("  (측정 없음)")
    for sc in s_scores:
        out.append(f"  {sc.axis:<10s} {sc.hit:>6d}/{sc.n:<6d} {_score_cell(sc)}")

    out.append("[G] 실측 정확도 — 대외 인용이 가능한 유일한 지표. 좌표 등급 표기 필수.")
    if not g_scores:
        out.append("  (측정 없음)")
    for sc in g_scores:
        out.append(
            f"  {sc.axis:<10s} [{sc.grade}] {sc.hit:>6d}/{sc.n:<6d} {_score_cell(sc)}")
    if independence is not None:
        v = independence.get("verdict", "?")
        r = independence.get("ratio_pct")
        ne = independence.get("n_effective", "?")
        nn = independence.get("n", "?")
        rs = "?" if r is None else f"{r:.2f}%"
        out.append(f"  독립성: {v} · 자기참조 {rs} · 유효 {ne}/{nn}")

    out.append("* S 와 G 는 서로 다른 축이다 — 더하거나 섞지 않는다 (A003 §6.0 P1).")
    return "\n".join(out)


def _fwd_latlon(resp):
    """순방향 응답에서 좌표를 꺼낸다. 없으면 None.

    실측한 계약은 좌표가 `results[i]` 안에 있다는 것이다(최상위에는 없다). 신규 코드는
    그 계약을 따르되, 최상위 키도 폴백으로 읽어 둔다. 기존 `cmd_measure` 는 최상위만
    읽는 옛 계약 그대로 두었다 — 자산 재작성 금지 제약이다(docs §7 경고 참조).
    """
    if not isinstance(resp, dict):
        return None
    cand = None
    results = resp.get("results")
    if isinstance(results, list) and results and isinstance(results[0], dict):
        cand = results[0]
    if cand is None and "lat" in resp and "lon" in resp:
        cand = resp
    if cand is None:
        return None
    try:
        return float(cand["lat"]), float(cand["lon"])
    except (KeyError, TypeError, ValueError):
        return None


def _rev_jibun(resp):
    """역방향 응답에서 지번을 꺼낸다. `address` 가 None 일 수 있어 방어적으로 읽는다."""
    if not isinstance(resp, dict):
        return ""
    addr = resp.get("address")
    if isinstance(addr, dict):
        v = addr.get("parcel")
        if isinstance(v, str):
            return v
    return ""


def _http_getter(server, timeout):
    """실제 HTTP 호출기. 테스트는 이 자리에 주입기를 끼워 서버를 부르지 않는다."""
    import urllib.parse
    import urllib.request

    def get(path, **params):
        url = f"{server}{path}?{urllib.parse.urlencode(params)}"
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read().decode())
    return get


def cmd_check_independence(args, get=None):
    """G 표본이 우리 순방향 출력과 독립인지 검사한다 (A003 §6.6, I3).

    각 행의 기대 지번을 순방향에 넣어 나온 좌표와 표본 좌표의 거리 d 를 잰다.
    d 가 0.5 m 미만이면 그 행은 우리 출력을 되받은 것이므로 독립 좌표원이 아니다.

    거리는 **양의(兩義)적**이다 — 작으면 자기참조이고, 크면 정확도가 나쁜 것일 수도
    데이텀이 어긋난 것일 수도 있다. 그래서 이 명령은 정확도를 말하지 않는다.
    독립성만 판정하고, 정확도는 `measure-ground` 가 따로 잰다.
    """
    server = resolve_server(args.server, args.allow_production)
    rows = load_ground_sample(args.sample)
    if get is None:
        get = _http_getter(server, args.timeout)

    per_row, dists, hits, n_no_result = [], [], 0, 0
    for r in rows:
        try:
            ll = _fwd_latlon(get("/geocode", q=r["jibun"]))
        except Exception as exc:                       # noqa: BLE001
            ll = None
            per_row.append({"no": r["no"], "d_m": None, "self_ref": None,
                            "note": f"오류: {exc}"})
        if ll is None:
            # 무응답 행은 hits 로도 n_effective 로도 세지 않는다. 판정 자료가 없다.
            n_no_result += 1
            if not per_row or per_row[-1].get("no") != r["no"]:
                per_row.append({"no": r["no"], "d_m": None, "self_ref": None})
            continue
        d = haversine_m(r["lat"], r["lon"], ll[0], ll[1])
        sr = is_self_ref(d)
        hits += 1 if sr else 0
        dists.append(d)
        per_row.append({"no": r["no"], "d_m": round(d, 3), "self_ref": sr})

    n = len(rows)
    n_effective = len(dists)
    d_min = round(min(dists), 3) if dists else None
    verdict = judge_independence(hits, n, n_effective, d_min)
    ratio_pct = round(100.0 * hits / n, 4) if n else 0.0

    report = {
        "schema": "ground-independence/1",
        "sample_path": os.path.abspath(args.sample),
        "sample_fingerprint": sha256_of(args.sample),
        "crs": GROUND_CRS,
        "server": server,
        "threshold_m": GROUND_SELF_REF_M,
        "contam_threshold_pct": 100.0 * GROUND_CONTAM_NUM / GROUND_CONTAM_DEN,
        "min_effective_pct": 100.0 * GROUND_MIN_EFF_NUM / GROUND_MIN_EFF_DEN,
        "systematic_threshold_m": GROUND_SYSTEMATIC_M,
        "n": n,
        "n_effective": n_effective,
        "n_no_result": n_no_result,
        "hits": hits,
        "ratio_pct": ratio_pct,
        # 거리 분포는 유효 행만으로 낸다. 데이텀 오선언을 읽어내는 근거라서
        # 판정값(verdict) 옆에 반드시 같이 실린다.
        "d_m_min": d_min,
        "d_m_median": _quantile_m(dists, 0.5),
        "d_m_p90": _quantile_m(dists, 0.9),
        "verdict": verdict,
        "per_row": per_row,
    }
    # 보고서는 **종료 전에** 쓴다. 실패했을 때가 증거가 가장 필요한 때다.
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2)

    print(f"서버      : {server}")
    print(f"표본      : {args.sample}  (n={n}, 유효={n_effective}, 무응답={n_no_result})")
    print(f"자기참조  : {hits}건 = {ratio_pct}%  (임계 "
          f"{100.0 * GROUND_CONTAM_NUM / GROUND_CONTAM_DEN}%)")
    print(f"거리      : min={d_min} median={report['d_m_median']} p90={report['d_m_p90']} (m)")
    print(f"판정      : {verdict}")
    print(f"보고서    : {args.out}")

    if verdict != "pass":
        sys.stderr.write(
            f"독립성 판정 {verdict}: 자기참조 {ratio_pct}% · "
            f"유효 {n_effective}/{n} · 보고서 {args.out}\n")
        raise SystemExit(3 if verdict == "fail" else 4)
    return 0


def cmd_measure_ground(args, get=None):
    """실측 정확도(G)를 잰다 (I4). 독립성 통과 보고서가 없으면 시작하지 않는다.

    게이트를 끄는 플래그는 **의도적으로 없다.** 끌 수 있는 게이트는 게이트가 아니다.
    통과하지 못한 표본으로 낸 수치는 A003 §6.0 P4 를 만족한다고 말할 수 없다.

    G 축은 역지오(S-2)와 같은 이유로 저장 덤프 재채점이 불가하다 — 재측정은 서버
    재호출로만 한다. 그래서 `rescore --axis ground` 는 argparse 단계에서 거부된다.
    """
    server = resolve_server(args.server, args.allow_production)
    rows = load_ground_sample(args.sample)

    try:
        with open(args.independence_report, encoding="utf-8") as fh:
            rep = json.load(fh)
    except (OSError, ValueError) as exc:
        raise SystemExit(f"독립성 보고서를 읽을 수 없다: {args.independence_report} ({exc})")

    if rep.get("sample_fingerprint") != sha256_of(args.sample):
        raise SystemExit(
            "독립성 보고서가 이 표본의 것이 아니다 (fingerprint 불일치). "
            "표본을 고쳤다면 check-independence 를 다시 돌려라.")
    if rep.get("crs") != GROUND_CRS:
        raise SystemExit(f"보고서 좌표계가 {GROUND_CRS} 가 아니다: {rep.get('crs')!r}")
    if rep.get("verdict") != "pass":
        raise SystemExit(
            f"독립성 판정이 통과가 아니다: {rep.get('verdict')!r}. "
            "이 표본으로 낸 정확도는 대외 인용할 수 없다 (A003 §6.0 P4).")

    # 여기까지 온 뒤에야 네트워크가 나간다.
    if get is None:
        get = _http_getter(server, args.timeout)
    normalize = make_normalizer(parse_rules(args.rules))

    tally, bad = {}, []
    for r in rows:
        try:
            ours = _rev_jibun(get("/reverse", lat=r["lat"], lon=r["lon"]))
        except Exception as exc:                       # noqa: BLE001
            ours = f"<오류: {exc}>"
        hit = bool(ours) and normalize(ours) == normalize(r["jibun"])
        # 등급을 섞어 한 점수로 내면 A003 §6.0 P2 위반이다. 등급별로만 집계한다.
        cell = tally.setdefault(r["grade"], [0, 0])
        cell[0] += 1 if hit else 0
        cell[1] += 1
        if not hit:
            bad.append({"no": r["no"], "grade": r["grade"],
                        "expected": r["jibun"], "ours": ours})

    # 표본에 **있는** 등급만 점수를 만든다. 없는 등급에 0/0 을 만들어 두면
    # 보고서에 '0.0%' 로 찍혀 정확도 0% 와 구별되지 않는다.
    g_scores = [AxisScore("G", "ground", hit, tot, grade=g)
                for g, (hit, tot) in sorted(tally.items())]

    print(f"서버      : {server}")
    print(f"표본      : {args.sample}  (n={len(rows)})")
    print(render_report([], g_scores, independence=rep))

    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump({
                "schema": "ground-measure/1",
                "sample_path": os.path.abspath(args.sample),
                "sample_fingerprint": rep.get("sample_fingerprint"),
                "crs": GROUND_CRS,
                "server": server,
                "independence_report": os.path.abspath(args.independence_report),
                "independence_verdict": rep.get("verdict"),
                "n": len(rows),
                "grades": [{"grade": s.grade, "hit": s.hit, "n": s.n,
                            "rate_pct": None if s.n == 0 else round(s.rate(), 4)}
                           for s in g_scores],
                "bad": bad,
            }, fh, ensure_ascii=False, indent=2)
        print(f"보고서    : {args.out}")
    return 0



def main():
    ap = argparse.ArgumentParser(
        description="행안부 595건 채점 — 정규화 규칙 전/후 비교",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--selftest", action="store_true", help="내장 점검 후 종료")
    sub = ap.add_subparsers(dest="cmd")

    def add_common(p, need_xlsx=False):
        p.add_argument("--rules", default=None,
                       help="적용 규칙. 'none' 이면 기저 정규화만. 예: 'C-1,A,B' (기본: 전체)")
        p.add_argument("--corrections", default=None, help="정정표 CSV 경로")
        if need_xlsx:
            p.add_argument("--xlsx", required=True, help="원본 xlsx (read_only 로만 연다)")

    p = sub.add_parser("rescore", help="저장 덤프 재채점 (서버 호출 없음)")
    p.add_argument("--axis", required=True, choices=RESCORE_AXES)
    p.add_argument("--dump", required=True)
    add_common(p)
    p.set_defaults(func=cmd_rescore)

    p = sub.add_parser("invariant", help="산번지≠일반번지 전수 검사")
    p.add_argument("--xlsx", required=True)
    p.set_defaults(func=cmd_invariant)

    p = sub.add_parser("check-monotonic", help="합격선 #1 — broken=0")
    p.add_argument("--xlsx", required=True)
    p.add_argument("--dump", default=None)
    p.set_defaults(func=cmd_check_monotonic)

    p = sub.add_parser("check-corrections-scope", help="합격선 #2 — applied_to_passing=0")
    p.add_argument("--dump", required=True)
    p.add_argument("--corrections", required=True)
    p.add_argument("--rules", default=None)
    p.set_defaults(func=cmd_check_corrections_scope)

    p = sub.add_parser("check-collision", help="합격선 #3(a) — collisions=0")
    p.add_argument("--xlsx", required=True)
    p.add_argument("--dump", default=None)
    p.set_defaults(func=cmd_check_collision)

    p = sub.add_parser("corrections-stats", help="정정표 구성 집계")
    p.add_argument("--corrections", required=True)
    p.set_defaults(func=cmd_corrections_stats)

    p = sub.add_parser("measure", help="서버 호출 측정 (--server 필수, 기본값 없음)")
    p.add_argument("--server", default=None, help="대상 서버. 기본값 없음 — 반드시 지정")
    p.add_argument("--allow-production", action="store_true")
    p.add_argument("--timeout", type=float, default=10.0)
    p.add_argument("--out", default=None)
    add_common(p, need_xlsx=True)
    p.set_defaults(func=cmd_measure)

    # --- G 축. `--server` 무기본값과 PRODUCTION_HOSTS 가드를 그대로 물려받는다.
    p = sub.add_parser("check-independence",
                       help="G 표본이 우리 순방향 출력과 독립인지 검사 (A003 §6.6)")
    p.add_argument("--sample", required=True, help="G 표본 CSV 7열")
    p.add_argument("--server", default=None, help="대상 서버. 기본값 없음 — 반드시 지정")
    p.add_argument("--allow-production", action="store_true")
    p.add_argument("--timeout", type=float, default=10.0)
    p.add_argument("--out", required=True, help="독립성 보고서 JSON 출력 경로")
    p.add_argument("--rules", default=None,
                   help="CLI 대칭용. 거리 계산에는 쓰이지 않는다")
    p.set_defaults(func=cmd_check_independence)

    p = sub.add_parser("measure-ground",
                       help="실측 정확도(G) 측정. 독립성 통과 보고서가 있어야 실행된다")
    p.add_argument("--sample", required=True, help="G 표본 CSV 7열")
    p.add_argument("--independence-report", required=True,
                   help="check-independence 가 낸 보고서. verdict=pass 여야 한다")
    p.add_argument("--server", default=None, help="대상 서버. 기본값 없음 — 반드시 지정")
    p.add_argument("--allow-production", action="store_true")
    p.add_argument("--timeout", type=float, default=10.0)
    p.add_argument("--out", default=None)
    p.add_argument("--rules", default=None,
                   help="적용 규칙. 'none' 이면 기저 정규화만. 예: 'C-1,A,B'")
    p.set_defaults(func=cmd_measure_ground)

    args = ap.parse_args()
    if args.selftest:
        return _selftest()
    if not getattr(args, "func", None):
        ap.print_help()
        return 2
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
