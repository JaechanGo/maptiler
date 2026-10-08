#!/usr/bin/env python3
"""07-gen-route-graph.sh 무중단 교체 흐름 단위테스트 — docker·curl·하위 스크립트를 스텁으로 바꿔 끼운다.

임시 ROOT 에 실제 07 을 복사해 실행한다(ROOT 는 스크립트 위치에서 유도 → 저장소·실서버 비오염).
스텁 docker 는 osrm-customize 호출 시 마운트 디렉토리에 mldgr('new')을 만들고, 모든 호출을 로그에 남긴다.
기존 그래프는 'old-<프로필>' 로 심어 교체·롤백 여부를 파일 내용으로 판정한다.
구버전 셸 호환을 겸해 /bin/bash(macOS 3.2)로 실행한다(.244 는 4.2).

실행:  python3 scripts/test_route_graph_swap.py
"""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(_HERE, "07-gen-route-graph.sh")
OLD = "osrm/osrm-backend:v5.25.0"
NEW = "ghcr.io/project-osrm/osrm-backend:v26.10.0-debian"
PROFILES = ("car", "foot", "bicycle")

DOCKER_STUB = r"""#!/bin/bash
echo "docker $*" >> "$STUB_LOG"
case "$1" in
  run)
    shift; detached=0; data=""
    while [ $# -gt 0 ]; do
      case "$1" in
        -d) detached=1; shift ;;
        --rm) shift ;;
        --name|-p) shift 2 ;;
        -v) case "$2" in *:/data) data="${2%:/data}" ;; esac; shift 2 ;;
        *) break ;;
      esac
    done
    # $1=이미지 $2=명령
    if [ "$2" = osrm-customize ] && [ -n "$data" ] && [ -z "${STUB_FAIL_CUSTOMIZE:-}" ]; then
      echo new > "$data/south-korea.osrm.mldgr"
    fi
    [ "$detached" = 1 ] && echo cid0
    exit 0 ;;
  ps) [ -n "${STUB_PS-x}" ] && printf '%s\n' ${STUB_PS-server-osrm-car-1 server-osrm-foot-1 server-osrm-bike-1}; exit 0 ;;
  *) exit 0 ;;
esac
"""

CURL_STUB = "#!/bin/bash\nprintf 200\n"

DF_STUB = """#!/bin/bash
echo "Filesystem 1024-blocks Used Available Capacity Mounted on"
echo "/dev/fake 100000000 99999000 1000 100% /"
"""

SETVER_STUB = r"""#!/bin/bash
R="$(cd "$(dirname "$0")/.." && pwd)"
case "$1" in
  --print) cat "$STUB_PIN" ;;
  --check) echo "set --check" >> "$STUB_LOG"; exit "${STUB_CHECK_RC:-0}" ;;
  *) echo "set $1 car=$(cat "$R/route/car/south-korea.osrm.mldgr" 2>/dev/null)" >> "$STUB_LOG"; echo "$1" > "$STUB_PIN" ;;
esac
"""

QC_STUB = r"""#!/usr/bin/env python3
import os, sys
api = sys.argv[sys.argv.index("--api") + 1]
staging = "127.0.0.1:159" in api
with open(os.environ["STUB_LOG"], "a") as f:
    f.write("13i %s %s\n" % ("staging" if staging else "post", " ".join(sys.argv[1:])))
sys.exit(int(os.environ.get("STUB_13I_STG_RC" if staging else "STUB_13I_POST_RC", "0")))
"""

DRIFT_STUB = r"""#!/usr/bin/env python3
import os, sys
with open(os.environ["STUB_LOG"], "a") as f:
    f.write("13k " + " ".join(sys.argv[1:]) + "\n")
sys.exit(int(os.environ.get("STUB_13K_RC", "0")))
"""


def _w(path, text, mode=0o644):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    os.chmod(path, mode)


class RouteGraphSwapTest(unittest.TestCase):

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="route07_")
        self.addCleanup(shutil.rmtree, self.root, True)
        r = self.root
        os.makedirs(os.path.join(r, "scripts"))
        shutil.copy(SCRIPT, os.path.join(r, "scripts", "07-gen-route-graph.sh"))
        _w(os.path.join(r, "scripts/versions.sh"), 'export OSRM_IMAGE="${OSRM_IMAGE:-%s}"\n' % OLD)
        _w(os.path.join(r, "scripts/set-osrm-version.sh"), SETVER_STUB, 0o755)
        _w(os.path.join(r, "scripts/13i-route-qc.py"), QC_STUB, 0o755)
        _w(os.path.join(r, "scripts/13k-route-drift.py"), DRIFT_STUB, 0o755)
        for p in PROFILES:
            _w(os.path.join(r, "scripts/route-profiles/%s.lua" % p), "")
            _w(os.path.join(r, "route", p, "south-korea.osrm.mldgr"), "old-%s\n" % p)
            _w(os.path.join(r, "route", p, ".osrm-image"), OLD + "\n")
        _w(os.path.join(r, "data/osm/south-korea.osm.pbf"), "PBF" * 100)
        _w(os.path.join(r, "server/docker-compose.yml"), "services: {}\n")
        self.bin = os.path.join(r, "_bin")
        _w(os.path.join(self.bin, "docker"), DOCKER_STUB, 0o755)
        _w(os.path.join(self.bin, "curl"), CURL_STUB, 0o755)
        self.log = os.path.join(r, "_calls.log")
        self.pin = os.path.join(r, "_pin")
        _w(self.pin, OLD + "\n")
        _w(self.log, "")

    def run07(self, **env_extra):
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("OSRM_", "ROUTE_", "STUB_", "PROFILES", "PBF"))}
        env.update(PATH=self.bin + os.pathsep + "/usr/bin:/bin:/usr/sbin:/sbin",
                   STUB_LOG=self.log, STUB_PIN=self.pin, PYTHON=sys.executable,
                   ROUTE_DISK_CHECK="0", ROUTE_BASELINE_API="http://gw.test:18080", OSRM_THREADS="2")
        env.update(env_extra)
        return subprocess.run(["/bin/bash", os.path.join(self.root, "scripts/07-gen-route-graph.sh")],
                              env=env, capture_output=True, text=True, timeout=120)

    # ── 판정 헬퍼 ──
    def calls(self):
        with open(self.log, encoding="utf-8") as f:
            return [ln.rstrip("\n") for ln in f if ln.strip()]

    def graph(self, p, base="route"):
        with open(os.path.join(self.root, base, p, "south-korea.osrm.mldgr"), encoding="utf-8") as f:
            return f.read().strip()

    def stamp(self, p):
        with open(os.path.join(self.root, "route", p, ".osrm-image"), encoding="utf-8") as f:
            return f.read().strip()

    def pinned(self):
        with open(self.pin, encoding="utf-8") as f:
            return f.read().strip()

    def exists(self, rel):
        return os.path.exists(os.path.join(self.root, rel))

    def compose_ups(self):
        return [c for c in self.calls() if c.startswith("docker compose") and " up -d --force-recreate" in c]

    def set_writes(self):
        return [c for c in self.calls() if c.startswith("set ") and not c.startswith("set --")]

    def docker_runs(self):
        return [c for c in self.calls() if c.startswith("docker run")]

    def assert_untouched(self):
        for p in PROFILES:
            self.assertEqual(self.graph(p), "old-" + p)
            self.assertEqual(self.stamp(p), OLD)
        self.assertEqual(self.pinned(), OLD)
        self.assertFalse(self.exists("route/.staging"))
        self.assertEqual(self.set_writes(), [])

    def assert_staging_containers_removed(self):
        c = self.calls()
        for i, ln in enumerate(c):
            if ln.startswith("docker run -d --name osrm-staging-"):
                name = ln.split()[4]
                self.assertTrue(any(x == "docker rm -f " + name for x in c[i + 1:]), name + " 미제거")

    # ── 케이스 ──
    def test_success_without_next(self):
        r = self.run07()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        for p in PROFILES:
            self.assertEqual(self.graph(p), "new")
            self.assertEqual(self.stamp(p), OLD)
        for d in ("route/.staging", "route/.prev", "route/.failed"):
            self.assertFalse(self.exists(d), d)
        ups = self.compose_ups()
        self.assertEqual(len(ups), 1)
        self.assertTrue(ups[0].endswith("--no-deps osrm-car osrm-foot osrm-bike"), ups[0])
        self.assertEqual(self.set_writes(), [])
        self.assertEqual(len([c for c in self.calls() if c.startswith("13i post")]), 1)
        self.assertFalse(any(c.startswith("docker restart") for c in self.calls()))
        self.assertFalse(self.exists("route/car/south-korea.osm.pbf"))   # pbf 사본 정리
        self.assert_staging_containers_removed()

    def test_success_with_next_updates_pin_after_swap(self):
        r = self.run07(OSRM_IMAGE_NEXT=NEW)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        for p in PROFILES:
            self.assertEqual((self.graph(p), self.stamp(p)), ("new", NEW))
        self.assertEqual(self.pinned(), NEW)
        self.assertEqual(self.set_writes(), ["set %s car=new" % NEW])   # 교체(mv) 이후에 호출됐다
        c = self.calls()
        i_set = c.index("set %s car=new" % NEW)
        i_last_val = max(i for i, x in enumerate(c) if x.startswith("13k "))
        i_up = c.index(self.compose_ups()[0])
        self.assertTrue(i_last_val < i_set < i_up)   # 검증 → 고정값 갱신 → 재생성
        self.assertTrue(all(NEW in x for x in self.docker_runs()))   # 빌드·검증 모두 새 이미지

    def test_staging_qc_failure_keeps_everything(self):
        r = self.run07(OSRM_IMAGE_NEXT=NEW, STUB_13I_STG_RC="1")
        self.assertNotEqual(r.returncode, 0)
        self.assert_untouched()
        self.assertEqual(self.compose_ups(), [])
        self.assertTrue(any(c.startswith("docker restart") for c in self.calls()))   # mmap 함정 가드
        self.assert_staging_containers_removed()

    def test_drift_failure_keeps_everything(self):
        r = self.run07(STUB_13K_RC="1")
        self.assertNotEqual(r.returncode, 0)
        self.assert_untouched()
        self.assertEqual(self.compose_ups(), [])
        self.assert_staging_containers_removed()

    def test_drift_baseline_unavailable_proceeds(self):
        r = self.run07(STUB_13K_RC="3")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("기준선", r.stdout)
        for p in PROFILES:
            self.assertEqual(self.graph(p), "new")

    def test_drift_skip_env(self):
        r = self.run07(ROUTE_DRIFT_SKIP="1", STUB_13K_RC="1")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertFalse(any(c.startswith("13k ") for c in self.calls()))

    def test_post_swap_failure_rolls_back_graph_and_pin(self):
        r = self.run07(OSRM_IMAGE_NEXT=NEW, STUB_13I_POST_RC="1")
        self.assertNotEqual(r.returncode, 0)
        for p in PROFILES:
            self.assertEqual(self.graph(p), "old-" + p)
            self.assertEqual(self.stamp(p), OLD)
            self.assertEqual(self.graph(p, "route/.failed"), "new")
        self.assertEqual(self.pinned(), OLD)
        w = self.set_writes()
        self.assertEqual(len(w), 2)
        self.assertTrue(w[0].startswith("set " + NEW) and w[1].startswith("set " + OLD), w)
        self.assertEqual(len(self.compose_ups()), 2)   # 새 이미지로 1회 + 롤백 후 1회
        self.assertIn("롤백", r.stderr)

    def test_next_requires_all_profiles(self):
        r = self.run07(OSRM_IMAGE_NEXT=NEW, PROFILES="car")
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(self.docker_runs(), [])
        self.assert_untouched()

    def test_next_with_auto_restart_off_rejected(self):
        r = self.run07(OSRM_IMAGE_NEXT=NEW, OSRM_AUTO_RESTART="0")
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(self.docker_runs(), [])

    def test_pin_mismatch_rejected(self):
        r = self.run07(STUB_CHECK_RC="1")
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(self.docker_runs(), [])
        self.assert_untouched()

    def test_disk_shortage_rejected_before_build(self):
        _w(os.path.join(self.bin, "df"), DF_STUB, 0o755)
        r = self.run07(ROUTE_DISK_CHECK="1")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("디스크 부족", r.stderr)
        self.assertEqual(self.docker_runs(), [])

    def test_build_failure_cleans_staging_and_restarts(self):
        r = self.run07(STUB_FAIL_CUSTOMIZE="1")
        self.assertNotEqual(r.returncode, 0)
        self.assert_untouched()
        self.assertFalse(self.exists("route/.staging/car/south-korea.osm.pbf"))
        self.assertTrue(any(c.startswith("docker restart") for c in self.calls()))

    def test_no_serving_containers_skips_recreate(self):
        r = self.run07(STUB_PS="")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.compose_ups(), [])
        self.assertFalse(any(c.startswith("13i post") for c in self.calls()))
        for p in PROFILES:
            self.assertEqual(self.graph(p), "new")

    def test_staging_profile_ports_and_qc_profiles(self):
        r = self.run07()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        stg = [c for c in self.calls() if c.startswith("13i staging")]
        self.assertEqual([c.split("--profiles ")[1].split()[0] for c in stg], ["driving", "walking", "cycling"])
        self.assertEqual([c.split("--api ")[1].split()[0] for c in stg],
                         ["http://127.0.0.1:15900", "http://127.0.0.1:15901", "http://127.0.0.1:15902"])


if __name__ == "__main__":
    unittest.main()
