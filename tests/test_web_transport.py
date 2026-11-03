"""任务传输的关键边界：写入超时不自动重发、令牌不回显、禁止越域接口。"""

import unittest
import urllib.error
from unittest.mock import patch

from soutui_image.web_transport import RequestOutcomeUnknown, WebTransport


class TaskTransportTests(unittest.TestCase):
    def test_write_timeout_is_unknown_and_never_retried(self):
        client = WebTransport("unused")
        with patch("soutui_image.web_transport.load_session", return_value={"token": "dummy-test-token"}), \
                patch.object(client.opener, "open", side_effect=TimeoutError()) as opened:
            with self.assertRaises(RequestOutcomeUnknown):
                client.request_json("POST", "/api/web/tasks/submit", body={"clientTaskId": "test-only"})
            self.assertEqual(opened.call_count, 1)

    def test_server_error_on_write_does_not_resubmit(self):
        client = WebTransport("unused")
        error = urllib.error.HTTPError("https://ai.51dingxiao.com", 502, "", {}, None)
        with patch("soutui_image.web_transport.load_session", return_value={"token": "dummy-test-token"}), \
                patch.object(client.opener, "open", side_effect=error) as opened:
            with self.assertRaises(RequestOutcomeUnknown):
                client.request_json("POST", "/api/web/tasks/submit", body={})
            self.assertEqual(opened.call_count, 1)

    def test_disallowed_endpoint_never_loads_credentials(self):
        client = WebTransport("unused")
        with patch("soutui_image.web_transport.load_session", side_effect=AssertionError("must not load")):
            for method, path in (("POST", "https://other.example"), ("POST", "/api/auth/login"),
                                 ("DELETE", "/api/web/tasks/known-id"), ("POST", "/api/api-keys")):
                with self.subTest(method=method, path=path), self.assertRaises(ValueError):
                    client.request_json(method, path, body={})


if __name__ == "__main__":
    unittest.main()
