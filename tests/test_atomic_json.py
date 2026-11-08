"""Windows 运行记录被短暂读取占用时的落盘保护；网站接口全部为本地测试替身。"""

import copy
import ctypes
import errno
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from soutui_image.atomic_json import atomic_write_json
from soutui_image.stage_d_web_generation import submit_job


def win_error(code):
    error = PermissionError(errno.EACCES, "测试文件占用")
    error.winerror = code
    return error


class WindowsReader:
    """用真实 Win32 只读句柄模拟界面读取；允许其他读写，但不允许删除/替换。"""

    def __init__(self, path):
        from ctypes import wintypes
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                           wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD,
                                           wintypes.HANDLE]
        self.kernel.CreateFileW.restype = wintypes.HANDLE
        self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self.kernel.CloseHandle.restype = wintypes.BOOL
        self.handle = self.kernel.CreateFileW(str(path), 0x80000000, 0x1 | 0x2, None, 3, 0x80, None)
        if self.handle == ctypes.c_void_p(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        self.lock = threading.Lock()

    def close(self):
        with self.lock:
            if self.handle is not None:
                handle, self.handle = self.handle, None
                if not self.kernel.CloseHandle(handle):
                    raise ctypes.WinError(ctypes.get_last_error())


class JsonFixture(unittest.TestCase):
    def setUp(self):
        scratch = Path(__file__).resolve().parents[1] / ".maintenance-tmp"
        scratch.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="atomic_json_", dir=scratch)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / "round_01.json"
        self.original = {"state": "submitting", "client_task_id": "mdtask-test"}
        self.path.write_text(json.dumps(self.original), encoding="utf-8")

    def candidates(self):
        return sorted(self.root.glob(f".{self.path.name}.*.tmp"))

    def read(self, path=None):
        return json.loads((path or self.path).read_text(encoding="utf-8"))


class AtomicJsonTests(JsonFixture):
    def test_only_windows_sharing_errors_retry_the_same_complete_candidate(self):
        real_replace = Path.replace
        for code in (5, 32, 33):
            with self.subTest(winerror=code):
                attempts = []
                candidate_payload = {"state": "submitted_needs_verification", "task_id": f"任务-{code}"}

                def replace(source, destination):
                    attempts.append(source)
                    self.assertEqual(source.parent, self.path.parent)
                    self.assertEqual(self.read(source), candidate_payload)
                    if len(attempts) < 3:
                        raise win_error(code)
                    return real_replace(source, destination)

                with patch.object(Path, "replace", replace):
                    atomic_write_json(self.path, candidate_payload, retry_seconds=1, retry_interval=.001)
                self.assertEqual(len(attempts), 3)
                self.assertEqual(len(set(attempts)), 1)
                self.assertEqual(self.read(), candidate_payload)
                self.assertEqual(self.candidates(), [])

    def test_other_io_errors_fail_immediately_preserving_original_and_complete_candidate(self):
        for error in (OSError(errno.ENOSPC, "测试磁盘空间不足"), PermissionError(errno.EACCES, "非Windows占用错误")):
            with self.subTest(error=type(error).__name__):
                before = self.path.read_bytes()
                payload = {"task_id": "server-receipt", "state": "submitted_needs_verification"}
                with patch.object(Path, "replace", side_effect=error) as replaced:
                    with self.assertRaises(type(error)):
                        atomic_write_json(self.path, payload)
                self.assertEqual(replaced.call_count, 1)
                self.assertEqual(self.path.read_bytes(), before)
                self.assertTrue(any(self.read(p) == payload for p in self.candidates()))

    def test_later_failed_write_does_not_overwrite_previous_candidate(self):
        before = self.path.read_bytes()
        payloads = [{"task_id": f"receipt-{index}", "state": "submitted_needs_verification"} for index in range(2)]
        with patch.object(Path, "replace", side_effect=win_error(5)):
            for payload in payloads:
                with self.assertRaises(PermissionError):
                    atomic_write_json(self.path, payload, retry_seconds=0)
        candidates = self.candidates()
        self.assertEqual(len(candidates), 2)
        self.assertEqual({self.read(p)["task_id"] for p in candidates}, {p["task_id"] for p in payloads})
        self.assertEqual(self.path.read_bytes(), before)

    @unittest.skipUnless(os.name == "nt", "需要真实 Windows 文件共享语义")
    def test_real_windows_read_handle_is_retried_until_released(self):
        reader = WindowsReader(self.path)
        self.addCleanup(reader.close)
        blocked = threading.Event()
        released = threading.Event()
        observed_errors = []

        def release():
            if blocked.wait(2):
                reader.close()
                released.set()

        releaser = threading.Thread(target=release)
        releaser.start()
        real_replace = Path.replace

        def replace(source, destination):
            try:
                return real_replace(source, destination)
            except OSError as error:
                observed_errors.append(error.winerror)
                blocked.set()
                raise

        payload = {"state": "awaiting_audit", "successful_results": 5}
        try:
            with patch.object(Path, "replace", replace):
                atomic_write_json(self.path, payload, retry_seconds=2, retry_interval=.01)
        finally:
            reader.close()
            releaser.join(3)
        self.assertFalse(releaser.is_alive())
        self.assertTrue(released.is_set())
        self.assertTrue(observed_errors)
        self.assertTrue(set(observed_errors).issubset({5, 32, 33}))
        self.assertEqual(self.read(), payload)
        self.assertEqual(self.candidates(), [])

    @unittest.skipUnless(os.name == "nt", "需要真实 Windows 文件共享语义")
    def test_persistent_windows_read_lock_times_out_without_removing_original(self):
        reader = WindowsReader(self.path)
        self.addCleanup(reader.close)
        before = self.path.read_bytes()
        payload = {"state": "submitted_needs_verification", "task_id": "receipt-kept"}
        started = time.monotonic()
        with self.assertRaises(OSError) as raised:
            atomic_write_json(self.path, payload, retry_seconds=.08, retry_interval=.01)
        self.assertIn(raised.exception.winerror, (5, 32, 33))
        self.assertLess(time.monotonic() - started, 2)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(len(self.candidates()), 1)
        self.assertEqual(self.read(self.candidates()[0]), payload)
        reader.close()


class SubmissionPersistenceTests(JsonFixture):
    """在收费请求返回后注入真实本地文件锁，证明恢复只查询原任务。"""

    def setUp(self):
        super().setUp()
        params = {"resolution": "2K", "aspectRatio": "3:4",
                  "inputImageUrls": ["https://example.test/original"],
                  "inputImageUrls2": [f"https://example.test/ref-{i}" for i in range(5)],
                  "extraParams": {"imageTier": "balanced", "garmentType": "套装",
                                  "resultsPerReference": "1", "selectedPoseCount": "5"}}
        self.record = {"state": "prepared", "round_index": 1, "client_task_id": "mdtask-test",
                       "task_id": None, "reference_ids": [str(i) for i in range(5)],
                       "payload": {"clientTaskId": "mdtask-test", "params": params}}
        self.task = {"id": "mdtask-test", "featureType": "model-dressing", "expectedResultCount": 5,
                     "status": "done", **params,
                     "resultSlots": [{"id": f"slot-{i}", "referenceIndex": i, "status": "done",
                                      "imageUrl": f"https://example.test/result-{i}"} for i in range(5)]}
        self.path.write_text(json.dumps(self.record), encoding="utf-8")

    def transport(self, on_post):
        task = self.task

        class LocalTransport:
            def __init__(self):
                self.methods = []

            def request_json(inner, method, path, **kwargs):
                inner.methods.append(method)
                if method == "POST":
                    on_post()
                    return {"success": True, "task": copy.deepcopy(task)}
                if method == "GET":
                    return {"success": True, "tasks": [copy.deepcopy(task)], "hasMore": False}
                raise AssertionError("不应出现其他接口方法")

        return LocalTransport()

    @unittest.skipUnless(os.name == "nt", "需要真实 Windows 文件共享语义")
    def test_post_receipt_short_read_lock_retries_file_write_without_reposting(self):
        readers, threads = [], []
        blocked = threading.Event()
        observed = []

        def on_post():
            self.assertEqual(self.read()["state"], "submitting")
            reader = WindowsReader(self.path)
            readers.append(reader)

            def release():
                if blocked.wait(2):
                    reader.close()

            thread = threading.Thread(target=release)
            threads.append(thread)
            thread.start()

        transport = self.transport(on_post)
        real_replace = Path.replace

        def replace(source, destination):
            try:
                return real_replace(source, destination)
            except OSError as error:
                observed.append(error.winerror)
                blocked.set()
                raise

        try:
            with patch.object(Path, "replace", replace):
                result = submit_job(self.path, transport)
        finally:
            for reader in readers:
                reader.close()
            for thread in threads:
                thread.join(3)
        self.assertTrue(observed)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(transport.methods, ["POST"])
        self.assertEqual(result["state"], "awaiting_audit")
        self.assertEqual(self.read()["successful_results"], 5)
        self.assertEqual(self.candidates(), [])

    @unittest.skipUnless(os.name == "nt", "需要真实 Windows 文件共享语义")
    def test_post_receipt_persistent_lock_keeps_evidence_and_next_start_only_gets(self):
        readers = []

        def on_post():
            self.assertEqual(self.read()["state"], "submitting")
            readers.append(WindowsReader(self.path))

        transport = self.transport(on_post)

        def bounded_write(path, record):
            return atomic_write_json(path, record, retry_seconds=.06, retry_interval=.01)

        try:
            with patch("soutui_image.stage_d_web_generation.atomic_write_json", bounded_write):
                with self.assertRaises(OSError) as raised:
                    submit_job(self.path, transport)
            self.assertIn(raised.exception.winerror, (5, 32, 33))
            self.assertEqual(self.read()["state"], "submitting")
            candidate = self.read(self.candidates()[0])
            self.assertEqual(candidate["task_id"], self.task["id"])
            self.assertEqual(candidate["state"], "submitted_needs_verification")
            self.assertEqual(transport.methods, ["POST"])
        finally:
            for reader in readers:
                reader.close()
        recovered = submit_job(self.path, transport)
        self.assertEqual(transport.methods, ["POST", "GET"])
        self.assertEqual(recovered["state"], "awaiting_audit")
        self.assertEqual(recovered["round_index"], 1)
        self.assertEqual(recovered["task_id"], self.task["id"])
        self.assertEqual(len(self.candidates()), 1, "旧失败候选作为恢复证据保留")

    @unittest.skipUnless(os.name == "nt", "需要真实 Windows 文件共享语义")
    def test_failure_to_persist_submitting_stops_before_post(self):
        reader = WindowsReader(self.path)
        transport = self.transport(lambda: self.fail("未保存提交意图时不能POST"))

        def bounded_write(path, record):
            return atomic_write_json(path, record, retry_seconds=.06, retry_interval=.01)

        try:
            with patch("soutui_image.stage_d_web_generation.atomic_write_json", bounded_write):
                with self.assertRaises(OSError):
                    submit_job(self.path, transport)
            self.assertEqual(transport.methods, [])
            self.assertEqual(self.read()["state"], "prepared")
            self.assertEqual(self.read(self.candidates()[0])["state"], "submitting")
        finally:
            reader.close()


if __name__ == "__main__":
    unittest.main()
