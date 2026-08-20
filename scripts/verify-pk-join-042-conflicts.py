# -*- coding: utf-8 -*-
"""
T042 보조측정 A - PK4 대표지번 충돌 키의 성격 규명 (완료조건 2)

verify-pk-join-042.py 본측정에서 PK4(도로명코드|지하여부|건물본번|건물부번) 대표지번
충돌이 발견된 시도만 다시 훑어, 충돌 키 하나하나의 **구조적 성격**을 수치로 특정한다.

취급방침 판정 B 준수: 키 값도 리명도 출력하지 않는다. 키별로
  대표행수 / 서로 다른 PK6 수 / 법정동코드 수 / 시군구 수 / 읍면동 수 / 리코드 수
  / 리명 종수 / 지번 종수 / 건물관리번호 종수
같은 **개수**만 낸다. 아울러 그 키에 조인되는 match_build 행 수를 세어 오염 규모를 잰다.

사용법:
  python3 scripts/verify-pk-join-042-conflicts.py --src-dir <D> --out-dir <RAW> gyunggi sejong
  python3 scripts/verify-pk-join-042-conflicts.py --src-archive --out-dir <RAW> gyunggi sejong

`--src-dir` 와 `--src-archive` 중 하나는 반드시 지정해야 한다(본측정과 같은 규약).
`--src-archive` 는 아카이브를 해제하지 않고 `7z x -so` stdout 스트림으로 읽는다.
아카이브 경로와 7z 실행 경로는 본측정 모듈의 `ARCHIVE`/`SEVENZ` 상수를 그대로 쓰며,
동명의 환경변수로 덮어쓸 수 있다.
"""
import argparse
import collections
import json
import os

from importlib.machinery import SourceFileLoader

_base = SourceFileLoader(
    "vpj042", os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "verify-pk-join-042.py")).load_module()
stream = _base.stream


def run(sido, src_dir):
    reps = collections.defaultdict(list)      # k4 -> [(lawd, ri, jb, pk6, mgt)]
    for raw in stream(f"match_jibun_{sido}.txt", src_dir):
        c = raw.rstrip(b"\r\n").split(b"|")
        if len(c) < 20 or c[12] != b"0":
            continue
        k4 = c[8] + b"|" + c[9] + b"|" + c[10] + b"|" + c[11]
        reps[k4].append((c[0], c[4].strip(), c[5] + b"|" + c[6] + b"|" + c[7], c[19], c[18]))

    bad = {k: v for k, v in reps.items()
           if len({x[1] for x in v}) > 1 or len({x[2] for x in v}) > 1}
    del reps

    build_rows = collections.Counter()
    if bad:
        for raw in stream(f"match_build_{sido}.txt", src_dir):
            c = raw.rstrip(b"\r\n").split(b"|")
            if len(c) < 27:
                continue
            k4 = c[4] + b"|" + c[6] + b"|" + c[7] + b"|" + c[8]
            if k4 in bad:
                build_rows[k4] += 1

    out = []
    for k, v in bad.items():
        lawds = {x[0] for x in v}
        out.append({
            "rep_rows": len(v),
            "distinct_pk6": len({x[3] for x in v}),
            "distinct_lawd": len(lawds),
            "distinct_sigungu": len({x[0][:5] for x in v}),
            "distinct_eupmyeondong": len({x[0][:8] for x in v}),
            "distinct_ri_code": len({x[0][8:10] for x in v}),
            "ri_code_zero_present": any(x[0][8:10] == b"00" for x in v),
            "distinct_ri_name": len({x[1] for x in v}),
            "blank_ri_present": any(not x[1] for x in v),
            "distinct_jibun": len({x[2] for x in v}),
            "distinct_mgt": len({x[4] for x in v}),
            "mgt10_all_same": len({x[4][:10] for x in v}) == 1,
            "build_rows_joined": build_rows.get(k, 0),
        })
    out.sort(key=lambda d: (-d["rep_rows"], -d["build_rows_joined"]))
    return {"sido": sido, "conflict_keys": len(out),
            "build_rows_affected": sum(d["build_rows_joined"] for d in out),
            "keys": out}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("sido", nargs="+")
    ap.add_argument("--src-dir", default=None, help="선행 T040 보존 해제본 디렉터리")
    ap.add_argument("--src-archive", action="store_true", help="아카이브 stdout 스트림으로 읽는다")
    ap.add_argument("--out-dir", required=True)
    a = ap.parse_args()
    if not a.src_archive and not a.src_dir:
        ap.error("--src-dir 또는 --src-archive 중 하나를 지정하라")
    src_dir = None if a.src_archive else a.src_dir
    os.makedirs(a.out_dir, exist_ok=True)
    for s in a.sido:
        R = run(s, src_dir)
        with open(os.path.join(a.out_dir, f"conflicts-{s}.json"), "w", encoding="utf-8") as f:
            json.dump(R, f, ensure_ascii=False, indent=1)
        print(f"{s}: 충돌키={R['conflict_keys']} 영향건물행={R['build_rows_affected']}")
        for d in R["keys"]:
            print("   " + " ".join(f"{k}={v}" for k, v in d.items()))


if __name__ == "__main__":
    main()
