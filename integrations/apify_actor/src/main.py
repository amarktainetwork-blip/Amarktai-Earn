from __future__ import annotations

import asyncio
from html.parser import HTMLParser
import ipaddress
import socket
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

import httpx
from apify import Actor


MAX_REDIRECTS = 5
MAX_BYTES = 5 * 1024 * 1024
MAX_ROBOTS_BYTES = 512 * 1024
MAX_LINKS = 500
USER_AGENT = "AmarktaiEarnApifyActor/1.0"


class PageParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title = ""
        self.description = ""
        self.headings: list[dict] = []
        self.links: list[dict] = []
        self._capture_title = False
        self._heading_tag = ""
        self._heading_text: list[str] = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        lower = tag.lower()
        if lower == "title":
            self._capture_title = True
        elif lower in {"h1", "h2", "h3"}:
            self._heading_tag = lower
            self._heading_text = []
        elif lower == "meta" and str(attrs.get("name") or "").lower() == "description":
            self.description = str(attrs.get("content") or "")[:2000]
        elif lower == "a" and len(self.links) < MAX_LINKS:
            href = str(attrs.get("href") or "").strip()
            if href:
                self.links.append({"href": href, "text": ""})

    def handle_endtag(self, tag):
        lower = tag.lower()
        if lower == "title":
            self._capture_title = False
        elif lower == self._heading_tag:
            text = " ".join("".join(self._heading_text).split())
            if text:
                self.headings.append({"level": self._heading_tag, "text": text[:1000]})
            self._heading_tag = ""
            self._heading_text = []

    def handle_data(self, data):
        if self._capture_title:
            self.title = (self.title + data)[:2000]
        if self._heading_tag:
            self._heading_text.append(data)


def _public_host(hostname: str) -> bool:
    try:
        infos = socket.getaddrinfo(hostname, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        return False
    addresses = {item[4][0] for item in infos}
    if not addresses:
        return False
    for raw in addresses:
        try:
            ip = ipaddress.ip_address(raw)
        except ValueError:
            return False
        if not ip.is_global:
            return False
    return True


def _validate_url(raw: str) -> str:
    parsed = urlparse(str(raw or "").strip())
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Only public HTTPS URLs are accepted")
    if not _public_host(parsed.hostname):
        raise ValueError("Private, local, reserved, or unresolved destinations are rejected")
    return parsed.geturl()


async def _bounded_stream(client: httpx.AsyncClient, url: str, *, max_bytes: int, headers: dict, timeout: int):
    async with client.stream("GET", url, headers=headers, timeout=timeout) as response:
        chunks: list[bytes] = []
        size = 0
        async for chunk in response.aiter_bytes():
            size += len(chunk)
            if size > max_bytes:
                raise ValueError("Response exceeds the Actor byte limit")
            chunks.append(chunk)
        return response.status_code, dict(response.headers), b"".join(chunks)


async def _robots_allows(client: httpx.AsyncClient, url: str) -> bool:
    parsed = urlparse(url)
    robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
    try:
        status, _, body = await _bounded_stream(
            client,
            robots_url,
            max_bytes=MAX_ROBOTS_BYTES,
            headers={"User-Agent": USER_AGENT},
            timeout=10,
        )
    except (httpx.HTTPError, ValueError):
        return False
    if status >= 400:
        return False
    parser = RobotFileParser()
    parser.set_url(robots_url)
    parser.parse(body.decode("utf-8", errors="replace").splitlines())
    return parser.can_fetch(USER_AGENT, url)


async def _fetch_page(url: str) -> tuple[str, bytes, str]:
    current = _validate_url(url)
    async with httpx.AsyncClient(follow_redirects=False) as client:
        if not await _robots_allows(client, current):
            raise ValueError("robots.txt does not permit this Actor user agent")
        for _ in range(MAX_REDIRECTS + 1):
            try:
                status, headers, body = await _bounded_stream(
                    client,
                    current,
                    max_bytes=MAX_BYTES,
                    headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"},
                    timeout=20,
                )
            except httpx.HTTPError as exc:
                raise ValueError("Page request failed") from exc
            if status in {301, 302, 303, 307, 308}:
                location = headers.get("location")
                if not location:
                    raise ValueError("Redirect response omitted Location")
                current = _validate_url(urljoin(current, location))
                if not await _robots_allows(client, current):
                    raise ValueError("redirect target is disallowed by robots.txt")
                continue
            if status >= 400:
                raise ValueError(f"Page request failed with HTTP {status}")
            content_type = headers.get("content-type", "").split(";", 1)[0].strip().lower()
            if content_type not in {"text/html", "application/xhtml+xml"}:
                raise ValueError("Only HTML/XHTML pages are supported")
            return current, body, content_type
    raise ValueError("Too many redirects")


def _extract(final_url: str, body: bytes, scope: str) -> dict:
    text = body.decode("utf-8", errors="replace")
    parser = PageParser()
    parser.feed(text)
    links = []
    for item in parser.links:
        try:
            absolute = urljoin(final_url, item["href"])
            parsed = urlparse(absolute)
            if parsed.scheme in {"http", "https"} and parsed.netloc:
                links.append(absolute[:3000])
        except Exception:
            continue
    result = {
        "url": final_url,
        "scope": scope,
        "title": " ".join(parser.title.split())[:2000],
        "description": parser.description,
        "headings": parser.headings[:200],
        "links": list(dict.fromkeys(links))[:MAX_LINKS],
        "html_bytes": len(body),
    }
    if scope == "metadata_only":
        result["links"] = []
        result["headings"] = []
    elif scope == "headings":
        result["links"] = []
    return result


async def main() -> None:
    async with Actor:
        actor_input = await Actor.get_input() or {}
        url = _validate_url(actor_input.get("public_url"))
        scope = str(actor_input.get("extraction_scope") or "page_summary").strip().lower()
        if scope not in {"metadata_only", "headings", "page_summary"}:
            raise ValueError("Unsupported extraction_scope")
        final_url, body, _ = await _fetch_page(url)
        await Actor.push_data(_extract(final_url, body, scope))


if __name__ == "__main__":
    asyncio.run(main())
