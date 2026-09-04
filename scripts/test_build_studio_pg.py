#!/usr/bin/env python3
"""build-studio _ensure_postgis — PostGIS 를 쓰는 타깃 실행 전 자동 기동·대기 로직 단위테스트(도커 없이 모의)."""
import importlib.util, pathlib, sys, unittest
from unittest import mock

HERE = pathlib.Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("build_studio", HERE / "build-studio.py")
bs = importlib.util.module_from_spec(spec); spec.loader.exec_module(bs)


class EnsurePostgis(unittest.TestCase):
    def setUp(self):
        self.msgs = []
        self.emit = self.msgs.append

    def test_already_up_no_docker_call(self):
        with mock.patch.object(bs, "_pg_port_open", return_value=True), \
             mock.patch.object(bs.subprocess, "run") as run:
            self.assertTrue(bs._ensure_postgis(self.emit))
            run.assert_not_called(); self.assertEqual(self.msgs, [])

    def test_remote_host_not_started(self):
        with mock.patch.object(bs, "_pg_port_open", return_value=False), \
             mock.patch.dict(bs.os.environ, {"PGHOST": "10.0.0.9"}), \
             mock.patch.object(bs.subprocess, "run") as run:
            self.assertFalse(bs._ensure_postgis(self.emit))
            run.assert_not_called(); self.assertIn("원격", self.msgs[-1])

    def test_starts_and_waits_until_ready(self):
        ports = iter([False, False, True, True])
        with mock.patch.object(bs, "_pg_port_open", side_effect=lambda *a, **k: next(ports)), \
             mock.patch.object(bs, "_pg_ready", return_value=True), \
             mock.patch.object(bs.time, "sleep"), \
             mock.patch.object(pathlib.Path, "exists", return_value=True), \
             mock.patch.object(bs.subprocess, "run", return_value=mock.Mock(returncode=0, stdout="", stderr="")) as run:
            self.assertTrue(bs._ensure_postgis(self.emit, wait_s=60))
            args = run.call_args_list[0].args[0]
            self.assertEqual(args[:5], ["docker", "compose", "-f", str(bs.ROOT / "server" / "docker-compose.yml"), "start"])
            self.assertEqual(run.call_args_list[0].kwargs["env"]["COMPOSE_PROFILES"], "postgis")
            self.assertTrue(any("기동 확인" in m for m in self.msgs))

    def test_start_fails_then_up_d(self):
        calls = iter([mock.Mock(returncode=1, stdout="", stderr="no such container"),
                      mock.Mock(returncode=0, stdout="", stderr="")])
        ports = iter([False, True])
        with mock.patch.object(bs, "_pg_port_open", side_effect=lambda *a, **k: next(ports)), \
             mock.patch.object(bs, "_pg_ready", return_value=True), \
             mock.patch.object(bs.time, "sleep"), \
             mock.patch.object(pathlib.Path, "exists", return_value=True), \
             mock.patch.object(bs.subprocess, "run", side_effect=lambda *a, **k: next(calls)) as run:
            self.assertTrue(bs._ensure_postgis(self.emit, wait_s=60))
            self.assertEqual(run.call_args_list[1].args[0][4:6], ["up", "-d"])

    def test_never_ready_returns_false(self):
        t = iter(range(0, 10_000, 50))
        with mock.patch.object(bs, "_pg_port_open", return_value=False), \
             mock.patch.object(bs.time, "sleep"), mock.patch.object(bs.time, "time", side_effect=lambda: next(t)), \
             mock.patch.object(pathlib.Path, "exists", return_value=True), \
             mock.patch.object(bs.subprocess, "run", return_value=mock.Mock(returncode=0, stdout="", stderr="")):
            self.assertFalse(bs._ensure_postgis(self.emit, wait_s=100))
            self.assertIn("접속 불가", self.msgs[-1])


if __name__ == "__main__":
    unittest.main()
