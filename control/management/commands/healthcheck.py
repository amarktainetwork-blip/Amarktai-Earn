from django.core.management.base import BaseCommand
from django.db import connection
class Command(BaseCommand):
    def handle(self, *args, **kwargs):
        with connection.cursor() as c:
            c.execute("SELECT 1")
            c.fetchone()
        self.stdout.write("ok")
