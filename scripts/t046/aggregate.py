#!/usr/bin/env python3
"""T046 §7 — 판정결과 → 집계표. **순수 함수**. 외부 호출이 없다.

## 이 층이 지켜야 하는 두 가지

**① 분모를 섞지 않는다(§4).** 지표마다 분모가 다르다. 거리·임계통과율은
`양측 응답` 건, 완전 실패율만 `표본 전체`, 분류표는 `D0 = 표본 − E1 − E2` 다.
하나로 뭉치면 "우리가 답을 못 낸 건"이 거리 분포에서 조용히 사라지고 통과율이
올라간다. `DENOM_OF` 가 지표별 분모를 코드로 선언한다.

**② 개별 주소를 출력하지 않는다(§6 판정 B).** 원천 취급방침이 집계·분포·건수만
허용한다. 여기서는 "출력에 안 실렸다"보다 강한 조건을 건다 — 집계층은 금지 필드를
**읽지도 않는다**. `test_aggregate.py::TrapDict` 가 읽는 순간 터뜨려 이를 강제한다.
식별자(`sid`·`pnu`)도 금지다. 표본 파일과 대조하면 개별 주소가 복원되기 때문이다.

## 층 가중의 대가를 숫자로 적는다

층 배분이 균등(층당 200)인데 모집단은 세종 2,264 ~ 경기 695,705 로 **317 배** 벌어져
있다. 사후 가중을 하면 분산이 `deff = H·ΣW_h² = 1.7473` 배로 커진다 — 12,000 건을
뽑아도 유효 표본은 3,434 건이다. 이 수치를 리포트에 싣지 않으면 "12,000 건이나
쟀다"는 인상만 남는다.

FPC 는 **적용하지 않는다**. 최소층에서도 보정계수가 0.955 라 CI 를 4.5% 좁힐 뿐이고,
30 개 층 중 하나에만 의미가 있는 보정을 전체에 걸면 해석이 더 어려워진다. 대신
`fpc()` 를 남겨 각주로 인용한다.
"""
import math
import os
import json
from collections import namedtuple

__all__ = [
    "Z", "FPC_APPLIED", "N_PER_STRATUM", "C_GROUP", "CLASSES", "FLAG_ORDER",
    "FORBIDDEN_RECORD_FIELDS", "DENOM_OF",
    "srs_halfwidth", "weights", "sum_w_squared", "design_effect", "effective_n",
    "weighted_halfwidth", "fpc", "fpc_simple", "rate",
    "Denominators", "denominators", "response_rate", "post_stratified_rate",
    "class8_denominators", "class8_row", "class2_row", "relax12_counts",
    "class_rate_strict_and_relaxed", "render_report", "load_verdicts",
]

Z = 1.96                    # 95% 양측
FPC_APPLIED = False         # §2.2 — 각주로만 남긴다
N_PER_STRATUM = 200         # §2.2 설계 배분
C_GROUP = (7, 8, 9, 10, 11)
CLASSES = tuple(range(1, 12))
FLAG_ORDER = ("F1", "F2", "F3", "F4", "F5", "F6", "F7", "F8")

# 집계층이 **읽어서도 안 되는** 필드. 개별 주소와 그 복원 열쇠(§6 판정 B).
FORBIDDEN_RECORD_FIELDS = frozenset((
    "query", "vw_text", "our_text", "text", "display", "address",
    "lon", "lat", "v_pt", "m_pt", "m5", "point",
    "sid", "pnu", "bm25", "level4LC", "level5",
))


# ─────────────────────────────────────────────────────────────────────
# 신뢰구간
# ─────────────────────────────────────────────────────────────────────
def srs_halfwidth(n, p=0.5):
    """단순임의추출 가정의 95% CI 반폭 `Z√(p(1−p)/n)`.

    `p` 를 안 주면 최악(0.5)을 쓴다 — 어떤 실제 비율도 이보다 넓을 수 없으므로
    "적어도 이만큼은 벌어진다"는 보수적 진술이 된다. 모집단 크기를 인자로 받지
    않는다: FPC 가 몰래 끼어들 여지를 없애기 위해서다.
    """
    if not n:
        return None
    return Z * math.sqrt(p * (1.0 - p) / float(n))


def weights(pop):
    """층 가중 `W_h = N_h / N`. 합은 1 이다."""
    total = float(sum(pop.values()))
    if total <= 0:
        return {}
    return {h: n / total for h, n in pop.items()}


def sum_w_squared(pop):
    """`Σ W_h²`. 층이 균등하면 `1/H`, 편중될수록 커진다."""
    return sum(w * w for w in weights(pop).values())


def design_effect(n_strata, sum_w_sq):
    """`deff = H · ΣW_h²`. 균등 배분 + 사후 가중일 때의 분산 확대 배수."""
    return n_strata * sum_w_sq


def effective_n(n, deff):
    """유효 표본크기 `n / deff`. 계획은 반올림을 썼다(3,433.85 → 3,434)."""
    if not deff:
        return None
    return int(round(n / float(deff)))


def weighted_halfwidth(sum_w_sq, n_h, p=0.5):
    """사후 가중 CI 반폭 `Z√(ΣW_h²·p(1−p)/n_h)` (층당 표본 `n_h` 균등)."""
    if not n_h:
        return None
    return Z * math.sqrt(sum_w_sq * p * (1.0 - p) / float(n_h))


def fpc(n_pop, n):
    """유한모집단 보정 `√((N−n)/(N−1))`. **적용하지 않는다** — 각주용."""
    if n_pop <= 1:
        return None
    return math.sqrt((n_pop - n) / float(n_pop - 1))


def fpc_simple(n_pop, n):
    """간이형 `√(1 − n/N)`. 큰 `N` 에서 `fpc()` 와 사실상 같다."""
    if n_pop <= 0:
        return None
    return math.sqrt(1.0 - n / float(n_pop))


def rate(num, den):
    """분모가 0 이면 `None`.

    0/0 을 0.0 으로 적으면 표에서 "0% 실패 = 완벽" 으로 읽힌다. 관측이 없었다는
    사실과 관측 결과가 0 이라는 사실은 다르다.
    """
    if not den:
        return None
    return num / float(den)


# ─────────────────────────────────────────────────────────────────────
# 분모 분리(§4)
# ─────────────────────────────────────────────────────────────────────
Denominators = namedtuple(
    "Denominators", "total e1 e2 d0 both_responded")


def _gate(r):
    return r.get("gate")


def denominators(records):
    """§4 의 네 분모를 한 번에 센다."""
    total = len(records)
    e1 = sum(1 for r in records if _gate(r) == "E1")
    e2 = sum(1 for r in records if _gate(r) == "E2")
    both = sum(1 for r in records
               if _gate(r) is None
               and r.get("vw_status") == "OK"
               and (r.get("our_addr_count") or 0) >= 1)
    return Denominators(total=total, e1=e1, e2=e2,
                        d0=total - e1 - e2, both_responded=both)


# 지표별 분모를 코드가 선언한다 — 표를 만드는 쪽이 고를 수 없게.
DENOM_OF = {
    "distance": lambda d: d.both_responded,
    "threshold_pass": lambda d: d.both_responded,
    "total_failure": lambda d: d.total,        # 게이트 건도 실패다
    "class_table": lambda d: d.d0,
}


def response_rate(records, strict=True):
    """§4.1 — 엄격(`kind='addr'`≥1) / 광의(`results`≥1).

    차이가 곧 F2(카테고리 오폴백)다. 광의만 적으면 "찾았다"가 부풀려진다.
    """
    live = [r for r in records if _gate(r) is None]
    if not live:
        return None
    field = "our_addr_count" if strict else "our_result_count"
    hit = sum(1 for r in live if (r.get(field) or 0) >= 1)
    return hit / float(len(live))


# ─────────────────────────────────────────────────────────────────────
# 사후 가중
# ─────────────────────────────────────────────────────────────────────
def post_stratified_rate(num, den, pop, with_dropped=False):
    """층별 비율을 모집단 가중으로 합성한다.

    관측이 0 인 층은 비율 자체가 정의되지 않으므로 **빼고**, 남은 층의 가중을 다시
    1 로 정규화한다. 0 을 0.0 으로 넣으면 그 층의 모집단 몫만큼 전체가 끌어내려진다.
    빠진 층은 반환해 리포트에 적는다 — 조용히 빠지면 커버리지 결손이 숨는다.
    """
    unknown = set(den) - set(pop)
    if unknown:
        raise KeyError("모집단표에 없는 층: %s" % sorted(unknown))
    dropped = sorted(h for h in den if not den[h])
    live = {h: pop[h] for h in den if den[h]}
    wsum = float(sum(live.values()))
    if wsum <= 0:
        return (None, dropped) if with_dropped else None
    val = sum(pop[h] * (num.get(h, 0) / float(den[h])) for h in live) / wsum
    return (val, dropped) if with_dropped else val


# ─────────────────────────────────────────────────────────────────────
# 분류 8 의 유효 분모(§8-11)
# ─────────────────────────────────────────────────────────────────────
Class8Row = namedtuple("Class8Row", "count rate_effective rate_overall")
Class2Row = namedtuple("Class2Row", "a_group all_group")
Relax12Counts = namedtuple("Relax12Counts", "oracle_relax f6_flag")


def _in_c_group(r):
    return r.get("cls") in C_GROUP


def class8_denominators(records):
    """`(유효 분모, 전체 분모)`.

    지번층에서 우리 답이 `source='parcel'` 이면 그 좌표는 필지 대표점
    (`ST_PointOnSurface`)이므로 심판 `R(M)=ST_Contains` 가 **정의상 참**이다.
    분류 8 은 `R(V) ∧ ¬R(M)` 이라 그런 건은 원리상 들어갈 수 없다. 분모에
    남겨두면 분류 8 비율이 구조적으로 과소평가된다.

    도로명층은 심판이 출입구·건물중심 거리라 `parcel` 이어도 `R(M)` 이 강제되지
    않는다 — 빼지 않는다.
    """
    c = [r for r in records if _in_c_group(r)]
    overall = len(c)
    eff = sum(1 for r in c
              if not (r.get("atype") == "jibun" and r.get("source") == "parcel"))
    return eff, overall


def class8_row(records):
    eff, overall = class8_denominators(records)
    n8 = sum(1 for r in records if r.get("cls") == 8)
    return Class8Row(count=n8,
                     rate_effective=rate(n8, eff),
                     rate_overall=rate(n8, overall))


def class2_row(records):
    """조건 3 — 분류 2 를 A 군 밖에서도 계상한다.

    A 군의 `source='parcel' ∧ O=P` 는 이미 분류 2 로 잡히지만, 같은 조합이 B/C 군에도
    나타난다(우리가 필지 폴백으로 뭔가 답은 냈다). 그쪽을 안 세면 "필지 폴백이
    주소 부재를 가리는" 규모가 A 군 크기만큼으로 축소돼 보인다. F8 이 그 나머지다.
    """
    a = sum(1 for r in records if r.get("cls") == 2)
    f8 = sum(1 for r in records if "F8" in (r.get("flags") or ()))
    return Class2Row(a_group=a, all_group=a + f8)


def relax12_counts(records):
    """조건 1 — 오라클 완화 발동은 **F6 과 별개**로 센다.

    F6 은 `b_code` 비교(§조건 2)에서 완화가 판정을 뒤집은 건이고, 오라클 완화는
    PNU 조회 축에서 `12 → 46/29` 재조회가 발동한 건이다. 같은 원인(세종 분리)에서
    나오지만 축이 달라 건수가 일치하지 않는다. 한 칸에 합치면 어느 축이 얼마나
    영향을 받았는지 말할 수 없다.
    """
    return Relax12Counts(
        oracle_relax=sum(1 for r in records if r.get("relax12_used")),
        f6_flag=sum(1 for r in records if "F6" in (r.get("flags") or ())))


def class_rate_strict_and_relaxed(records, cls):
    """§8-10 — 완화 전/후 두 수치를 병기한다.

    엄격 수치는 완화로 살아난 건을 그 분류에서 뺀 값이다. 완화가 성적을 얼마나
    만들었는지는 이 차이로만 말할 수 있다.
    """
    n = len(records)
    if not n:
        return None, None
    relaxed = sum(1 for r in records if r.get("cls") == cls)
    strict = sum(1 for r in records
                 if r.get("cls") == cls and not r.get("relax12_used"))
    return strict / float(n), relaxed / float(n)


# ─────────────────────────────────────────────────────────────────────
# 리포트 렌더
# ─────────────────────────────────────────────────────────────────────
def _int(n):
    return "{:,}".format(n)


def _pct(x, places=2):
    return "—" if x is None else ("%.*f%%" % (places, 100.0 * x))


def _num(x, places=6):
    return "—" if x is None else ("%.*f" % (places, x))


def render_report(records, pop, title="T046 집계"):
    """집계 마크다운을 만든다. 개별 레코드의 식별 정보는 **읽지 않는다**.

    0 건인 분류도 표에서 빼지 않는다 — "도달 불가인가, 관측되지 않았을 뿐인가"는
    분류 체계의 완전성 논의라 빈 행 자체가 결과다.
    """
    d = denominators(records)
    H = len(pop)
    sw2 = sum_w_squared(pop)
    deff = design_effect(H, sw2)
    n_design = H * N_PER_STRATUM
    w = weights(pop)
    spread = (max(w.values()) / min(w.values())) if w else None
    small = min(pop, key=pop.get) if pop else None

    L = []
    L.append("# %s" % title)
    L.append("")
    L.append("## 분모(§4)")
    L.append("| 구분 | 건수 |")
    L.append("| --- | ---: |")
    L.append("| 표본 전체 | %s |" % _int(d.total))
    L.append("| 게이트 E1 (VWorld status≠OK) | %s |" % _int(d.e1))
    L.append("| 게이트 E2 (우리 8092 5xx·타임아웃) | %s |" % _int(d.e2))
    L.append("| 공통 분모 D0 = 표본 − E1 − E2 | %s |" % _int(d.d0))
    L.append("| 양측 응답 (거리·임계통과의 분모) | %s |" % _int(d.both_responded))
    L.append("")
    L.append("지표별 분모: 거리·임계통과 = 양측 응답, 완전 실패 = 표본 전체, "
             "분류표 = D0. 통과율에는 `(통과/양측응답, 양측응답/표본전체)` 를 병기한다.")
    L.append("")
    L.append("## 응답률(§4.1)")
    L.append("- 엄격 (`kind='addr'` ≥ 1): %s" % _pct(response_rate(records, True)))
    L.append("- 광의 (`results` ≥ 1): %s" % _pct(response_rate(records, False)))
    L.append("- 차이 = F2(카테고리 오폴백)")
    L.append("")
    L.append("## 통계 근거(§2.2)")
    L.append("- 층 수 H = %d, 층당 배분 n_h = %d, 설계 표본 n = %s"
             % (H, N_PER_STRATUM, _int(n_design)))
    L.append("- 층 가중 편차 (최대/최소) = %s 배" % _num(spread, 1))
    L.append("- ΣW_h² = %s" % _num(sw2, 8))
    L.append("- 설계효과 deff = H·ΣW_h² = %s" % _num(deff, 4))
    L.append("- 유효 표본크기 n/deff = %s 건" % _int(effective_n(n_design, deff)))
    L.append("- SRS 95%% CI 반폭 (n=%s) = ±%s" % (_int(n_design),
                                                  _num(srs_halfwidth(n_design), 8)))
    L.append("- 사후 가중 95%% CI 반폭 (n_h=%d) = ±%s"
             % (N_PER_STRATUM, _num(weighted_halfwidth(sw2, N_PER_STRATUM), 8)))
    if small:
        L.append("- FPC 미적용(%s). 최소층 N=%s 에서 f=%s, fpc=%s "
                 "— 보정해도 CI 가 %s 좁아질 뿐이라 각주로만 남긴다."
                 % (FPC_APPLIED, _int(pop[small]),
                    _num(N_PER_STRATUM / float(pop[small]), 7),
                    _num(fpc(pop[small], N_PER_STRATUM), 6),
                    _pct(1.0 - (fpc(pop[small], N_PER_STRATUM) or 1.0), 1)))
    L.append("")
    L.append("## 배타 분류 분포 (분모 D0 = %s)" % _int(d.d0))
    L.append("| 분류 | 건수 | 비율 |")
    L.append("| --- | ---: | ---: |")
    counts = {}
    for r in records:
        c = r.get("cls")
        if c is not None:
            counts[c] = counts.get(c, 0) + 1
    for n in CLASSES:
        L.append("| 분류 %d | %s | %s |"
                 % (n, _int(counts.get(n, 0)), _pct(rate(counts.get(n, 0), d.d0))))
    L.append("")
    c8 = class8_row(records)
    eff8, all8 = class8_denominators(records)
    L.append("분류 8 은 분모가 둘이다 — 유효 %s(지번 `source='parcel'` 제외) 기준 %s, "
             "전체 %s 기준 %s. 필지 대표점은 정의상 그 필지 안이라 R(M) 이 강제된다."
             % (_int(eff8), _pct(c8.rate_effective), _int(all8), _pct(c8.rate_overall)))
    c2 = class2_row(records)
    L.append("분류 2 계상: A 군 %s 건 / 전체 %s 건(F8 포함)."
             % (_int(c2.a_group), _int(c2.all_group)))
    L.append("")
    L.append("## 직교 플래그 (분모 D0)")
    L.append("| 플래그 | 건수 | 비율 |")
    L.append("| --- | ---: | ---: |")
    for f in FLAG_ORDER:
        n = sum(1 for r in records if f in (r.get("flags") or ()))
        L.append("| %s | %s | %s |" % (f, _int(n), _pct(rate(n, d.d0))))
    rc = relax12_counts(records)
    L.append("")
    L.append("시도코드 12 완화: 오라클 축 %s 건, F6(b_code 축) %s 건. "
             "축이 달라 건수는 일치하지 않는다."
             % (_int(rc.oracle_relax), _int(rc.f6_flag)))
    return "\n".join(L) + "\n"


# ─────────────────────────────────────────────────────────────────────
# measure.py 산출물 어댑터
# ─────────────────────────────────────────────────────────────────────
def load_verdicts(path, relaxed=False):
    """`verdict_*.jsonl` → 이 모듈의 레코드 형태.

    `measure.py` 는 측정 축의 이름(`layer`·`v_status`)을 쓰고 이 모듈은 통계 축의
    이름(`atype`·`vw_status`)을 쓴다. 층 구분자도 `|` 와 `:` 로 다르다. 어느
    한쪽에 맞춰 이름을 통일하는 대신 경계에서 옮긴다 — 두 층의 테스트가 서로의
    이름 변경에 끌려다니지 않게.

    `relaxed=True` 면 완화 적용 분류(`cls_relaxed`)를 `cls` 자리에 놓는다.
    """
    out = []
    with open(path, "r") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            v = json.loads(line)
            sido, urban, atype = (v.get("stratum") or "::").replace("|", ":").split(":")
            out.append({
                "stratum": "%s:%s:%s" % (sido, urban, atype),
                "sido": sido, "urban": urban, "atype": atype,
                "gate": v.get("gate"),
                "cls": v.get("cls_relaxed") if relaxed else v.get("cls"),
                "flags": tuple(v.get("flags_relaxed" if relaxed else "flags") or ()),
                "vw_status": v.get("v_status"),
                "our_addr_count": v.get("our_addr_count") or 0,
                "our_result_count": ((v.get("our_addr_count") or 0)
                                     + (v.get("nonaddr_count") or 0)),
                "d_top1": v.get("d_top1"), "d_min5": v.get("d_min5"),
                "oracle": v.get("oracle"), "o_apx": v.get("o_apx"),
                "source": v.get("source"),
                "r_v": v.get("r_v"), "r_m": v.get("r_m"),
                "relax12_used": bool(v.get("relax12_used")),
            })
    return out


def load_population(path):
    """`strata.json` → `{층: N_h}`. 층 이름을 `:` 구분으로 통일한다."""
    with open(path, "r") as fh:
        doc = json.load(fh)
    strata = doc.get("strata", doc)
    pop = {}
    for name, meta in strata.items():
        n = meta.get("N_h") if isinstance(meta, dict) else meta
        if n:
            pop[name.replace("|", ":")] = n
    return pop


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description="T046 §7 집계")
    ap.add_argument("--verdicts", required=True)
    ap.add_argument("--strata",
                    default=os.path.expanduser("~/geocode-build/t046/sample/strata.json"))
    ap.add_argument("--layer", choices=("jibun", "road"), default="jibun")
    ap.add_argument("--relaxed", action="store_true")
    ap.add_argument("--out")
    args = ap.parse_args(argv)

    records = [r for r in load_verdicts(args.verdicts, args.relaxed)
               if r["atype"] == args.layer]
    pop = {h: n for h, n in load_population(args.strata).items()
           if h.endswith(":" + args.layer)}
    text = render_report(records, pop, title="T046 집계 — %s 층" % args.layer)
    if args.out:
        with open(args.out, "w") as fh:
            fh.write(text)
    else:
        import sys
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
