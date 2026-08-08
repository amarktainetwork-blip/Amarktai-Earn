from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from django.core.management.base import BaseCommand

from control.sandbox_tokens import SandboxTokenError, verify_sandbox_token
from control.services.sandbox_genx_proxy import SandboxGenXProxyError, proxy_chat_completion, stream_wrapper


class Handler(BaseHTTPRequestHandler):
    server_version = "AmarktaiSandboxGenX/1"

    def log_message(self, fmt, *args):
        return

    def _token(self) -> str:
        auth = self.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            raise SandboxTokenError("missing bearer token")
        return auth.split(" ", 1)[1].strip()

    def _json(self, status: int, payload: dict):
        data = json.dumps(payload, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/health":
            return self._json(200, {"ok": True})
        if self.path == "/v1/models":
            try:
                claims = verify_sandbox_token(self._token())
            except SandboxTokenError as exc:
                return self._json(401, {"error": {"message": str(exc)}})
            return self._json(200, {"object": "list", "data": [{"id": claims.model, "object": "model", "owned_by": "amarktai-genx-proxy"}]})
        return self._json(404, {"error": {"message": "not found"}})

    def do_POST(self):
        if self.path != "/v1/chat/completions":
            return self._json(404, {"error": {"message": "not found"}})
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > int(os.getenv("SANDBOX_LLM_MAX_REQUEST_BYTES", "2097152")):
                raise SandboxGenXProxyError("invalid request size", status_code=413)
            body = json.loads(self.rfile.read(length))
            payload, stream = proxy_chat_completion(self._token(), body)
            if stream:
                data = stream_wrapper(payload)
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            return self._json(200, payload)
        except SandboxTokenError as exc:
            return self._json(401, {"error": {"message": str(exc)}})
        except SandboxGenXProxyError as exc:
            return self._json(exc.status_code, {"error": {"message": str(exc)}})
        except Exception as exc:
            return self._json(500, {"error": {"message": exc.__class__.__name__}})


class Command(BaseCommand):
    help = "Run the internal job-scoped GenX OpenAI-compatible proxy for coding sandboxes."

    def handle(self, *args, **options):
        host = os.getenv("SANDBOX_GENX_PROXY_HOST", "0.0.0.0")
        port = int(os.getenv("SANDBOX_GENX_PROXY_PORT", "8081"))
        self.stdout.write(f"sandbox-genx-proxy listening on {host}:{port}")
        ThreadingHTTPServer((host, port), Handler).serve_forever()
