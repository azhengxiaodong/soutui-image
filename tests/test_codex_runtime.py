"""验证网络恢复边界；模拟响应仅检查控制流程，不作为图片准确率证据。"""
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from soutui_image.codex_runtime import image_process_environment, run_image_call


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.response = self.root / "response.json"
        self.command = ["codex.exe", "exec", "--model", "gpt-6.1-sol", "--image", "original.png",
                        "--output-last-message", str(self.response), "-"]

    def run_call(self):
        return run_image_call(self.command, stage="test", cwd=self.root, timeout=600, input="unchanged")

    def test_transient_retry_preserves_images_model_prompt_and_evidence(self):
        def fake(command, **kwargs):
            self.assertEqual(kwargs["input"], "unchanged")
            self.assertEqual(kwargs["timeout"], 600)
            self.assertEqual(command[command.index("--model") + 1], "gpt-6.1-sol")
            self.assertIn("original.png", command)
            self.assertNotIn("--ignore-user-config", command)
            self.assertIn("project_doc_max_bytes=0", command)
            target = Path(command[command.index("--output-last-message") + 1])
            target.write_text('{"attempt":1}' if calls.call_count == 1 else '{"attempt":2}')
            return subprocess.CompletedProcess(command, 1 if calls.call_count == 1 else 0,
                                               "", "workspace routing discovery failed" if calls.call_count == 1 else "")
        with patch("soutui_image.codex_runtime.subprocess.run", side_effect=fake) as calls:
            self.assertEqual(self.run_call().returncode, 0)
            self.assertEqual(calls.call_count, 2)
        self.assertEqual(self.response.read_text(), '{"attempt":2}')
        self.assertEqual(len(list(self.root.glob("transport_*/stderr.txt"))), 2)

    def test_missing_response_cannot_reuse_stale_file(self):
        self.response.write_text('stale')
        with patch("soutui_image.codex_runtime.subprocess.run", return_value=subprocess.CompletedProcess([], 0, "", "")) as calls:
            self.assertNotEqual(self.run_call().returncode, 0)
            self.assertEqual(calls.call_count, 1)

    def test_permanent_error_not_retried(self):
        for error in ("401 Unauthorized", "usage limit reached", "invalid config", "403 connection reset"):
            with patch("soutui_image.codex_runtime.subprocess.run", return_value=subprocess.CompletedProcess([], 1, "", error)) as calls:
                self.assertEqual(self.run_call().returncode, 1)
                self.assertEqual(calls.call_count, 1)

    def test_timeout_preserves_partial_logs(self):
        with patch("soutui_image.codex_runtime.subprocess.run", side_effect=subprocess.TimeoutExpired([], 600, b"partial", b"error")):
            with self.assertRaises(subprocess.TimeoutExpired): self.run_call()
        self.assertEqual(next(self.root.glob("transport_*/stdout.txt")).read_text(), "partial")

    def test_existing_process_proxy_and_credentials_unchanged(self):
        env = {"HTTPS_PROXY": "http://existing:1234", "NO_PROXY": "private.test", "TLS_SETTING": "unchanged"}
        with patch.dict(os.environ, env, clear=True), patch("soutui_image.codex_runtime._system_proxy") as system:
            self.assertEqual(image_process_environment(), env)
            system.assert_not_called()

    def test_inherit_enabled_system_proxy_only_when_unset(self):
        with patch.dict(os.environ, {}, clear=True), patch("soutui_image.codex_runtime._system_proxy", return_value={"HTTPS_PROXY": "http://127.0.0.1:7897"}):
            self.assertEqual(image_process_environment()["HTTPS_PROXY"], "http://127.0.0.1:7897")
