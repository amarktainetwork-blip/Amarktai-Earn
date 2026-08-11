from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]


class PublicShellRegressionTests(unittest.TestCase):
    def test_owner_dashboard_links_are_normal_navigation_links(self):
        landing = (ROOT / "control/templates/control/landing.html").read_text(encoding="utf-8")
        self.assertGreaterEqual(landing.count("href=\"{% url 'login' %}\""), 4)
        self.assertIsNone(re.search(r"<a[^>]*\sdownload(?:\s|=|>)", landing, flags=re.IGNORECASE))

        caddy = (ROOT / "Caddyfile").read_text(encoding="utf-8")
        self.assertIn("@html_pages path / /login/ /terms/", caddy)
        self.assertIn('header @html_pages Content-Disposition "inline"', caddy)

    def test_header_has_room_before_it_can_overflow(self):
        landing = (ROOT / "control/templates/control/landing.html").read_text(encoding="utf-8")
        shell_css = (ROOT / "control/static/control/landing-shell.css").read_text(encoding="utf-8")
        self.assertIn("What it does", landing)
        self.assertIn("Ways to earn", landing)
        self.assertIn("How it works", landing)
        nav = landing[landing.index('<nav class="main-nav"'):landing.index("</nav>")]
        self.assertNotIn("Your control", nav)
        self.assertIn("@media (max-width:1180px)", shell_css)
        self.assertIn(".main-nav{display:none}", shell_css)
        self.assertIn("white-space:nowrap", shell_css)

    def test_public_footers_use_one_brand_structure_and_blue_ai_spans(self):
        landing = (ROOT / "control/templates/control/landing.html").read_text(encoding="utf-8")
        terms = (ROOT / "control/templates/control/terms.html").read_text(encoding="utf-8")
        shell_css = (ROOT / "control/static/control/landing-shell.css").read_text(encoding="utf-8")

        expected_copyright = '© 2026 Amarkt<span class="brand-ai">AI</span> Earn. All rights reserved.'
        expected_network = 'Part of the Amarkt<span class="brand-ai">AI</span> Network'
        for source in (landing, terms):
            self.assertIn(expected_copyright, source)
            self.assertIn(expected_network, source)

        footer_start = landing.index('<footer class="footer shell">')
        footer_end = landing.index("</footer>", footer_start)
        footer = landing[footer_start:footer_end]
        self.assertEqual(footer.count("AMARKT<span class=\"brand-ai\">AI</span>"), 1)
        self.assertNotIn("AI-powered earning infrastructure by", footer)
        self.assertIn(".footer .brand-ai{color:var(--blue)}", shell_css)


if __name__ == "__main__":
    unittest.main()
