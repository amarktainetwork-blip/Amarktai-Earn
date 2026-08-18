from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]


class PublicShellRegressionTests(unittest.TestCase):
    def test_owner_dashboard_links_are_normal_navigation_links(self):
        landing = (ROOT / "control/templates/control/landing.html").read_text(encoding="utf-8")
        self.assertGreaterEqual(landing.count("href=\"{% url 'login' %}\""), 2)
        self.assertIsNone(re.search(r"<a[^>]*\sdownload(?:\s|=|>)", landing, flags=re.IGNORECASE))

        caddy = (ROOT / "Caddyfile").read_text(encoding="utf-8")
        self.assertIn("@html_pages path / /login/ /terms/", caddy)
        self.assertIn('header @html_pages Content-Disposition "inline"', caddy)

    def test_header_has_room_before_it_can_overflow(self):
        landing = (ROOT / "control/templates/control/landing.html").read_text(encoding="utf-8")
        shell_css = (ROOT / "control/static/control/launch.css").read_text(encoding="utf-8")
        self.assertIn("Products", landing)
        self.assertIn("API business", landing)
        self.assertIn("Economic engine", landing)
        nav = landing[landing.index('<nav class="main-nav"'):landing.index("</nav>")]
        self.assertNotIn("Your control", nav)
        self.assertIn("@media(max-width:768px)", shell_css)
        self.assertIn(".main-nav{display:none", shell_css)
        self.assertIn("white-space:nowrap", shell_css)

    def test_public_footers_use_one_brand_structure_and_blue_ai_spans(self):
        landing = (ROOT / "control/templates/control/landing.html").read_text(encoding="utf-8")
        shell_css = (ROOT / "control/static/control/launch.css").read_text(encoding="utf-8")
        footer_start = landing.index('<footer class="site-footer">')
        footer_end = landing.index("</footer>", footer_start)
        footer = landing[footer_start:footer_end]
        self.assertEqual(footer.count("AMARKT<span>AI</span>"), 1)
        self.assertNotIn("AI-powered earning infrastructure by", footer)
        self.assertIn(".launch-brand strong span{color:var(--blue)}", shell_css)


if __name__ == "__main__":
    unittest.main()
