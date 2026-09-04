#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""F-ri — 리(里) 절단 결함 수정의 **재분류 전후 대조**.

`split_pnu` 가 `emd_cd = pnu[:8]` 로 자르는 바람에 법정동코드 10 자리에서
리 2 자리가 사라졌고, 존재(`O=P`)·근사(`O_apx`)·심판 세 축이 모두 같은
절단 키로 조인됐다. 수정 후 수치를 내되 **전후를 같은 자리에서** 보인다.

## 세 상태를 누적으로 잰다

    S0  HEAD(8f87b9c, 구 9e26fdd) 코드 그대로 = 보고서에 실린 값 (절단, 보정 없음)
    S1  현행 코드, `legacy=False`            = 리 수정만
    S2  현행 코드, `legacy=True`             = 리 수정 + F2 구 시도코드 보정

S0 은 재구성이 아니라 **커밋된 파일 자체**를 불러 돌린다(`oracle_head.py` 는
`git show HEAD:scripts/t046/oracle.py` 의 출력이다). HEAD 판에는 `apx_batch`·
`referee_parcel_batch` 에 `legacy` 인자가 아예 없으므로 S0 은 무보정뿐이다 —
그것이 사실이고, 그래서 F2 지적이 성립한다.

## `r_v` 는 되살릴 수 없다

심판은 두 점을 본다: VWorld 순방향 점(`r_v`)과 우리 top-1 점(`r_m`).
판정 파일은 §3.3 에 따라 좌표를 버렸고, **VWorld 순방향 재호출은 금지**다.
따라서 `r_v` 는 이 대조에서 **미확인**이다. 다만 심판 SQL 이 정확 PNU 로
바뀌면서 다음이 성립한다:

    질의 PNU 가 `parcel` 에 없다  →  `r_v`·`r_m` 이 **둘 다** None  →  분류 11

즉 "필지 부재" 쪽은 `r_v` 없이도 **확정**된다. 필지가 있는 건만
`r_m` 이 정해지고 `r_v` 가 미확인이라 분류가 두 값으로 좁혀진다.
그 경계를 그대로 적는다. 추정으로 메우지 않는다.

읽기 전용이다. 기존 판정 파일·`f2_reclass.json`·`ri_truncation.json` 을
건드리지 않고 새 파일에 쓴다.
"""
import importlib.util
import json
import os
import sys
import time
import urllib.parse
import urllib.request

T046 = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, T046)

import classify                                   # noqa: E402
import oracle as oracle_now                       # noqa: E402

BUILD = os.path.expanduser("~/geocode-build/t046")
OUR = "http://127.0.0.1:8092"
OUT = os.path.join(BUILD, "out", "ri_reclass.json")


HEAD_REF = os.environ.get("T046_HEAD_REF", "8f87b9c")


def load_head_module():
    """`git show <REF>:scripts/t046/oracle.py` 를 그 자리에서 뽑아 모듈로 올린다.

    S0 은 **재구성이 아니라 커밋된 파일 자체**여야 한다. 지금 코드로 옛 동작을
    흉내내면 그 흉내가 맞는지를 다시 증명해야 한다. 참조는 `T046_HEAD_REF`
    환경변수로 바꿀 수 있다(기본 `8f87b9c` — 보고서 게재값을 낸 커밋. 리베이스
    전 SHA 는 `9e26fdd` 였고 `scripts/t046`·`tests/t046` 전 파일이 동일하다).
    """
    import subprocess
    import tempfile

    src = subprocess.check_output(
        ["git", "-C", os.path.dirname(os.path.dirname(T046)),
         "show", "%s:scripts/t046/oracle.py" % HEAD_REF])
    fd, path = tempfile.mkstemp(suffix="_oracle_head.py")
    with os.fdopen(fd, "wb") as fh:
        fh.write(src)
    spec = importlib.util.spec_from_file_location("oracle_head", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    if "p.pnu = k.pnu" in mod.sql_referee_parcel_batch(1):
        raise AssertionError("HEAD 판인데 정확 PNU 조인이 들어 있다 — 추출 실패")
    return mod


oracle_head = load_head_module()


def load_jsonl(path):
    with open(path, encoding="utf-8") as fh:
        return [json.loads(l) for l in fh if l.strip()]


def our_forward(query, limit=5, timeout=15.0):
    url = OUR + "/geocode?" + urllib.parse.urlencode([("q", query),
                                                      ("limit", limit)])
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def cand_pt(c):
    """후보 → `(lon, lat)`. `measure.cand_lonlat` 과 같은 규약."""
    for kx, ky in (("lon", "lat"), ("x", "y"), ("longitude", "latitude")):
        if c.get(kx) is not None and c.get(ky) is not None:
            try:
                return float(c[kx]), float(c[ky])
            except (TypeError, ValueError):
                return None
    return None


def parse_ours(body):
    res = (body or {}).get("results") or []
    addrs = [c for c in res if (c.get("kind") or "addr") == "addr"]
    return addrs, len(res) - len(addrs)


def timed(fn, *a, **kw):
    t = time.time()
    out = fn(*a, **kw)
    return out, round(time.time() - t, 2)


# ── 분류 보조 ─────────────────────────────────────────────────────────
def base_obs(r):
    """판정 행 → 분류 술어가 보는 관측(오라클·심판은 호출자가 갈아끼운다)."""
    return {"layer": r["layer"], "oracle": r["oracle"], "o_apx": r["o_apx"],
            "r_v": r["r_v"], "r_m": r["r_m"], "d_top1": r["d_top1"],
            "d_min5": r["d_min5"], "T": r["T"],
            "our_addr_count": r["our_addr_count"]}


def cls_of(o):
    for k in classify.CLASS_ORDER:
        if classify.PREDICATES[k](o):
            return k
    raise AssertionError("어느 분류에도 걸리지 않았다: %r" % (o,))


def is_c_group(r):
    """C 군(상위 5 후보가 전부 임계 밖) 여부. 오라클과 무관하므로 상태 불변."""
    return classify._c_group(base_obs(r))


def bump(d, k):
    d[k] = d.get(k, 0) + 1


# ── 본체 ──────────────────────────────────────────────────────────────
def run(tag, vname, sname):
    vp = os.path.join(BUILD, "out", vname)
    sp = os.path.join(BUILD, "sample", sname)
    if not (os.path.exists(vp) and os.path.exists(sp)):
        return None

    sample = {r["sid"]: r for r in load_jsonl(sp)}
    rows = [r for r in load_jsonl(vp) if r["gate"] is None]
    jb = [r for r in rows if r["layer"] == "jibun"]
    keys = {r["sid"]: str(sample[r["sid"]]["pnu"]) for r in jb
            if sample.get(r["sid"], {}).get("pnu")}

    rec = {"d0": len(rows), "jibun_d0": len(jb), "keys": len(keys),
           "timing_s": {}, "counters": {}}

    # ── 존재 오라클 O ────────────────────────────────────────────────
    # 축을 섞지 않는다. HEAD 의 `jibun_batch` 는 `legacy=True` 가 **기본값**
    # 이므로 그냥 부르면 리 축과 F2 축이 동시에 바뀐다. 네 칸으로 가른다:
    #
    #     S0  = 절단 · legacy off   ← 본 측정 재현(stored 와 같아야 한다)
    #     S0L = 절단 · legacy on    ← 이미 §7.6 에 게재된 F2 보정값
    #     S1  = 수정 · legacy off   ← S0 대비 **리 축 단독**
    #     S2  = 수정 · legacy on    ← S1 대비 **F2 축 단독**, S0L 대비 리 축 단독
    cells = {}
    o0, rec["timing_s"]["oracle_S0"] = timed(
        oracle_head.Oracle().jibun_batch, dict(keys), relax12=True,
        legacy=False)
    cells["S0"] = o0
    orc0l = oracle_head.Oracle()
    o0l, rec["timing_s"]["oracle_S0L"] = timed(
        orc0l.jibun_batch, dict(keys), relax12=True, legacy=True)
    cells["S0L"] = o0l
    orc1 = oracle_now.Oracle()
    o1, rec["timing_s"]["oracle_S1"] = timed(
        orc1.jibun_batch, dict(keys), relax12=True, legacy=False)
    cells["S1"] = o1
    orc2 = oracle_now.Oracle()
    o2, rec["timing_s"]["oracle_S2"] = timed(
        orc2.jibun_batch, dict(keys), relax12=True, legacy=True)
    cells["S2"] = o2
    rec["counters"]["oracle_S0L_legacy"] = {
        "attempts": orc0l.legacy_attempts, "hits": orc0l.legacy_hits}
    rec["counters"]["oracle_S2_legacy"] = {
        "attempts": orc2.legacy_attempts, "hits": orc2.legacy_hits}

    rec["oracle_dist"] = {s: {} for s in cells}
    for s, out in cells.items():
        for v in out.values():
            bump(rec["oracle_dist"][s], v)
    rec["oracle_stored_dist"] = {}
    for r in jb:
        bump(rec["oracle_stored_dist"], r["oracle"])
    rec["oracle_moves"] = {"S0->S1": {}, "S0L->S2": {}, "S0->S0L": {},
                           "S1->S2": {}, "stored->S0": {}}
    for k in keys:
        for tag, a, b in (("S0->S1", o0, o1),        # 리 축 단독(무보정)
                          ("S0L->S2", o0l, o2),      # 리 축 단독(보정)
                          ("S0->S0L", o0, o0l),      # F2 축 단독(절단)
                          ("S1->S2", o1, o2)):       # F2 축 단독(수정)
            if a[k] != b[k]:
                bump(rec["oracle_moves"][tag], "%s->%s" % (a[k], b[k]))
    stored = {r["sid"]: r["oracle"] for r in jb}
    for k in keys:
        if stored[k] != o0[k]:
            bump(rec["oracle_moves"]["stored->S0"], "%s->%s" % (stored[k], o0[k]))

    # ── 본번 근사 O_apx ──────────────────────────────────────────────
    apx = {}
    apx["S0"], rec["timing_s"]["apx_S0"] = timed(
        oracle_head.Oracle().apx_batch, dict(keys))
    apx["S1"], rec["timing_s"]["apx_S1"] = timed(
        oracle_now.Oracle().apx_batch, dict(keys), legacy=False)
    orc_a2 = oracle_now.Oracle()
    apx["S2"], rec["timing_s"]["apx_S2"] = timed(
        orc_a2.apx_batch, dict(keys), legacy=True)
    rec["counters"]["apx_S2_legacy"] = {
        "attempts": orc_a2.legacy_apx_attempts, "hits": orc_a2.legacy_apx_hits}
    rec["apx_true"] = {s: sum(1 for v in out.values() if v)
                       for s, out in apx.items()}
    rec["apx_true"]["stored"] = sum(1 for r in jb if r["o_apx"])
    rec["apx_moves"] = {
        "S0->S1": sum(1 for k in keys if apx["S0"][k] != apx["S1"][k]),
        "S1->S2": sum(1 for k in keys if apx["S1"][k] != apx["S2"][k]),
        "S0->S1_true_to_false": sum(1 for k in keys
                                    if apx["S0"][k] and not apx["S1"][k]),
    }

    # ── 심판 축 — C 군만. 우리 순방향으로 좌표를 되살린다 ─────────────
    cgrp = [r for r in jb if is_c_group(r) and r["sid"] in keys]
    rec["c_group"] = len(cgrp)
    fwd = {"ok": 0, "mismatch": 0, "failed": 0, "no_pt": 0}
    pts = {}
    for r in cgrp:
        s = sample[r["sid"]]
        try:
            body = our_forward(s["query"], 5)
        except Exception:                                  # noqa: BLE001
            fwd["failed"] += 1
            continue
        addrs, _ = parse_ours(body)
        pt = cand_pt(addrs[0]) if addrs else None
        same = (len(addrs) == r["our_addr_count"]
                and (addrs[0].get("source") if addrs else None) == r["source"])
        fwd["ok" if same else "mismatch"] += 1
        if pt:
            pts[r["sid"]] = (keys[r["sid"]], pt[0], pt[1])
        else:
            fwd["no_pt"] += 1
    rec["fwd_recall"] = dict(fwd, with_pt=len(pts))

    ref = {}
    ref["S0"], rec["timing_s"]["ref_S0"] = timed(
        oracle_head.Oracle().referee_parcel_batch, dict(pts))
    ref["S1"], rec["timing_s"]["ref_S1"] = timed(
        oracle_now.Oracle().referee_parcel_batch, dict(pts), legacy=False)
    orc_r2 = oracle_now.Oracle()
    ref["S2"], rec["timing_s"]["ref_S2"] = timed(
        orc_r2.referee_parcel_batch, dict(pts), legacy=True)
    rec["counters"]["ref_S2_legacy"] = {
        "attempts": orc_r2.legacy_referee_attempts,
        "hits": orc_r2.legacy_referee_hits}

    def dist(out):
        d = {"True": 0, "False": 0, "None": 0}
        for v in out.values():
            d[str(v)] += 1
        return d

    rec["referee_r_m"] = {s: dist(out) for s, out in ref.items()}
    rec["referee_r_m"]["stored"] = dist(
        {r["sid"]: r["r_m"] for r in cgrp if r["sid"] in pts})
    rec["referee_moves"] = {"S0->S1": {}, "S1->S2": {}}
    for k in pts:
        if ref["S0"][k] is not ref["S1"][k]:
            bump(rec["referee_moves"]["S0->S1"],
                 "%s->%s" % (ref["S0"][k], ref["S1"][k]))
        if ref["S1"][k] is not ref["S2"][k]:
            bump(rec["referee_moves"]["S1->S2"],
                 "%s->%s" % (ref["S1"][k], ref["S2"][k]))

    # ── 재분류 ───────────────────────────────────────────────────────
    # A·B 군은 `r_v` 와 무관하므로 세 상태 모두 확정 분류가 나온다.
    # C 군은 `r_v` 가 미확인이라 다음만 확정된다:
    #   필지 부재(`r_m is None`) → 11 확정
    #   필지 존재                → `r_m` 에 따라 {7,9}(True) 또는 {8,10}(False)
    rec["cls"] = {}
    for s in ("S0", "S1", "S2"):
        d = {"determined": {}, "c_group_bounded": {"{7,9}": 0, "{8,10}": 0},
             "c_group_cls11": 0}
        for r in jb:
            if r["sid"] not in keys:
                continue
            o = base_obs(r)
            o["oracle"] = cells[s][r["sid"]]
            o["o_apx"] = apx[s][r["sid"]]
            if not is_c_group(r):
                bump(d["determined"], str(cls_of(o)))
                continue
            rm = ref[s].get(r["sid"], "absent")
            if r["sid"] not in pts:
                d.setdefault("c_group_no_pt", 0)
                d["c_group_no_pt"] += 1
            elif rm is None:
                d["c_group_cls11"] += 1
            elif rm is True:
                d["c_group_bounded"]["{7,9}"] += 1
            else:
                d["c_group_bounded"]["{8,10}"] += 1
        rec["cls"][s] = d

    rec["cls_stored"] = {}
    for r in jb:
        bump(rec["cls_stored"], str(r["cls"]))

    rec["r_v_status"] = ("미확인 — VWorld 순방향 좌표가 판정 파일에 보존되지 "
                         "않았고(§3.3) 순방향 재호출은 금지다. 심판 SQL 수정의 "
                         "`r_v` 쪽 효과는 측정하지 못했다.")
    return rec


def main():
    t0 = time.time()
    out = {"generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
           "states": {"S0": "HEAD 8f87b9c(구 9e26fdd) — 절단 조인, 보정 없음(보고서 게재값)",
                      "S1": "현행 — 리 수정, legacy=False",
                      "S2": "현행 — 리 수정 + F2 구 시도코드 보정"},
           "runs": {}}
    for tag, v, s in (("main", "verdict_main.jsonl", "sample_a.jsonl"),
                      ("perturb", "verdict_perturb.jsonl", "sample_b.jsonl")):
        rec = run(tag, v, s)
        if rec:
            out["runs"][tag] = rec
    out["elapsed_s"] = round(time.time() - t0, 1)
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=False, indent=1)
    print(json.dumps(out, ensure_ascii=False, indent=1))
    print("→", OUT)


if __name__ == "__main__":
    main()
