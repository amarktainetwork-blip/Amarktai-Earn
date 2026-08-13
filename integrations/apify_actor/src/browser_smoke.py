from __future__ import annotations

import asyncio

from playwright.async_api import async_playwright


async def main() -> None:
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.goto("about:blank")
        if page.url != "about:blank":
            raise RuntimeError("Chromium smoke page did not remain on about:blank")
        await browser.close()
    print("APIFY_BROWSER_LAUNCH_OK")


if __name__ == "__main__":
    asyncio.run(main())
