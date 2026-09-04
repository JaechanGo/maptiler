#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""F-ri 실증 — 심판 `True→False` 123 건이 정말 **이웃 리** 때문인가.

`ri_reclass.py` 는 절단 조인의 심판 True 123 건이 정확 PNU 조인에서 전부
False 로 뒤집힌다는 것을 보였다. 그 자체로는 "왜"를 말하지 못한다. 여기서는
우리 top-1 좌표를 **PIP 로 역조회**해 그 점이 실제로 들어 있는 필지의 PNU 를
얻고, 질의 PNU 와 자리별로 대조한다.

    bcode10 다름 ∧ 산·본번·부번 같음   →  **이웃 리의 동일 번지** (진단 확증)
    본번·부번도 다름                   →  전혀 다른 필지 (다른 원인)
    포함 필지 없음                     →  절단 조인이 무엇을 물었는지 별도 확인

집계만 낸다. 개별 주소·PNU 를 나열하지 않는다(판정 B).
"""
import json
import os
import sys
import time
import urllib.parse
import urllib.request

T046 = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, T046)

import classify                                   # noqa: E402
import pgprobe                                    # noqa: E402
from oracle import Oracle, split_pnu, _lit, sido_legacy_candidates  # noqa: E402

HEAD_REF = os.environ.get("T046_HEAD_REF", "8f87b9c")


def load_head_module():
    """`git show <REF>:scripts/t046/oracle.py` 를 그 자리에서 뽑아 모듈로 올린다.

    절단 조인 판은 **재구성이 아니라 커밋된 파일 자체**여야 한다. 지금 코드로
    옛 동작을 흉내내면 그 흉내가 맞는지를 다시 증명해야 하기 때문이다.
    """
    import importlib.util
    import subprocess
    import tempfile

    here = os.path.dirname(os.path.abspath(__file__))
    src = subprocess.check_output(
        ["git", "-C", os.path.dirname(os.path.dirname(here)),
         "show", "%s:scripts/t046/oracle.py" % HEAD_REF])
    fd, path = tempfile.mkstemp(suffix="_oracle_head.py")
    with os.fdopen(fd, "wb") as fh:
        fh.write(src)
    spec = importlib.util.spec_from_file_location("oracle_head", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    if "p.pnu = k.pnu" in mod.sql_referee_parcel_batch(1):
        raise AssertionError("절단 판이어야 하는데 정확 PNU 조인이 들어 있다")
    return mod

BUILD = os.path.expanduser("~/geocode-build/t046")
OUR = "http://127.0.0.1:8092"
OUT = os.path.join(BUILD, "out", "ri_verify_pip.json")
CHUNK = 400

# 점을 품는 필지를 **시도 파티션 안에서** 찾는다. GiST `parcel_geom_gix` 를 탄다.
SQL_PIP = """/* t046:pip_lookup */
WITH k(i, sido_cd, lon, lat) AS (VALUES %s)
SELECT k.i, p.pnu
FROM k JOIN parcel p
  ON p.sido_cd = k.sido_cd
 AND ST_Contains(p.geom, ST_SetSRID(ST_MakePoint(k.lon, k.lat), 4326))"""

# 시도 파티션 제약 없이 — 우리 top-1 이 다른 시도로 넘어간 건을 위해.
SQL_PIP_ALL = """/* t046:pip_any */
WITH k(i, lon, lat) AS (VALUES %s)
SELECT k.i, p.pnu
FROM k JOIN parcel p
  ON ST_Contains(p.geom, ST_SetSRID(ST_MakePoint(k.lon, k.lat), 4326))"""

# 절단 키가 실제로 몇 개 필지를 물었는지 — "이웃 리를 빌렸다"의 반대편 증거.
SQL_TRUNC = """/* t046:trunc_rows */
WITH k(i, sido_cd, emd_cd, san, ji_main, ji_sub) AS (VALUES %s)
SELECT k.i, count(*), count(DISTINCT substr(p.pnu, 1, 10))
FROM k JOIN parcel p
  ON p.sido_cd = k.sido_cd AND p.emd_cd = k.emd_cd
 AND p.san = k.san AND p.ji_main = k.ji_main AND p.ji_sub = k.ji_sub
GROUP BY k.i"""


def load_jsonl(path):
    with open(path, encoding="utf-8") as fh:
        return [json.loads(l) for l in fh if l.strip()]


def our_forward(query, limit=5, timeout=15.0):
    url = OUR + "/geocode?" + urllib.parse.urlencode([("q", query),
                                                      ("limit", limit)])
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def cand_pt(c):
    for kx, ky in (("lon", "lat"), ("x", "y"), ("longitude", "latitude")):
        if c.get(kx) is not None and c.get(ky) is not None:
            return float(c[kx]), float(c[ky])
    return None


def pip(items):
    """`items` = [(키, sido_cd, lon, lat), …] → `{키: [포함 필지 PNU, …]}`.

    `sid` 는 **정수**인데 psql 은 `i` 를 문자열로 돌려준다. 키를 그대로 쓰면
    전량 미스가 나고 그것이 "포함 필지 없음"으로 위장된다 — 인덱스를 키로
    보내고 `keymap` 으로 되돌린다(`ri_truncation_probe.py` 와 같은 규약).
    """
    got = {}
    for off in range(0, len(items), CHUNK):
        part = items[off:off + CHUNK]
        keymap = {str(n + off): k for n, (k, _s, _x, _y) in enumerate(part)}
        rows = []
        for n, (_k, sido, lon, lat) in enumerate(part):
            i = str(n + off)
            if n == 0:
                rows.append("(%s::text,%s::char(2),%.7f::double precision,"
                            "%.7f::double precision)"
                            % (_lit(i), _lit(sido), lon, lat))
            else:
                rows.append("(%s,%s,%.7f,%.7f)" % (_lit(i), _lit(sido), lon, lat))
        for row in pgprobe.run_sql(SQL_PIP % ", ".join(rows)):
            if row[0] in keymap:
                got.setdefault(keymap[row[0]], []).append(row[1])
    return got


def trunc_rows(items):
    """`items` = [(키, pnu19), …] → `{키: (행수, 서로 다른 법정동코드 수)}`."""
    got = {}
    for off in range(0, len(items), CHUNK):
        part = items[off:off + CHUNK]
        keymap = {str(n + off): k for n, (k, _p) in enumerate(part)}
        rows = []
        for n, (_k, pnu) in enumerate(part):
            p = split_pnu(pnu)
            i = str(n + off)
            if n == 0:
                rows.append("(%s::text,%s::char(2),%s::char(8),%d::smallint,"
                            "%d::int,%d::int)"
                            % (_lit(i), _lit(p["sido_cd"]), _lit(p["emd_cd"]),
                               p["san"], p["ji_main"], p["ji_sub"]))
            else:
                rows.append("(%s,%s,%s,%d,%d,%d)"
                            % (_lit(i), _lit(p["sido_cd"]), _lit(p["emd_cd"]),
                               p["san"], p["ji_main"], p["ji_sub"]))
        for row in pgprobe.run_sql(SQL_TRUNC % ", ".join(rows)):
            if row[0] in keymap:
                got[keymap[row[0]]] = (int(row[1]), int(row[2]))
    return got


def bump(d, k):
    d[k] = d.get(k, 0) + 1


def main():
    t0 = time.time()
    sample = {r["sid"]: r for r in load_jsonl(BUILD + "/sample/sample_a.jsonl")}
    jb = [r for r in load_jsonl(BUILD + "/out/verdict_main.jsonl")
          if r["gate"] is None and r["layer"] == "jibun"]

    def obs(r):
        return {"layer": r["layer"], "oracle": r["oracle"], "o_apx": r["o_apx"],
                "r_v": r["r_v"], "r_m": r["r_m"], "d_top1": r["d_top1"],
                "d_min5": r["d_min5"], "T": r["T"],
                "our_addr_count": r["our_addr_count"]}

    cg = [r for r in jb if classify._c_group(obs(r))
          and sample.get(r["sid"], {}).get("pnu")]

    # 우리 좌표 복원 — 우리 순방향만 부른다(VWorld 순방향은 금지).
    pts, pnus = {}, {}
    for r in cg:
        s = sample[r["sid"]]
        body = our_forward(s["query"], 1)
        addrs = [c for c in (body.get("results") or [])
                 if (c.get("kind") or "addr") == "addr"]
        pt = cand_pt(addrs[0]) if addrs else None
        if pt:
            pts[r["sid"]] = pt
            pnus[r["sid"]] = str(s["pnu"])

    # 절단 / 정확 두 심판을 같은 좌표로 돌려 뒤집힌 건을 고른다.
    keys_m = {k: (pnus[k], pts[k][0], pts[k][1]) for k in pts}
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    head = load_head_module()

    r_trunc = head.Oracle().referee_parcel_batch(dict(keys_m))
    r_exact = Oracle().referee_parcel_batch(dict(keys_m), legacy=False)
    flipped = [k for k in keys_m
               if r_trunc.get(k) is True and r_exact.get(k) is False]

    # PIP 역조회 — 점이 실제로 들어 있는 필지.
    items = [(k, pnus[k][:2], pts[k][0], pts[k][1]) for k in flipped]
    inside = pip(items)
    route = {k: "질의 시도 파티션" for k in inside}
    # 구 시도코드 건은 파티션이 달라 못 찾는다 — 신코드로 한 번 더.
    retry = [(k, sido_legacy_candidates(pnus[k])[0][:2], pts[k][0], pts[k][1])
             for k in flipped
             if k not in inside and sido_legacy_candidates(pnus[k])]
    for k, v in pip(retry).items():
        inside[k], route[k] = v, "구코드 보정 파티션"
    # 그래도 없으면 우리 점이 아예 다른 시도다 — 전 파티션으로 확인한다.
    rest = [(k, pts[k][0], pts[k][1]) for k in flipped if k not in inside]
    if rest:
        got = {}
        for off in range(0, len(rest), CHUNK):
            part = rest[off:off + CHUNK]
            keymap = {str(n + off): k for n, (k, _x, _y) in enumerate(part)}
            rows = []
            for n, (_k, lon, lat) in enumerate(part):
                i = str(n + off)
                rows.append(("(%s::text,%.7f::double precision,"
                             "%.7f::double precision)" % (_lit(i), lon, lat))
                            if n == 0 else
                            "(%s,%.7f,%.7f)" % (_lit(i), lon, lat))
            for row in pgprobe.run_sql(SQL_PIP_ALL % ", ".join(rows)):
                if row[0] in keymap:
                    got.setdefault(keymap[row[0]], []).append(row[1])
        for k, v in got.items():
            inside[k], route[k] = v, "타 시도 파티션"

    tr = trunc_rows([(k, pnus[k]) for k in flipped])

    diag = {"flipped": len(flipped), "pip_found": 0, "pip_none": 0,
            "verdict": {}, "n_inside_hist": {}, "trunc_rows_hist": {},
            "trunc_bcode_hist": {}, "route": {}}
    for k in flipped:
        bump(diag["route"], route.get(k, "미발견"))
    # 정합성 — 절단 조인이 0 행이면 절단 심판은 True 를 낼 수 없다.
    # 그런데도 0 이 보이면 **키 매핑이 깨진 것**이지 자료 부재가 아니다.
    dead = [k for k in flipped if tr.get(k, (0, 0))[0] == 0]
    if dead:
        raise AssertionError(
            "절단 심판이 True 를 낸 %d 건에서 절단 조인이 0 행이다 — "
            "키 매핑이 깨졌다. '자료 부재'로 오독하지 마라." % len(dead))
    for k in flipped:
        q = pnus[k]
        got = inside.get(k) or []
        bump(diag["n_inside_hist"], str(min(len(got), 5)))
        n_rows, n_bcode = tr.get(k, (0, 0))
        bump(diag["trunc_rows_hist"], str(min(n_rows, 10)))
        bump(diag["trunc_bcode_hist"], str(min(n_bcode, 10)))
        if not got:
            diag["pip_none"] += 1
            bump(diag["verdict"], "포함 필지 없음")
            continue
        diag["pip_found"] += 1
        # 질의 PNU 와 자리별 대조. 시도코드는 구/신 둘 다 인정한다.
        alt = sido_legacy_candidates(q)
        qq = {q} | ({alt[0]} if alt else set())
        tails = {x[10:] for x in qq}          # 산+본번+부번 9 자리
        bcodes = {x[:10] for x in qq}
        if any(x in qq for x in got):
            bump(diag["verdict"], "질의 필지 자신")
        elif any(x[10:] in tails and x[:10] not in bcodes for x in got):
            bump(diag["verdict"], "다른 법정동(리)의 동일 번지")
        elif any(x[:10] in bcodes for x in got):
            bump(diag["verdict"], "같은 법정동, 다른 번지")
        else:
            bump(diag["verdict"], "법정동·번지 모두 다름")

    out = {"generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
           "c_group": len(cg), "with_pt": len(pts),
           "r_trunc_true": sum(1 for v in r_trunc.values() if v is True),
           "r_exact_true": sum(1 for v in r_exact.values() if v is True),
           "diag": diag, "elapsed_s": round(time.time() - t0, 1)}
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=False, indent=1)
    print(json.dumps(out, ensure_ascii=False, indent=1))
    print("→", OUT)


if __name__ == "__main__":
    main()
