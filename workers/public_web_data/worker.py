from __future__ import annotations

import ipaddress
import json
import os
import socket
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlsplit
from urllib.robotparser import RobotFileParser

import requests

from workers.base import WorkRequest, WorkResult, Worker


class PublicWebError(ValueError):
    pass


class _TextParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True); self.parts = []; self.title = ""; self._title = False
    def handle_starttag(self, tag, attrs):
        if tag.casefold() == "title": self._title = True
    def handle_endtag(self, tag):
        if tag.casefold() == "title": self._title = False
    def handle_data(self, data):
        cleaned = " ".join(data.split())
        if cleaned:
            self.parts.append(cleaned)
            if self._title: self.title += (" " if self.title else "") + cleaned


def _validated_url(raw: str) -> str:
    parts = urlsplit(raw.strip())
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password or parts.port not in {None, 443}:
        raise PublicWebError("PUBLIC_WEB_URL_NOT_APPROVED")
    try:
        addresses = {row[4][0] for row in socket.getaddrinfo(parts.hostname, 443, type=socket.SOCK_STREAM)}
    except socket.gaierror as exc:
        raise PublicWebError("PUBLIC_WEB_DNS_FAILED") from exc
    if not addresses or any(not ipaddress.ip_address(value).is_global for value in addresses):
        raise PublicWebError("PUBLIC_WEB_PRIVATE_ADDRESS_BLOCKED")
    return parts.geturl()


def _reject_private_peer(response) -> None:
    connection = getattr(getattr(response, "raw", None), "_connection", None)
    sock = getattr(connection, "sock", None)
    if sock is None:
        return
    try:
        peer = sock.getpeername()[0]
        if not ipaddress.ip_address(peer).is_global:
            raise PublicWebError("PUBLIC_WEB_PRIVATE_ADDRESS_BLOCKED")
    except (OSError, ValueError) as exc:
        raise PublicWebError("PUBLIC_WEB_PEER_VALIDATION_FAILED") from exc


def _robots_allowed(session: requests.Session, url: str, user_agent: str, timeout: tuple[float, float]) -> bool:
    parts = urlsplit(url); robots_url = f"{parts.scheme}://{parts.netloc}/robots.txt"
    response = session.get(robots_url, headers={"User-Agent": user_agent, "Accept": "text/plain"}, timeout=timeout, allow_redirects=False, stream=True)
    try:
        if 300 <= response.status_code < 400:
            return False
        if response.status_code in {401, 403}:
            return False
        if response.status_code >= 400:
            return True
        chunks = []; total = 0
        for chunk in response.iter_content(chunk_size=32768):
            total += len(chunk)
            if total > 256000:
                return False
            chunks.append(chunk)
        text = b"".join(chunks).decode(response.encoding or "utf-8", errors="replace")
    finally:
        response.close()
    parser = RobotFileParser(); parser.set_url(robots_url); parser.parse(text.splitlines())
    return parser.can_fetch(user_agent, url)


class PublicWebDataWorker(Worker):
    worker_class = "public_web_data"

    def execute(self, request: WorkRequest) -> WorkResult:
        try:
            if request.inputs.get("operation") != "public_web_extract":
                return WorkResult(ok=False, error="unsupported public-web operation")
            if os.getenv("PUBLIC_WEB_DATA_ENABLED", "0") != "1":
                raise PublicWebError("PUBLIC_WEB_DATA_DISABLED")
            if request.inputs.get("authorization_confirmed") is not True or request.inputs.get("terms_permit") is not True:
                raise PublicWebError("PUBLIC_WEB_POLICY_PROOF_REQUIRED")
            purpose = str(request.inputs.get("purpose") or "").strip()
            if not purpose:
                raise PublicWebError("PUBLIC_WEB_PURPOSE_REQUIRED")
            maximum = max(1024, min(int(os.getenv("PUBLIC_WEB_MAX_RESPONSE_BYTES", str(5 * 1024 * 1024))), 25 * 1024 * 1024))
            redirects = max(0, min(int(os.getenv("PUBLIC_WEB_MAX_REDIRECTS", "3")), 5))
            timeout = (float(os.getenv("PUBLIC_WEB_CONNECT_TIMEOUT_SECONDS", "5")), float(os.getenv("PUBLIC_WEB_READ_TIMEOUT_SECONDS", "20")))
            user_agent = os.getenv("PUBLIC_WEB_USER_AGENT", "AmarktaiEarn/1.0 bounded-public-research")
            url = _validated_url(str(request.inputs.get("url") or ""))
            with requests.Session() as session:
                session.trust_env = False
                if not _robots_allowed(session, url, user_agent, timeout):
                    raise PublicWebError("PUBLIC_WEB_ROBOTS_BLOCKED")
                response = None
                for _ in range(redirects + 1):
                    response = session.get(url, headers={"User-Agent": user_agent, "Accept": "text/html,text/plain,application/json"}, timeout=timeout, allow_redirects=False, stream=True)
                    if response.status_code not in {301, 302, 303, 307, 308}:
                        break
                    location = response.headers.get("Location")
                    response.close()
                    if not location: raise PublicWebError("PUBLIC_WEB_REDIRECT_INVALID")
                    url = _validated_url(urljoin(url, location))
                if response is None or response.status_code in {301, 302, 303, 307, 308}:
                    raise PublicWebError("PUBLIC_WEB_REDIRECT_LIMIT")
                if response.status_code != 200:
                    raise PublicWebError(f"PUBLIC_WEB_HTTP_{response.status_code}")
                _reject_private_peer(response)
                content_type = response.headers.get("Content-Type", "").split(";", 1)[0].casefold()
                if content_type not in {"text/html", "text/plain", "application/json"}:
                    raise PublicWebError("PUBLIC_WEB_CONTENT_TYPE_BLOCKED")
                declared = int(response.headers.get("Content-Length") or 0)
                if declared > maximum: raise PublicWebError("PUBLIC_WEB_RESPONSE_LIMIT")
                chunks = []; total = 0
                for chunk in response.iter_content(chunk_size=65536):
                    total += len(chunk)
                    if total > maximum: raise PublicWebError("PUBLIC_WEB_RESPONSE_LIMIT")
                    chunks.append(chunk)
                final_url = _validated_url(response.url or url); response.close()
            raw = b"".join(chunks).decode(response.encoding or "utf-8", errors="replace")
            if content_type == "text/html":
                parser = _TextParser(); parser.feed(raw); text = "\n".join(parser.parts); title = parser.title
            else:
                text = raw; title = ""
            payload = {"url": final_url, "purpose": purpose, "content_type": content_type, "title": title, "text": text[:maximum], "bytes": total}
            request.workspace.mkdir(parents=True, exist_ok=True)
            target = request.workspace / "public-web-extract.json"
            target.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            return WorkResult(ok=True, artifacts=[target], evidence={
                "operation": "public_web_extract", "final_url": final_url, "bytes": total,
                "content_type": content_type, "robots_checked": True, "policy_confirmed": True,
            })
        except (OSError, TypeError, ValueError, requests.RequestException, PublicWebError) as exc:
            return WorkResult(ok=False, error=str(exc))
