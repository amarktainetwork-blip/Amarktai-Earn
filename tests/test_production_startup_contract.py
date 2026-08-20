from pathlib import Path

from django.test import SimpleTestCase


class ProductionStartupContractTests(SimpleTestCase):
    def test_start_web_bootstraps_launch_commercial_before_gunicorn(self):
        script = (
            Path(__file__).resolve().parents[1] / "scripts" / "start-web.sh"
        ).read_text(encoding="utf-8")
        bootstrap = "python manage.py bootstrap_launch_commercial"
        gunicorn = "exec gunicorn"

        self.assertEqual(script.count(bootstrap), 1)
        self.assertIn(gunicorn, script)
        self.assertLess(script.index(bootstrap), script.index(gunicorn))
