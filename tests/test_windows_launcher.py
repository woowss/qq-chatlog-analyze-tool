# Copyright (C) 2026 woowss
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
#
# -*- coding: utf-8 -*-
"""Windows 启动器的无 GUI 逻辑测试。"""

import importlib.util
import os
import socket
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


ROOT = Path(__file__).resolve().parent.parent
LAUNCHER_PATH = ROOT / "packaging" / "windows" / "launcher.py"
SPEC = importlib.util.spec_from_file_location("qqchatlog_windows_launcher", LAUNCHER_PATH)
assert SPEC is not None and SPEC.loader is not None
LAUNCHER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(LAUNCHER)


class TestWindowsLauncherHelpers(unittest.TestCase):
    def test_service_url_handles_ipv4_and_ipv6(self):
        self.assertEqual(LAUNCHER.build_service_url("127.0.0.1", 5000), "http://127.0.0.1:5000/")
        self.assertEqual(LAUNCHER.build_service_url("::1", 5001), "http://[::1]:5001/")
        self.assertEqual(LAUNCHER.build_service_url("0.0.0.0", 5002), "http://127.0.0.1:5002/")

    def test_wait_for_health_retries_until_ready(self):
        calls = []

        def fake_probe(url, timeout):
            calls.append((url, timeout))
            return len(calls) >= 3

        self.assertTrue(
            LAUNCHER.wait_for_health(
                "http://127.0.0.1:5000/health", timeout=0.2, poll_interval=0.001, probe=fake_probe
            )
        )
        self.assertEqual(len(calls), 3)

    def test_runtime_info_round_trip_and_owner_guard(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runtime.json"
            LAUNCHER.write_runtime_info(path, "127.0.0.1", 5123, pid=1234)
            self.assertEqual(
                LAUNCHER.read_runtime_info(path), {"host": "127.0.0.1", "port": 5123, "pid": 1234}
            )
            LAUNCHER.remove_runtime_info(path, pid=9999)
            self.assertTrue(path.exists())
            LAUNCHER.remove_runtime_info(path, pid=1234)
            self.assertFalse(path.exists())

    def test_env_template_does_not_overwrite_existing_config(self):
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            path = LAUNCHER.ensure_env_file(data_dir)
            first = path.read_text(encoding="utf-8")
            self.assertIn("DEEPSEEK_API_KEY", first)
            path.write_text("DEEPSEEK_API_KEY=test-value\n", encoding="utf-8")
            LAUNCHER.ensure_env_file(data_dir)
            self.assertEqual(path.read_text(encoding="utf-8"), "DEEPSEEK_API_KEY=test-value\n")

    def test_mutex_name_is_stable_and_scoped_to_data_dir(self):
        with tempfile.TemporaryDirectory() as directory:
            first = Path(directory) / "one"
            second = Path(directory) / "two"
            self.assertEqual(LAUNCHER.mutex_name(first), LAUNCHER.mutex_name(first))
            self.assertNotEqual(LAUNCHER.mutex_name(first), LAUNCHER.mutex_name(second))

    def test_server_skips_real_occupied_port(self):
        def app(environ, start_response):
            start_response("200 OK", [("Content-Type", "text/plain")])
            return [b"ok"]

        with socket.socket() as occupied:
            occupied.bind(("127.0.0.1", 0))
            occupied.listen()
            port = occupied.getsockname()[1]
            if port == 65535:
                self.skipTest("系统分配了端口范围上限")
            server, selected = LAUNCHER._create_server(SimpleNamespace(app=app), "127.0.0.1", port)
            try:
                self.assertGreater(selected, port)
            finally:
                server.server_close()

    def test_server_retries_werkzeug_exit_after_bind_race(self):
        sentinel = object()
        with (
            patch.object(LAUNCHER, "port_is_available", return_value=True),
            patch("werkzeug.serving.make_server", side_effect=[SystemExit(1), sentinel]) as make_server,
        ):
            server, selected = LAUNCHER._create_server(SimpleNamespace(app=object()), "127.0.0.1", 5000)
        self.assertIs(server, sentinel)
        self.assertEqual(selected, 5001)
        self.assertEqual(make_server.call_count, 2)

    @unittest.skipUnless(os.name == "nt", "Windows named mutex")
    def test_single_instance_can_be_acquired_again_after_release(self):
        with tempfile.TemporaryDirectory() as directory:
            try:
                self.assertTrue(LAUNCHER.acquire_single_instance(Path(directory)))
                self.assertFalse(LAUNCHER.acquire_single_instance(Path(directory)))
                LAUNCHER.release_single_instance()
                self.assertTrue(LAUNCHER.acquire_single_instance(Path(directory)))
            finally:
                LAUNCHER.release_single_instance()


if __name__ == "__main__":
    unittest.main(verbosity=2)
