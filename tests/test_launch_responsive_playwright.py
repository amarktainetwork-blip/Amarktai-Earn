import os
import unittest
from pathlib import Path

from django.contrib.auth import get_user_model
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.test import override_settings

from control.jwt_auth import issue_access
from control.services.commercial_api import bootstrap_commercial_catalog
from control.services.commercial_intelligence import bootstrap_commercial_packages

try:
    from playwright.sync_api import sync_playwright
except ImportError:  # Local unit runs can omit the CI-only browser dependency.
    sync_playwright = None


@unittest.skipUnless(sync_playwright is not None, "Playwright is installed by the responsive CI step")
@override_settings(
    JWT_ACCESS_SECONDS=3600,
    JWT_ACTIVE_KID="browser",
    JWT_SIGNING_KEYS={"browser": "browser-responsive-proof-signing-key-with-more-than-sixty-four-characters-2026"},
    JWT_ISSUER="amarktai-browser-proof",
    JWT_AUDIENCE="amarktai-browser-owner",
    ACCESS_COOKIE_NAME="amarktai_access",
)
class LaunchResponsivePlaywrightTests(StaticLiveServerTestCase):
    """Structural viewport proof, intentionally not a brittle pixel snapshot test."""

    @classmethod
    def setUpClass(cls):
        cls.previous_async_unsafe = os.environ.get("DJANGO_ALLOW_ASYNC_UNSAFE")
        os.environ["DJANGO_ALLOW_ASYNC_UNSAFE"] = "true"
        super().setUpClass()
        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch(headless=True)
        cls.output = Path(os.getenv("AMARKTAI_RESPONSIVE_ARTIFACT_DIR", "test-artifacts/responsive"))
        cls.output.mkdir(parents=True, exist_ok=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close(); cls.pw.stop()
        super().tearDownClass()

        if cls.previous_async_unsafe is None:
            os.environ.pop("DJANGO_ALLOW_ASYNC_UNSAFE", None)
        else:
            os.environ["DJANGO_ALLOW_ASYNC_UNSAFE"] = cls.previous_async_unsafe

    def setUp(self):
        bootstrap_commercial_catalog(); bootstrap_commercial_packages()
        self.owner = get_user_model().objects.create_user(username="browser-owner", password="test", is_staff=True)

    def assert_layout(self, page, path, width, height=900, authenticated=False, screenshot=False):
        context = self.browser.new_context(viewport={"width": width, "height": height}, reduced_motion="reduce")
        if authenticated:
            context.add_cookies([{"name": "amarktai_access", "value": issue_access(self.owner), "url": self.live_server_url}])
        tab = context.new_page()
        errors = []
        tab.on("pageerror", lambda error: errors.append(str(error)))
        tab.on("console", lambda message: errors.append(message.text) if message.type == "error" and "favicon" not in message.text.lower() else None)
        tab.on("response", lambda response: errors.append(f"HTTP {response.status} {response.url}") if response.status >= 400 else None)
        response = tab.goto(self.live_server_url + path, wait_until="networkidle")
        self.assertIsNotNone(response); self.assertLess(response.status, 400, (path, response.status))
        self.assertEqual(tab.url.rstrip("/"), (self.live_server_url + path).rstrip("/"), (path, tab.url))
        tab.locator("main").wait_for(state="visible")
        dimensions = tab.evaluate("""() => ({scroll: document.documentElement.scrollWidth, client: document.documentElement.clientWidth, body: document.body.getBoundingClientRect().width})""")
        self.assertLessEqual(dimensions["scroll"], width + 2, (path, width, dimensions))
        self.assertLessEqual(dimensions["body"], width + 2, (path, width, dimensions))
        main = tab.locator("main").bounding_box()
        self.assertIsNotNone(main); self.assertGreater(main["width"], 0); self.assertGreater(main["height"], 0)
        if screenshot:
            safe = path.strip("/").replace("/", "-") or "landing"
            tab.screenshot(path=str(self.output / f"{safe}-{width}.png"), full_page=True)
        self.assertEqual(errors, [], (path, width, errors))
        context.close()

    def test_public_pages_at_all_release_viewports(self):
        for width in (320, 360, 375, 390, 414, 768, 1024, 1280, 1440):
            self.assert_layout(None, "/", width, screenshot=width in {360, 768, 1440})
            self.assert_layout(None, "/api/docs/", width)
            self.assert_layout(None, "/terms/", width)

    def test_login_and_owner_routes_mobile_tablet_desktop(self):
        for width in (360, 768, 1280):
            self.assert_layout(None, "/login/", width, screenshot=width == 360)
            for path in ("/ops/overview/", "/ops/markets/", "/ops/banking/", "/ops/jobs/", "/ops/commercial/"):
                self.assert_layout(None, path, width, authenticated=True, screenshot=path == "/ops/commercial/" and width in {360, 1280})

    def test_mobile_menu_keyboard_and_focus_contract(self):
        context = self.browser.new_context(viewport={"width": 360, "height": 780}, reduced_motion="reduce")
        page = context.new_page(); page.goto(self.live_server_url + "/", wait_until="networkidle")
        toggle = page.locator("#navToggle")
        self.assertTrue(toggle.is_visible()); toggle.click()
        self.assertEqual(toggle.get_attribute("aria-expanded"), "true")
        self.assertTrue(page.locator("#mainNav").is_visible())
        self.assertTrue(page.locator("#mainNav a").first.evaluate("element => element === document.activeElement"))
        page.keyboard.press("Escape")
        self.assertEqual(toggle.get_attribute("aria-expanded"), "false")
        self.assertTrue(toggle.evaluate("element => element === document.activeElement"))
        toggle.click()
        page.locator("main").click(position={"x": 8, "y": 8})
        self.assertEqual(toggle.get_attribute("aria-expanded"), "false")
        context.close()
