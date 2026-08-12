from __future__ import annotations

import asyncio
from html.parser import HTMLParser
import ipaddress
import re
import socket
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

import httpx
from apify import Actor
from playwright.async_api import Route, async_playwright


MAX_REDIRECTS = 5
MAX_BYTES = 5 * 1024 * 1024
MAX_ROBOTS_BYTES = 512 * 1024
MAX_LINKS = 500
MAX_BROWSER_TEXT = 200_000
MAX_FORM_FIELDS = 200
USER_AGENT = "AmarktaiEarnApifyActor/2.0"
BROWSER_SCOPES = {"browser_snapshot", "browser_extract", "form_inspect", "form_fill_preview"}
HTTP_SCOPES = {"metadata_only", "headings", "page_summary"}
SAFE_FIELD_NAME = re.compile(r"^[A-Za-z0-9_.:-]{1,120}$")


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


async def _browser_route(route: Route, request) -> None:
    raw = str(request.url or "")
    parsed = urlparse(raw)
    if parsed.scheme in {"data", "blob", "about"}:
        await route.continue_()
        return
    try:
        url = _validate_url(raw)
    except ValueError:
        await route.abort("blockedbyclient")
        return
    if request.resource_type == "document":
        async with httpx.AsyncClient(follow_redirects=False) as client:
            if not await _robots_allows(client, url):
                await route.abort("blockedbyclient")
                return
    await route.continue_()


def _clean_form_values(raw) -> dict[str, str | bool]:
    if raw in (None, {}):
        return {}
    if not isinstance(raw, dict):
        raise ValueError("form_values must be an object keyed by public form field name")
    result: dict[str, str | bool] = {}
    for key, value in raw.items():
        name = str(key)
        if not SAFE_FIELD_NAME.fullmatch(name):
            raise ValueError("form field names may contain only letters, digits, dot, colon, underscore, or hyphen")
        if isinstance(value, bool):
            result[name] = value
        else:
            text = str(value)
            if len(text) > 2000:
                raise ValueError("form preview value exceeds the per-field limit")
            result[name] = text
    if len(result) > MAX_FORM_FIELDS:
        raise ValueError("too many form preview fields")
    return result


async def _browser_extract(url: str, scope: str, form_values: dict[str, str | bool]) -> dict:
    validated = _validate_url(url)
    async with httpx.AsyncClient(follow_redirects=False) as client:
        if not await _robots_allows(client, validated):
            raise ValueError("robots.txt does not permit this Actor user agent")

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent=USER_AGENT,
            viewport={"width": 1440, "height": 900},
            service_workers="block",
        )
        await context.route("**/*", _browser_route)
        page = await context.new_page()
        response = await page.goto(validated, wait_until="domcontentloaded", timeout=30_000)
        final_url = _validate_url(page.url)
        if response is None or response.status >= 400:
            await browser.close()
            raise ValueError("browser navigation failed")

        filled: list[str] = []
        if scope == "form_fill_preview":
            for name, value in form_values.items():
                locator = page.locator(f'[name="{name}"]').first
                if await locator.count() == 0:
                    continue
                input_type = str(await locator.get_attribute("type") or "").casefold()
                if input_type in {"password", "hidden", "file", "submit", "image"}:
                    continue
                tag = str(await locator.evaluate("el => el.tagName.toLowerCase()"))
                if input_type in {"checkbox", "radio"} and isinstance(value, bool):
                    if value:
                        await locator.check(timeout=3000)
                    else:
                        await locator.uncheck(timeout=3000)
                elif tag == "select":
                    await locator.select_option(label=str(value), timeout=3000)
                else:
                    await locator.fill(str(value), timeout=3000)
                filled.append(name)

        title = (await page.title())[:2000]
        body_text = " ".join((await page.locator("body").inner_text(timeout=5000)).split())[:MAX_BROWSER_TEXT]
        headings = await page.locator("h1,h2,h3").evaluate_all(
            "els => els.slice(0,200).map(el => ({level: el.tagName.toLowerCase(), text: (el.innerText || '').trim().slice(0,1000)}))"
        )
        links = await page.locator("a[href]").evaluate_all(
            "els => els.slice(0,500).map(el => ({href: el.href, text: (el.innerText || '').trim().slice(0,500)}))"
        )
        forms = await page.locator("form").evaluate_all(
            """forms => forms.slice(0,50).map((form, formIndex) => ({
                form_index: formIndex,
                action: form.action || '',
                method: (form.method || 'get').toLowerCase(),
                fields: Array.from(form.querySelectorAll('input,textarea,select')).slice(0,200).map(el => ({
                    name: el.name || '',
                    tag: el.tagName.toLowerCase(),
                    type: (el.type || '').toLowerCase(),
                    required: !!el.required,
                    disabled: !!el.disabled
                }))
            }))"""
        )
        screenshot = await page.screenshot(full_page=False, type="png")
        screenshot_key = f"browser-{scope}-screenshot.png"
        await Actor.set_value(screenshot_key, screenshot, content_type="image/png")
        await browser.close()

    result = {
        "url": final_url,
        "scope": scope,
        "title": title,
        "text": body_text if scope in {"browser_snapshot", "browser_extract", "form_fill_preview"} else "",
        "headings": headings if scope in {"browser_snapshot", "browser_extract", "form_fill_preview"} else [],
        "links": links if scope in {"browser_snapshot", "browser_extract", "form_fill_preview"} else [],
        "forms": forms if scope in {"form_inspect", "form_fill_preview", "browser_snapshot"} else [],
        "filled_fields": filled,
        "form_submitted": False,
        "screenshot_key": screenshot_key,
        "browser_engine": "playwright_chromium",
        "robots_checked": True,
    }
    return result


async def main() -> None:
    async with Actor:
        actor_input = await Actor.get_input() or {}
        url = _validate_url(actor_input.get("public_url"))
        scope = str(actor_input.get("extraction_scope") or "page_summary").strip().lower()
        if scope in HTTP_SCOPES:
            final_url, body, _ = await _fetch_page(url)
            result = _extract(final_url, body, scope)
        elif scope in BROWSER_SCOPES:
            if actor_input.get("authorization_confirmed") is not True:
                raise ValueError("browser modes require explicit authorization confirmation")
            result = await _browser_extract(url, scope, _clean_form_values(actor_input.get("form_values")))
        else:
            raise ValueError("Unsupported extraction_scope")
        await Actor.push_data(result)


if __name__ == "__main__":
    asyncio.run(main())
