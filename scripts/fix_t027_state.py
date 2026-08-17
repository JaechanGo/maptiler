#!/usr/bin/env python3
"""T027 오염 상태 정정 — 2026-08-10 0바이트 수집이 남긴 sources_state 거짓 기록을 되돌린다.

배경
----
2026-08-10 수집은 VWorld 세션 만료로 **모든 파일을 0바이트로 받고도 성공으로 기록**했다.
그 과정에서 `rmtree(dest)` 선삭제로 staged 원본(parcel·building)이 소실됐는데,
`sources_state` 는 여전히 "2026-08-10 에 최신화됨"이라고 말한다. 이 무증상성이 사고를 덮었다.

고착(freeze) — 정정이 두 갈래여야 하는 이유
-------------------------------------------
`staged/parcel/` 에 0바이트 파일이 1개 남아 있어 `_nonempty_dir`(build-studio.py:468,
`p.is_dir() and any(p.iterdir())`)이 True 를 돌려준다. 저장된 `staged_sig` 도 같은 빈 문자열
SHA256 이라 재계산 서명과 일치해 `allreused == True` 가 된다. 그 결과 run_collect 의
`(not allreused) or (not _nonempty_dir(dest))` 가 거짓이 되어 **재추출 자체를 건너뛴다.**
즉 다음 수집이 "성공"으로 끝나도 손상이 영구 고착된다.

따라서 이 스크립트의 `staged_sig` NULL 화와 0바이트 잔재를 staged 밖으로 옮기는 일은
**반드시 같은 작업 단위에서 함께** 해야 한다. 하나만 하면 고착이 남는다.
(향후 "staged_sig 는 기록으로 남겨두자"고 되돌리면 재발한다 — 이유를 여기 남긴다.)

`latest` 는 건드리지 않는다 — 원천의 최신 게시일이지 우리 보유 상태가 아니다.

사용
----
    python3 scripts/fix_t027_state.py                          # dry-run(기본). 아무것도 쓰지 않는다
    python3 scripts/fix_t027_state.py --dump before.json       # 현재 상태만 덤프하고 종료
    python3 scripts/fix_t027_state.py --apply --dump after.json  # 실제 정정 후 덤프
"""
import argparse
import hashlib
import importlib.util
import json
import pathlib
import sqlite3
import sys
import time

# ── 정정 대상 — 이 두 키 외에는 어떤 경로로도 쓰지 않는다(아래 _assert_target 이 코드로 강제) ──
TARGET_KEYS = ("parcel", "building_db")

# 빈 문자열의 SHA256. 2026-08-10 사고의 지문이다 — staged_sig 의 모든 세그먼트가 이 값이면 오염이다.
EMPTY_SHA256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"

# 정정 후 값. staged_sig·current 는 NULL, 나머지 3개는 _set_validation 이 쓴다.
FIX_STATUS = "collect_failed"
FIX_MSG = "T027: 2026-08-10 0바이트 수집으로 staged 소실. 재수집 필요"

# C-15: 백업이 선행 조건. 이 글롭에 걸리는 파일이 하나도 없으면 --apply 를 거부한다.
BACKUP_GLOB = "build-studio.db.*-preT027"
BACKUP_DIR = pathlib.Path("~/maptiler-rescue").expanduser()

# 읽기 순서를 고정해 전후 덤프를 나란히 비교할 수 있게 한다.
COLUMNS = ("key", "current", "latest", "checked_at", "file", "staged_sig",
           "validation_status", "validation_msg", "validated_at")


def _assert_target(key):
    """대상 키 강제 — 쓰기 직전에 반드시 통과해야 한다."""
    if key not in TARGET_KEYS:
        raise SystemExit(f"중단: 대상이 아닌 키에 쓰려 했다 -> {key!r} (허용: {TARGET_KEYS})")
    return key


def _load_build_studio():
    """모듈명에 하이픈이 있어 일반 import 가 안 된다. 파일 경로로 로드한다.

    BUILD_HOME 은 import 시점에 굳으므로 여기서 환경변수를 건드리지 않는다 — 이 작업의
    대상은 실홈(~/geocode-build)이다. import 자체는 상수 정의와 idle 데몬 스레드뿐이라
    파일시스템·DB 부작용이 없다(확인함).
    """
    path = pathlib.Path(__file__).resolve().with_name("build-studio.py")
    if not path.is_file():
        raise SystemExit(f"중단: build-studio.py 를 찾을 수 없다 -> {path}")
    spec = importlib.util.spec_from_file_location("build_studio", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _ro_conn(db_path):
    """정정 시점 외에는 읽기 전용으로만 연다(C-15)."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _describe_sig(sig):
    """staged_sig 는 값이 아니라 형태만 기록한다(parcel 은 17,029자라 덤프에 넣지 않는다)."""
    if sig is None:
        return {"present": False}
    segs = sig.split(",")
    return {
        "present": True,
        "length": len(sig),
        "prefix24": sig[:24],
        "segments": len(segs),
        "all_empty_hash": all(s == EMPTY_SHA256 for s in segs),
    }


def _row_to_record(row):
    rec = {c: row[c] for c in COLUMNS if c != "staged_sig"}
    rec["staged_sig"] = _describe_sig(row["staged_sig"])
    return rec


def _read_targets(db_path):
    with _ro_conn(db_path) as conn:
        out = {}
        for key in TARGET_KEYS:
            r = conn.execute(
                f"SELECT {','.join(COLUMNS)} FROM sources_state WHERE key = ?", (key,)
            ).fetchone()
            out[key] = r
        return out


def _fingerprint_others(db_path):
    """대상 외 행들의 지문 — 정정이 그 행들을 건드리지 않았음을 사후에 증명한다."""
    with _ro_conn(db_path) as conn:
        rows = conn.execute(
            f"SELECT {','.join(COLUMNS)} FROM sources_state ORDER BY key"
        ).fetchall()
    out = {}
    for r in rows:
        if r["key"] in TARGET_KEYS:
            continue
        blob = repr(tuple(r[c] for c in COLUMNS)).encode()
        out[r["key"]] = hashlib.sha256(blob).hexdigest()
    return out


def _classify(row):
    """현재 상태 판정 — 오염됐는지, 이미 정정됐는지, 예상 밖인지."""
    if row is None:
        return "missing"
    sig = row["staged_sig"]
    if sig is None and row["current"] is None and row["validation_status"] == FIX_STATUS:
        return "corrected"
    if sig and all(s == EMPTY_SHA256 for s in sig.split(",")):
        return "contaminated"
    return "unexpected"


def _find_backups(db_path):
    """백업 존재 확인 + 현재 DB 와 내용이 같은 백업이 있는지 대조."""
    cur_sha = hashlib.sha256(pathlib.Path(db_path).read_bytes()).hexdigest()
    found = []
    for p in sorted(BACKUP_DIR.glob(BACKUP_GLOB)):
        sha = hashlib.sha256(p.read_bytes()).hexdigest()
        found.append({"path": str(p), "size": p.stat().st_size,
                      "sha256": sha, "matches_current": sha == cur_sha})
    return cur_sha, found


def _dump(db_path, path):
    p = pathlib.Path(db_path)
    doc = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "db_path": str(p),
        "db_size_bytes": p.stat().st_size,
        "db_sha256": hashlib.sha256(p.read_bytes()).hexdigest(),
        "target_keys": list(TARGET_KEYS),
        "rows": {},
        "other_rows_fingerprint": _fingerprint_others(db_path),
    }
    for key, row in _read_targets(db_path).items():
        doc["rows"][key] = None if row is None else _row_to_record(row)
        doc.setdefault("classification", {})[key] = _classify(row)
    out = pathlib.Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return doc


def _print_plan(db_path):
    """dry-run 출력 — 무엇을 어떻게 바꿀지 사람이 읽을 수 있게."""
    print(f"대상 DB : {db_path}")
    print(f"대상 키 : {', '.join(TARGET_KEYS)}  (그 외 키는 건드리지 않는다)")
    print()
    rows = _read_targets(db_path)
    for key in TARGET_KEYS:
        row = rows[key]
        state = _classify(row)
        print(f"── {key}  [판정: {state}]")
        if row is None:
            print("   행이 없다.")
            continue
        d = _describe_sig(row["staged_sig"])
        sig_now = ("없음(NULL)" if not d["present"] else
                   f"{d['length']}자 / {d['segments']}세그먼트 / 프리픽스 {d['prefix24']}… / "
                   f"전부 빈해시={d['all_empty_hash']}")
        print(f"   staged_sig        : {sig_now}")
        print("                     → NULL")
        print(f"   current           : {row['current']!r}")
        print("                     → NULL")
        print(f"   validation_status : {row['validation_status']!r}")
        print(f"                     → {FIX_STATUS!r}")
        print(f"   validation_msg    : {row['validation_msg']!r}")
        print(f"                     → {FIX_MSG!r}")
        print(f"   validated_at      : {row['validated_at']!r}")
        print("                     → 정정 시각")
        print(f"   latest            : {row['latest']!r}   ← 변경하지 않는다(원천 게시일)")
        print(f"   checked_at        : {row['checked_at']!r}   ← 변경하지 않는다")
        print(f"   file              : {row['file']!r}   ← 변경하지 않는다")
        print()
    return rows


def main(argv=None):
    ap = argparse.ArgumentParser(description="T027 sources_state 오염 정정")
    ap.add_argument("--apply", action="store_true",
                    help="실제로 쓴다. 없으면 dry-run(기본)")
    ap.add_argument("--dump", metavar="PATH", help="상태를 JSON 으로 덤프할 경로")
    ap.add_argument("--allow-stale-backup", action="store_true",
                    help="현재 DB 와 내용이 일치하는 백업이 없어도 --apply 를 진행한다")
    args = ap.parse_args(argv)

    M = _load_build_studio()
    db_path = pathlib.Path(M.DB_PATH)
    if not db_path.is_file():
        raise SystemExit(f"중단: DB 가 없다 -> {db_path}")

    print(f"=== T027 오염 상태 정정 ({'APPLY' if args.apply else 'DRY-RUN'}) ===")
    rows = _print_plan(db_path)

    # ── C-15: 백업이 선행 조건 ──
    cur_sha, backups = _find_backups(db_path)
    print(f"현재 DB sha256 : {cur_sha}")
    if not backups:
        print(f"백업          : 없음 ({BACKUP_DIR}/{BACKUP_GLOB})")
    for b in backups:
        mark = "일치" if b["matches_current"] else "불일치"
        print(f"백업          : {b['path']}  {b['size']}B  현재DB와 {mark}")
    print()

    states = {k: _classify(rows[k]) for k in TARGET_KEYS}
    if all(s == "corrected" for s in states.values()):
        print("이미 정정된 상태다. 쓸 것이 없다.")
        if args.dump:
            _dump(db_path, args.dump)
            print(f"덤프 저장: {args.dump}")
        return 0
    bad = {k: s for k, s in states.items() if s not in ("contaminated", "corrected")}
    if bad:
        raise SystemExit(f"중단: 예상 밖의 상태다 -> {bad}. 사람이 확인해야 한다")

    if not args.apply:
        print("dry-run 이라 아무것도 쓰지 않았다. 실제 정정은 --apply 를 붙여라.")
        if args.dump:
            _dump(db_path, args.dump)
            print(f"덤프 저장: {args.dump}")
        return 0

    if not backups:
        raise SystemExit(
            f"중단: 백업이 없다. C-15 에 따라 백업 없이는 DML 을 실행하지 않는다.\n"
            f"      {db_path} 를 {BACKUP_DIR}/build-studio.db.<YYYYMMDD-HHMM>-preT027 로 먼저 복사하라")
    if not any(b["matches_current"] for b in backups) and not args.allow_stale_backup:
        raise SystemExit(
            "중단: 현재 DB 와 내용이 일치하는 백업이 없다. 되돌릴 수 없는 정정은 하지 않는다.\n"
            "      지금 상태를 백업하거나, 의도한 것이라면 --allow-stale-backup 을 붙여라")

    before_others = _fingerprint_others(db_path)

    # ── 실제 정정 ──
    # 순서 주의: validation 3컬럼을 먼저 쓴다. 중간에 죽어도 배지가 'collect_failed' 로
    # 남아 사람에게 보이는 쪽이, staged_sig 만 지워지고 낡은 'ok' 가 남는 쪽보다 안전하다.
    changed = {}
    for key in TARGET_KEYS:
        _assert_target(key)
        M._set_validation(key, FIX_STATUS, FIX_MSG)   # validation_status/msg/validated_at 만 upsert

    conn = sqlite3.connect(db_path, timeout=30)
    try:
        for key in TARGET_KEYS:
            _assert_target(key)
            # 값까지 전부 파라미터 바인딩. f-string/% 로 SQL 을 조립하지 않는다.
            cur = conn.execute(
                "UPDATE sources_state SET staged_sig = ?, current = ? WHERE key = ?",
                (None, None, key))
            changed[key] = cur.rowcount
            if cur.rowcount > 1:
                conn.rollback()
                raise SystemExit(f"중단: {key} 에서 {cur.rowcount}행이 걸렸다. key 는 PRIMARY KEY 여야 한다")
        conn.commit()
    finally:
        conn.close()

    for key, n in changed.items():
        print(f"{key}: staged_sig/current UPDATE 변경 행 수 = {n}")
    print(f"validation 3컬럼 upsert = {len(TARGET_KEYS)}건")

    # ── 사후 검증: 대상 외 행이 그대로인가 ──
    after_others = _fingerprint_others(db_path)
    if before_others != after_others:
        diff = sorted(set(before_others) ^ set(after_others)) or \
            [k for k in before_others if before_others[k] != after_others.get(k)]
        raise SystemExit(f"경고: 대상 외 행이 바뀌었다 -> {diff}. 백업에서 복원하라")
    print(f"대상 외 {len(after_others)}개 행 무변경 확인")

    # ── 사후 검증: 대상 행이 의도대로 바뀌었나(별도 읽기전용 연결) ──
    for key, row in _read_targets(db_path).items():
        assert row is not None, key
        assert row["staged_sig"] is None, f"{key}: staged_sig 가 NULL 이 아니다"
        assert row["current"] is None, f"{key}: current 가 NULL 이 아니다"
        assert row["validation_status"] == FIX_STATUS, f"{key}: validation_status 불일치"
        print(f"{key}: staged_sig=NULL / current=NULL / "
              f"validation_status={row['validation_status']} / latest={row['latest']!r}")

    if args.dump:
        _dump(db_path, args.dump)
        print(f"덤프 저장: {args.dump}")

    print()
    print("주의: 이 정정만으로는 고착이 풀리지 않는다. staged/parcel·staged/gis 의 0바이트")
    print("      잔재를 디렉터리 밖으로 옮겨 _nonempty_dir 이 False 가 되게 해야 한다.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
