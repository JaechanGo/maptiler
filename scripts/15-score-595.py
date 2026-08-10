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

판정: `rescore` 는 통과 건수와 비율을 출력한다. 규칙 적용 전/후를 **둘 다** 출력해야
      비교가 성립하므로 `--rules none` 실행을 반드시 함께 남긴다.

주의: `--server` 에 **기본값이 없다**. 미지정이면 오류 종료한다. 운영 서버는
      `--allow-production` 없이는 거부한다. `measure` 는 네트워크를 쓰는 유일한
      경로이며, T024 측정에서는 사용하지 않았다(저장 덤프 재채점만 수행).

의존: 표준 라이브러리(argparse·csv·json·urllib) + addr_norm. 선택적으로 openpyxl
      (원본 xlsx 를 읽을 때만, 항상 read_only=True).
"""
import argparse
import csv
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from addr_norm import RULES, base_norm, norm_jibun, post_norm, rule_b  # noqa: E402

PRODUCTION_HOSTS = ("192.168.102.245",)
AXES = ("roundtrip", "reverse")


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
    p.add_argument("--axis", required=True, choices=AXES)
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

    args = ap.parse_args()
    if args.selftest:
        return _selftest()
    if not getattr(args, "func", None):
        ap.print_help()
        return 2
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
