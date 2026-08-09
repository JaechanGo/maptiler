#!/usr/bin/env python3
"""ri_backfill_s5_apply.py — T018 Phase 1 / S5. address.bcode 리 자리 배치 백필.

_ri_backfill_todo 를 id 오름차순으로 청크 단위 UPDATE 한다. 약 674만 행.

설계 요점
---------
* **청크는 행 수 기준**(id 범위 기준이 아니다). address.id 는 구간별 밀도 편차가 커서
  고정 id 폭으로 자르면 어떤 청크는 수 행, 어떤 청크는 수십만 행이 된다.
  todo 를 `ORDER BY id LIMIT n` 으로 훑어 그 구간의 max(id) 를 상한으로 잡는다.
* **청크 1개 = 트랜잭션 1개.** UPDATE address / UPDATE todo.done / UPSERT last_id
  세 문장이 한 원자 단위다. 중간에 죽어도 last_id 와 done 이 서로 어긋나지 않는다.
* **재개 가능.** _ri_backfill_progress.last_id 부터 이어서 돈다.
* **HOT 갱신 불가.** address 는 fillfactor 100 에 인덱스 13개(GIN 5·GiST 2)라
  bcode 만 바꿔도 새 힙 튜플 + 인덱스 13개 전부에 새 엔트리가 들어간다.
  → 데드 튜플이 빠르게 쌓이므로 임계치 초과 시 청크 사이에 VACUUM 을 끼운다
    (ANALYZE 없이 — 통계는 종료 후 한 번만).

롤백
----
    BEGIN;
      UPDATE address a SET bcode = t.old_bcode
        FROM _ri_backfill_todo t WHERE a.id = t.id AND t.done;
    COMMIT;
    VACUUM (ANALYZE) address;
    DELETE FROM _ri_backfill_progress WHERE k='last_id';   -- ← S3 재실행 가드 해제. 필수.

사용
----
    scripts/postgis/ri_backfill_s5_apply.py            # 실행
    scripts/postgis/ri_backfill_s5_apply.py --dry-run  # 청크 경계만 계산, UPDATE 안 함
    scripts/postgis/ri_backfill_s5_apply.py --chunk 100000
"""
from __future__ import annotations

import argparse
import os
import sys
import time

import psycopg

CHUNK_DEFAULT = 200_000
DEAD_TUP_THRESHOLD = 1_500_000


def conninfo() -> str:
    return (
        f"host={os.environ.get('PGHOST', 'localhost')} "
        f"port={os.environ.get('PGPORT', '5433')} "
        f"user={os.environ.get('PGUSER', 'cuvia')} "
        f"dbname={os.environ.get('PGDATABASE', 'cuvia')} "
        f"password={os.environ.get('PGPASSWORD', 'cuvia')}"
    )


def fmt_hms(sec: float) -> str:
    sec = max(0, int(sec))
    return f"{sec // 3600:02d}:{sec % 3600 // 60:02d}:{sec % 60:02d}"


def preflight(cur) -> tuple[int, int]:
    """선행조건 확인. (전체 대상 수, 남은 수) 반환."""
    cur.execute("SELECT to_regclass('public._ri_backfill_todo')")
    if cur.fetchone()[0] is None:
        sys.exit("[FATAL] _ri_backfill_todo 가 없다. S3 를 먼저 실행하라.")

    # S4 백업 없이는 절대 진행하지 않는다 (지시서 원칙 2).
    cur.execute("SELECT to_regclass('public.address_bcode_bak_20260810')")
    if cur.fetchone()[0] is None:
        sys.exit("[FATAL] 백업 address_bcode_bak_20260810 이 없다. S4 를 건너뛰고 S5 를 실행할 수 없다.")
    cur.execute("SELECT count(*) FROM address_bcode_bak_20260810")
    n_bak = cur.fetchone()[0]
    cur.execute("SELECT count(*) FROM address WHERE kind='addr'")
    n_src = cur.fetchone()[0]
    if n_bak != n_src:
        sys.exit(f"[FATAL] 백업 {n_bak:,} 행 ≠ 원본 {n_src:,} 행. 백업이 불완전하다.")

    cur.execute("SELECT count(*), count(*) FILTER (WHERE NOT done) FROM _ri_backfill_todo")
    total, remain = cur.fetchone()
    if total == 0:
        sys.exit("[FATAL] _ri_backfill_todo 가 비었다.")
    print(f"[ri-backfill] 백업 {n_bak:,} 행 확인 · 대상 {total:,} 행 (남은 {remain:,})")
    return total, remain


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--chunk", type=int, default=CHUNK_DEFAULT)
    ap.add_argument("--dry-run", action="store_true", help="청크 경계만 계산하고 UPDATE 는 하지 않는다")
    ap.add_argument("--max-chunks", type=int, default=0, help="0 이면 전량. 시험 실행용")
    args = ap.parse_args()

    conn = psycopg.connect(conninfo())
    conn.autocommit = True          # 트랜잭션은 BEGIN/COMMIT 으로 직접 관리한다(VACUUM 때문).
    cur = conn.cursor()

    total, remain = preflight(cur)

    # 진행 상태 테이블 — last_id 초기값은 min(id)-1.
    cur.execute("CREATE TABLE IF NOT EXISTS _ri_backfill_progress (k text PRIMARY KEY, v text)")
    cur.execute("""
        COMMENT ON TABLE _ri_backfill_progress IS
          'T018 S5: 배치 백필 재개 지점. 롤백 후에는 반드시 last_id 행을 지울 것(S3 재실행 가드).'
    """)
    if not args.dry_run:
        cur.execute("""
            INSERT INTO _ri_backfill_progress(k,v)
            SELECT 'last_id', (min(id)-1)::text FROM _ri_backfill_todo
            ON CONFLICT (k) DO NOTHING
        """)
    cur.execute("SELECT v::bigint FROM _ri_backfill_progress WHERE k='last_id'")
    row = cur.fetchone()
    if row is None:                          # --dry-run 최초 실행
        cur.execute("SELECT min(id)-1 FROM _ri_backfill_todo")
        row = cur.fetchone()
    lo = row[0]

    done_before = total - remain
    n_chunks_est = -(-remain // args.chunk)  # 올림
    print(f"[ri-backfill] 청크 {args.chunk:,} 행 · 예상 {n_chunks_est} 청크 · last_id={lo:,}"
          f"{' · DRY-RUN' if args.dry_run else ''}")

    t_start = time.monotonic()
    cum = 0
    idx = 0
    eta_fixed: float | None = None           # 첫 3청크 실측으로 확정하는 ETA(초)

    while True:
        if args.max_chunks and idx >= args.max_chunks:
            print(f"[ri-backfill] --max-chunks {args.max_chunks} 도달 — 중단")
            break

        cur.execute(
            """SELECT max(id) FROM (
                 SELECT id FROM _ri_backfill_todo
                  WHERE id > %(lo)s AND NOT done
                  ORDER BY id LIMIT %(chunk)s
               ) s""",
            {"lo": lo, "chunk": args.chunk},
        )
        hi = cur.fetchone()[0]
        if hi is None:
            break

        idx += 1
        t0 = time.monotonic()

        if args.dry_run:
            cur.execute(
                "SELECT count(*) FROM _ri_backfill_todo WHERE id > %(lo)s AND id <= %(hi)s AND NOT done",
                {"lo": lo, "hi": hi},
            )
            n_upd = cur.fetchone()[0]
        else:
            with conn.transaction():
                cur.execute(
                    """UPDATE address a SET bcode = t.new_bcode
                         FROM _ri_backfill_todo t
                        WHERE a.id = t.id AND t.id > %(lo)s AND t.id <= %(hi)s""",
                    {"lo": lo, "hi": hi},
                )
                n_upd = cur.rowcount
                cur.execute(
                    "UPDATE _ri_backfill_todo SET done = true WHERE id > %(lo)s AND id <= %(hi)s",
                    {"lo": lo, "hi": hi},
                )
                cur.execute(
                    """INSERT INTO _ri_backfill_progress(k,v) VALUES ('last_id', %(hi)s)
                         ON CONFLICT (k) DO UPDATE SET v = EXCLUDED.v""",
                    {"hi": str(hi)},
                )

        dt = time.monotonic() - t0
        cum += n_upd
        elapsed = time.monotonic() - t_start

        # ETA: 첫 3청크 평균으로 고정하고, 이후엔 누적 평균으로 계속 보정한다.
        rate = cum / elapsed if elapsed > 0 else 0
        eta = (remain - cum) / rate if rate > 0 else 0
        if idx == 3 and eta_fixed is None:
            eta_fixed = eta
            print(f"[ri-backfill] ★ 첫 3청크 실측 ETA 확정: {fmt_hms(eta)} "
                  f"(평균 {rate:,.0f} rows/s, 3청크 {elapsed:.1f}s)")

        pct = (done_before + cum) / total * 100
        print(f"[ri-backfill] chunk {idx:03d}/{n_chunks_est:03d}  id {lo:,}..{hi:,}  "
              f"updated {n_upd:,}  {dt:.1f}s")
        print(f"              누계 {done_before + cum:,}/{total:,} ({pct:.1f}%)  ETA {fmt_hms(eta)}")
        sys.stdout.flush()

        lo = hi

        if not args.dry_run:
            cur.execute(
                "SELECT n_dead_tup FROM pg_stat_user_tables WHERE relname='address'"
            )
            r = cur.fetchone()
            dead = r[0] if r and r[0] is not None else 0
            if dead > DEAD_TUP_THRESHOLD:
                tv = time.monotonic()
                cur.execute("VACUUM address")     # ANALYZE 없이 — 통계는 종료 후 1회.
                print(f"[ri-backfill]   VACUUM address (dead {dead:,}) {time.monotonic()-tv:.1f}s")
                sys.stdout.flush()

    elapsed = time.monotonic() - t_start
    rate = cum / elapsed if elapsed > 0 else 0
    print(f"[ri-backfill] 완료 — {idx} 청크 · {cum:,} 행 · {fmt_hms(elapsed)} "
          f"(평균 {rate:,.0f} rows/s)")
    if eta_fixed is not None:
        diff = elapsed - eta_fixed
        print(f"[ri-backfill] 첫3청크 ETA {fmt_hms(eta_fixed)} vs 실제 {fmt_hms(elapsed)} "
              f"(차이 {'+' if diff >= 0 else '-'}{fmt_hms(abs(diff))})")

    if not args.dry_run:
        tv = time.monotonic()
        cur.execute("VACUUM (ANALYZE) address")
        print(f"[ri-backfill] VACUUM (ANALYZE) address {time.monotonic()-tv:.1f}s")

    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
