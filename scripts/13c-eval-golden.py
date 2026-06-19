#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""골든셋 평가 — 라벨된 CSV(13b 출력 + 사람이 label 0/1)로 dedup_er 의 precision/recall 측정 + 임계 스윕.

입력: 13b-golden-extract.py 가 뽑고 사람이 label(1=같은점포/0=다른점포) 채운 CSV.
출력(stdout): ① 현 모델(decision=AUTO 병합) 혼동행렬·precision/recall, ② REVIEW 밴드 라벨분포,
  ③ Pr 임계 스윕(precision≥--min-precision 제약하 F1 최대 τ 추천), ④ 오병합(FP) 신호 진단.
사용: python3 13c-eval-golden.py --golden golden.csv [--min-precision 0.97] [--auto 0.90]
주의: recall 은 '후보집합(Pr≥min_pr) 내' 값 — Pr<min_pr 인 진짜중복은 골든셋에 없어 전체재현율은 과대평가될 수 있음.
"""
import argparse, csv, sys
from collections import Counter

def _f1(p, r): return 2*p*r/(p+r) if (p+r) else 0.0

def _metrics(preds, labels):
    tp = sum(1 for p, l in zip(preds, labels) if p and l)
    fp = sum(1 for p, l in zip(preds, labels) if p and not l)
    fn = sum(1 for p, l in zip(preds, labels) if not p and l)
    tn = sum(1 for p, l in zip(preds, labels) if not p and not l)
    prec = tp/(tp+fp) if (tp+fp) else 1.0
    rec = tp/(tp+fn) if (tp+fn) else 1.0
    return dict(tp=tp, fp=fp, fn=fn, tn=tn, precision=prec, recall=rec, f1=_f1(prec, rec))

def load(path):
    rows = []
    for r in csv.DictReader(open(path, encoding="utf-8-sig")):
        lab = (r.get("label") or "").strip()
        if lab not in ("0", "1"):
            continue
        rows.append({"pr": float(r["pr"]), "decision": r["decision"], "label": int(lab),
                     "name_sim": float(r.get("name_sim") or 0), "phone_eq": int(r.get("phone_eq") or 0),
                     "same_bld": int(r.get("same_bld") or 0),
                     "name_a": r.get("name_a", ""), "name_b": r.get("name_b", "")})
    return rows

def report(rows, min_precision=0.97, auto=0.90):
    out = []
    n = len(rows); pos = sum(r["label"] for r in rows)
    out.append(f"라벨 {n}건 (양성 {pos} · 음성 {n-pos})")
    if not n:
        return "\n".join(out + ["(라벨된 행 없음 — CSV의 label 컬럼을 채우세요)"])
    labels = [r["label"] for r in rows]
    # ① 현 모델: decision=='AUTO' 가 병합
    m = _metrics([r["decision"] == "AUTO" for r in rows], labels)
    out.append("\n[① 현 모델 (decision=AUTO 병합)]")
    out.append(f"  TP {m['tp']} · FP {m['fp']} · FN {m['fn']} · TN {m['tn']}")
    out.append(f"  precision {m['precision']:.3f} · recall(후보내) {m['recall']:.3f} · F1 {m['f1']:.3f}")
    # ② REVIEW 밴드 라벨 분포 → 검수밴드가 실제 양/음 어느 쪽인지
    rv = [r for r in rows if r["decision"] == "REVIEW"]
    if rv:
        rp = sum(r["label"] for r in rv)
        out.append(f"\n[② REVIEW 밴드] {len(rv)}건 중 양성 {rp} · 음성 {len(rv)-rp}"
                   f"  → {'대부분 양성=AUTO로 내려도 됨' if rp>=0.8*len(rv) else '대부분 음성=NO로 올려도 됨' if rp<=0.2*len(rv) else '혼재=사람검수 유지'}")
    # ③ Pr 임계 스윕 (병합 = pr>=τ; 게이트 미반영 what-if)
    out.append(f"\n[③ Pr 임계 스윕] (병합=pr≥τ, precision≥{min_precision} 제약하 F1 최대)")
    out.append("   τ     prec   recall   F1   (TP/FP/FN)")
    best = None
    taus = sorted({round(0.50 + i*0.02, 2) for i in range(26)})  # 0.50~1.00
    for tau in taus:
        mm = _metrics([r["pr"] >= tau for r in rows], labels)
        mark = ""
        if mm["precision"] >= min_precision and (best is None or mm["f1"] > best[1]["f1"]):
            best = (tau, mm)
        out.append(f"  {tau:.2f}  {mm['precision']:.3f}  {mm['recall']:.3f}  {mm['f1']:.3f}   "
                   f"({mm['tp']}/{mm['fp']}/{mm['fn']}){' ←현재' if abs(tau-auto)<1e-9 else ''}")
    if best:
        out.append(f"  ⇒ 추천 τ={best[0]:.2f} (precision {best[1]['precision']:.3f} · recall {best[1]['recall']:.3f} "
                   f"· F1 {best[1]['f1']:.3f})  현재 AUTO={auto:.2f}")
    else:
        out.append(f"  ⇒ precision≥{min_precision} 만족하는 τ 없음 — 점수식/신호 보강 필요(전화 IDF·주소TF 등)")
    # ④ 오병합(FP) 신호 진단 — 현 모델 기준
    fps = [r for r in rows if r["decision"] == "AUTO" and r["label"] == 0]
    if fps:
        out.append(f"\n[④ 오병합(FP) {len(fps)}건 신호 진단]")
        out.append(f"  전화일치 {sum(r['phone_eq'] for r in fps)} · 건물일치 {sum(r['same_bld'] for r in fps)}"
                   f" · 이름유사<0.6 {sum(1 for r in fps if r['name_sim']<0.6)}")
        for r in fps[:5]:
            out.append(f"    · '{r['name_a']}' ↔ '{r['name_b']}' (pr={r['pr']:.2f}, sim={r['name_sim']:.2f},"
                       f" phone={r['phone_eq']}, bld={r['same_bld']})")
    out.append("\n주의: recall 은 후보집합(Pr≥min_pr) 내 값. Pr<min_pr 진짜중복은 골든셋에 없어 전체재현율은 별도 추정 필요.")
    return "\n".join(out)

def _selftest():
    # 합성 라벨셋: 알려진 TP/FP/FN 으로 지표 검산
    rows = [
        {"pr":0.99,"decision":"AUTO","label":1,"name_sim":0.9,"phone_eq":1,"same_bld":1,"name_a":"a","name_b":"a"},  # TP
        {"pr":0.95,"decision":"AUTO","label":0,"name_sim":0.1,"phone_eq":1,"same_bld":0,"name_a":"x","name_b":"y"},  # FP
        {"pr":0.70,"decision":"REVIEW","label":1,"name_sim":0.4,"phone_eq":0,"same_bld":0,"name_a":"c","name_b":"c"},# FN(현모델 미병합)
        {"pr":0.55,"decision":"REVIEW","label":0,"name_sim":0.3,"phone_eq":0,"same_bld":0,"name_a":"d","name_b":"e"},# TN
    ]
    m = _metrics([r["decision"]=="AUTO" for r in rows], [r["label"] for r in rows])
    ok = (m["tp"],m["fp"],m["fn"],m["tn"])==(1,1,1,1) and abs(m["precision"]-0.5)<1e-9 and abs(m["recall"]-0.5)<1e-9
    txt = report(rows, min_precision=0.97, auto=0.90)
    ok = ok and "추천 τ" in txt or "만족하는 τ 없음" in txt
    print(txt); print("="*50); print("EVAL SELFTEST:", "PASS ✓" if ok else "FAIL ✗")
    return ok

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--golden", help="라벨된 골든셋 CSV(13b 출력 + label 채움)")
    ap.add_argument("--min-precision", type=float, default=0.97)
    ap.add_argument("--auto", type=float, default=0.90, help="현 AUTO 임계(표시용)")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        sys.exit(0 if _selftest() else 1)
    if not args.golden:
        ap.error("--golden CSV 필요 (또는 --selftest)")
    rows = load(args.golden)
    print(report(rows, args.min_precision, args.auto))

if __name__ == "__main__":
    main()
