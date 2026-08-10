from pathlib import Path
import unittest


class ProductionBootstrapInvariantTests(unittest.TestCase):
    def test_web_startup_bootstraps_revenue_catalog_and_channel_packages_after_migrations(self):
        script = Path("scripts/start-web.sh").read_text(encoding="utf-8")
        migrate = script.index("python manage.py migrate --noinput")
        revenue_bootstrap = script.index("python manage.py bootstrap_revenue_catalog")
        package_bootstrap = script.index("python manage.py bootstrap_channel_packages")
        gunicorn = script.index("exec gunicorn")
        self.assertLess(migrate, revenue_bootstrap)
        self.assertLess(revenue_bootstrap, package_bootstrap)
        self.assertLess(package_bootstrap, gunicorn)


if __name__ == "__main__":
    unittest.main()
