#!/usr/bin/env python3
"""set-osrm-version.sh 단위테스트 — 임시 사본만 건드린다(실제 versions.sh·compose 불변).

픽스처는 실제 파일 형식을 따른다: versions.sh 는 저장소 OSRM_IMAGE 줄(주석 포함)을 그대로 복사,
compose 는 osrm 서비스 3개 + osrm 아닌 서비스 2개. 구버전 셸 호환을 겸해 /bin/bash(macOS 3.2)로 실행한다.

실행:  python3 scripts/test_set_osrm_version.py
"""
import os
import re
import stat
import subprocess
import tempfile
import time
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(_HERE, "set-osrm-version.sh")
OLD = "osrm/osrm-backend:v5.25.0"
NEW = "ghcr.io/project-osrm/osrm-backend:v26.10.0-debian"


def _real_versions_line():
    with open(os.path.join(_HERE, "versions.sh"), encoding="utf-8") as f:
        for line in f:
            if line.startswith('export OSRM_IMAGE="${OSRM_IMAGE:-'):
                return re.sub(r":-[^}]+\}", ":-" + OLD + "}", line.rstrip("\n"), count=1)
    raise RuntimeError("versions.sh 에 OSRM_IMAGE 줄 없음")


VERSIONS = ("#!/usr/bin/env bash\n"
            "# 픽스처\n"
            'export MAPLIBRE_VERSION="${MAPLIBRE_VERSION:-5.16.0}"\n'
            + _real_versions_line() + "\n")

COMPOSE = f"""services:
  tileserver:
    image: maptiler/tileserver-gl:v5.5.0
    restart: unless-stopped

  osrm-car:
    image: {OLD}
    platform: linux/amd64
    command: ["osrm-routed", "--algorithm", "mld", "/data/south-korea.osrm"]

  osrm-foot:
    image: {OLD}
    platform: linux/amd64

  osrm-bike:
      image: {OLD}   # 들여쓰기·꼬리 주석 보존 확인용
    platform: linux/amd64

  gateway:
    image: nginx:1.27-alpine
"""


class SetOsrmVersionTest(unittest.TestCase):

    def setUp(self):
        self.td = tempfile.mkdtemp(prefix="setosrm_")
        self.vf = os.path.join(self.td, "versions.sh")
        self.cf = os.path.join(self.td, "docker-compose.yml")
        self._write(self.vf, VERSIONS)
        self._write(self.cf, COMPOSE)
        os.chmod(self.vf, 0o644)
        os.chmod(self.cf, 0o644)

    def _write(self, p, s):
        with open(p, "w", encoding="utf-8") as f:
            f.write(s)

    def _read(self, p):
        with open(p, encoding="utf-8") as f:
            return f.read()

    def run_s(self, *args):
        env = dict(os.environ, OSRM_VERSIONS_FILE=self.vf, OSRM_COMPOSE_FILE=self.cf)
        return subprocess.run(["/bin/bash", SCRIPT, *args], env=env, capture_output=True, text=True)

    def test_write_changes_only_target_lines(self):
        r = self.run_s(NEW)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("compose 3줄", r.stdout)
        v, c = self._read(self.vf), self._read(self.cf)
        self.assertEqual(v, VERSIONS.replace(OLD, NEW))       # 주석 포함 나머지 바이트 동일
        self.assertEqual(c, COMPOSE.replace(OLD, NEW))        # 들여쓰기·꼬리 주석·비 osrm 서비스 보존
        self.assertIn("nginx:1.27-alpine", c)

    def test_idempotent_keeps_mtime(self):
        self.assertEqual(self.run_s(NEW).returncode, 0)
        old = (os.stat(self.vf).st_mtime_ns, os.stat(self.cf).st_mtime_ns)
        time.sleep(0.05)
        r = self.run_s(NEW)
        self.assertEqual(r.returncode, 0)
        self.assertIn("변경 없음", r.stdout)
        self.assertEqual((os.stat(self.vf).st_mtime_ns, os.stat(self.cf).st_mtime_ns), old)

    def test_check_ok_and_mismatch(self):
        r = self.run_s("--check")
        self.assertEqual((r.returncode, r.stdout.strip()), (0, "OK " + OLD))
        self._write(self.cf, COMPOSE.replace(OLD, NEW, 1))   # osrm-car 줄만 다르게
        r = self.run_s("--check")
        self.assertEqual(r.returncode, 1)
        self.assertRegex(r.stdout, r"불일치: versions.sh=\S+ compose=\S+\(osrm-car 줄 7\)")

    def test_print(self):
        r = self.run_s("--print")
        self.assertEqual((r.returncode, r.stdout.strip()), (0, OLD))

    def test_bad_refs_rejected_files_untouched(self):
        for bad in ("nginx:latest", "osrm-backend", "", "x/osrm-backend:v1;rm -rf /",
                    "ghcr.io/x/osrm-backend:$(id)", "ghcr.io/x/osrm-backend:a\nb", "--bogus"):
            r = self.run_s(bad)
            self.assertEqual(r.returncode, 2, repr(bad))
        self.assertEqual(self._read(self.vf), VERSIONS)
        self.assertEqual(self._read(self.cf), COMPOSE)

    def test_usage_without_args(self):
        env = dict(os.environ, OSRM_VERSIONS_FILE=self.vf, OSRM_COMPOSE_FILE=self.cf)
        r = subprocess.run(["/bin/bash", SCRIPT], env=env, capture_output=True, text=True)
        self.assertEqual(r.returncode, 2)

    def test_no_compose_target_writes_nothing(self):
        bare = "services:\n  gateway:\n    image: nginx:1.27-alpine\n"
        self._write(self.cf, bare)
        r = self.run_s(NEW)
        self.assertEqual(r.returncode, 1)
        self.assertEqual(self._read(self.vf), VERSIONS)   # versions.sh 도 불변
        self.assertEqual(self._read(self.cf), bare)

    def test_no_versions_target_writes_nothing(self):
        self._write(self.vf, "#!/usr/bin/env bash\n")
        r = self.run_s(NEW)
        self.assertEqual(r.returncode, 1)
        self.assertEqual(self._read(self.cf), COMPOSE)

    def test_permissions_preserved(self):
        self.assertEqual(self.run_s(NEW).returncode, 0)
        for p in (self.vf, self.cf):
            self.assertEqual(stat.S_IMODE(os.stat(p).st_mode), 0o644, p)
        leftovers = [n for n in os.listdir(self.td) if n not in ("versions.sh", "docker-compose.yml")]
        self.assertEqual(leftovers, [])   # 임시파일 잔재 없음

    def test_repo_files_consistent(self):
        """실제 저장소 versions.sh 와 compose 는 언제나 일치해야 한다(드리프트 회귀 감시)."""
        r = subprocess.run(["/bin/bash", SCRIPT, "--check"], capture_output=True, text=True,
                           env={k: v for k, v in os.environ.items() if not k.startswith("OSRM_")})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)


if __name__ == "__main__":
    unittest.main()
