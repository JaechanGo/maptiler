#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""골든셋 추출 — dedup_er 후보쌍 중 라벨링 가치 높은 것을 층화 샘플링해 CSV로.

목적: dedup_er 임계(AUTO 0.90 / REVIEW 0.50)·_addr_weight 등을 실데이터로 보정하려면
  사람이 0/1 라벨한 정답쌍이 필요. 이 도구가 경계(REVIEW)·자동병합(AUTO) 쌍을 features와 함께 뽑는다.
사용: python3 13b-golden-extract.py [--db ~/geocode-build/geocode.sqlite] [--out golden.csv]
      [--min-pr 0.5] [--per-group 40] [--max 800]
라벨링: 출력 CSV 의 label 에 1(같은 점포)/0(다른 점포). 그 뒤 별도 eval 로 precision/recall·임계 스윕.
  · decision=AUTO 인데 label=0 → 오병합(false merge, precision 손실)
  · decision=REVIEW → 임계 경계, 라벨 분포로 0.90/0.50 타당성 판단
출력 CSV 는 dedup_er 와 동일 스코어링(동일 코드 재사용)이라 라벨↔모델 정합.
"""
import argparse, csv, os, pathlib, sqlite3, sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from dedup_er import export_pairs

COLS = ["label", "decision", "pr", "name_sim", "dist_m", "phone_eq", "same_bld",
        "name_a", "src_a", "name_b", "src_b", "id_a", "id_b"]

def _simbucket(s):
    return "0.85+" if s >= 0.85 else "0.60-0.85" if s >= 0.60 else "0.30-0.60" if s >= 0.30 else "<0.30"

def _stratify(pairs, per_group, total_max):
    """그룹키=(decision, name_sim버킷, phone_eq, same_bld). 그룹별 pr 균등간격 추출(결정적·다양성)."""
    groups = {}
    for p in pairs:
        k = (p["decision"], _simbucket(p["name_sim"]), p["phone_eq"], p["same_bld"])
        groups.setdefault(k, []).append(p)
    picked = []
    for k in sorted(groups):                      # 키 정렬 → 결정적
        g = sorted(groups[k], key=lambda p: (p["pr"], p["id_a"], p["id_b"]))
        if len(g) <= per_group:
            sel = g
        else:                                     # 균등간격(pr 분포 전체를 대표)
            step = len(g) / per_group
            sel = [g[int(i*step)] for i in range(per_group)]
        picked.append((k, sel))
    # 전체 상한: 그룹 라운드로빈으로 잘라 한 그룹이 독식 안 하게
    out = []; idx = 0
    while len(out) < total_max:
        added = False
        for _, sel in picked:
            if idx < len(sel):
                out.append(sel[idx]); added = True
                if len(out) >= total_max: break
        if not added: break
        idx += 1
    return out, groups

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=os.path.expanduser("~/geocode-build/geocode.sqlite"))
    ap.add_argument("--out", default=os.path.expanduser("~/geocode-build/golden.csv"))
    ap.add_argument("--min-pr", type=float, default=0.5, help="이 Pr 이상 후보만(경계+자동병합)")
    ap.add_argument("--per-group", type=int, default=40, help="(decision×유사도×전화×건물) 그룹당 최대")
    ap.add_argument("--max", type=int, default=800, help="전체 샘플 상한")
    args = ap.parse_args()
    if not pathlib.Path(args.db).exists():
        sys.exit(f"DB 없음: {args.db}")
    db = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    pairs = export_pairs(db, min_pr=args.min_pr)
    if not pairs:
        sys.exit(f"후보쌍 0건 (biz 없음/단건 또는 min_pr={args.min_pr} 과대). dedup 입력 확인.")
    sample, groups = _stratify(pairs, args.per_group, args.max)
    sample.sort(key=lambda p: (p["decision"], -p["pr"]))   # 사람이 보기 좋게 decision·pr 정렬
    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w", encoding="utf-8-sig", newline="") as f:   # Excel 한글 호환 BOM
        w = csv.DictWriter(f, fieldnames=COLS); w.writeheader()
        for p in sample:
            w.writerow({**{c: "" for c in COLS}, **{k: v for k, v in p.items() if k in COLS}})
    # 요약
    from collections import Counter
    dec = Counter(p["decision"] for p in pairs)
    print(f"후보쌍(Pr≥{args.min_pr}): {len(pairs):,}  (AUTO {dec.get('AUTO',0):,} · REVIEW {dec.get('REVIEW',0):,})",
          file=sys.stderr)
    print(f"층(그룹) {len(groups)}개 → 샘플 {len(sample):,}건 → {args.out}", file=sys.stderr)
    sdec = Counter(p["decision"] for p in sample)
    print(f"  샘플 구성: AUTO {sdec.get('AUTO',0)} · REVIEW {sdec.get('REVIEW',0)}", file=sys.stderr)
    print("  → label 에 1(같은 점포)/0(다른 점포) 채운 뒤 임계 보정에 사용.", file=sys.stderr)

if __name__ == "__main__":
    main()
