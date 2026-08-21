#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""T046 N-1 — 역방향 계층 축의 **완화 전(`_strict`)** 수치를 낸다.

2 차 검수 N-1(Minor): 순방향에는 §5.6 "법정동코드 일치 — 완화 전후 병기" 표가
있는데 역방향 §6.7.3 에는 대응 표가 없다. 그 비대칭을 닫는다.

**`_strict` 는 "정규화 전"이 아니라 "완화 전"이다.** 계층 축(법정동코드 접두)은
숫자 문자열이라 문자열 정규화의 여지가 없다. `revmetrics._prefix_strict` 와
`_prefix_norm` 의 유일한 차이는 **§4.3 조건 1 의 시도코드 12 완화(`relax12`)**
뿐이다. §6.7.4 의 정규화 3 단(원문 / `bare` / 정규화)과 혼동하면 안 된다.

    _strict  a[:n] == b[:n]                        완화 없음
    (없음)   위 + sido_code_match(relax12=True)    앞 2 자리에만 완화

측정은 하지 않는다. **이미 산출돼 원자료에 남은 축을 다시 셀 뿐이다.**
VWorld 를 부르지 않고 `~/geocode-build/t046/out/rev2_*.jsonl` 만 읽는다.

분모 규약은 §6.7.3 과 같다 — `layer` 별로 나눈 뒤 `basis_ok` 인 행만,
그리고 **판정가능 분모**(양쪽 축이 존재한 건)를 쓴다. `aggregate_axis` 를
그대로 재사용해 규약이 어긋날 여지를 없앤다.

실행:
    /usr/bin/python3 scripts/t046/strict_table.py --tags src fwd
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from aggregate import _int, _pct                      # noqa: E402
from aggregate_rev2 import load_rows                  # noqa: E402
from revmetrics import aggregate_axis                 # noqa: E402

DEFAULT_ROOT = os.path.expanduser("~/geocode-build/t046")

#: 계층 축 — (표시 이름, 축 이름). `_strict` 가 존재하는 축은 이 넷뿐이다.
PREFIX_AXES = (
    ("시도 (앞 2)", "sido"),
    ("시군구 (앞 5)", "sgg"),
    ("읍면동 (앞 8)", "emd"),
    ("리 포함 10 자리", "ri_code"),
)

#: 칸 순서 — §6.7.3 표와 같다.
CELLS = (("src", "jibun"), ("src", "road"), ("fwd", "jibun"), ("fwd", "road"))
CELL_LABELS = ("`src` 지번", "`src` 도로명", "`fwd` 지번", "`fwd` 도로명")


def select(rows, layer):
    """§6.7.3 과 **같은** 선택 규약 — 그 층이고 기준 좌표를 확보한 건만."""
    return [r for r in rows if r.get("layer") == layer and r.get("basis_ok")]


def axis_strict_pair(rows, axis):
    """한 축의 완화 전/후를 **같은 분모 위에서** 낸다.

    `flipped`  완화가 불일치를 일치로 뒤집은 건수 (§5.6 의 "완화가 뒤집은 건").
    `reversed_` 완화가 일치를 깬 건수. 완화는 조건을 **넓히기만** 하므로
               원리상 0 이어야 한다. 0 이 아니면 축 정의가 깨진 것이다.
    `judgeable_mismatch` 완화 전후의 판정가능 건수 차. 판정가능 여부는 양쪽
               `bcode` 존재로만 정해지므로 역시 0 이어야 한다.

    이 두 자기검증 값을 **항상 함께 낸다.** 조용히 맞는 척하지 않기 위해서다.
    """
    a_s = aggregate_axis(rows, axis + "_strict")
    a_n = aggregate_axis(rows, axis)
    flipped = reversed_ = 0
    for r in rows:
        ax = r.get("axes") or {}
        s = (ax.get(axis + "_strict") or {}).get("eq")
        n = (ax.get(axis) or {}).get("eq")
        if s is None or n is None:
            continue
        if n and not s:
            flipped += 1
        elif s and not n:
            reversed_ += 1
    return {
        "axis": axis,
        "judgeable": a_s["judgeable"],
        "judgeable_norm": a_n["judgeable"],
        "judgeable_mismatch": a_s["judgeable"] - a_n["judgeable"],
        "eq_strict": a_s["eq"],
        "eq_norm": a_n["eq"],
        "rate_strict": a_s["rate_judgeable"],
        "rate_norm": a_n["rate_judgeable"],
        "flipped": flipped,
        "reversed": reversed_,
    }


def collect(root, tags):
    """`{(tag, layer): {축: 쌍}}`. 파일이 없으면 그 칸을 **비워 남긴다.**"""
    out = {}
    for tag in tags:
        path = os.path.join(root, "out", "rev2_%s.jsonl" % tag)
        rows = load_rows(path)
        for layer in ("jibun", "road"):
            part = select(rows, layer)
            out[(tag, layer)] = {
                "n": len(part),
                "axes": {ax: axis_strict_pair(part, ax)
                         for _label, ax in PREFIX_AXES},
            }
    return out


def _cells_present(data):
    return [c for c in CELLS if c in data]


def render(data, out=None):
    out = [] if out is None else out
    labels = [CELL_LABELS[CELLS.index(c)] for c in _cells_present(data)]
    out.append("#### 완화 전(`_strict`) 병기 — §5.6 과 대칭")
    out.append("")
    out.append("`_strict` 는 **정규화 전이 아니라 완화 전**이다 — §4.3 조건 1 의 "
               "시도코드 12 완화를 걸기 전. §6.7.4 의 정규화 3 단과 다른 축이다.")
    out.append("")
    out.append("| 축 | " + " | ".join(labels) + " |")
    out.append("| --- | " + " | ".join(["---:"] * len(labels)) + " |")
    for label, ax in PREFIX_AXES:
        row = []
        for c in _cells_present(data):
            d = data[c]["axes"][ax]
            row.append("%s → %s" % (_pct(d["rate_strict"]), _pct(d["rate_norm"])))
        out.append("| %s | %s |" % (label, " | ".join(row)))
    out.append("")
    out.append("완화가 뒤집은 건수(판정가능 분모):")
    out.append("")
    out.append("| 축 | " + " | ".join(labels) + " |")
    out.append("| --- | " + " | ".join(["---:"] * len(labels)) + " |")
    for label, ax in PREFIX_AXES:
        row = []
        for c in _cells_present(data):
            d = data[c]["axes"][ax]
            row.append("%s / %s" % (_int(d["flipped"]), _int(d["judgeable"])))
        out.append("| %s | %s |" % (label, " | ".join(row)))
    out.append("")
    bad = [(c, ax) for c in _cells_present(data) for _l, ax in PREFIX_AXES
           if data[c]["axes"][ax]["reversed"]
           or data[c]["axes"][ax]["judgeable_mismatch"]]
    if bad:
        out.append("**자기검증 실패 — 아래 칸에서 완화가 일치를 깨거나 판정가능 "
                   "분모가 어긋났다. 축 정의를 의심하라.**")
        out.append("")
        for c, ax in bad:
            d = data[c]["axes"][ax]
            out.append("- `%s`·%s `%s` — 역전 %d, 분모 차 %d"
                       % (c[0], c[1], ax, d["reversed"], d["judgeable_mismatch"]))
        out.append("")
    else:
        out.append("자기검증: 완화가 일치를 깬 건 **0**, 완화 전후 판정가능 분모 "
                   "차 **0** — 전 칸. 완화는 조건을 넓히기만 했다.")
        out.append("")
    return "\n".join(out) + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser(description="T046 N-1 완화 전(_strict) 병기")
    ap.add_argument("--tags", nargs="+", default=["src", "fwd"])
    ap.add_argument("--root", default=DEFAULT_ROOT)
    args = ap.parse_args(argv)

    data = collect(args.root, args.tags)
    doc = render(data)

    rep_dir = os.path.join(args.root, "out", "report")
    if not os.path.isdir(rep_dir):
        os.makedirs(rep_dir)
    with open(os.path.join(rep_dir, "rev2_strict.md"), "w") as fh:
        fh.write(doc)
    with open(os.path.join(rep_dir, "rev2_strict.json"), "w") as fh:
        json.dump({"%s|%s" % k: v for k, v in sorted(data.items())}, fh,
                  ensure_ascii=False, indent=1, sort_keys=True)
    sys.stdout.write(doc)
    sys.stderr.write("→ %s\n" % os.path.join(rep_dir, "rev2_strict.md"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
