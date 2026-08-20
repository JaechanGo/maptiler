# -*- coding: utf-8 -*-
"""
T042 보조측정 B - 대표지번의 '리명 공란'이 진짜 동 지역인지 판정 (완료조건 5)

완료조건 5 는 "리 보유율이 현행보다 떨어지는 시도에서, 그것이 정말 리가 없는 동
지역인지 조인 실패인지" 를 묻는다. 본측정에서 조인 실패(cur_ri_pk_blank_pkmiss)는
전 시도 0 으로 이미 배제됐다. 남은 것은 '리명 공란'의 성격이다.

법정동코드(10) = 시군구5 + 읍면동3 + 리2 이므로, 리코드(뒤 2자리)가 "00" 이면
그 법정동은 리를 갖지 않는 동 지역이다. 따라서 match_jibun 대표지번 행에 대해
  (리명 공란 여부) x (리코드 == "00" 여부)
2x2 교차표를 내면, 공란이 구조적 사실인지 원천 결손인지 결정된다.

취급방침 판정 B 준수: 리명도 코드값도 출력하지 않는다. 4 칸의 행 수만 낸다.

사용법:
  python3 scripts/verify-pk-join-042-riblank.py --src-dir <D> --out-dir <RAW> --all
  python3 scripts/verify-pk-join-042-riblank.py --src-archive --out-dir <RAW> --all

`--src-dir` 와 `--src-archive` 중 하나는 반드시 지정해야 한다(본측정과 같은 규약).
`--src-archive` 는 아카이브를 해제하지 않고 `7z x -so` stdout 스트림으로 읽는다.
아카이브 경로와 7z 실행 경로는 본측정 모듈의 `ARCHIVE`/`SEVENZ` 상수를 그대로 쓰며,
동명의 환경변수로 덮어쓸 수 있다.
"""
import argparse
import json
import os

from importlib.machinery import SourceFileLoader

_base = SourceFileLoader(
    "vpj042", os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "verify-pk-join-042.py")).load_module()
stream = _base.stream
SIDO16 = _base.SIDO16


def run(sido, src_dir):
    # [리명공란][리코드00] 2x2
    x = {(0, 0): 0, (0, 1): 0, (1, 0): 0, (1, 1): 0}
    eupmyeondong_zero = 0
    lawd_short = 0
    for raw in stream(f"match_jibun_{sido}.txt", src_dir):
        c = raw.rstrip(b"\r\n").split(b"|")
        if len(c) < 20 or c[12] != b"0":
            continue
        lawd = c[0]
        if len(lawd) != 10:
            lawd_short += 1
            continue
        blank = 1 if not c[4].strip() else 0
        ricode0 = 1 if lawd[8:10] == b"00" else 0
        x[(blank, ricode0)] += 1
        if lawd[5:8] == b"000":
            eupmyeondong_zero += 1
    tot = sum(x.values())
    return {
        "sido": sido,
        "rep_rows": tot,
        "ri_blank_and_ricode_zero": x[(1, 1)],      # 동 지역 - 구조적으로 리 없음
        "ri_blank_but_ricode_nonzero": x[(1, 0)],   # 리코드 있는데 리명 공란 = 원천 결손
        "ri_named_and_ricode_zero": x[(0, 1)],      # 리명 있는데 리코드 00 = 원천 불일치
        "ri_named_and_ricode_nonzero": x[(0, 0)],   # 정상 리 지역
        "lawd_len_not_10": lawd_short,
        "eupmyeondong_code_zero": eupmyeondong_zero,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("sido", nargs="*")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--src-dir", default=None, help="선행 T040 보존 해제본 디렉터리")
    ap.add_argument("--src-archive", action="store_true", help="아카이브 stdout 스트림으로 읽는다")
    ap.add_argument("--out-dir", required=True)
    a = ap.parse_args()
    if not a.src_archive and not a.src_dir:
        ap.error("--src-dir 또는 --src-archive 중 하나를 지정하라")
    src_dir = None if a.src_archive else a.src_dir
    todo = SIDO16 if a.all else a.sido
    os.makedirs(a.out_dir, exist_ok=True)
    for s in todo:
        R = run(s, src_dir)
        with open(os.path.join(a.out_dir, f"riblank-{s}.json"), "w", encoding="utf-8") as f:
            json.dump(R, f, ensure_ascii=False, indent=1)
        print(f"{s:16} 대표행={R['rep_rows']:>8} 공란&리코드00={R['ri_blank_and_ricode_zero']:>8} "
              f"공란&리코드유={R['ri_blank_but_ricode_nonzero']:>6} "
              f"리명유&리코드00={R['ri_named_and_ricode_zero']:>6} "
              f"리명유&리코드유={R['ri_named_and_ricode_nonzero']:>8} "
              f"len!=10={R['lawd_len_not_10']}")


if __name__ == "__main__":
    main()
