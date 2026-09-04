#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""T046 §4.2 역방향 주소축 — 추출·비교·집계. **순수 함수만** 둔다.

왜 별도 모듈인가. B 단계는 역방향 축을 `measure.py` 안에 직접 짜 넣었고,
그래서 HTTP 없이는 한 줄도 시험할 수 없었다(검수 F4: 추가 테스트 0 개).
비교식을 여기로 빼면 응답 형태만 주고 전부 시험할 수 있다.

### 응답 형태(추측 아님 — 2026-08-21 실측)

VWorld 역방향 `type=both` 는 `response.result[]` 에 최대 2 항목을 담는다.
**순서를 보장하지 않는다.** 첫 항목 고정은 결함이다(`split_reverse_entries`).

| | `parcel` 항목 | `road` 항목 |
| --- | --- | --- |
| `level4L` | 법정동명(읍면동) | **도로명** |
| `level4LC` | **법정동코드 10** | **도로명코드 7** — 법정동코드가 아니다 |
| `level5` | 지번(지목 접미 가능) | **건물번호** |
| `text` | 리 명칭이 실리는 **유일한** 자리 | — |

우리 `/reverse` 는 `address.structure` 다. `results` 키는 **없다**(순방향
`/geocode` 의 형태를 역방향에 적용한 것이 스키마 v1 결함이었다).

### 정규화 3 단 — "정규화 전후 두 수치"를 뭉개지 않기 위해

| 단 | 규칙 | 접미어 `_strict` / `_bare` / 없음 |
| --- | --- | --- |
| strict | 원문 그대로(`str.strip()` 만) | `_strict`, `_raw` |
| bare | 공백만 제거 + `\\d+(-\\d+)?` 만 인정 | `_bare` |
| norm | §4.2 규칙 1~12 전체(`normalize.py`) | 접미어 없음 |

`bare` 는 지목 접미(`"210-1도"`)·산·번지를 **읽지 못한다**. 그래서 그 단의
판정불가 건수가 곧 "정규화가 없었으면 잃었을 건수"다.

### 부재 규약(규칙 12)

한쪽이라도 없으면 `eq = None`(판정불가)이다. 부재를 일치로 세지 않는다.
분모는 집계층이 **두 가지 모두** 낸다 — `rate_judgeable`(양쪽 존재)과
`rate_all`(전체, 부재=불일치). 어느 하나만 내면 유리한 쪽을 고른 셈이 된다.
"""
import re

from normalize import (
    norm_road,
    parse_bld_no,
    parse_jibun_detail,
    sido_code_match,
    strip_spaces,
)

BCODE_LEN = 10
_RE_BARE = re.compile(r"^(\d+)(?:-(\d+))?$")

#: `response.result[]` 에서 우리가 인정하는 항목 종류.
VW_ENTRY_TYPES = ("parcel", "road")

#: 비교 축 전체. 이름 끝의 `_strict`·`_bare`·`_raw` 는 정규화 단을 뜻한다.
AXES = (
    # 법정동코드 접두 — 시도 2 / 시군구 5 / 읍면동 8 / 리 포함 10
    "sido_strict", "sido",
    "sgg_strict", "sgg",
    "emd_strict", "emd",
    "ri_code_strict", "ri_code",
    # 지번
    "jibun_raw", "jibun_main_bare", "jibun_sub_bare",
    "jibun_main", "jibun_sub", "san",
    # 도로명
    "road_raw", "road_name",
    # 건물번호
    "bld_raw", "bld_main_bare", "bld_sub_bare", "bld_main", "bld_sub",
)

#: 법정동코드 접두 축 → (축이름, 자릿수).
_PREFIX_AXES = (("sido", 2), ("sgg", 5), ("emd", 8), ("ri_code", 10))


# ── 잔재주 ────────────────────────────────────────────────────────────
def _txt(v):
    """`None`·빈 문자열·공백만인 값을 `None` 으로 접는다. 부재는 성공이 아니다."""
    s = "" if v is None else str(v).strip()
    return s or None


def _bare_pair(value):
    """공백만 제거하고 `\\d+(-\\d+)?` 로만 읽는다 → `(본번, 부번)` 또는 `None`.

    지목 접미·산·번지는 **일부러** 읽지 못한다. 이 단의 실패 건수가 정규화의
    기여분이다.
    """
    s = strip_spaces(value)
    if not s:
        return None
    m = _RE_BARE.match(s)
    if not m:
        return None
    return int(m.group(1)), int(m.group(2) or 0)


def _ri_from_text(text, emd_name):
    """VWorld `text` 에서 리 명칭. `structure` 에는 리 필드가 없다(실측).

    규칙: 마지막 토큰은 지번이고, 그 앞 토큰이 `리` 로 끝나면서 읍면동명과
    다르면 리다. 도시 지점은 앞 토큰이 `…동`·`…로` 라 자연히 `None` 이 된다.
    """
    toks = (text or "").split()
    if len(toks) < 2:
        return None
    cand = toks[-2]
    if not cand.endswith("리"):
        return None
    if emd_name and cand == str(emd_name).strip():
        return None
    return cand


# ── VWorld 응답 ───────────────────────────────────────────────────────
def vw_status(body):
    """`response.status`. 없으면 `None` — `"OK"` 로 낙관하지 않는다."""
    return _txt(((body or {}).get("response") or {}).get("status"))


def split_reverse_entries(body):
    """`type=both` 응답 → `{"parcel": 항목|None, "road": 항목|None}`.

    **`result[0]` 을 쓰지 않는다.** 두 항목의 순서는 계약이 아니고, 지번만
    있는 지점은 항목이 하나뿐이다. 첫 항목을 지번으로 가정하면 도로명만 온
    지점에서 도로명을 지번으로 읽는다.
    """
    out = {"parcel": None, "road": None}
    res = ((body or {}).get("response") or {}).get("result")
    if not isinstance(res, list):
        return out
    for entry in res:
        if not isinstance(entry, dict):
            continue
        kind = (entry.get("type") or "").strip().lower()
        if kind in VW_ENTRY_TYPES and out[kind] is None:
            out[kind] = entry
    return out


def vw_parcel_axes(entry):
    """지번 항목 → 비교용 축. 값이 없으면 그 축만 `None` 이다."""
    st = ((entry or {}).get("structure") or {})
    bcode = _txt(st.get("level4LC"))
    if bcode is not None and (len(bcode) != BCODE_LEN or not bcode.isdigit()):
        bcode = None                       # 자릿수가 다르면 법정동코드가 아니다
    raw = _txt(st.get("level5"))
    tri, _flags = parse_jibun_detail(raw) if raw else (None, None)
    bare = _bare_pair(raw)
    return {
        "bcode": bcode,
        "emd_name": _txt(st.get("level4L")),
        "jibun_raw": raw,
        "jibun_bare": bare,
        "jibun": tri,
        "ri_name": _ri_from_text((entry or {}).get("text"), _txt(st.get("level4L"))),
    }


def vw_road_axes(entry):
    """도로명 항목 → 비교용 축.

    `level4LC` 는 **7 자리 도로명코드**다. 법정동코드 축에 절대 넣지 않는다 —
    그래서 이 dict 에는 `bcode` 키 자체가 없다.
    """
    st = ((entry or {}).get("structure") or {})
    raw = _txt(st.get("level5"))
    return {
        "road_name": _txt(st.get("level4L")),
        "road_cd": _txt(st.get("level4LC")),
        "bld_raw": raw,
        "bld_bare": _bare_pair(raw),
        "bld_no": parse_bld_no(raw) if raw else None,
    }


# ── 우리 응답 ─────────────────────────────────────────────────────────
def ours_axes(body):
    """우리 `/reverse` → 비교용 축. **`address.structure` 만** 읽는다.

    `results` 키가 있어도 무시한다. 스키마 v1 은 `body["results"][0]` 를 읽어
    전건 `None` 이었다 — 순방향 응답 형태를 역방향에 적용한 오독이다.
    """
    st = ((body or {}).get("address") or {}).get("structure") or {}

    ji_main = st.get("ji_main")
    ji_sub = st.get("ji_sub")
    san = bool(st.get("san"))
    if ji_main is None:
        jibun_raw = jibun = jibun_bare = None
    else:
        ji_sub = int(ji_sub or 0)
        ji_main = int(ji_main)
        jibun_raw = "%d" % ji_main if not ji_sub else "%d-%d" % (ji_main, ji_sub)
        if san:
            jibun_raw = "산 " + jibun_raw
        jibun = (san, ji_main, ji_sub)
        # bare 는 산을 표현하지 못한다 — 표현 못 하는 것을 일치로 세지 않는다.
        jibun_bare = None if san else (ji_main, ji_sub)

    main_no = st.get("main_no")
    sub_no = st.get("sub_no")
    if main_no is None:
        bld_raw = bld_no = bld_bare = None
    else:
        main_no, sub_no = int(main_no), int(sub_no or 0)
        bld_raw = "%d" % main_no if not sub_no else "%d-%d" % (main_no, sub_no)
        bld_no = bld_bare = (main_no, sub_no)

    bcode = _txt(st.get("b_code"))
    if bcode is not None and (len(bcode) != BCODE_LEN or not bcode.isdigit()):
        bcode = None
    return {
        "bcode": bcode,
        "ji_main": ji_main, "ji_sub": ji_sub, "san": san if ji_main is not None else None,
        "jibun_raw": jibun_raw, "jibun_bare": jibun_bare, "jibun": jibun,
        "road_name": _txt(st.get("road_name")),
        "bld_raw": bld_raw, "bld_bare": bld_bare, "bld_no": bld_no,
        "ri_name": _txt(st.get("ri")),
        "address_source": _txt(body.get("address_source") if isinstance(body, dict)
                               else None),
    }


# ── 비교 ─────────────────────────────────────────────────────────────
def _cell(vw, ours, eq_fn):
    """한 축의 판정. 한쪽이라도 부재면 `eq = None`(판정불가)."""
    has_v, has_o = vw is not None, ours is not None
    return {"vw": has_v, "ours": has_o,
            "eq": bool(eq_fn(vw, ours)) if (has_v and has_o) else None}


def _prefix_strict(n):
    return lambda a, b: a[:n] == b[:n]


def _prefix_norm(n):
    """접두 비교 + 시도코드 완화(§4.3 조건 1). 완화는 **앞 2 자리에만** 건다."""
    def _eq(a, b):
        if a[:n] == b[:n]:
            return True
        return sido_code_match(a[:2], b[:2], relax12=True) and a[2:n] == b[2:n]
    return _eq


def compare_axes(vwp, vwr, ours):
    """`{축: {"vw": bool, "ours": bool, "eq": bool|None}}` — 축 전체를 항상 낸다.

    빠진 축이 있으면 집계 분모가 축마다 달라져 비교가 무너진다. 그래서 부재도
    **자리를 남긴다**.
    """
    out = {}
    for name, n in _PREFIX_AXES:
        out[name + "_strict"] = _cell(vwp["bcode"], ours["bcode"], _prefix_strict(n))
        out[name] = _cell(vwp["bcode"], ours["bcode"], _prefix_norm(n))

    out["jibun_raw"] = _cell(vwp["jibun_raw"], ours["jibun_raw"],
                             lambda a, b: a == b)
    out["jibun_main_bare"] = _cell(vwp["jibun_bare"], ours["jibun_bare"],
                                   lambda a, b: a[0] == b[0])
    out["jibun_sub_bare"] = _cell(vwp["jibun_bare"], ours["jibun_bare"],
                                  lambda a, b: a[1] == b[1])
    out["jibun_main"] = _cell(vwp["jibun"], ours["jibun"], lambda a, b: a[1] == b[1])
    out["jibun_sub"] = _cell(vwp["jibun"], ours["jibun"], lambda a, b: a[2] == b[2])
    out["san"] = _cell(vwp["jibun"], ours["jibun"], lambda a, b: a[0] == b[0])

    out["road_raw"] = _cell(vwr["road_name"], ours["road_name"], lambda a, b: a == b)
    out["road_name"] = _cell(vwr["road_name"], ours["road_name"],
                             lambda a, b: norm_road(a) == norm_road(b))

    out["bld_raw"] = _cell(vwr["bld_raw"], ours["bld_raw"], lambda a, b: a == b)
    out["bld_main_bare"] = _cell(vwr["bld_bare"], ours["bld_bare"],
                                 lambda a, b: a[0] == b[0])
    out["bld_sub_bare"] = _cell(vwr["bld_bare"], ours["bld_bare"],
                                lambda a, b: a[1] == b[1])
    out["bld_main"] = _cell(vwr["bld_no"], ours["bld_no"], lambda a, b: a[0] == b[0])
    out["bld_sub"] = _cell(vwr["bld_no"], ours["bld_no"], lambda a, b: a[1] == b[1])
    return out


def ri_cells(vwp, ours):
    """리 4 지표의 **행 재료**. 코드상 유무와 문자열 유무를 따로 남긴다.

    법정동코드 끝 2 자리가 `00` 이면 리 없음이다. 코드와 문자열이 어긋나는
    비율 자체가 §4.2 가 요구한 지표다(우리 쪽 결함 지표이기도 하다).

    **문자열을 그대로 남기지 않는다.** 판정 레코드는 §3.3 대로 불리언만
    싣는다 — 리 명칭은 주소 원문의 일부다. 일치 여부만 여기서 확정한다.
    """
    def _code_ri(bcode):
        return None if not bcode else (bcode[8:10] != "00")

    no, nv = ours["ri_name"], vwp["ri_name"]
    return {
        "ri_code_ours": _code_ri(ours["bcode"]),
        "ri_code_vw": _code_ri(vwp["bcode"]),
        "ri_fill_ours": bool(no),
        "ri_fill_vw": bool(nv),
        # 둘 다 비어 있는 것을 '일치'로 세면 §4.2 요구를 정면으로 어긴다.
        "ri_name_eq": (strip_spaces(no) == strip_spaces(nv)) if (no and nv) else None,
    }


# ── 집계 ─────────────────────────────────────────────────────────────
def _rate(num, den):
    return (num / float(den)) if den else None


def ri_metrics(rows):
    """계획 §4.2 의 리 4 지표. **채움률이 아니라 정확도**를 함께 낸다.

    입력은 `ri_cells` 가 낸 불리언 칸이다(문자열은 판정 레코드에 남지 않는다).
    """
    n = len(rows)
    fill_ours = fill_vw = 0
    code_j = code_eq = 0
    name_j = name_eq = 0
    conf_j = conf_ours = conf_vw = 0
    for r in rows:
        co, cv = r.get("ri_code_ours"), r.get("ri_code_vw")
        fo, fv = bool(r.get("ri_fill_ours")), bool(r.get("ri_fill_vw"))
        eq = r.get("ri_name_eq")
        fill_ours += fo
        fill_vw += fv
        if co is not None and cv is not None:
            code_j += 1
            code_eq += (co == cv)
            conf_j += 1
            conf_ours += (co != fo)
            conf_vw += (cv != fv)
        if eq is not None:
            name_j += 1
            name_eq += bool(eq)
    return {
        "n": n,
        "fill_ours": fill_ours, "fill_vw": fill_vw,
        "fill_rate_ours": _rate(fill_ours, n), "fill_rate_vw": _rate(fill_vw, n),
        "code_judgeable": code_j, "code_eq": code_eq,
        "code_rate": _rate(code_eq, code_j),
        "name_judgeable": name_j, "name_eq": name_eq,
        "name_rate": _rate(name_eq, name_j),
        "conflict_judgeable": conf_j,
        "conflict_ours": conf_ours, "conflict_vw": conf_vw,
        "conflict_rate_ours": _rate(conf_ours, conf_j),
        "conflict_rate_vw": _rate(conf_vw, conf_j),
    }


def aggregate_axis(rows, axis):
    """한 축의 집계. **분모 두 가지를 모두** 낸다(F5).

    `rate_judgeable` 양쪽이 존재한 건만. 축 자체의 정확도.
    `rate_all`       전체 분모. 규칙 12 대로 부재를 불일치로 센다.

    하나만 내면 유리한 쪽을 고른 것이 된다. 둘의 차이가 곧 결측의 크기다.
    """
    n = len(rows)
    judgeable = eq = 0
    for r in rows:
        cell = (r.get("axes") or {}).get(axis) or {}
        v = cell.get("eq")
        if v is None:
            continue
        judgeable += 1
        eq += bool(v)
    return {"axis": axis, "n": n, "judgeable": judgeable, "eq": eq,
            "rate_judgeable": _rate(eq, judgeable),
            "rate_all": _rate(eq, n)}


def source_split(rows, key="address_source"):
    """`address_source` 별로 행을 쪼갠다(계획 §8-15).

    합성(`knn`)과 실주소(`pip_key`)를 섞으면 무엇의 정확도인지 알 수 없다.
    `None`(부재)도 **버리지 않고** 자기 칸에 모은다.
    """
    out = {}
    for r in rows:
        out.setdefault(r.get(key), []).append(r)
    return out
