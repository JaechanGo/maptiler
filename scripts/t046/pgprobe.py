#!/usr/bin/env python3
"""T046 §3.1 — PostGIS 조회 실행층. `docker exec psql` 래퍼.

호스트에 psycopg 가 없다(§1.7 실측). 컨테이너의 psql 을 통해 조회한다.

## 구분자

필드 구분자로 `\\x01`, 레코드 구분자로 개행, NULL 마커로 `\\x02` 를 쓴다.
주소 문자열에 `|` 나 탭이 섞여 있어도 파싱이 깨지지 않는다.

## SQL 전달 경로

`-c` 인자가 아니라 **stdin** 으로 넘긴다. 1,000 키 배치의 `VALUES` 목록은
40 KB 를 넘길 수 있고, 인자 길이 상한에 걸리면 조용히 잘리는 것이 아니라
셸 계층에서 실패한다. stdin 에는 그런 상한이 없다.

`ON_ERROR_STOP=1` 로 첫 오류에서 멈춘다 — 오류를 빈 결과로 오독하면
분모가 조용히 틀어진다.
"""
import os
import subprocess

__all__ = ["run_sql", "run_sql_raw", "explain", "PgError", "DOCKER", "CONTAINER"]

DOCKER = os.environ.get("T046_DOCKER", "/opt/homebrew/bin/docker")
CONTAINER = os.environ.get("T046_PG_CONTAINER", "server-postgis-1")
PGUSER = os.environ.get("T046_PG_USER", "cuvia")
PGDB = os.environ.get("T046_PG_DB", "cuvia")

FS = "\x01"          # 필드 구분자
NULLMARK = "\x02"    # NULL 마커 — 빈 문자열과 구분한다
DEFAULT_TIMEOUT = 180.0


class PgError(RuntimeError):
    """psql 이 0 이 아닌 코드로 끝났다."""


def _argv():
    return [
        DOCKER, "exec", "-i", CONTAINER,
        "psql", "-U", PGUSER, "-d", PGDB,
        "-v", "ON_ERROR_STOP=1",
        "-t",            # 헤더·행수 푸터 제거
        "-A",            # 정렬 없는 출력
        "-F", FS,
        "-P", "null=" + NULLMARK,
    ]


def run_sql_raw(sql, timeout=DEFAULT_TIMEOUT):
    """psql stdout 전문을 돌려준다."""
    proc = subprocess.run(
        _argv(),
        input=sql.encode("utf-8"),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
    )
    if proc.returncode != 0:
        raise PgError(
            "psql exit=%d\n--- stderr ---\n%s"
            % (proc.returncode, proc.stderr.decode("utf-8", "replace")[-4000:])
        )
    return proc.stdout.decode("utf-8", "replace")


def run_sql(sql, timeout=DEFAULT_TIMEOUT):
    """SQL 을 실행하고 `[[셀, …], …]` 을 돌려준다. NULL 은 `None`."""
    out = run_sql_raw(sql, timeout=timeout)
    rows = []
    for line in out.split("\n"):
        if not line:
            continue
        rows.append([None if c == NULLMARK else c for c in line.split(FS)])
    return rows


def explain(sql, analyze=False, timeout=DEFAULT_TIMEOUT):
    """실행계획 전문. 파티션 pruning 확인용(§1.12)."""
    head = "EXPLAIN (ANALYZE, BUFFERS)" if analyze else "EXPLAIN"
    return run_sql_raw("%s %s" % (head, sql), timeout=timeout)
