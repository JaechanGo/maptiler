#!/usr/bin/env python3
"""T046 §2.4 — 층화 표본 추출. **외부 호출 없음**(VWorld·PostGIS 를 부르지 않는다).

예외는 표본 B 의 교란 검증뿐이고, 그것도 `probe` 콜러블을 **주입받아** 쓴다.
그래야 추출 로직 전체가 DB 없이 단위 검정된다.

## 1 패스 reservoir — 그리고 그 대가

19M 행을 전량 적재할 수 없으므로 알고리즘 R 로 상수 메모리 추출한다. 대가는
**행 순서 의존**이다(M11). 같은 시드라도 원천이 한 바이트만 달라지면 다른 표본이
나온다. 그래서 `manifest_entry()` 로 32 파일의 경로·바이트·SHA-256 을 기록하고
읽기 순서를 파일명 사전순으로 **고정**한다. 이 셋이 없으면 시드는 재현성을
보장하지 못한다.

## 제외는 두 가지뿐이다(§2.4)

`match_jibun` 의 대표지번 아님(지번일련번호≠0), `match_build` 의 지하(≠0).
**시군구 결측(세종)은 제외하지 않는다** — 제외하면 sejong 층이 통째로 왜곡된다.
질의 조립에서 시군구를 생략할 뿐이다.

출입구 좌표 부재는 **제외 사유가 아니다.** 모집단에서 빼면 `N_h` 가 실제 모집단이
아니게 되고 사후가중의 분모가 틀어진다. 좌표가 없는 건은 심판 자료 부재로
분류 11 에 정직하게 계상된다(`classify.py` 조건 4).

## 미지 읍면동 접미 — 계획에 없는 결정

`urban_rural()` 은 미지 접미에 `ValueError` 를 던진다(테스트 계약). 그런데 전수
스캔 중 그 예외로 죽으면 19M 행이 중단되고, 그렇다고 한쪽 층에 몰아넣으면 층이
조용히 왜곡된다. 그래서 `scan_file()` 은 이를 **`unknown_locality` 제외 사유로
계상**해 항등식 `kept + Σ제외 == rows` 를 유지하고 건수를 리포트에 드러낸다.
"몇 건이었나"에 답할 수 있어야 왜곡 여부를 판단할 수 있다.
"""
import hashlib
import json
import os
import random
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from oracle import resolve_pnu  # noqa: E402 — PNU 조립을 두 곳에서 따로 하면 어긋난다

__all__ = [
    "MASTER_SEED", "stratum_seed", "stratum_seed_b", "SIDO_KEYS",
    "sido_key_from_filename", "atype_from_filename", "Reservoir",
    "urban_rural", "jibun_exclusion", "build_exclusion", "EXCLUSION_REASONS",
    "jibun_record", "build_record", "record_key", "pnu_of",
    "scan_file", "ScanStats", "source_files", "manifest_entry", "sha256_file",
    "ENCODING", "DELIMITER", "SOURCE_DIR", "OUT_DIR",
    "TOTAL_ROWS_JIBUN", "TOTAL_ROWS_BUILD",
    "SAMPLE_A_PER_STRATUM", "SAMPLE_B_PER_STRATUM", "PERTURB_LIMIT",
    "exclude_keys", "perturb_candidates", "perturb",
]

MASTER_SEED = 20460821

ENCODING = "cp949"
DELIMITER = "|"

SOURCE_DIR = os.path.expanduser("~/geocode-build/staged/navi")
OUT_DIR = os.path.expanduser("~/geocode-build/t046/sample")

# §1.4 실측. 스캔 결과와 대조해 원천이 바뀌었는지 확인하는 고정점이다.
TOTAL_ROWS_JIBUN = 8192209
TOTAL_ROWS_BUILD = 10722641

SAMPLE_A_PER_STRATUM = 200
SAMPLE_B_PER_STRATUM = 40
PERTURB_LIMIT = 10

# 원천 파일명의 시도 키 16 개. `gwangju` 는 없다 — 원천 202607 이 광주를 전남과
# 합쳐 `jeonnamgwangju` 하나로 싣기 때문이다(정규화 규칙 5 의 시도코드 12).
SIDO_KEYS = (
    "busan", "chungbuk", "chungnam", "daegu", "daejeon", "gangwon",
    "gyeongbuk", "gyeongnam", "gyunggi", "incheon", "jeju", "jeonbuk",
    "jeonnamgwangju", "sejong", "seoul", "ulsan",
)
_SIDO_SET = frozenset(SIDO_KEYS)

_PREFIX_ATYPE = (("match_jibun_", "jibun"), ("match_build_", "road"))

# ── 필드 인덱스(실측 확정) ───────────────────────────────────────────
J_BCODE, J_SIDO, J_SIGUNGU, J_EMD, J_RI = 0, 1, 2, 3, 4
J_SAN, J_MAIN, J_SUB = 5, 6, 7
J_SEQ = 12          # 지번일련번호. 0 이 대표지번이다.
J_BM25 = 18
J_WIDTH = 20

B_BCODE, B_SIDO, B_SIGUNGU, B_EMD = 0, 1, 2, 3
B_ROAD = 5
B_BASEMENT = 6      # 지하여부
B_MAIN, B_SUB = 7, 8
B_BM25 = 10
B_CX, B_CY = 23, 24     # 건물중심 5179
B_EX, B_EY = 25, 26     # 출입구 5179
B_WIDTH = 33

EXCLUSION_REASONS = (
    "not_representative",   # match_jibun 지번일련번호 ≠ 0
    "basement",             # match_build 지하여부 ≠ 0
    "unknown_locality",     # 읍면동 접미가 다섯 종 밖 — 위 독스트링 참조
    "short_row",            # 필드 수 부족. 조용히 넘기면 IndexError 로 죽는다
)


# ── 시드 ──────────────────────────────────────────────────────────────
def _seed(text):
    return int(hashlib.sha256(text.encode("utf-8")).hexdigest()[:16], 16)


def stratum_seed(sido, urban, atype):
    """층별 시드. 층마다 독립이라 한 층을 다시 뽑아도 다른 층이 흔들리지 않는다."""
    return _seed("%d:%s:%s:%s" % (MASTER_SEED, sido, urban, atype))


def stratum_seed_b(sido, urban, atype):
    """표본 B 용. A 와 같은 시드를 쓰면 두 표본이 같은 행을 집어 여집합이 비게 된다."""
    return _seed("%d:%s:%s:%s:b" % (MASTER_SEED, sido, urban, atype))


# ── 파일명 → 층 축 ────────────────────────────────────────────────────
def _split_filename(name):
    for prefix, atype in _PREFIX_ATYPE:
        if name.startswith(prefix) and name.endswith(".txt"):
            key = name[len(prefix):-len(".txt")]
            if key in _SIDO_SET:
                return key, atype
            raise ValueError("알 수 없는 시도 키: %r (%s)" % (key, name))
    raise ValueError("원천 파일명 규약에 맞지 않는다: %r" % (name,))


def sido_key_from_filename(name):
    return _split_filename(os.path.basename(name))[0]


def atype_from_filename(name):
    return _split_filename(os.path.basename(name))[1]


# ── reservoir(알고리즘 R) ─────────────────────────────────────────────
class Reservoir(object):
    """k 개 상수 메모리 균등 추출. `seen` 은 제안받은 전량 — 여기서 `N_h` 가 나온다.

    k 미만이면 전량을 **입력 순서 그대로** 보존한다. 이 성질이 있어야 작은 층에서
    표본이 모집단과 같아지고, 층이 비면 빈 리스트가 나온다(예외가 아니다).
    """

    __slots__ = ("k", "seen", "_items", "_rng")

    def __init__(self, k, seed):
        self.k = k
        self.seen = 0
        self._items = []
        self._rng = random.Random(seed)

    def offer(self, item):
        self.seen += 1
        if len(self._items) < self.k:
            self._items.append(item)
            return
        j = self._rng.randrange(self.seen)
        if j < self.k:
            self._items[j] = item

    def items(self):
        return list(self._items)


# ── 층 판정 ───────────────────────────────────────────────────────────
_URBAN_SUFFIX = ("동", "가", "로")
_RURAL_SUFFIX = ("읍", "면")


def urban_rural(emd):
    """읍면동명 접미로 지역성격을 가른다.

    행정동이 아니라 **법정동** 명칭이므로 접미가 다섯 종으로 닫힌다(실측). 미지
    접미에 기본값을 주지 않고 죽이는 이유는, 조용히 한쪽에 몰면 층이 왜곡되고
    그 왜곡이 사후가중을 통해 전체 수치에 스며들기 때문이다.
    """
    s = (emd or "").strip()
    if s:
        if s[-1] in _URBAN_SUFFIX:
            return "urban"
        if s[-1] in _RURAL_SUFFIX:
            return "rural"
    raise ValueError("읍면동 접미를 판정할 수 없다: %r" % (emd,))


def jibun_exclusion(fields):
    """§2.4 — 대표지번(`지번일련번호 == 0`)만 남긴다. 시군구 결측은 제외 사유가 아니다."""
    if (fields[J_SEQ] or "0").strip().lstrip("0") not in ("", "0"):
        return "not_representative"
    return None


def build_exclusion(fields):
    """§2.4 — 지하(`지하여부 != 0`) 제외. '지하' 접두는 양쪽 파서 거동이 달라 별개 문제다."""
    if (fields[B_BASEMENT] or "0").strip().lstrip("0") not in ("", "0"):
        return "basement"
    return None


# ── 레코드 조립 ───────────────────────────────────────────────────────
def _join(parts):
    """빈 요소를 건너뛰고 공백 하나로 잇는다. 세종처럼 시군구가 없어도 공백이 겹치지 않는다."""
    return " ".join(p for p in parts if p)


def _int0(value):
    s = (value or "").strip()
    try:
        return int(s)
    except ValueError:
        return 0


def _jibun_tail(san, main, sub):
    head = "산 %d" % main if san else "%d" % main
    return head if sub == 0 else "%s-%d" % (head, sub)


def jibun_record(fields, sido_key):
    """`match_jibun` 1 행 → 표본 레코드.

    PNU 는 원천 필드 조립본과 건물관리번호 `[:19]` 두 경로로 산출하고, 조건 5 에 따라
    **불일치 시 BM25 를 채택**한다. 불일치 건수는 `pnu_mismatch` 로 남긴다.
    """
    bcode = (fields[J_BCODE] or "").strip()
    san = _int0(fields[J_SAN])
    main = _int0(fields[J_MAIN])
    sub = _int0(fields[J_SUB])
    bm25 = (fields[J_BM25] or "").strip()
    pnu, mismatch = resolve_pnu(bcode, san, main, sub, bm25)

    sido_name = (fields[J_SIDO] or "").strip()
    sigungu = (fields[J_SIGUNGU] or "").strip()
    emd = (fields[J_EMD] or "").strip()
    ri = (fields[J_RI] or "").strip()
    return {
        "layer": "jibun",
        "sido": sido_key,
        "urban": urban_rural(emd),
        "query": _join([sido_name, sigungu, emd, ri, _jibun_tail(san, main, sub)]),
        "pnu": pnu,
        "pnu_mismatch": mismatch,
        "bcode": bcode,
        "san": san,
        "ji_main": main,
        "ji_sub": sub,
        "bm25": bm25,
        # 교란(표본 B)이 질의를 재조립하려면 원본 요소가 필요하다.
        "sido_name": sido_name,
        "sigungu": sigungu,
        "emd": emd,
        "ri": ri,
    }


def _xy(fields, ix, iy):
    """5179 좌표쌍. 어느 한쪽이라도 없으면 `None` — 반쪽 좌표는 심판 자료가 아니다."""
    try:
        return float(fields[ix]), float(fields[iy])
    except (TypeError, ValueError):
        return None


def build_record(fields, sido_key):
    """`match_build` 1 행 → 표본 레코드. 좌표 둘은 §4.3 도로명 심판용이다."""
    bm25 = (fields[B_BM25] or "").strip()
    main = _int0(fields[B_MAIN])
    sub = _int0(fields[B_SUB])
    sido_name = (fields[B_SIDO] or "").strip()
    sigungu = (fields[B_SIGUNGU] or "").strip()
    emd = (fields[B_EMD] or "").strip()
    road = (fields[B_ROAD] or "").strip()
    bld = "%d" % main if sub == 0 else "%d-%d" % (main, sub)
    # 시군구가 있으면 읍면동은 넣지 않는다(§2.4 조립 규칙). 세종만 읍면동으로 대체된다.
    locality = sigungu or emd
    return {
        "layer": "road",
        "sido": sido_key,
        "urban": urban_rural(emd),
        "query": _join([sido_name, locality, road, bld]),
        "bm25": bm25,
        "pnu": bm25[:19],
        "bcode": (fields[B_BCODE] or "").strip(),
        "bld_main": main,
        "bld_sub": sub,
        "entrance_5179": _xy(fields, B_EX, B_EY),
        "center_5179": _xy(fields, B_CX, B_CY),
        "sido_name": sido_name,
        "sigungu": sigungu,
        "emd": emd,
        "road": road,
    }


def record_key(rec):
    """중복 판정 키. **층 스코프**다.

    도로명을 PNU 로 잡으면 같은 필지의 서로 다른 건물이 한 키로 뭉개진다.
    지번은 PNU, 도로명은 건물관리번호 25 자리가 각 층의 자연키다.
    """
    if rec["layer"] == "jibun":
        return ("jibun", rec["pnu"])
    return ("road", rec["bm25"])


def pnu_of(rec):
    """레코드에서 PNU 를 다시 조립한다. 교란 후보의 키 생성에 쓴다."""
    if rec["layer"] == "road":
        return rec["bm25"][:19]
    return resolve_pnu(rec["bcode"], rec["san"], rec["ji_main"], rec["ji_sub"], "")[0]


# ── 1 패스 스캔 ───────────────────────────────────────────────────────
class ScanStats(object):
    __slots__ = ("path", "sido", "atype", "rows", "kept", "excluded", "n_h", "decode_replaced")

    def __init__(self, path, sido, atype):
        self.path = path
        self.sido = sido
        self.atype = atype
        self.rows = 0
        self.kept = 0
        self.excluded = {}
        self.n_h = {}
        self.decode_replaced = 0

    def check(self):
        """`kept + Σ제외 == rows`. 이 항등식이 깨지면 층 계수를 믿을 수 없다."""
        return self.kept + sum(self.excluded.values()) == self.rows

    def __repr__(self):
        return "ScanStats(%s, rows=%d, kept=%d, excluded=%r)" % (
            os.path.basename(self.path), self.rows, self.kept, self.excluded)


def scan_file(path, sink=None):
    """원천 1 파일을 1 패스 읽어 층 계수를 산출한다.

    `sink(stratum_key, fields)` 를 주면 채택된 행의 **원시 필드**를 넘긴다.
    레코드 조립은 매 행 하기엔 비싸다 — 최종 선택분만 조립하면 된다.
    """
    name = os.path.basename(path)
    sido, atype = _split_filename(name)
    exclusion = jibun_exclusion if atype == "jibun" else build_exclusion
    width = J_WIDTH if atype == "jibun" else B_WIDTH
    emd_ix = J_EMD if atype == "jibun" else B_EMD

    st = ScanStats(path, sido, atype)
    bump = st.excluded
    with open(path, "r", encoding=ENCODING, errors="replace", newline="") as fh:
        for line in fh:
            line = line.rstrip("\r\n")
            st.rows += 1
            if "�" in line:
                st.decode_replaced += 1
            fields = line.split(DELIMITER)

            if len(fields) < width:
                bump["short_row"] = bump.get("short_row", 0) + 1
                continue
            reason = exclusion(fields)
            if reason is None:
                try:
                    urban = urban_rural(fields[emd_ix])
                except ValueError:
                    reason = "unknown_locality"
            if reason is not None:
                bump[reason] = bump.get(reason, 0) + 1
                continue

            st.kept += 1
            key = (sido, urban, atype)
            st.n_h[key] = st.n_h.get(key, 0) + 1
            if sink is not None:
                sink(key, fields)
    return st


# ── 원천 목록·체크섬 ──────────────────────────────────────────────────
def source_files(directory=None):
    """32 개. 읽기 순서는 **파일명 사전순**이다(M11 재현성 조건)."""
    directory = directory or SOURCE_DIR
    out = []
    for name in os.listdir(directory):
        try:
            _split_filename(name)
        except ValueError:
            continue    # match_rs_entrc.txt 등 시도별이 아닌 파일
        out.append(os.path.join(directory, name))
    return sorted(out)


def sha256_file(path, chunk=1 << 20):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            block = fh.read(chunk)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


def manifest_entry(path):
    path = os.path.abspath(path)
    return {"path": path, "bytes": os.path.getsize(path), "sha256": sha256_file(path)}


# ── 표본 B — 여집합과 교란 ────────────────────────────────────────────
def exclude_keys(recs, a_keys):
    """표본 A 의 키를 뺀 여집합(M12). A 와 B 가 겹치면 두 표본이 독립이 아니다."""
    a_keys = set(a_keys)
    return [r for r in recs if record_key(r) not in a_keys]


def perturb_candidates(rec):
    """부번만 바꾼 후보 최대 10 개. `(bcode, san, ji_main)` 은 고정한다.

    본번을 흔들면 다른 필지가 되어 "존재하지 않는 지번" 이 아니라 "다른 실존 지번"
    을 질의하게 된다. 부번 0(부번 없음)은 실존 확률이 높아 **맨 뒤**로 민다.
    """
    origin = rec["ji_sub"]
    subs = [s for s in range(1, PERTURB_LIMIT + 1) if s != origin]
    if origin != 0:
        subs.append(0)
    out = []
    for s in subs[:PERTURB_LIMIT]:
        c = dict(rec)
        c["ji_sub"] = s
        c["pnu"] = resolve_pnu(rec["bcode"], rec["san"], rec["ji_main"], s, "")[0]
        c["query"] = _join([rec["sido_name"], rec["sigungu"], rec["emd"], rec["ri"],
                            _jibun_tail(rec["san"], rec["ji_main"], s)])
        out.append(c)
    return out


def perturb(rec, probe):
    """`probe` 로 후보 전량을 **한 번에** 확인하고 첫 부존재를 채택한다.

    후보마다 조회하면 층당 수백 번의 왕복이 된다. 그리고 채택 전에 반드시
    부존재를 확인해야 한다 — 실존 지번을 "없는 주소" 로 질의하면 표본 B 가
    측정하려던 것(부존재 입력에 대한 거동)을 측정하지 못한다(C4 회귀).
    """
    cands = perturb_candidates(rec)
    if not cands:
        return None
    existing = set(probe([c["pnu"] for c in cands]))
    for c in cands:
        if c["pnu"] not in existing:
            out = dict(c)
            out["perturbed"] = True
            out["origin_pnu"] = pnu_of(rec)
            out["origin_sub"] = rec["ji_sub"]
            return out
    return None


# ── 실행부 ────────────────────────────────────────────────────────────
def _write_jsonl(path, rows):
    with open(path, "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n")


class _CachedProbe(object):
    """미리 조회한 실존 집합을 되돌려 준다. `perturb()` 계약(호출 1 회)은 그대로 두고,
    실제 DB 왕복만 층 단위로 묶는다 — 건별로 물으면 층당 40 왕복, 30 층이면 1,200 회다.
    """

    __slots__ = ("existing",)

    def __init__(self, existing):
        self.existing = existing

    def __call__(self, pnus):
        return [p for p in pnus if p in self.existing]


def _lookup_existing(orc, pnus):
    """`address ∪ parcel` 에 있는 PNU 집합. 계획 §2.3 의 `parcel` 단독보다 엄격하다 —
    부존재 판정은 넓게 볼수록 안전하고, 여기서 놓치면 실존 지번을 '없는 주소' 로
    질의하게 되어 표본 B 가 재려던 것을 재지 못한다(C4).
    """
    pnus = sorted(set(pnus))
    if not pnus:
        return set()
    found = set()
    for i in range(0, len(pnus), 2000):
        block = pnus[i:i + 2000]
        verdict = orc.jibun_batch({p: p for p in block}, relax12=True)
        found.update(p for p, v in verdict.items() if v != "N")
    return found


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    out_dir = OUT_DIR
    if "--out" in argv:
        out_dir = os.path.expanduser(argv[argv.index("--out") + 1])
    if not os.path.isdir(out_dir):
        os.makedirs(out_dir)

    t0 = time.time()
    paths = source_files()
    print("원천 %d 파일. 체크섬 산출 중…" % len(paths))
    manifest = [manifest_entry(p) for p in paths]
    print("  체크섬 완료 (%.1f s)" % (time.time() - t0))

    res_a, res_b = {}, {}
    n_h, excluded, decode_replaced = {}, {}, 0
    rows_by_atype = {"jibun": 0, "road": 0}

    for path in paths:
        sido, atype = _split_filename(os.path.basename(path))

        def sink(key, fields, _atype=atype):
            ra = res_a.get(key)
            if ra is None:
                ra = res_a[key] = Reservoir(SAMPLE_A_PER_STRATUM, stratum_seed(*key))
            ra.offer(fields)
            if _atype == "jibun":
                rb = res_b.get(key)
                if rb is None:
                    rb = res_b[key] = Reservoir(SAMPLE_A_PER_STRATUM, stratum_seed_b(*key))
                rb.offer(fields)

        t1 = time.time()
        st = scan_file(path, sink)
        if not st.check():
            raise AssertionError("항등식 위반: %r" % (st,))
        rows_by_atype[atype] += st.rows
        decode_replaced += st.decode_replaced
        for k, v in st.n_h.items():
            n_h[k] = n_h.get(k, 0) + v
        for k, v in st.excluded.items():
            excluded[k] = excluded.get(k, 0) + v
        print("  %-28s rows=%8d kept=%8d (%.1f s)"
              % (os.path.basename(path), st.rows, st.kept, time.time() - t1))

    total_rows = rows_by_atype["jibun"] + rows_by_atype["road"]
    print("\n스캔 완료: %d 행 (%.1f s)" % (total_rows, time.time() - t0))
    if rows_by_atype["jibun"] != TOTAL_ROWS_JIBUN or rows_by_atype["road"] != TOTAL_ROWS_BUILD:
        print("  ! 행수가 §1.4 실측과 다르다: jibun=%d(기대 %d) road=%d(기대 %d)"
              % (rows_by_atype["jibun"], TOTAL_ROWS_JIBUN,
                 rows_by_atype["road"], TOTAL_ROWS_BUILD))

    # 표본 A
    sample_a, sid = [], 0
    for key in sorted(res_a):
        sido, urban, atype = key
        make = jibun_record if atype == "jibun" else build_record
        for fields in res_a[key].items():
            sid += 1
            rec = make(fields, sido)
            rec["sid"] = sid
            rec["stratum"] = "%s|%s|%s" % key
            sample_a.append(rec)
    a_keys = {record_key(r) for r in sample_a}
    print("표본 A: %d 건 / %d 층 (고유키 %d)" % (len(sample_a), len(res_a), len(a_keys)))

    # 표본 B — A 의 여집합에서 뽑아 부번을 교란한다
    from oracle import Oracle
    orc = Oracle()
    sample_b, bid, exhausted = [], 0, []
    probed_total = 0
    for key in sorted(res_b):
        sido, urban, atype = key
        pool = [jibun_record(f, sido) for f in res_b[key].items()]
        pool = exclude_keys(pool, a_keys)
        # 층의 후보 PNU 를 한 번에 확인한다. 건별로 물으면 층당 40 왕복이다.
        cand = [c["pnu"] for rec in pool for c in perturb_candidates(rec)]
        probed_total += len(set(cand))
        probe = _CachedProbe(_lookup_existing(orc, cand))
        taken = 0
        for rec in pool:
            if taken >= SAMPLE_B_PER_STRATUM:
                break
            out = perturb(rec, probe)
            if out is None:
                continue
            bid += 1
            out["sid"] = bid
            out["stratum"] = "%s|%s|%s" % key
            sample_b.append(out)
            taken += 1
        if taken < SAMPLE_B_PER_STRATUM:
            exhausted.append(("%s|%s|%s" % key, taken))
    print("표본 B: %d 건 / %d 층" % (len(sample_b), len(res_b)))
    if exhausted:
        print("  ! 목표 미달 층: %r" % (exhausted,))

    strata = {
        "master_seed": MASTER_SEED,
        "n_h": {"%s|%s|%s" % k: v for k, v in sorted(n_h.items())},
        "achieved_a": {"%s|%s|%s" % k: len(r.items()) for k, r in sorted(res_a.items())},
        "seen_a": {"%s|%s|%s" % k: r.seen for k, r in sorted(res_a.items())},
        "excluded": excluded,
        "rows": {"jibun": rows_by_atype["jibun"], "road": rows_by_atype["road"],
                 "total": total_rows},
        "identity_ok": sum(n_h.values()) + sum(excluded.values()) == total_rows,
        "decode_replaced_rows": decode_replaced,
        "pnu_mismatch": sum(1 for r in sample_a if r.get("pnu_mismatch")),
        "empty_strata": sorted(
            "%s|%s|%s" % (s, u, a)
            for s in SIDO_KEYS for u in ("urban", "rural") for a in ("jibun", "road")
            if (s, u, a) not in n_h),
        "sample_b_short_strata": exhausted,
        "sample_b_candidate_pnus_probed": probed_total,
        "sample_b_oracle_queries": orc.queries,
        "encoding": ENCODING,
        "delimiter": DELIMITER,
        "read_order": "파일명 사전순 오름차순, 파일 내부는 물리적 행 순서",
        "elapsed_seconds": round(time.time() - t0, 1),
    }

    _write_jsonl(os.path.join(out_dir, "sample_a.jsonl"), sample_a)
    _write_jsonl(os.path.join(out_dir, "sample_b.jsonl"), sample_b)
    with open(os.path.join(out_dir, "manifest.json"), "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, ensure_ascii=False, indent=1, sort_keys=True)
    with open(os.path.join(out_dir, "strata.json"), "w", encoding="utf-8") as fh:
        json.dump(strata, fh, ensure_ascii=False, indent=1, sort_keys=True)

    print("\n실효 층 %d / 공집합 %d" % (len(n_h), len(strata["empty_strata"])))
    print("항등식 Σ N_h + Σ제외 == 전체행: %s" % strata["identity_ok"])
    print("PNU 이중경로 불일치: %d 건" % strata["pnu_mismatch"])
    print("출력: %s" % out_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
