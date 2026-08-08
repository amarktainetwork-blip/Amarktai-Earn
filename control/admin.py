from django.contrib import admin
from .models import Marketplace, Job, JobScore, JobLock, Worker, GenXCall, Payout, AuditEvent
for model in [Marketplace, Job, JobScore, JobLock, Worker, GenXCall, Payout, AuditEvent]:
    admin.site.register(model)
