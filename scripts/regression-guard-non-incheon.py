#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""T026 §9-4 — 인천 외 시도 표본 회귀 가드 (595 채점셋의 **대체품**).

왜 이것이 필요한가:
  기존 회귀 가드였던 `15-score-595.py` 는 xlsx 원본과 기준선 dump 가 모두 없어 돌릴 수 없다(§7-4).
  그렇다고 회귀 확인을 통째로 건너뛰면, 인천 치환이 인천 **밖** 주소를 망가뜨려도 아무도 모른다.
  그래서 외부 자산에 의존하지 않는 가드를 자체 조달한다.

설계 원칙 — **기대값을 사람이 채점하지 않는다**:
  이 가드는 "정답"을 모른다. 변경 전후 응답을 **바이트로** 비교할 뿐이다.
  덕분에 채점 기준이 필요 없고, 채점자의 착각이 개입할 여지도 없다.

이 가드가 말할 수 있는 것과 없는 것 (정직한 한계 — 보고서에 그대로 쓴다):
  ○ 말할 수 있다 : "이번 변경으로 인한 회귀 없음" (820 질의 범위에서)
  ✕ 말할 수 없다 : "절대 정확도 유지" — 기준선이 없으므로 히트율·b_code 정확도를 못 낸다
  ✕ 말할 수 없다 : "인천 자체의 정확도" — 인천은 **제외** 표본이다. I1·I11·L2-1 이 담당한다

표본 (scripts/regression-samples/non-incheon-280.csv):
  인천(28)·전남광주 계열(46·29)을 제외한 **14개 시도 × 20건 = 280건**.
  계획서 §9-4 는 "15개 시도 × 20건 = 300건"으로 적었으나 address 의 실측 시도는 17개이고
  셋을 빼면 14개다. 총량(300)이 아니라 계획서가 명시한 "시도별 균등 20건" 쪽을 지켰다.
  추출은 결정적이다(무작위 금지) — 상세는 CSV 헤더 주석과 impl-notes.md 참조.

질의 형태 (행마다 3가지, 세종만 2가지):
  ① "시도 시군구 읍면동 지번"   완전형
  ② "시군구 읍면동 지번"        시도 생략 — 지역 좁힘 경로를 탄다
  ③ "읍면동 지번"               동명 중복 경로
  세종특별자치시는 시군구 계층이 없어(sigungu IS NULL) ② 가 성립하지 않는다.
  → 13 × 20 × 3 + 20 × 2 = **820 질의**

사용:
  # A/B 동시 대조 — 두 컨테이너가 함께 살아 있을 때(권장)
  scripts/regression-guard-non-incheon.py guard --before http://127.0.0.1:8092 \\
                                                --after  http://127.0.0.1:8093

  # 파일 스냅샷 모드 — 같은 서버를 고쳐 가며 볼 때 필수(S6c 검증이 이 경우다)
  scripts/regression-guard-non-incheon.py snapshot --base http://127.0.0.1:8093 --out before.jsonl
  #   … 여기서 S6c 를 적용하고 컨테이너 재시작 …
  scripts/regression-guard-non-incheon.py snapshot --base http://127.0.0.1:8093 --out after.jsonl
  scripts/regression-guard-non-incheon.py compare before.jsonl after.jsonl

  scripts/regression-guard-non-incheon.py --selftest    # 네트워크 없이 질의 생성기만 검증

판정: 바이트 diff 가 0 이면 exit 0, 아니면 exit 1 (다른 질의를 최대 --show 건 출력).

주의: 스냅샷 파일은 응답 전문을 담아 수 MB 가 된다. **커밋 대상은 CSV 표본뿐**이다.
      스냅샷은 스크래치 영역에 두고, 재현이 필요하면 CSV 로부터 다시 뽑는다.

의존: 표준 라이브러리만(urllib) — 폐쇄망/무의존 일관.
"""
import argparse
import concurrent.futures
import csv
import hashlib
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_SAMPLES = os.path.join(HERE, "regression-samples", "non-incheon-280.csv")

# 표본이 이 접두를 하나라도 포함하면 가드의 전제가 깨진 것이다.
# (인천·전남·광주는 이번 변경의 **대상**이라 응답이 달라지는 게 정상이므로 표본에 있으면 안 된다.)
EXCLUDED_SIDO = ("28", "46", "29")


# ─────────────────────────────────────────────────────────────────────────────
# 질의 생성
# ─────────────────────────────────────────────────────────────────────────────
def queries_for(row):
    """표본 1행 → 질의 문자열 목록.

    `jibun` 은 이미 "읍면동 [리] 번지" 형태다(예: '청운동 144-3', '기장읍 내리 124-14').
    따라서 앞에 시도·시군구를 붙이는 것만으로 세 형태가 만들어진다.
    """
    sido = (row.get("sido") or "").strip()
    sigungu = (row.get("sigungu") or "").strip()
    jibun = (row.get("jibun") or "").strip()
    if not (sido and jibun):
        return []
    out = []
    if sigungu:
        out.append(f"{sido} {sigungu} {jibun}")   # ① 완전형
        out.append(f"{sigungu} {jibun}")          # ② 시도 생략
    else:
        # 세종특별자치시: 시군구 계층이 없다. ② 를 만들면 ③ 과 같아져 중복 질의가 된다.
        out.append(f"{sido} {jibun}")             # ① 완전형(시군구 없음)
    out.append(jibun)                             # ③ 동명 중복 경로
    return out


def load_queries(path):
    """CSV → (질의 목록, 시도별 표본수). 표본 자체의 건전성도 함께 검사한다."""
    with open(path, encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        raise SystemExit(f"표본이 비었다: {path}")

    per_sido = {}
    bad = []
    for r in rows:
        cd = (r.get("sido_cd") or "").strip()
        per_sido[cd] = per_sido.get(cd, 0) + 1
        if cd in EXCLUDED_SIDO:
            bad.append(r)
    if bad:
        # 가드가 스스로를 무력화하는 가장 조용한 실패 방식이다 → 즉시 죽인다.
        raise SystemExit(
            f"✖ 표본에 제외 대상 시도({'/'.join(EXCLUDED_SIDO)})가 {len(bad)}건 섞여 있다. "
            "이 가드는 '인천·전남광주 **밖**의 무회귀'만 판정한다."
        )

    qs = []
    for r in rows:
        qs.extend(queries_for(r))
    # 같은 질의는 같은 응답이다 — 중복은 시간 낭비이므로 첫 등장 순서를 지켜 유일화한다.
    seen = set()
    uniq = []
    for q in qs:
        if q not in seen:
            seen.add(q)
            uniq.append(q)
    return uniq, per_sido


# ─────────────────────────────────────────────────────────────────────────────
# 호출
# ─────────────────────────────────────────────────────────────────────────────
def fetch(base, q, timeout=20):
    """/geocode 응답 **원문**을 그대로 돌려준다. json.loads 로 재직렬화하면
    키 순서·공백이 정규화되어 우리가 잡으려는 차이를 지워 버릴 수 있다."""
    url = base.rstrip("/") + "/geocode?" + urllib.parse.urlencode(
        {"q": q}, quote_via=urllib.parse.quote)
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.read().decode("utf-8"), None
    except urllib.error.HTTPError as e:
        return f"<HTTP {e.code}>", f"HTTP {e.code}"
    except Exception as e:  # 네트워크/타임아웃
        return f"<ERR {type(e).__name__}>", f"{type(e).__name__}: {e}"


def fetch_all(base, queries, workers=4, timeout=20, label=""):
    """질의 목록 → 입력 순서를 유지한 응답 원문 목록."""
    out = [None] * len(queries)
    errs = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(fetch, base, q, timeout): i for i, q in enumerate(queries)}
        done = 0
        for fut in concurrent.futures.as_completed(futs):
            i = futs[fut]
            body, err = fut.result()
            out[i] = body
            if err:
                errs += 1
            done += 1
            if done % 100 == 0:
                print(f"  {label}{done}/{len(queries)} …", file=sys.stderr)
    if errs:
        print(f"  ⚠ {label}호출 실패 {errs}건 — 실패 응답도 비교 대상에 포함된다"
              f"(양쪽이 같은 이유로 실패하면 diff 는 0 이다).", file=sys.stderr)
    return out


def write_snapshot(path, queries, bodies):
    with open(path, "w", encoding="utf-8") as f:
        for q, b in zip(queries, bodies):
            f.write(json.dumps(
                {"q": q, "sha256": hashlib.sha256(b.encode("utf-8")).hexdigest(), "body": b},
                ensure_ascii=False) + "\n")


def read_snapshot(path):
    rows = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


# ─────────────────────────────────────────────────────────────────────────────
# 판정
# ─────────────────────────────────────────────────────────────────────────────
def report_diff(pairs, show):
    """pairs: [(q, before, after)] → diff 건수 출력 후 개수 반환."""
    diffs = [(q, a, b) for q, a, b in pairs if a != b]
    total = len(pairs)
    print()
    print(f"════ §9-4 회귀 가드 ════  질의 {total}건 · 바이트 diff **{len(diffs)}건**")
    if not diffs:
        print("판정: PASS — 인천·전남광주 밖 응답이 한 바이트도 달라지지 않았다.")
        print("      (말할 수 있는 것은 '회귀 없음'뿐이다. '정확도 유지'가 아니다.)")
        return 0
    print("판정: ✖FAIL — 인천 밖 응답이 달라졌다. 변경을 되돌리고 원인을 특정해야 한다.")
    for q, a, b in diffs[:show]:
        print(f"\n  질의: {q}")
        print(f"    before: {a[:300]}")
        print(f"    after : {b[:300]}")
    if len(diffs) > show:
        print(f"\n  … 외 {len(diffs) - show}건 (--show 로 더 볼 수 있다)")
    return len(diffs)


# ─────────────────────────────────────────────────────────────────────────────
def selftest():
    ok = True

    def eq(got, want, name):
        nonlocal ok
        if got != want:
            ok = False
            print(f"  ✖ {name}\n     got ={got}\n     want={want}")
        else:
            print(f"  ✔ {name}")

    eq(queries_for({"sido": "서울특별시", "sigungu": "종로구", "jibun": "청운동 144-3"}),
       ["서울특별시 종로구 청운동 144-3", "종로구 청운동 144-3", "청운동 144-3"],
       "일반 시도 → 3형태")
    eq(queries_for({"sido": "세종특별자치시", "sigungu": "", "jibun": "반곡동 971"}),
       ["세종특별자치시 반곡동 971", "반곡동 971"],
       "세종(시군구 없음) → 2형태 · ②는 ③과 중복이라 생성하지 않는다")
    eq(queries_for({"sido": "부산광역시", "sigungu": "기장군", "jibun": "기장읍 내리 124-14"}),
       ["부산광역시 기장군 기장읍 내리 124-14", "기장군 기장읍 내리 124-14", "기장읍 내리 124-14"],
       "리 포함 지번도 jibun 하나로 조립된다")
    eq(queries_for({"sido": "", "sigungu": "종로구", "jibun": "청운동 1"}), [],
       "시도 없는 행은 버린다")
    print("\nselftest:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser(
        description="T026 §9-4 인천 외 회귀 가드 (바이트 비교)",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--selftest", action="store_true", help="네트워크 없이 질의 생성기 검증")
    sub = ap.add_subparsers(dest="cmd")

    def common(p):
        p.add_argument("--samples", default=DEFAULT_SAMPLES)
        p.add_argument("--workers", type=int, default=4)
        p.add_argument("--timeout", type=float, default=20)
        p.add_argument("--limit", type=int, default=0, help="앞에서 N 질의만(연습용)")

    p_snap = sub.add_parser("snapshot", help="한 서버의 응답을 파일로 저장")
    p_snap.add_argument("--base", required=True)
    p_snap.add_argument("--out", required=True)
    common(p_snap)

    p_cmp = sub.add_parser("compare", help="스냅샷 두 개를 바이트 비교")
    p_cmp.add_argument("a")
    p_cmp.add_argument("b")
    p_cmp.add_argument("--show", type=int, default=5)

    p_guard = sub.add_parser("guard", help="두 서버를 동시에 호출해 즉시 비교")
    p_guard.add_argument("--before", default="http://127.0.0.1:8092")
    p_guard.add_argument("--after", default="http://127.0.0.1:8093")
    p_guard.add_argument("--show", type=int, default=5)
    common(p_guard)

    a = ap.parse_args()
    if a.selftest:
        return selftest()
    if not a.cmd:
        ap.print_help()
        return 2

    if a.cmd == "compare":
        ra, rb = read_snapshot(a.a), read_snapshot(a.b)
        if len(ra) != len(rb):
            print(f"✖ 스냅샷 길이가 다르다: {len(ra)} vs {len(rb)} — 같은 표본으로 뽑았는지 확인하라.")
            return 1
        pairs = []
        for x, y in zip(ra, rb):
            if x["q"] != y["q"]:
                print(f"✖ 질의 순서가 어긋난다: {x['q']!r} vs {y['q']!r}")
                return 1
            pairs.append((x["q"], x["body"], y["body"]))
        return 1 if report_diff(pairs, a.show) else 0

    queries, per_sido = load_queries(a.samples)
    if a.limit:
        queries = queries[:a.limit]
    print(f"표본: {a.samples}")
    print(f"  시도 {len(per_sido)}개 · 표본 {sum(per_sido.values())}건 → 질의 {len(queries)}건")
    print("  시도별: " + " ".join(f"{k}:{v}" for k, v in sorted(per_sido.items())))

    if a.cmd == "snapshot":
        bodies = fetch_all(a.base, queries, a.workers, a.timeout, label=f"{a.base} ")
        write_snapshot(a.out, queries, bodies)
        print(f"스냅샷 저장: {a.out} ({len(queries)}행)")
        return 0

    # guard
    print(f"\nbefore = {a.before}")
    ba = fetch_all(a.before, queries, a.workers, a.timeout, label="before ")
    print(f"after  = {a.after}")
    bb = fetch_all(a.after, queries, a.workers, a.timeout, label="after  ")
    return 1 if report_diff(list(zip(queries, ba, bb)), a.show) else 0


if __name__ == "__main__":
    sys.exit(main())
