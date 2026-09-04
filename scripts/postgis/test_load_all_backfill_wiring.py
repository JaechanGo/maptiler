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

# bare ogr2ogr 스텁(보강용; 본 케이스는 직접 단언 안 함): 호출만 로깅.
_BARE_STUB = """#!/usr/bin/env bash
echo "{tag} $*" >> "$CALLLOG"
exit 0
"""

# bare python3 스텁: 호출을 로깅하고 **스크립트별로** 종료코드를 제어한다.
#   [T026] (c) load_lawd_code.py 실패 → (d)(e) 미호출, (f) v2 실패 → 인천 SQL 미호출을
#   각각 독립으로 재현해야 해서 전역 exit 하나로는 부족하다.
#   ⚠ 패턴 순서 주의: `*load_lawd_code*` 는 v2 도 함께 물므로 **v2 를 먼저** 판정한다.
_PYTHON3_STUB = """#!/usr/bin/env bash
echo "PYTHON3 $*" >> "$CALLLOG"
case " $* " in
  *load_lawd_code_v2.py*) exit "${STUB_PY_LAWD_V2_EXIT:-0}" ;;
  *load_lawd_code.py*)    exit "${STUB_PY_LAWD_CODE_EXIT:-0}" ;;
esac
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
    def _run(self, steps, **kw):
        """반환: (returncode, [호출로그 라인…]). 표준출력이 필요하면 _run_full 을 쓴다."""
        rc, lines, _out = self._run_full(steps, **kw)
        return rc, lines

    def _run_full(self, steps, *, parcel_exit=0, building_exit=0, skip_backfill=False,
                  make_parcel=True, make_gis=False,
                  lawd_code_exit=0, lawd_v2_exit=0, make_v2_assets=True):
        """격리 런 디렉토리를 새로 만들어 load-all.sh 복사본을 STEPS 로 실행.
        반환: (returncode, [호출로그 라인…], stdout+stderr).

        [T026] 추가 파라미터
          lawd_code_exit / lawd_v2_exit : (c)·(f) 파이썬 로더의 종료코드(부분 갱신 방지 경로 재현)
          make_v2_assets                : (f) 의 **파일 존재 가드** 재현 — False 면 두 자산을 두지
                                          않아 인천 치환 단계가 통째로 건너뛰어지는지 확인한다.
        """
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
        _write_exec(os.path.join(bindir, "python3"), _PYTHON3_STUB)
        _write_exec(os.path.join(bindir, "ogr2ogr"), _BARE_STUB.format(tag="OGR2OGR"))

        # (c) [T026] (f) 의 파일 존재 가드 대상 자산. 내용은 불필요하다 —
        #     .py 는 PATH 의 python3 스텁이, .sql 은 psql 스텁이 각각 가로채기 때문이다.
        if make_v2_assets:
            for name in ("load_lawd_code_v2.py", "build_incheon_remap_from_old_lawdcd.sql"):
                with open(os.path.join(here, name), "w") as f:
                    f.write("")

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
        env["STUB_PY_LAWD_CODE_EXIT"] = str(lawd_code_exit)
        env["STUB_PY_LAWD_V2_EXIT"] = str(lawd_v2_exit)
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
        return proc.returncode, lines, (proc.stdout or "") + (proc.stderr or "")

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

    # ══════════════════════════════════════════════════════════════
    #  [T026] lawd 단계 배선 — (c) lawd_code / (d) lawd_ri / (e) sido_remap / (f) 인천 sgg_remap
    #
    #  왜 여기에 붙이는가: 이 파일이 이미 "load-all.sh 의 단계 배선을 실 DB 없이 호출
    #  유무·순서·조건만으로 검증"하는 하니스를 갖고 있다. 인천 치환도 결국 같은 종류의
    #  배선 회귀(조용한 미배선)를 막는 일이라 하니스를 공유하는 편이 맞다.
    #
    #  ⚠ R9(조용한 skip 이 미배선을 낳는다): 실패 경로는 rc==0 이더라도 **반드시 경고를
    #    남긴다**를 함께 단언한다. 경고 없는 skip 이 T018/T021 의 미배선을 만든 원인이다.
    # ══════════════════════════════════════════════════════════════

    # ── 로그 판정 헬퍼(부분문자열 오탐 방지) ───────────────────────
    # 'build_incheon_remap.sql' 은 'build_incheon_remap_from_old_lawdcd.sql' 의 부분문자열이고,
    # 'load_lawd_code.py' 와 'load_lawd_code_v2.py' 도 서로 헷갈린다 → **완전일치**로만 판정한다.
    @staticmethod
    def _psql_hits(lines, basename):
        return [ln for ln in lines if ln == "PSQL_F " + basename]

    @staticmethod
    def _py_hits(lines, basename):
        out = []
        for ln in lines:
            if not ln.startswith("PYTHON3 "):
                continue
            args = ln.split()[1:]
            if args and os.path.basename(args[0]) == basename:
                out.append(ln)
        return out

    def _assert_psql(self, lines, basename, msg=""):
        self.assertEqual(len(self._psql_hits(lines, basename)), 1,
                         f"{basename} 이 정확히 1회 호출되어야 함 {msg}: {lines}")

    def _assert_py(self, lines, basename, msg=""):
        self.assertEqual(len(self._py_hits(lines, basename)), 1,
                         f"{basename} 이 정확히 1회 호출되어야 함 {msg}: {lines}")

    # ── 케이스 24: lawd 단계가 lawd_code 원본 로더를 부른다 ────────
    def test_24_lawd_step_loads_lawd_code(self):
        rc, lines = self._run("lawd", make_parcel=False)
        self.assertEqual(rc, 0, lines)
        self._assert_py(lines, "load_lawd_code.py", "(lawd_ri 의 유일한 진실 원천)")

    # ── 케이스 25: lawd 단계가 ri 사전·시도/시군구 대응표를 만든다 ──
    def test_25_lawd_step_builds_remap_and_ri_dict(self):
        rc, lines = self._run("lawd", make_parcel=False)
        self.assertEqual(rc, 0, lines)
        self._assert_psql(lines, "build_ri_dict_from_lawd_code.sql", "(원본 기반 lawd_ri)")
        self._assert_psql(lines, "build_sido_remap.sql", "(전남·광주 46·29→12)")
        self._assert_py(lines, "load_lawd_code_v2.py", "(VWorld 30505)")
        self._assert_psql(lines, "build_incheon_remap_from_old_lawdcd.sql", "(인천 자치구 개편)")

    # ── 케이스 26: 의존 순서 ──────────────────────────────────────
    #   build_dong_dict.sql → load_lawd_code.py → build_ri_dict_from_lawd_code.sql
    #   → build_sido_remap.sql → load_lawd_code_v2.py → build_incheon_remap_from_old_lawdcd.sql
    def test_26_lawd_step_order(self):
        rc, lines = self._run("lawd", make_parcel=False)
        self.assertEqual(rc, 0, lines)
        seq = [
            ("build_dong_dict.sql", self._idx(lines, lambda ln: ln == "PSQL_F build_dong_dict.sql")),
            ("load_lawd_code.py", self._idx(lines, lambda ln: ln in self._py_hits(lines, "load_lawd_code.py"))),
            ("build_ri_dict_from_lawd_code.sql",
             self._idx(lines, lambda ln: ln == "PSQL_F build_ri_dict_from_lawd_code.sql")),
            ("build_sido_remap.sql", self._idx(lines, lambda ln: ln == "PSQL_F build_sido_remap.sql")),
            ("load_lawd_code_v2.py", self._idx(lines, lambda ln: ln in self._py_hits(lines, "load_lawd_code_v2.py"))),
            ("build_incheon_remap_from_old_lawdcd.sql",
             self._idx(lines, lambda ln: ln == "PSQL_F build_incheon_remap_from_old_lawdcd.sql")),
        ]
        for name, i in seq:
            self.assertNotEqual(i, -1, f"{name} 호출이 로그에 없음: {lines}")
        for (a, ia), (b, ib) in zip(seq, seq[1:]):
            self.assertLess(ia, ib, f"{a} 가 {b} 보다 앞서야 함(의존 순서): {lines}")

    # ── 케이스 27: lawd 아닌 단계엔 치환 배선이 새지 않는다 ────────
    def test_27_non_lawd_steps_no_remap_wiring(self):
        rc, lines = self._run("parcel")
        for name in ("build_sido_remap.sql", "build_incheon_remap_from_old_lawdcd.sql",
                     "build_ri_dict_from_lawd_code.sql"):
            self.assertEqual(self._psql_hits(lines, name), [],
                             f"parcel 단계에 {name} 이 새면 안 됨: {lines}")
        for name in ("load_lawd_code.py", "load_lawd_code_v2.py"):
            self.assertEqual(self._py_hits(lines, name), [],
                             f"parcel 단계에 {name} 이 새면 안 됨: {lines}")

    # ── 케이스 28: lawd 배선 추가가 parcel 자동체인을 깨지 않는다 ──
    def test_28_lawd_wiring_keeps_parcel_chain(self):
        rc, lines = self._run("parcel lawd", parcel_exit=0)
        self.assertEqual(rc, 0, lines)
        jibun = [ln for ln in lines if "backfill_parcel_jibun.sql" in ln]
        self.assertEqual(len(jibun), 1, f"parcel 체인은 여전히 정확히 1회: {lines}")
        self._assert_py(lines, "load_lawd_code_v2.py", "(lawd 단계도 함께 동작)")

    # ── 케이스 29: (c) 실패는 비치명이되 **경고를 남기고** (d)(e) 를 건너뛴다 ──
    def test_29_lawd_code_failure_is_non_fatal_but_warns(self):
        rc, lines, out = self._run_full("lawd", make_parcel=False, lawd_code_exit=1)
        self.assertEqual(rc, 0, f"lawd_code 실패는 비치명(전체 빌드를 죽이지 않음): {out}")
        self.assertRegex(out, r"(⚠|경고|건너뜀)", f"조용한 skip 금지(R9): {out}")
        self.assertEqual(self._psql_hits(lines, "build_ri_dict_from_lawd_code.sql"), [],
                         f"(c) 실패 시 (d) 미호출(부분 갱신 방지): {lines}")
        self.assertEqual(self._psql_hits(lines, "build_sido_remap.sql"), [],
                         f"(c) 실패 시 (e) 미호출(부분 갱신 방지): {lines}")
        # (f) 는 (c) 와 독립 계통(VWorld 30505) 이므로 계속 돈다.
        self._assert_py(lines, "load_lawd_code_v2.py", "((f) 는 (c) 와 독립)")

    # ── 케이스 30: (f) v2 실패 시 인천 SQL 미호출 + 경고(부분 치환 방지) ──
    def test_30_v2_failure_skips_incheon_remap(self):
        rc, lines, out = self._run_full("lawd", make_parcel=False, lawd_v2_exit=1)
        self.assertEqual(rc, 0, f"v2 실패도 비치명(API 는 fail-open): {out}")
        self.assertRegex(out, r"(⚠|경고|건너뜁)", f"조용한 skip 금지(R9): {out}")
        self.assertEqual(self._psql_hits(lines, "build_incheon_remap_from_old_lawdcd.sql"), [],
                         f"v2 실패 시 인천 대응표 생성 미호출(부분 치환 방지): {lines}")
        # (c)(d)(e) 는 v2 와 무관하게 정상 수행되어야 한다.
        self._assert_psql(lines, "build_ri_dict_from_lawd_code.sql", "((d) 는 v2 와 무관)")

    # ── 케이스 30b: 자산 미배치면 (f) 를 통째로 건너뛴다 ───────────
    #   S7 을 S2·S3 보다 먼저 커밋해도 빌드가 죽지 않는다는 **파일 존재 가드**의 근거.
    def test_30b_missing_v2_assets_skip_silently_but_note(self):
        rc, lines, out = self._run_full("lawd", make_parcel=False, make_v2_assets=False)
        self.assertEqual(rc, 0, f"자산 미배치는 현행 동작 유지(빌드 무영향): {out}")
        self.assertEqual(self._py_hits(lines, "load_lawd_code_v2.py"), [],
                         f"자산 없으면 v2 로더 미호출: {lines}")
        self.assertEqual(self._psql_hits(lines, "build_incheon_remap_from_old_lawdcd.sql"), [],
                         f"자산 없으면 인천 대응표 미호출: {lines}")
        self.assertRegex(out, r"(ℹ|건너뜀|미배치)", f"건너뛴 사실은 로그에 남긴다(R9): {out}")
        # 나머지 lawd 배선은 그대로 살아있어야 한다(가드가 단계 전체를 죽이면 안 됨).
        self._assert_psql(lines, "build_sido_remap.sql", "(가드는 (e) 에 영향 없음)")

    # ── 케이스 31: P1 회귀 가드 — 폐기된 명칭 조인 SQL 미참조 ──────
    #   build_incheon_remap.sql 은 명칭 꼬리 조인(T021)으로 대응을 **추론**하던 폐기 자산이다.
    #   대응의 유일한 축은 VWorld dsId=30505 의 OLD_LAWDCD 컬럼뿐이다.
    def test_31_no_name_join_sql_referenced(self):
        rc, lines = self._run("lawd", make_parcel=False)
        self.assertEqual(self._psql_hits(lines, "build_incheon_remap.sql"), [],
                         f"폐기된 명칭 조인 SQL(build_incheon_remap.sql)이 호출되면 안 됨(P1): {lines}")
        # 소스 단언은 **실행 참조**(`"$HERE/build_incheon_remap.sql"`)만 금지한다.
        # 주석의 "되살리지 말 것(폐기된 build_incheon_remap.sql)" 경고는 오히려 남겨야 하는
        # 안전장치다 — 파일명을 지우면 미래의 누군가가 같은 명칭 조인을 재발명한다.
        with open(_LOAD_ALL) as f:
            src = f.read()
        self.assertNotRegex(src, r'\$HERE/build_incheon_remap\.sql',
                            "load-all.sh 가 폐기된 명칭 조인 SQL 을 실행하면 안 됨(P1)")

    # ── 케이스 32: OLD_LAWDCD 근거 표기 가드(§7-3(2)) ─────────────
    def test_32_old_lawdcd_attribution_guard(self):
        guard = os.path.join(_SCRIPTS, "check-old-lawdcd-attribution.sh")
        if not os.path.exists(guard):
            self.skipTest(f"가드 스크립트 미배치(fail-open): {guard}")
        proc = subprocess.run(["bash", guard], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0,
                         f"OLD_LAWDCD 근거 표기 가드 실패:\n{proc.stdout}\n{proc.stderr}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
