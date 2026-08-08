import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class LaunchHardeningContractTests(unittest.TestCase):
    def test_watcher_shares_source_asset_storage_with_worker(self):
        compose = (ROOT / "docker-compose.yml").read_text()
        watcher = compose[compose.index("\n  watcher:"):compose.index("\n  postgres:")]
        self.assertIn("uploads:/var/lib/amarktai-earn/uploads", watcher)
        self.assertIn("job_data:/var/lib/amarktai-earn/jobs", watcher)
        self.assertIn("production_check", watcher)

    def test_runtime_containers_drop_privilege_escalation(self):
        compose = (ROOT / "docker-compose.yml").read_text()
        for service, end in (("web", "worker"), ("worker", "watcher"), ("watcher", "postgres")):
            block = compose[compose.index(f"\n  {service}:"):compose.index(f"\n  {end}:")]
            self.assertIn("no-new-privileges:true", block)
            self.assertIn("init: true", block)

    def test_shared_app_volumes_are_initialized_before_nonroot_services(self):
        compose = (ROOT / "docker-compose.yml").read_text()
        init_block = compose[compose.index("\n  volume-init:"):compose.index("\n  web:")]
        self.assertIn('user: "0:0"', init_block)
        self.assertIn("/app/scripts/init-volumes.sh", init_block)
        self.assertIn("backups:/var/lib/amarktai-earn/backups", init_block)
        for service, end in (("web", "worker"), ("worker", "watcher"), ("watcher", "postgres")):
            block = compose[compose.index(f"\n  {service}:"):compose.index(f"\n  {end}:")]
            self.assertIn("volume-init:", block)
            self.assertIn("service_completed_successfully", block)
        script = (ROOT / "scripts" / "init-volumes.sh").read_text()
        self.assertIn("chown amarktai:amarktai", script)
        self.assertIn("chmod 0750", script)

    def test_docker_build_context_excludes_runtime_secrets(self):
        ignore = (ROOT / ".dockerignore").read_text()
        self.assertIn(".env\n", ignore)
        self.assertIn(".env.*", ignore)
        self.assertIn("!.env.example", ignore)
        self.assertIn(".git", ignore)

    def test_image_contains_backup_dependencies_and_scripts_are_executable(self):
        dockerfile = (ROOT / "Dockerfile").read_text()
        self.assertIn("postgresql-client", dockerfile)
        self.assertIn("gnupg", dockerfile)
        self.assertIn("chmod +x /app/scripts/*.sh", dockerfile)

    def test_backup_uses_remote_postgres_and_no_redundant_gzip(self):
        backup = (ROOT / "scripts" / "backup.sh").read_text()
        restore = (ROOT / "scripts" / "restore.sh").read_text()
        for text in (backup, restore):
            self.assertIn('PGHOST="${POSTGRES_HOST:-postgres}"', text)
            self.assertIn('PGPORT="${POSTGRES_PORT:-5432}"', text)
            self.assertIn('PGPASSWORD="$POSTGRES_PASSWORD"', text)
            self.assertNotIn("gunzip", text)
            self.assertIn("GNUPGHOME", text)
            self.assertIn("mktemp -d /tmp/amarktai-gnupg.XXXXXX", text)
            self.assertIn('chmod 700 "$GNUPGHOME"', text)
        self.assertNotIn("| gzip", backup)
        self.assertIn("pg_restore --list", backup)
        self.assertIn("--passphrase-fd 3", backup)
        self.assertNotIn('--passphrase "$BACKUP_PASSPHRASE"', backup)
        self.assertIn("--exit-on-error", restore)

    def test_web_start_fails_closed_through_production_preflight(self):
        text = (ROOT / "scripts" / "start-web.sh").read_text()
        self.assertIn("python manage.py production_check", text)
        preflight = (ROOT / "control" / "management" / "commands" / "production_check.py").read_text()
        for value in ("DJANGO_SECRET_KEY", "POSTGRES_PASSWORD", "BACKUP_PASSPHRASE", "LOW_RISK", "FULL", "Fernet"):
            self.assertIn(value, preflight)


if __name__ == "__main__":
    unittest.main()
