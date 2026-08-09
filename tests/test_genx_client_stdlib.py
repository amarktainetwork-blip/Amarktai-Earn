import json
import unittest
from unittest.mock import patch

from gateways.genx.client import GenXClient, GenXError


class FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self.ok = 200 <= status_code < 300
        self._payload = payload if payload is not None else {}
        self.content = json.dumps(self._payload).encode()

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return self.responses.pop(0)


class GenXClientTests(unittest.TestCase):
    def test_generate_caps_metadata_and_uses_documented_endpoint(self):
        session = FakeSession([FakeResponse(payload={"job_id": "j1"})])
        client = GenXClient("gnxk_test", session=session)
        response = client.generate("model-x", {"prompt": "hello"}, metadata={f"k{i}": i for i in range(20)})
        self.assertEqual(response["job_id"], "j1")
        method, url, kwargs = session.calls[0]
        self.assertEqual(method, "POST")
        self.assertTrue(url.endswith("/api/v1/generate"))
        self.assertEqual(len(kwargs["json"]["metadata"]), 16)
        self.assertNotIn("gnxk_test", repr(response))

    def test_http_error_does_not_leak_response_body_or_key(self):
        session = FakeSession([FakeResponse(status_code=401, payload={"detail": "secret diagnostic"})])
        client = GenXClient("gnxk_secret", session=session)
        with self.assertRaises(GenXError) as caught:
            client.credits()
        message = str(caught.exception)
        self.assertIn("401", message)
        self.assertNotIn("secret diagnostic", message)
        self.assertNotIn("gnxk_secret", message)

    def test_session_message_posts_content_with_idempotency_and_no_retry(self):
        client = GenXClient("gnxk_test", session=FakeSession([]))
        with patch.object(client, "_request", return_value={"accepted": True}) as request:
            response = client.session_message("session-1", "Return OK", idempotency_key="abc")

        self.assertEqual(response, {"accepted": True})
        request.assert_called_once_with(
            "POST",
            "/api/v1/sessions/session-1/messages",
            json={"content": "Return OK", "idempotency_key": "abc"},
            retries=0,
        )
        self.assertNotIn("message", request.call_args.kwargs["json"])

    def test_session_message_passes_content_part_array_through_unchanged(self):
        content = [{"type": "documented-by-caller", "opaque": {"value": 1}}]
        client = GenXClient("gnxk_test", session=FakeSession([]))
        with patch.object(client, "_request", return_value={}) as request:
            client.session_message("session-1", content)

        self.assertIs(request.call_args.kwargs["json"]["content"], content)
        self.assertEqual(request.call_args.kwargs["retries"], 0)

    def test_session_message_rejects_invalid_content_without_http_request(self):
        session = FakeSession([])
        client = GenXClient("gnxk_test", session=session)

        for invalid in ("", " ", [], None, 0, 1.5, False, True, {}):
            with self.subTest(invalid=invalid):
                with self.assertRaisesRegex(GenXError, "non-empty string or content-part array"):
                    client.session_message("session-1", invalid)

        self.assertEqual(session.calls, [])


if __name__ == "__main__":
    unittest.main()
