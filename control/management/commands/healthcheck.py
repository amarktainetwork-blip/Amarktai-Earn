import os
from django.core.management.base import BaseCommand, CommandError
from django.db import connection
import redis


class Command(BaseCommand):
    help = "Fail unless PostgreSQL and Redis are reachable."

    def handle(self, *args, **options):
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1")
                if cursor.fetchone()[0] != 1:
                    raise RuntimeError("database probe returned unexpected result")
            client = redis.Redis.from_url(os.getenv("REDIS_URL", "redis://redis:6379/0"), socket_connect_timeout=3, socket_timeout=3)
            if not client.ping():
                raise RuntimeError("redis PING failed")
        except Exception as exc:
            raise CommandError(f"healthcheck failed: {exc}") from exc
        self.stdout.write(self.style.SUCCESS("database=ok redis=ok"))
