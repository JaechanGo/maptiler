#!/usr/bin/env python3
"""T029 F3 집계 — 전후 필지소속 일치율 (완료조건 #4).

입력은 13g-parcel-membership.py 가 쓴 결과 CSV — F3_OUT (기본 /tmp/f3-result.csv).
DB 도 API 도 쓰지 않으므로 호스트에서 그대로 돌려도 된다.
출력 표의 해석은 docs/geocode-pnu-join-verification.md §8.3 에 있다.
"""
import csv
import collections
import io
import os

OUT = os.environ.get("F3_OUT", "/tmp/f3-result.csv")
rows = list(csv.DictReader(io.open(OUT, encoding="utf-8")))


def pct(n, d):
    return f"{n}/{d} ({100.0*n/d:.1f}%)" if d else "0/0 (n/a)"


def block(title, rs):
    n = len(rs)
    if not n:
        print(f"\n■ {title}: 표본 0")
        return
    b = sum(1 for r in rs if r["before_inside"] == "1")
    a = sum(1 for r in rs if r["after_inside"] == "1")
    print(f"\n■ {title} (n={n})")
    print(f"  before 필지소속  {pct(b, n)}")
    print(f"  after  필지소속  {pct(a, n)}")
    print(f"  차이             {100.0*(a-b)/n:+.1f}pp  ({a-b:+d}건)")
    # 전이표
    tr = collections.Counter((r["before_inside"], r["after_inside"]) for r in rs)
    print(f"  전이  유지OK {tr[('1','1')]}  개선 {tr[('0','1')]}  퇴행 {tr[('1','0')]}  유지NG {tr[('0','0')]}")


print("=" * 72)
print("T029 F3 — 개선 전후 '필지 소속 일치율'")
print("=" * 72)

# 데이터 위생
err_b = sum(1 for r in rows if r["api_err_before"])
err_a = sum(1 for r in rows if r["api_err_after"])
xb = sum(1 for r in rows if r["xcheck_before"] == "1")
xa = sum(1 for r in rows if r["xcheck_after"] == "1")
print(f"\n[위생] 표본 {len(rows)} · API 오류 before {err_b} / after {err_a}")
print(f"[위생] 재현 충실성(API 도로명 == 재현행 도로명)  before {pct(xb, len(rows))} · after {pct(xa, len(rows))}")

pip_ok = sum(1 for r in rows if r["pip_is_src"] == "1")
print(f"[위생] PIP 가 원본 필지를 되찾은 비율          {pct(pip_ok, len(rows))}")

# address_source 분포
src = collections.Counter(r["address_source"] for r in rows)
print("\n[경로] address_source 분포")
for k, v in src.most_common():
    print(f"  {k or '(없음)':10s} {pct(v, len(rows))}")

block("전체 표본", rows)

dae = [r for r in rows if r["jimok"] == "대"]
block("대(垈) 부분집합", dae)

nondae = [r for r in rows if r["jimok"] != "대"]
block("대 이외 지목", nondae)

# 조인 성공분 — 키 일치가 기하 포함을 보장하는가 (F2 의 실측 근거)
joined = [r for r in rows if r["join_id"]]
print(f"\n■ 키조인 성공분 (n={len(joined)}) — 「같은 필지 안에 있다」는 확언의 검증")
if joined:
    ins = sum(1 for r in joined if r["after_inside"] == "1")
    print(f"  그중 기하적으로 필지 안   {pct(ins, len(joined))}")
    gaps = sorted((float(r["after_gap_m"]) for r in joined if r["after_gap_m"]), reverse=True)
    out_gaps = sorted((float(r["after_gap_m"]) for r in joined
                       if r["after_inside"] == "0" and r["after_gap_m"]), reverse=True)
    if out_gaps:
        print(f"  필지 밖 {len(out_gaps)}건의 이격  최대 {out_gaps[0]:.1f} m · "
              f"중앙 {out_gaps[len(out_gaps)//2]:.1f} m · 최소 {out_gaps[-1]:.1f} m")
        over = [g for g in out_gaps if g > 30]
        print(f"  그중 30 m 초과 {len(over)}건")

# 도로명축이 실제로 바뀐 건수
ch = sum(1 for r in rows if r["road_changed"] == "1")
print(f"\n■ 도로명축 응답이 before↔after 로 달라진 건수  {pct(ch, len(rows))}")
chr_ = [r for r in rows if r["road_changed"] == "1"]
if chr_:
    cb = sum(1 for r in chr_ if r["before_inside"] == "1")
    ca = sum(1 for r in chr_ if r["after_inside"] == "1")
    print(f"  변경된 {len(chr_)}건 안에서  before 소속 {pct(cb, len(chr_))} → after 소속 {pct(ca, len(chr_))}")

# 지목별 표본 구성
jm = collections.Counter(r["jimok"] for r in rows)
print("\n[구성] 지목별 표본  " + " · ".join(f"{k} {v}" for k, v in jm.most_common()))
