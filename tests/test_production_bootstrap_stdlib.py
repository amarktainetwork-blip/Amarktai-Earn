from pathlib import Path
import unittest


class ProductionBootstrapInvariantTests(unittest.TestCase):
    def test_web_startup_bootstraps_revenue_catalog_after_migrations(self):
        script = Path("scripts/start-web.sh").read_text(encoding="utf-8")
        migrate = script.index("python manage.py migrate --noinput")
        bootstrap = script.index("python manage.py bootstrap_revenue_catalog")
        gunicorn = script.index("exec gunicorn")
        self.assertLess(migrate, bootstrap)
        self.assertLess(bootstrap, gunicorn)


if __name__ == "__main__":
    unittest.main()
