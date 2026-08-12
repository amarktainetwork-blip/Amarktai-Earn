from django.core.management.base import BaseCommand
from rq import Queue, Worker

from control.queueing import QUEUE_NAMES, connection


class Command(BaseCommand):
    help = "Run the production RQ worker after Django has initialized its app registry."

    def handle(self, *args, **options):
        redis_connection = connection()
        queues = [Queue(name, connection=redis_connection) for name in QUEUE_NAMES.values()]
        worker = Worker(queues, connection=redis_connection)
        worker.work(with_scheduler=True)
