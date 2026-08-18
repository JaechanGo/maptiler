#!/usr/bin/env python3
"""폐기 스크립트 5개의 재도입 회귀 가드 (T028 §9 T5).

stdlib unittest 만 사용(pytest 미설치). 실행: python3 scripts/test_deadcode_guard.py

T028 커밋 1(0636a19)에서 실행 경로 참조가 0인 데드 스크립트 5개를 삭제했다.
이 테스트는 그것들이 **파일로 되살아나거나 실행 경로에서 다시 호출되는 것**을
막는다. 삭제 당시의 "참조 0" 은 실측이었지만, 되살아나는 경로는 두 가지다.

  1) 누군가 `git revert` 나 백업 복사로 파일을 되돌린다
  2) 파일은 없는데 호출부만 되살아난다(`bash scripts/12-build-poi.sh` 등)
     → 이쪽이 더 흔하고 더 나쁘다. 실행 시점까지 아무도 모른다.

■ 주석·docstring 은 걷어내고 **문자열 리터럴은 남긴다**

  이 가드의 성패는 여기서 갈린다. 폐기 스크립트를 다시 부르는 코드는
  `subprocess.run(["bash", ".../12-build-poi.sh"])` 처럼 **반드시 문자열 안에**
  파일명을 담는다. "문자열은 데이터니까 제외" 로 처리하면 정작 잡아야 할
  재도입을 통째로 놓친다. 걷어낼 것은 주석과 docstring 뿐이다.

  반대로 주석은 남기면 안 된다. 폐기 사실을 기록한 정당한 서술이 이미 3곳
  있고(`postgis/load_building.sh:3`, `_common/textnorm.py:21`, `09-gen-geocode.py:143`),
  그것까지 실패로 만들면 가드가 곧 무력화된다.

■ 범위 — "실행 경로" 의 정의

  `scripts/`·`server/` 전체 + 루트 `README.md`. README 는 설치·운영 절차를
  그대로 복붙해 실행하는 안내서라 실행 경로에 준한다. `docs/` 는 설계·이력
  서술이므로 제외한다(§9 T5 "문서는 제외"). 실제로 `docs/geocode-juso-plan.md`
  등에는 5개 파일명이 남아 있고, 그것이 정상이다.

■ 이 테스트는 red 로 시작할 수 없다

  현재 실행 경로 참조는 이미 0건이므로 신설 직후 초록이다. 배선 부재를
  검출하는 테스트가 아니라 **회귀 가드**이기 때문이다. 실효성은 뮤테이션으로만
  실증할 수 있고, 그래서 아래 셋을 테스트 자신에 대한 자기검증으로 넣었다.
  · test_scan_covers_key_files      — 빈 집합을 훑고 초록이 되는 사고 방지
  · test_stripper_keeps_string_literals   — 미탐(문자열 제외) 방지
  · test_stripper_drops_comment_and_docstring — 오탐(주석 검출) 방지
"""
import ast
import io
import os
import tokenize
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SELF = os.path.abspath(__file__)

# T028 커밋 1(0636a19)에서 삭제한 5개.
DEAD = (
    "07-gen-geocode.py",
    "08-gen-juso-geocode.py",
    "10-gen-buildings.sh",
    "12-build-poi.sh",
    "geocode-juso-poc.py",
)

SCAN_DIRS = ("scripts", "server")
SCAN_FILES = ("README.md",)
SKIP_DIRS = {"__pycache__", "node_modules", ".git", ".venv", "venv"}

# 스캔이 실제로 닿아야 하는 실행 경로 대표 4곳. 하나라도 빠지면 범위가 무너진 것이다.
KEY_FILES = (
    "scripts/setup-build-host.sh",
    "scripts/postgis/load-all.sh",
    "scripts/build-studio.py",
    "README.md",
)


def _strip_py(src):
    """`#` 주석과 docstring 만 걷어낸다 — 문자열 리터럴은 남긴다.

    docstring 판정은 ast 로 한다(`Expr` 문으로 홀로 선 문자열 상수). tokenize
    만으로는 docstring 과 일반 문자열을 구분할 수 없고, STRING 토큰을 통째로
    버리면 호출부를 놓친다.
    """
    doc_lines = set()
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if (isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, str)):
            doc_lines.update(range(node.lineno, (node.end_lineno or node.lineno) + 1))
    out = []
    for tok in tokenize.generate_tokens(io.StringIO(src).readline):
        if tok.type == tokenize.COMMENT or tok.start[0] in doc_lines:
            continue
        out.append(tok.string)
    return "\n".join(out)


def _line_comment(marker):
    """줄 전체가 주석인 것만 버린다.

    줄 중간의 `#` 은 문자열 안일 수 있어 건드리지 않는다. 남겨서 생기는 위험은
    오탐(=더 엄격)뿐이고, 잘라서 생기는 위험은 미탐이다.
    """
    def strip(src):
        return "\n".join(l for l in src.splitlines() if not l.lstrip().startswith(marker))
    return strip


_HASH = _line_comment("#")
_STRIPPERS = {
    ".py": _strip_py,
    ".sh": _HASH, ".bash": _HASH, ".yml": _HASH, ".yaml": _HASH,
    ".conf": _HASH, ".env": _HASH, ".toml": _HASH,
    ".sql": _line_comment("--"),
}
# 모르는 확장자(.json·.md·.csv 등)는 전문 검사한다 — 놓치느니 엄격한 쪽으로.


def _exec_text(path):
    """파일의 '실행 경로' 텍스트. 읽을 수 없으면 None(바이너리 등)."""
    try:
        with open(path, encoding="utf-8") as fh:
            src = fh.read()
    except (UnicodeDecodeError, OSError):
        return None
    name = os.path.basename(path)
    ext = os.path.splitext(name)[1]
    strip = _STRIPPERS.get(ext, _HASH if name == "Dockerfile" else None)
    if strip is None:
        return src
    try:
        return strip(src)
    except (SyntaxError, tokenize.TokenError, IndentationError):
        # 파싱 불가 파일은 걷어내지 않고 전문 검사한다(가드를 조용히 비우지 않는다).
        return src


def _targets():
    for d in SCAN_DIRS:
        for dirpath, dirnames, filenames in os.walk(os.path.join(ROOT, d)):
            dirnames[:] = [x for x in dirnames if x not in SKIP_DIRS]
            for fn in sorted(filenames):
                p = os.path.join(dirpath, fn)
                if os.path.abspath(p) != SELF:
                    yield p
    for fn in SCAN_FILES:
        p = os.path.join(ROOT, fn)
        if os.path.exists(p):
            yield p


class TestDeadScriptRegression(unittest.TestCase):
    def test_dead_files_absent(self):
        """5개가 파일로 되살아나지 않았다(저장소 전체)."""
        found = []
        for dirpath, dirnames, filenames in os.walk(ROOT):
            dirnames[:] = [x for x in dirnames if x not in SKIP_DIRS]
            for fn in filenames:
                if fn in DEAD:
                    found.append(os.path.relpath(os.path.join(dirpath, fn), ROOT))
        self.assertEqual(found, [], f"폐기 스크립트가 되살아났다: {found}\n"
                                    "T028 커밋 1 에서 실행 경로 참조 0 을 실측하고 지운 것이다. "
                                    "되살릴 이유가 있다면 계획서부터 갱신하라.")

    def test_no_reference_in_exec_path(self):
        """실행 경로 코드가 5개를 다시 호출하지 않는다(주석·docstring 제외)."""
        hits = []
        for path in _targets():
            text = _exec_text(path)
            if text is None:
                continue
            rel = os.path.relpath(path, ROOT)
            for name in DEAD:
                if name in text:
                    for n, line in enumerate(text.splitlines(), 1):
                        if name in line:
                            hits.append(f"{rel}: {line.strip()[:100]}")
                            break
        self.assertEqual(hits, [], "폐기 스크립트를 실행 경로에서 참조한다:\n  " +
                                   "\n  ".join(hits) +
                                   "\n(폐기 사실을 적는 서술이라면 주석·docstring 으로 옮겨라. "
                                   "주석은 이 가드가 걷어낸다.)")

    def test_scan_covers_key_files(self):
        """스캔이 실제 실행 경로에 닿는다.

        범위가 무너져 빈 집합을 훑으면 위 두 테스트가 아무것도 안 보고 초록이
        된다. 대표 4곳과 최소 파일 수를 못박아 그 사고를 막는다.
        """
        rels = {os.path.relpath(p, ROOT) for p in _targets()}
        for key in KEY_FILES:
            self.assertIn(key, rels, f"스캔 범위에서 빠졌다: {key}")
        self.assertGreater(len(rels), 50, f"스캔 대상이 {len(rels)}개뿐 — 범위가 무너졌다")
        self.assertNotIn(os.path.relpath(SELF, ROOT), rels, "자기 자신을 스캔하면 항상 실패한다")

    def test_stripper_keeps_string_literals(self):
        """문자열 리터럴은 남는다 — 미탐 방지의 핵심 성질."""
        src = ('import subprocess\n'
               'subprocess.run(["bash", "scripts/12-build-poi.sh"])\n'
               'p = f"{ROOT}/scripts/07-gen-geocode.py"\n')
        out = _strip_py(src)
        self.assertIn("12-build-poi.sh", out, "일반 문자열이 사라졌다")
        self.assertIn("07-gen-geocode.py", out, "f-string 경로가 사라졌다")

    def test_stripper_drops_comment_and_docstring(self):
        """주석과 docstring 은 걷어낸다 — 오탐 방지."""
        py = ('"""모듈 설명: 12-build-poi.sh 는 T028 에서 폐기됐다."""\n'
              'x = 1  # 07-gen-geocode.py 도 함께 폐기\n'
              'def f():\n'
              '    "함수 설명: geocode-juso-poc.py 참조"\n'
              '    return x\n')
        out = _strip_py(py)
        for name in ("12-build-poi.sh", "07-gen-geocode.py", "geocode-juso-poc.py"):
            self.assertNotIn(name, out, f"주석/docstring 의 {name} 가 남았다")
        sh = "# (구)10-gen-buildings.sh 와 동일\necho ok\n"
        self.assertNotIn("10-gen-buildings.sh", _HASH(sh))


if __name__ == "__main__":
    unittest.main(verbosity=2)
