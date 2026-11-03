"""会话基础的敏感信息边界检查；不会使用或展示真实网页登录态。"""

import json
import os
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from soutui_image.web_session import MusefitReadClient, ORIGIN, SessionExpired, load_session, save_session


@unittest.skipUnless(os.name == "nt", "Windows DPAPI")
class SessionStorageTests(unittest.TestCase):
    def test_ciphertext_round_trip_and_no_plaintext(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "session.dpapi"
            dummy = "test-session-value-not-real-12345"
            save_session({"origin": ORIGIN, "token": dummy, "password": "never-save"}, path)
            self.assertNotIn(dummy.encode(), path.read_bytes())
            result = load_session(path)
            self.assertEqual(result["token"], dummy)
            self.assertNotIn("password", result)

    def test_wrong_origin_or_invalid_token_never_saved(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "session.dpapi"
            for payload in ({"origin": "https://other.example", "token": "test"},
                            {"origin": ORIGIN, "token": ""}, {"origin": ORIGIN, "token": "bad\r\nheader"}):
                with self.subTest(payload=payload), self.assertRaises((ValueError, SessionExpired)):
                    save_session(payload, path)
            self.assertFalse(path.exists())


class SessionRequestTests(unittest.TestCase):
    def test_wrong_path_stops_before_loading_token(self):
        client = MusefitReadClient("unused")
        with patch("soutui_image.web_session.load_session", side_effect=AssertionError("must not load")):
            for path in ("https://other.example/api", "/api/web/tasks/submit", "/api/auth/login"):
                with self.subTest(path=path), self.assertRaises(ValueError):
                    client.get_json(path)

    def test_unauthorized_response_is_safe_and_not_retried(self):
        client = MusefitReadClient("unused")
        with patch("soutui_image.web_session.load_session", return_value={"token": "test-secret"}), \
                patch.object(client.opener, "open", side_effect=urllib.error.HTTPError(ORIGIN, 401, "secret?", {}, None)) as opened:
            with self.assertRaises(SessionExpired) as result:
                client.get_json("/api/web/favorites")
            self.assertNotIn("test-secret", str(result.exception))
            self.assertEqual(opened.call_count, 1)

    def test_reflected_token_is_not_returned_for_logging(self):
        class Response:
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def read(self, size): return json.dumps({"echo": "test-secret"}).encode()
        client = MusefitReadClient("unused")
        with patch("soutui_image.web_session.load_session", return_value={"token": "test-secret"}), \
                patch.object(client.opener, "open", return_value=Response()):
            with self.assertRaises(RuntimeError) as result:
                client.get_json("/api/web/favorites")
            self.assertNotIn("test-secret", str(result.exception))


if __name__ == "__main__":
    unittest.main()
