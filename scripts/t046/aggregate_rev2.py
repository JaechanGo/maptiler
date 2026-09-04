#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""T046 F1 — 역방향 재측정(`rev2_*.jsonl`) 집계. 마크다운을 낸다.

집계 규약(F5 — 분모를 뭉개지 않는다)

    판정가능   양쪽 축이 모두 존재한 건. 축 자체의 정확도.
    전체       기준 좌표를 만든 건 전부. 규칙 12 대로 **부재를 불일치**로 센다.
    사후가중   층별 판정가능 비율을 모집단 N_h 로 합성. 관측 0 인 층은 빼고
               남은 층의 가중을 1 로 재정규화한다(`aggregate.post_stratified_rate`).

세 수치를 모두 낸다. 하나만 내면 유리한 쪽을 고른 것이 된다.

정규화 3 단은 축 이름으로 구분한다 — `_strict`/`_raw`(원문) · `_bare`(공백만
제거, 지목 접미·산은 못 읽음) · 접미어 없음(§4.2 규칙 1~12 전체).

실행:
    /usr/bin/python3 scripts/t046/aggregate_rev2.py --tag src
"""
import argparse
import collections
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from aggregate import _int, _pct, load_population, post_stratified_rate  # noqa: E402
from revmetrics import AXES, aggregate_axis, ri_metrics, source_split    # noqa: E402

DEFAULT_ROOT = os.path.expanduser("~/geocode-build/t046")

#: 보고 순서. 정규화 전후를 **나란히** 놓아 기여분이 눈에 보이게 한다.
AXIS_GROUPS = (
    ("계층 — 법정동코드 접두", (
        ("시도 (앞 2)", "sido_strict", "sido"),
        ("시군구 (앞 5)", "sgg_strict", "sgg"),
        ("읍면동 (앞 8)", "emd_strict", "emd"),
        ("리 포함 10 자리", "ri_code_strict", "ri_code"),
    )),
    ("지번", (
        ("지번 원문", "jibun_raw", None),
        ("본번", "jibun_main_bare", "jibun_main"),
        ("부번", "jibun_sub_bare", "jibun_sub"),
        ("산 여부", None, "san"),
    )),
    ("도로명", (
        ("도로명", "road_raw", "road_name"),
    )),
    ("건물번호", (
        ("건물번호 원문", "bld_raw", None),
        ("건물 본번", "bld_main_bare", "bld_main"),
        ("건물 부번", "bld_sub_bare", "bld_sub"),
    )),
)


def load_rows(path):
    out = []
    with open(path, "r") as fh:
        for line in fh:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def _post(rows, axis, pop):
    """층별 판정가능 비율의 모집단 가중 합성. → `(값, 빠진 층 수)`."""
    num, den = collections.defaultdict(int), collections.defaultdict(int)
    for r in rows:
        h = (r.get("stratum") or "").replace("|", ":")
        eq = ((r.get("axes") or {}).get(axis) or {}).get("eq")
        if eq is None:
            den[h] += 0          # 층 자체는 등장시킨다(관측 0 으로 남는다)
            continue
        den[h] += 1
        num[h] += bool(eq)
    if not den:
        return None, 0
    val, dropped = post_stratified_rate(dict(num), dict(den), pop,
                                        with_dropped=True)
    return val, len(dropped)


def _axis_strata(rows, axis):
    """그 축을 **한 건이라도 판정한** 층의 수. 커버리지를 축마다 따로 낸다."""
    seen = set()
    for r in rows:
        if ((r.get("axes") or {}).get(axis) or {}).get("eq") is not None:
            seen.add(r.get("stratum"))
    return len(seen)


def axis_table(rows, pop, out):
    out.append("| 축 | 정규화 | 판정가능 | 판정가능 비율 | 일치(판정가능 분모) "
               "| 일치(전체 분모) | 사후가중 | 층 |")
    out.append("| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |")
    for group, items in AXIS_GROUPS:
        out.append("| **%s** | | | | | | | |" % group)
        for label, bare, norm in items:
            for tier, axis in (("전", bare), ("후", norm)):
                if axis is None:
                    continue
                a = aggregate_axis(rows, axis)
                pw, _dropped = _post(rows, axis, pop)
                out.append("| %s | %s | %s | %s | %s | %s | %s | %d |" % (
                    label, tier, _int(a["judgeable"]),
                    _pct(a["judgeable"] / float(a["n"]) if a["n"] else None),
                    _pct(a["rate_judgeable"]), _pct(a["rate_all"]),
                    _pct(pw), _axis_strata(rows, axis)))
    out.append("")


def ri_table(rows, out):
    m = ri_metrics([r.get("ri") or {} for r in rows])
    out.append("| 지표 | 우리 8092 | VWorld |")
    out.append("| --- | ---: | ---: |")
    out.append("| 리 문자열 **채움률** (전체 분모) | %s / %s = %s | %s / %s = %s |"
               % (_int(m["fill_ours"]), _int(m["n"]), _pct(m["fill_rate_ours"]),
                  _int(m["fill_vw"]), _int(m["n"]), _pct(m["fill_rate_vw"])))
    out.append("| 리 **코드** 일치율 (끝 2 자리, 판정가능 %s) | %s | — |"
               % (_int(m["code_judgeable"]), _pct(m["code_rate"])))
    out.append("| 리 **문자열** 일치율 (양측 채워진 %s 건만) | %s | — |"
               % (_int(m["name_judgeable"]), _pct(m["name_rate"])))
    out.append("| **코드-문자열 상호 불일치율** (판정가능 %s) | %s | %s |"
               % (_int(m["conflict_judgeable"]),
                  _pct(m["conflict_rate_ours"]), _pct(m["conflict_rate_vw"])))
    out.append("")
    out.append("리 문자열 일치는 **양쪽이 모두 채워졌을 때만** 묻는다. 둘 다 비어 "
               "있는 것을 '일치'로 세면 §4.2 의 '채움률이 아니라 정확도' 요구를 "
               "정면으로 어긴다.")
    out.append("")


def source_table(rows, out):
    sp = source_split(rows)
    total = float(len(rows)) or 1.0
    out.append("| `address_source` | 건수 | 비율 | 10 자리 일치 | 지번 본번 | "
               "도로명 | 건물 본번 | 리 문자열 채움 |")
    out.append("| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |")
    for src in sorted(sp, key=lambda s: (s is None, s)):
        part = sp[src]
        m = ri_metrics([r.get("ri") or {} for r in part])
        out.append("| `%s` | %s | %s | %s | %s | %s | %s | %s |" % (
            src, _int(len(part)), _pct(len(part) / total),
            _pct(aggregate_axis(part, "ri_code")["rate_judgeable"]),
            _pct(aggregate_axis(part, "jibun_main")["rate_judgeable"]),
            _pct(aggregate_axis(part, "road_name")["rate_judgeable"]),
            _pct(aggregate_axis(part, "bld_main")["rate_judgeable"]),
            _pct(m["fill_rate_ours"])))
    out.append("")


def coverage_table(rows, pop, out):
    n = len(rows)
    okrows = [r for r in rows if r.get("basis_ok")]
    strata = {r.get("stratum") for r in rows}
    ok_strata = {r.get("stratum") for r in okrows}
    covered = sum(pop.get((h or "").replace("|", ":"), 0) for h in ok_strata)
    total_pop = float(sum(pop.values())) or 1.0
    out.append("| 항목 | 값 |")
    out.append("| --- | ---: |")
    out.append("| 표본 | %s |" % _int(n))
    out.append("| 질의 좌표 확보 | %s (%s) |"
               % (_int(len(okrows)), _pct(len(okrows) / float(n) if n else None)))
    out.append("| 층 (기준 확보) | %d / %d |" % (len(ok_strata), len(strata)))
    out.append("| 모집단 몫 (기준 확보 층) | %s |" % _pct(covered / total_pop))
    out.append("")
    reasons = collections.Counter(r.get("skip") for r in rows if not r.get("basis_ok"))
    if reasons:
        out.append("기준 좌표를 만들지 못한 건 — **조용히 빼지 않는다.**")
        out.append("")
        out.append("| 사유 | 건수 | 비율 |")
        out.append("| --- | ---: | ---: |")
        for reason, c in reasons.most_common():
            out.append("| %s | %s | %s |" % (reason, _int(c), _pct(c / float(n))))
        out.append("")


def call_table(rows, out):
    n = len(rows)
    st = collections.Counter(r.get("rev_v_status") for r in rows)
    out.append("| 항목 | 건수 | 비율 |")
    out.append("| --- | ---: | ---: |")
    for k, c in st.most_common():
        out.append("| VWorld `status=%s` | %s | %s |"
                   % (k, _int(c), _pct(c / float(n))))
    out.append("| VWorld 응답에 **지번** 항목 | %s | %s |" % (
        _int(sum(1 for r in rows if r.get("vw_has_parcel"))),
        _pct(sum(1 for r in rows if r.get("vw_has_parcel")) / float(n))))
    out.append("| VWorld 응답에 **도로명** 항목 | %s | %s |" % (
        _int(sum(1 for r in rows if r.get("vw_has_road"))),
        _pct(sum(1 for r in rows if r.get("vw_has_road")) / float(n))))
    out.append("| 우리 8092 역방향 응답 | %s | %s |" % (
        _int(sum(1 for r in rows if r.get("ours_ok"))),
        _pct(sum(1 for r in rows if r.get("ours_ok")) / float(n))))
    out.append("")


def render(rows, pop, tag, basis):
    out = ["# T046 역방향 재측정 — 기준 `%s` (tag `%s`)" % (basis, tag), ""]
    out.append("질의 좌표 기준이 원본 본 측정과 **다르다.** 원본은 VWorld 순방향이 "
               "돌려준 좌표를 썼고 그 좌표는 판정 레코드에 남지 않았다(§3.3 이 "
               "좌표를 버린다). 순방향 재호출은 금지이므로 표본에서 재유도했다. "
               "따라서 이 표의 수치는 이미 발표한 `rev_*` 수치와 **직접 비교할 수 "
               "없다.**")
    out.append("")
    coverage_table(rows, pop, out)

    for layer in ("jibun", "road"):
        part = [r for r in rows if r.get("layer") == layer and r.get("basis_ok")]
        out.append("---")
        out.append("")
        out.append("## %s 층 (기준 확보 n=%s)" % (layer, _int(len(part))))
        out.append("")
        if not part:
            out.append("기준 좌표를 확보한 건이 없다.")
            out.append("")
            continue
        out.append("### 호출 결과")
        out.append("")
        call_table(part, out)
        out.append("### `address_source` 분해 (계획 §8-15)")
        out.append("")
        source_table(part, out)
        out.append("### 축별 일치율 — 정규화 전후 병기")
        out.append("")
        axis_table(part, pop, out)
        out.append("### 리(里) 4 지표")
        out.append("")
        ri_table(part, out)
        out.append("### 도농별")
        out.append("")
        out.append("| 도농 | 건수 | 10 자리 | 지번 본번 | 도로명 | 건물 본번 | "
                   "리 문자열 채움(우리) |")
        out.append("| --- | ---: | ---: | ---: | ---: | ---: | ---: |")
        for urban in ("urban", "rural"):
            sub = [r for r in part if r.get("urban") == urban]
            if not sub:
                continue
            m = ri_metrics([r.get("ri") or {} for r in sub])
            out.append("| %s | %s | %s | %s | %s | %s | %s |" % (
                urban, _int(len(sub)),
                _pct(aggregate_axis(sub, "ri_code")["rate_judgeable"]),
                _pct(aggregate_axis(sub, "jibun_main")["rate_judgeable"]),
                _pct(aggregate_axis(sub, "road_name")["rate_judgeable"]),
                _pct(aggregate_axis(sub, "bld_main")["rate_judgeable"]),
                _pct(m["fill_rate_ours"])))
        out.append("")
    return "\n".join(out) + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser(description="T046 역방향 재측정 집계(F1)")
    ap.add_argument("--tag", required=True)
    ap.add_argument("--root", default=DEFAULT_ROOT)
    args = ap.parse_args(argv)

    path = os.path.join(args.root, "out", "rev2_%s.jsonl" % args.tag)
    rows = load_rows(path)
    pop = load_population(os.path.join(args.root, "out", "population.json"))
    basis = rows[0].get("basis") if rows else "?"
    doc = render(rows, pop, args.tag, basis)

    rep_dir = os.path.join(args.root, "out", "report")
    if not os.path.isdir(rep_dir):
        os.makedirs(rep_dir)
    dest = os.path.join(rep_dir, "rev2_%s.md" % args.tag)
    with open(dest, "w") as fh:
        fh.write(doc)
    sys.stdout.write(doc)
    sys.stderr.write("→ %s\n" % dest)
    return 0


if __name__ == "__main__":
    sys.exit(main())
