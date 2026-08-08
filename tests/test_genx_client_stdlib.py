import json
import unittest

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


if __name__ == "__main__":
    unittest.main()
