# -*- coding: utf-8 -*-
"""
T042 - 내비게이션용DB match_build <-> match_jibun 조인 키 전면 재검토 (읽기 전용 측정)

목적
----
현행 `scripts/09-gen-geocode.py` 는 두 파일을 **건물관리번호**(match_build 필드11 /
match_jibun 필드19)로 잇는다. 원천 가이드가 설계한 조인 키는 그것이 아니라
match_jibun 의 PK1~PK5(도로명코드/지하여부/건물본번/건물부번/지번일련번호) 및
PK6(주소관할읍면동코드)이다. 이 스크립트는 **전국 16개 시도**에서

  - 원천 설계 PK 조인의 적중률/리 보유율/충돌 유무
  - 현행 건물관리번호 조인 대비 차이 (리/지번 각각)
  - 미적중 행의 원인별 분류
  - PK 공유키(단지형 건물) 분포
  - 대표지번(지번일련번호=0) 부재 PK 의 규모

를 실측한다. **원천을 수정하지 않고, 아무것도 재빌드하지 않는다.**

취급방침 (docs/원천-202607-취급방침.md 판정 B)
--------------------------------------------
이 스크립트가 내보내는 것은 **집계 수치뿐**이다. 리명/주소 문자열/건물관리번호/
도로명코드/법정동코드의 **개별 값은 어떤 출력 경로로도 내보내지 않는다.**
유일한 예외는 `지하여부` 같은 1자리 **코드값 집합**으로, 취급방침 4.1
"필드 레이아웃/순서/자릿수/코드값 정의" 에 해당한다.

필드 인덱스 근거: docs/원천-202607-스키마.md 3.5(건물정보 33필드) / 3.6(지번정보 20필드)

  match_jibun  c[0]=법정동코드  c[4]=리명  c[5]=산여부 c[6]=지번본번 c[7]=지번부번
               c[8]=도로명코드(PK1) c[9]=지하여부(PK2) c[10]=건물본번(PK3)
               c[11]=건물부번(PK4)  c[12]=지번일련번호(PK5, "0"=대표지번)
               c[18]=건물관리번호   c[19]=주소관할읍면동코드(PK6)
  match_build  c[0]=주소관할읍면동코드(PK6) c[4]=도로명코드 c[6]=지하여부
               c[7]=건물본번 c[8]=건물부번 c[10]=건물관리번호

바이트 모드 분할의 안전성: CP949 2바이트 문자의 후행 바이트 범위(0x41-0x5A,
0x61-0x7A, 0x81-0xFE)는 구분자 `|`(0x7C) 및 개행(0x0A/0x0D)과 겹치지 않는다.
따라서 디코딩 없이 split(b'|') 해도 오분할이 없다 (스키마 문서 제I부 1.1 실증).
이 스크립트는 값을 출력하지 않으므로 전 구간을 바이트로 처리한다.

용어
----
  PK4  = 도로명코드|지하여부|건물본번|건물부번          (프롬프트 1.1 이 지정한 키)
  PK5  = PK4 + 주소관할읍면동코드(PK6)                    (스키마 3.6 이 규정한 완전 키)
  mgt  = 건물관리번호 25자리 전체                          (현행 지번 조인 키)
  mgt10= 건물관리번호 앞 10자리 = 등록 시점 법정동코드     (현행 리 조인 키)

현행 재현 규칙 - 09-gen-geocode.py 실측
  - 지번: d[mgt] 은 **대표지번 필터 없이** match_jibun 의 첫 행 채택
  - 리  : rd[mgt[:10]] 은 **첫 비공란 값** 채택 (다수결 아님. 코드 주석 "첫 값 채택")
  두 규칙 모두 그대로 재현하고, 리는 다수결 변형도 함께 잰다.

사용법
------
  python3 scripts/verify-pk-join-042.py --src-dir <해제본디렉터리> --out-dir <RAW> sejong
  python3 scripts/verify-pk-join-042.py --src-archive --out-dir <RAW> sejong
  python3 scripts/verify-pk-join-042.py --src-dir <D> --out-dir <RAW> --all

환경변수 (기본값은 아래 상수 정의부 참조)
  ARCHIVE  원천 7z 아카이브 경로
  SEVENZ   7z 실행 파일 경로 (이름만 두면 zsh git-scm-breeze 래퍼에 걸리므로 절대경로)
  예) ARCHIVE=/mnt/src.7z SEVENZ=/usr/bin/7z python3 ... --src-archive --out-dir <RAW> sejong

시도 토큰 16개 (경기는 비표준 gyunggi, 전남/광주는 통합 jeonnamgwangju):
  busan chungbuk chungnam daegu daejeon gangwon gyeongbuk gyeongnam
  gyunggi incheon jeju jeonbuk jeonnamgwangju sejong seoul ulsan
"""
import argparse
import collections
import json
import os
import subprocess
import time

# 두 상수는 환경변수로 덮어쓸 수 있다. 기본값은 절대경로를 유지한다 -
# SEVENZ 를 이름(`7z`)으로 두면 zsh git-scm-breeze 래퍼에 걸려 exit 127 이 난다.
# 보조측정 2종(-riblank / -conflicts)은 이 모듈을 SourceFileLoader 로 로드해
# stream() 을 그대로 쓰므로 같은 환경변수가 그대로 적용된다.
ARCHIVE = os.environ.get(
    "ARCHIVE",
    "/Users/jaechango_cudo/maptiler-rescue/source-202607/202607_내비게이션용DB_전체분.7z")
SEVENZ = os.environ.get("SEVENZ", "/opt/homebrew/bin/7z")

SIDO16 = ["busan", "chungbuk", "chungnam", "daegu", "daejeon", "gangwon",
          "gyeongbuk", "gyeongnam", "gyunggi", "incheon", "jeju", "jeonbuk",
          "jeonnamgwangju", "sejong", "seoul", "ulsan"]

MULTI_BUCKETS = [(1, 1), (2, 2), (3, 3), (4, 5), (6, 9), (10, 19), (20, 49), (50, None)]


def bucket_of(n):
    for lo, hi in MULTI_BUCKETS:
        if n >= lo and (hi is None or n <= hi):
            if hi is None:
                return f"{lo}+"
            return str(lo) if lo == hi else f"{lo}-{hi}"
    return "?"


def stream(member, src_dir):
    """member 의 각 행을 bytes 로 흘린다. src_dir 이 None 이면 아카이브 stdout 스트림."""
    if src_dir:
        with open(os.path.join(src_dir, member), "rb") as f:
            for raw in f:
                yield raw
    else:
        p = subprocess.Popen([SEVENZ, "x", "-so", ARCHIVE, member],
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        for raw in p.stdout:
            yield raw
        p.stdout.close()
        p.wait()


def norm_num(b):
    """숫자 필드 정규화 - 선행 0 제거. 두 파일의 표기 차이에서 오는 미적중을 가려내는 데만 쓴다."""
    s = b.strip().lstrip(b"0")
    return s if s else b"0"


def scan_jibun(sido, src_dir, S):
    """match_jibun 1-pass. 조인 대상 사전을 만들고 지번측 통계를 S 에 채운다."""
    pk4_rep = {}          # k4 -> (법정동코드, 리, 산|본번|부번)  대표지번 첫 행
    pk5_rep = {}
    pk4_minseq = {}       # k4 -> 지번일련번호 최솟값 (0 이면 대표지번 존재)
    pk5_minseq = {}
    pk4n_rep = set()      # 정규화 PK4 (대표지번) - 미적중 원인 분류 전용
    pk4_ri_multi = {}     # k4 -> set(리)  서로 다른 값이 나올 때만 승격
    pk4_jb_multi = {}
    pk4_lawd_multi = {}
    pk5_ri_multi = {}
    pk5_jb_multi = {}

    mgt_full = {}         # 건물관리번호 -> (리, 산|본번|부번)  첫 행 = 현행 d[] 규칙
    mgt10_first = {}      # mgt[:10] -> 리  첫 비공란 = 현행 rd[] 규칙
    mgt10_cnt = collections.defaultdict(collections.Counter)   # 다수결 변형

    road_set = set()
    road_sub = set()
    road_sub_main = set()

    lines = 0; jn = 0; jbad = 0; rep = 0; rep_alt_zero = 0; ri_rows = 0
    road_empty = 0; mgt_dup_rows = 0; mgt_first_nonrep = 0
    ug = collections.Counter()

    for raw in stream(f"match_jibun_{sido}.txt", src_dir):
        lines += 1
        c = raw.rstrip(b"\r\n").split(b"|")
        if len(c) < 20:
            jbad += 1
            continue
        jn += 1
        road, sub, main, subno, seq = c[8], c[9], c[10], c[11], c[12]
        lawd = c[0]
        ri = c[4].strip()
        jb = c[5] + b"|" + c[6] + b"|" + c[7]
        if ri:
            ri_rows += 1
        if not road.strip():
            road_empty += 1
        ug[sub] += 1

        k4 = road + b"|" + sub + b"|" + main + b"|" + subno
        k5 = k4 + b"|" + c[19]
        road_set.add(road)
        road_sub.add(road + b"|" + sub)
        road_sub_main.add(road + b"|" + sub + b"|" + main)

        try:
            iseq = int(seq)
        except ValueError:
            iseq = -1
        prev = pk4_minseq.get(k4)
        if prev is None or (0 <= iseq < prev) or (prev < 0 <= iseq):
            pk4_minseq[k4] = iseq
        prev = pk5_minseq.get(k5)
        if prev is None or (0 <= iseq < prev) or (prev < 0 <= iseq):
            pk5_minseq[k5] = iseq

        is_rep = (seq == b"0")
        if not is_rep and iseq == 0:
            rep_alt_zero += 1        # 수치상 0 이지만 표기가 "0" 이 아닌 행

        if is_rep:
            rep += 1
            r4 = pk4_rep.get(k4)
            if r4 is None:
                pk4_rep[k4] = (lawd, ri, jb)
            else:
                if r4[1] != ri:
                    s = pk4_ri_multi.get(k4)
                    if s is None:
                        s = pk4_ri_multi[k4] = {r4[1]}
                    s.add(ri)
                if r4[2] != jb:
                    s = pk4_jb_multi.get(k4)
                    if s is None:
                        s = pk4_jb_multi[k4] = {r4[2]}
                    s.add(jb)
                if r4[0] != lawd:
                    s = pk4_lawd_multi.get(k4)
                    if s is None:
                        s = pk4_lawd_multi[k4] = {r4[0]}
                    s.add(lawd)
            r5 = pk5_rep.get(k5)
            if r5 is None:
                pk5_rep[k5] = (lawd, ri, jb)
            else:
                if r5[1] != ri:
                    s = pk5_ri_multi.get(k5)
                    if s is None:
                        s = pk5_ri_multi[k5] = {r5[1]}
                    s.add(ri)
                if r5[2] != jb:
                    s = pk5_jb_multi.get(k5)
                    if s is None:
                        s = pk5_jb_multi[k5] = {r5[2]}
                    s.add(jb)
            pk4n_rep.add(road.strip() + b"|" + sub.strip() + b"|" +
                         norm_num(main) + b"|" + norm_num(subno))

        m = c[18]
        if m in mgt_full:
            mgt_dup_rows += 1
        else:
            mgt_full[m] = (ri, jb)
            if not is_rep:
                mgt_first_nonrep += 1
        if ri:
            k10 = m[:10]
            if k10 not in mgt10_first:
                mgt10_first[k10] = ri
            mgt10_cnt[k10][ri] += 1

    mgt10_major = {k: cnt.most_common(1)[0][0] for k, cnt in mgt10_cnt.items()}

    norep_hist = collections.Counter()
    for v in pk4_minseq.values():
        if v != 0:
            norep_hist[-1 if v < 0 else (v if v <= 5 else 6)] += 1

    S["jibun"] = {
        "lines": lines, "rows": jn, "bad_fieldcount": jbad,
        "rep_rows": rep, "rep_alt_zero_rows": rep_alt_zero,
        "ri_nonempty_rows": ri_rows, "road_empty_rows": road_empty,
        "underground_valueset": sorted(x.decode("ascii", "replace") for x in ug),
        "mgt_distinct": len(mgt_full), "mgt_dup_rows": mgt_dup_rows,
        "mgt_first_row_not_rep": mgt_first_nonrep,
        "mgt10_keys_with_ri": len(mgt10_cnt),
        "mgt10_ri_collision_keys": sum(1 for v in mgt10_cnt.values() if len(v) > 1),
        "mgt10_ri_max_distinct": max((len(v) for v in mgt10_cnt.values()), default=0),
        "pk4_keys_total": len(pk4_minseq),
        "pk4_keys_with_rep": len(pk4_rep),
        "pk4_keys_norep": sum(1 for v in pk4_minseq.values() if v != 0),
        "pk4_norep_minseq_hist": {("비수치" if k == -1 else "6이상" if k == 6 else str(k)): n
                                  for k, n in sorted(norep_hist.items())},
        "pk5_keys_total": len(pk5_minseq),
        "pk5_keys_with_rep": len(pk5_rep),
        "pk5_keys_norep": sum(1 for v in pk5_minseq.values() if v != 0),
        "pk4_ri_conflict_keys": len(pk4_ri_multi),
        "pk4_ri_conflict_max_distinct": max((len(v) for v in pk4_ri_multi.values()), default=0),
        "pk4_ri_conflict_same_lawd_keys": sum(1 for k in pk4_ri_multi if k not in pk4_lawd_multi),
        "pk4_jibun_conflict_keys": len(pk4_jb_multi),
        "pk4_jibun_conflict_max_distinct": max((len(v) for v in pk4_jb_multi.values()), default=0),
        "pk4_lawd_conflict_keys": len(pk4_lawd_multi),
        "pk4_lawd_conflict_max_distinct": max((len(v) for v in pk4_lawd_multi.values()), default=0),
        "pk5_ri_conflict_keys": len(pk5_ri_multi),
        "pk5_ri_conflict_max_distinct": max((len(v) for v in pk5_ri_multi.values()), default=0),
        "pk5_jibun_conflict_keys": len(pk5_jb_multi),
        "pk5_jibun_conflict_max_distinct": max((len(v) for v in pk5_jb_multi.values()), default=0),
    }
    del pk4_ri_multi, pk4_jb_multi, pk4_lawd_multi, pk5_ri_multi, pk5_jb_multi
    del pk5_minseq, mgt10_cnt
    return (pk4_rep, pk5_rep, pk4_minseq, pk4n_rep, mgt_full,
            mgt10_first, mgt10_major, road_set, road_sub, road_sub_main)


def scan_build(sido, src_dir, S, D):
    """match_build 1-pass. 조인 성립 여부와 원인 분류를 S 에 채운다."""
    (pk4_rep, pk5_rep, pk4_minseq, pk4n_rep, mgt_full,
     mgt10_first, mgt10_major, road_set, road_sub, road_sub_main) = D

    lines = 0; bn = 0; bbad = 0
    h_pk4 = h_pk4_ri = h_pk5 = h_pk5_ri = h_mgt = h_mgt_ri = 0
    cur_ri = cur_ri_major = 0
    pk4_multi = collections.Counter()
    pk5_multi = collections.Counter()
    ug = collections.Counter()
    road_empty = main_zero = 0

    miss_norep = miss_road_empty = miss_road_absent = 0
    miss_sub_absent = miss_main_absent = miss_subno_absent = 0
    miss_norm_recoverable = 0
    miss_pk4_mgt_hit = 0
    hit_pk4_mgt_miss = 0
    ri_agree = ri_disagree = 0
    cur_ri_pk_blank_hit = cur_ri_pk_blank_miss = pk_ri_cur_blank = 0
    jb_agree = jb_disagree = 0

    for raw in stream(f"match_build_{sido}.txt", src_dir):
        lines += 1
        c = raw.rstrip(b"\r\n").split(b"|")
        if len(c) < 27:
            bbad += 1
            continue
        bn += 1
        road, sub, main, subno = c[4], c[6], c[7], c[8]
        ug[sub] += 1
        if not road.strip():
            road_empty += 1
        if norm_num(main) == b"0":
            main_zero += 1

        k4 = road + b"|" + sub + b"|" + main + b"|" + subno
        k5 = k4 + b"|" + c[0]
        pk4_multi[k4] += 1
        pk5_multi[k5] += 1

        rec4 = pk4_rep.get(k4)
        rec5 = pk5_rep.get(k5)
        m = c[10]
        recm = mgt_full.get(m)

        pk_ri = rec4[1] if rec4 else b""
        if rec4:
            h_pk4 += 1
            if pk_ri:
                h_pk4_ri += 1
        if rec5:
            h_pk5 += 1
            if rec5[1]:
                h_pk5_ri += 1
        if recm is not None:
            h_mgt += 1
            if recm[0]:
                h_mgt_ri += 1

        k10 = m[:10]
        c_ri = mgt10_first.get(k10, b"")
        if c_ri:
            cur_ri += 1
        if mgt10_major.get(k10, b""):
            cur_ri_major += 1

        # 완료조건 5 - 현행 리 vs 원천PK 리
        if c_ri and pk_ri:
            if c_ri == pk_ri:
                ri_agree += 1
            else:
                ri_disagree += 1
        elif c_ri:
            if rec4:
                cur_ri_pk_blank_hit += 1      # PK 적중, 원천이 "리 없음" 이라고 말함
            else:
                cur_ri_pk_blank_miss += 1     # PK 미적중 -> 조인 실패
        elif pk_ri:
            pk_ri_cur_blank += 1

        # 현행 지번 vs 원천PK 지번 (둘 다 적중한 행에 한해)
        if rec4 and recm is not None:
            if recm[1] == rec4[2]:
                jb_agree += 1
            else:
                jb_disagree += 1

        if rec4 is None:
            if recm is not None:
                miss_pk4_mgt_hit += 1
            if not road.strip():
                miss_road_empty += 1
            elif k4 in pk4_minseq:
                miss_norep += 1               # 키는 있는데 대표지번이 없음
            elif road not in road_set:
                miss_road_absent += 1
            elif road + b"|" + sub not in road_sub:
                miss_sub_absent += 1
            elif road + b"|" + sub + b"|" + main not in road_sub_main:
                miss_main_absent += 1
            else:
                miss_subno_absent += 1
            if (road.strip() + b"|" + sub.strip() + b"|" +
                    norm_num(main) + b"|" + norm_num(subno)) in pk4n_rep:
                miss_norm_recoverable += 1
        elif recm is None:
            hit_pk4_mgt_miss += 1

    h4 = collections.Counter()
    for v in pk4_multi.values():
        h4[bucket_of(v)] += 1
    h5 = collections.Counter()
    for v in pk5_multi.values():
        h5[bucket_of(v)] += 1

    S["build"] = {
        "lines": lines, "rows": bn, "bad_fieldcount": bbad,
        "road_empty_rows": road_empty, "main_no_zero_rows": main_zero,
        "underground_valueset": sorted(x.decode("ascii", "replace") for x in ug),
        "pk4_hit": h_pk4, "pk4_hit_ri": h_pk4_ri,
        "pk5_hit": h_pk5, "pk5_hit_ri": h_pk5_ri,
        "mgt_hit": h_mgt, "mgt_hit_ri": h_mgt_ri,
        "cur_ri_first": cur_ri, "cur_ri_majority": cur_ri_major,
        "pk4_distinct_keys": len(pk4_multi),
        "pk4_shared_keys": sum(1 for v in pk4_multi.values() if v > 1),
        "pk4_max_multiplicity": max(pk4_multi.values(), default=0),
        "pk4_multiplicity_hist": dict(h4),
        "pk5_distinct_keys": len(pk5_multi),
        "pk5_shared_keys": sum(1 for v in pk5_multi.values() if v > 1),
        "pk5_max_multiplicity": max(pk5_multi.values(), default=0),
        "pk5_multiplicity_hist": dict(h5),
        "miss_breakdown": {
            "total": bn - h_pk4,
            "road_code_empty": miss_road_empty,
            "pk_exists_but_no_rep": miss_norep,
            "road_code_absent_in_jibun": miss_road_absent,
            "underground_flag_absent": miss_sub_absent,
            "main_no_absent": miss_main_absent,
            "sub_no_absent": miss_subno_absent,
            "recoverable_by_numeric_normalization": miss_norm_recoverable,
            "also_mgt_hit": miss_pk4_mgt_hit,
        },
        "hit_pk4_but_mgt_miss": hit_pk4_mgt_miss,
        "ri_cross": {
            "both_agree": ri_agree, "both_disagree": ri_disagree,
            "cur_ri_pk_blank_pkhit": cur_ri_pk_blank_hit,
            "cur_ri_pk_blank_pkmiss": cur_ri_pk_blank_miss,
            "pk_ri_cur_blank": pk_ri_cur_blank,
        },
        "jibun_cross": {"both_agree": jb_agree, "both_disagree": jb_disagree},
    }


def measure(sido, src_dir):
    t0 = time.time()
    S = {"sido": sido, "source": "extract-dir" if src_dir else "archive-stream"}
    D = scan_jibun(sido, src_dir, S)
    S["elapsed_jibun_sec"] = round(time.time() - t0, 1)
    scan_build(sido, src_dir, S, D)
    S["elapsed_sec"] = round(time.time() - t0, 1)
    return S


def tsv_line(S):
    b, j = S["build"], S["jibun"]
    n = max(b["rows"], 1)
    kd = max(b["pk4_distinct_keys"], 1)
    return (f"{S['sido']}\tjibun={j['rows']}\trep={j['rep_rows']}\tbuild={b['rows']}\t"
            f"mgt적중={b['mgt_hit']/n*100:.2f}%\tmgt리={b['mgt_hit_ri']/n*100:.2f}%\t"
            f"현행리={b['cur_ri_first']/n*100:.2f}%\t"
            f"PK적중={b['pk4_hit']/n*100:.2f}%\tPK리={b['pk4_hit_ri']/n*100:.2f}%\t"
            f"PK6적중={b['pk5_hit']/n*100:.2f}%\tPK6리={b['pk5_hit_ri']/n*100:.2f}%\t"
            f"PK리충돌={j['pk4_ri_conflict_keys']}\tPK지번충돌={j['pk4_jibun_conflict_keys']}\t"
            f"PK공유키={b['pk4_shared_keys']}({b['pk4_shared_keys']/kd*100:.2f}%)\t"
            f"최대다중도={b['pk4_max_multiplicity']}\t{S['elapsed_sec']}s")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("sido", nargs="*", default=[])
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--src-dir", default=None, help="선행 T040 보존 해제본 디렉터리")
    ap.add_argument("--src-archive", action="store_true", help="아카이브 stdout 스트림으로 읽는다")
    ap.add_argument("--out-dir", required=True)
    a = ap.parse_args()

    todo = SIDO16 if a.all else a.sido
    if not todo:
        ap.error("시도 토큰을 지정하거나 --all 을 써라")
    bad = [s for s in todo if s not in SIDO16]
    if bad:
        ap.error(f"알 수 없는 시도 토큰: {bad}")
    if not a.src_archive and not a.src_dir:
        ap.error("--src-dir 또는 --src-archive 중 하나를 지정하라")
    src_dir = None if a.src_archive else a.src_dir
    os.makedirs(a.out_dir, exist_ok=True)

    for s in todo:
        S = measure(s, src_dir)
        with open(os.path.join(a.out_dir, f"{s}.json"), "w", encoding="utf-8") as f:
            json.dump(S, f, ensure_ascii=False, indent=1)
        line = tsv_line(S)
        with open(os.path.join(a.out_dir, f"{s}.tsv"), "w", encoding="utf-8") as f:
            f.write(line + "\n")
        print(line, flush=True)


if __name__ == "__main__":
    main()
