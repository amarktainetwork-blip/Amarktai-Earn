from pathlib import Path
import unittest


class BackupScriptContractTests(unittest.TestCase):
    def setUp(self):
        self.source = (Path(__file__).resolve().parents[1] / "scripts" / "backup.sh").read_text(encoding="utf-8")

    def test_backup_verification_uses_file_not_decrypt_pipe(self):
        self.assertIn('verify_tmp="$(mktemp /tmp/amarktai-backup-verify.', self.source)
        self.assertIn('--output "$verify_tmp" "$tmp"', self.source)
        self.assertIn('pg_restore --list "$verify_tmp" >/dev/null', self.source)
        self.assertNotIn('| pg_restore --list', self.source)

    def test_verified_archive_is_published_only_after_validation(self):
        verify_index = self.source.index('pg_restore --list "$verify_tmp" >/dev/null')
        publish_index = self.source.index('mv "$tmp" "$out"')
        self.assertLess(verify_index, publish_index)
        self.assertIn('rm -f "$tmp" "$verify_tmp"', self.source)


if __name__ == "__main__":
    unittest.main()
