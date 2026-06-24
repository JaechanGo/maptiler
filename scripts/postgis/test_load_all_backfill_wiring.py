#!/usr/bin/env python3
"""load-all.sh parcel→backfill_parcel_jibun 자동체인 배선 단위테스트 (실 DB 비의존).

대상: scripts/postgis/load-all.sh 의 parcel 단계가 `--fresh` 적재 성공 직후
`backfill_parcel_jibun.sql` 을 자동 체인하는지(그리고 geom_pt 는 제외·opt-out·실패전파·
수동 STEPS=backfill 경로 보존·혼재단계 순서·R2 표면 보존)를 호출 유무/순서/조건만으로 검증한다.

핵심 제약(§5/§2.1): load-all.sh 는 `load_parcel.sh`·`apply-schema.sh`·`*.sql` 등을
`"$HERE/…"` **절대경로**로 부른다 → PATH 앞단 스텁으로는 가로챌 수 없다. 따라서 2단 구성:
  (a) 임시 디렉토리에 load-all.sh + _pg-env.sh 를 복사하고, **같은 디렉토리에 스텁 형제 파일**
      (load_parcel.sh·load_building.sh)을 둔다 → 복사본 HERE 가 임시디렉토리를 가리켜
      "$HERE/…" 절대경로 호출이 스텁으로 향한다.
  (b) **bare 호출 psql·python3·ogr2ogr** 는 PATH 앞단 bin 디렉토리 스텁으로 가로챈다.
      psql 스텁은 `-f <경로>` 인자의 basename 만 호출로그에 기록(실제 SQL 파일·DB 연결 불필요).

구동방식은 test_build_studio.py 와 동일(stdlib unittest):
  python3 scripts/postgis/test_load_all_backfill_wiring.py
  또는  python3 -m unittest scripts.postgis.test_load_all_backfill_wiring -v
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))          # scripts/postgis
_SCRIPTS = os.path.dirname(_HERE)                           # scripts
_LOAD_ALL = os.path.join(_HERE, "load-all.sh")
_PG_ENV = os.path.join(_HERE, "_pg-env.sh")
_DISPLAY_PARITY = os.path.join(_SCRIPTS, "13f-display-parity.py")

# ── 스텁 본문 ─────────────────────────────────────────────────────
# 형제 스크립트 스텁: 호출을 로그에 append, exit 코드는 env 로 제어.
_SIBLING_STUB = """#!/usr/bin/env bash
echo "{tag} $*" >> "$CALLLOG"
exit "${{{exitvar}:-0}}"
"""

# bare psql 스텁: -f <경로> 가 있으면 basename 만 로깅(SQL 파일/DB 불필요), 그 외(-c 등)는 무로깅.
_PSQL_STUB = """#!/usr/bin/env bash
f=""
prev=""
for a in "$@"; do
  [ "$prev" = "-f" ] && f="$a"
  prev="$a"
done
[ -n "$f" ] && echo "PSQL_F $(basename "$f")" >> "$CALLLOG"
exit "${STUB_PSQL_EXIT:-0}"
"""

# bare python3/ogr2ogr 스텁(보강용; 본 케이스는 직접 단언 안 함): 호출만 로깅.
_BARE_STUB = """#!/usr/bin/env bash
echo "{tag} $*" >> "$CALLLOG"
exit 0
"""


def _write_exec(path, content):
    with open(path, "w") as f:
        f.write(content)
    os.chmod(path, 0o755)


class LoadAllBackfillWiring(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="load_all_wiring_")

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    # ── 실행 하니스 ──────────────────────────────────────────────
    def _run(self, steps, *, parcel_exit=0, building_exit=0, skip_backfill=False,
             make_parcel=True, make_gis=False):
        """격리 런 디렉토리를 새로 만들어 load-all.sh 복사본을 STEPS 로 실행.
        반환: (returncode, [호출로그 라인…])."""
        run_dir = tempfile.mkdtemp(dir=self.root)
        here = os.path.join(run_dir, "postgis")     # 복사본 HERE
        bindir = os.path.join(run_dir, "bin")       # PATH 앞단 스텁
        build_home = os.path.join(run_dir, "buildhome")
        calllog = os.path.join(run_dir, "calls.log")
        os.makedirs(here)
        os.makedirs(bindir)
        os.makedirs(build_home)

        # (a) load-all.sh + _pg-env.sh 복사 + 형제 스텁
        shutil.copy(_LOAD_ALL, os.path.join(here, "load-all.sh"))
        shutil.copy(_PG_ENV, os.path.join(here, "_pg-env.sh"))
        _write_exec(os.path.join(here, "load_parcel.sh"),
                    _SIBLING_STUB.format(tag="LOAD_PARCEL", exitvar="STUB_LOAD_PARCEL_EXIT"))
        _write_exec(os.path.join(here, "load_building.sh"),
                    _SIBLING_STUB.format(tag="LOAD_BUILDING", exitvar="STUB_LOAD_BUILDING_EXIT"))

        # (b) PATH 스텁(bare 호출)
        _write_exec(os.path.join(bindir, "psql"), _PSQL_STUB)
        _write_exec(os.path.join(bindir, "python3"), _BARE_STUB.format(tag="PYTHON3"))
        _write_exec(os.path.join(bindir, "ogr2ogr"), _BARE_STUB.format(tag="OGR2OGR"))

        # 소스 디렉토리 게이팅(존재해야 해당 단계 진입)
        if make_parcel:
            os.makedirs(os.path.join(build_home, "staged", "parcel"))
        if make_gis:
            os.makedirs(os.path.join(build_home, "staged", "gis"))

        env = dict(os.environ)
        env["PATH"] = bindir + os.pathsep + env.get("PATH", "")
        env["CALLLOG"] = calllog
        env["BUILD_HOME"] = build_home
        env["STEPS"] = steps
        env["STUB_LOAD_PARCEL_EXIT"] = str(parcel_exit)
        env["STUB_LOAD_BUILDING_EXIT"] = str(building_exit)
        if skip_backfill:
            env["PARCEL_SKIP_BACKFILL"] = "1"
        else:
            env.pop("PARCEL_SKIP_BACKFILL", None)

        proc = subprocess.run(
            ["bash", os.path.join(here, "load-all.sh")],
            env=env, cwd=run_dir, capture_output=True, text=True,
        )
        lines = []
        if os.path.exists(calllog):
            with open(calllog) as f:
                lines = [ln.rstrip("\n") for ln in f if ln.strip()]
        return proc.returncode, lines

    @staticmethod
    def _idx(lines, predicate):
        for i, ln in enumerate(lines):
            if predicate(ln):
                return i
        return -1

    # ── 케이스 1: 구문 ────────────────────────────────────────────
    def test_01_bash_syntax_ok(self):
        proc = subprocess.run(["bash", "-n", _LOAD_ALL], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)

    # ── 케이스 2: --fresh 성공 시 jibun 체인됨 / geom_pt 부재 ────────
    def test_02_parcel_success_chains_jibun_only(self):
        rc, lines = self._run("parcel", parcel_exit=0)
        self.assertTrue(any("LOAD_PARCEL" in ln for ln in lines), lines)
        self.assertTrue(any("PSQL_F backfill_parcel_jibun.sql" in ln for ln in lines),
                        f"parcel 적재 성공 후 backfill_parcel_jibun.sql 자동 체인 기대: {lines}")
        self.assertFalse(any("backfill_geom_pt.sql" in ln for ln in lines),
                         f"geom_pt 는 자동 체인 제외(결정2): {lines}")

    # ── 케이스 3: opt-out ────────────────────────────────────────
    def test_03_skip_backfill_opt_out(self):
        rc, lines = self._run("parcel", parcel_exit=0, skip_backfill=True)
        self.assertTrue(any("LOAD_PARCEL" in ln for ln in lines), lines)
        self.assertFalse(any("backfill_parcel_jibun.sql" in ln for ln in lines),
                         f"PARCEL_SKIP_BACKFILL=1 면 자동 체인 생략: {lines}")

    # ── 케이스 4: parcel 실패 시 미체인 + 종료코드 1 ───────────────
    def test_04_parcel_fail_no_chain_and_exit1(self):
        rc, lines = self._run("parcel", parcel_exit=1)
        self.assertFalse(any("backfill_parcel_jibun.sql" in ln for ln in lines),
                         f"parcel 적재 실패 시 backfill 미체인: {lines}")
        self.assertEqual(rc, 1, "parcel 적재 실패는 fail=1 → 종료코드 1 로 전파")

    # ── 케이스 5: parcel 미포함 단계엔 영향 없음 ───────────────────
    def test_05_unrelated_step_no_backfill(self):
        rc, lines = self._run("geocode", make_parcel=False)
        self.assertFalse(any("backfill_parcel_jibun.sql" in ln for ln in lines),
                         f"parcel 미포함 STEPS 엔 backfill 미발생: {lines}")
        self.assertFalse(any("backfill_geom_pt.sql" in ln for ln in lines), lines)

    # ── 케이스 6: 수동 STEPS=backfill 경로 보존(jibun+geom_pt) ──────
    def test_06_manual_backfill_step_preserved(self):
        rc, lines = self._run("backfill", make_parcel=False)
        self.assertTrue(any("PSQL_F backfill_parcel_jibun.sql" in ln for ln in lines),
                        f"수동 backfill 경로에 jibun 보존: {lines}")
        self.assertTrue(any("PSQL_F backfill_geom_pt.sql" in ln for ln in lines),
                        f"수동 backfill 경로에 geom_pt 보존: {lines}")

    # ── 케이스 7: 혼재 단계 — 체인 1회·building 앞 ─────────────────
    def test_07_mixed_steps_chain_once_before_building(self):
        rc, lines = self._run("parcel building", parcel_exit=0, make_gis=True)
        jibun_hits = [ln for ln in lines if "backfill_parcel_jibun.sql" in ln]
        self.assertEqual(len(jibun_hits), 1, f"체인은 정확히 1회: {lines}")
        i_jibun = self._idx(lines, lambda ln: "backfill_parcel_jibun.sql" in ln)
        i_build = self._idx(lines, lambda ln: "LOAD_BUILDING" in ln)
        self.assertNotEqual(i_jibun, -1, lines)
        self.assertNotEqual(i_build, -1, f"building 단계도 실행되어야: {lines}")
        self.assertLess(i_jibun, i_build, f"체인이 building 단계 앞에 위치해야: {lines}")

    # ── 케이스 8: R2 표면 — STEPS 기본값에 backfill 토큰 미포함 ─────
    def test_08_default_steps_no_backfill_token(self):
        with open(_LOAD_ALL) as f:
            src = f.read()
        default_line = next((ln for ln in src.splitlines()
                             if re.search(r'STEPS="\$\{STEPS:-', ln)), None)
        self.assertIsNotNone(default_line, "STEPS 기본값 라인을 찾지 못함")
        self.assertNotIn("backfill", default_line,
                         f"STEPS 기본값에 backfill 토큰이 들어가면 안 됨(T004/R2): {default_line}")

    # ── 케이스 9: 게이트 스크립트 무회귀 ──────────────────────────
    def test_09_display_parity_selftest(self):
        proc = subprocess.run([sys.executable, _DISPLAY_PARITY, "--selftest"],
                              capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("selftest OK", proc.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
