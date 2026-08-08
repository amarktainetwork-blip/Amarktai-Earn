import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class RefreshRotationContractTests(unittest.TestCase):
    def test_family_reuse_revocation_transaction_is_owned_by_rotation(self):
        auth = (ROOT / "control/jwt_auth.py").read_text(encoding="utf-8")
        views = (ROOT / "control/views.py").read_text(encoding="utf-8")
        rotate = auth[auth.index("def rotate_refresh"):auth.index("def revoke_refresh")]
        refresh_view = views[views.index("def refresh_tokens"):views.index("def logout_view")]
        self.assertIn("with transaction.atomic():", rotate)
        self.assertIn("reuse_detected = True", rotate)
        self.assertLess(rotate.index("with transaction.atomic():"), rotate.index("if reuse_detected:"))
        self.assertNotIn("with transaction.atomic():", refresh_view)


if __name__ == "__main__":
    unittest.main()
