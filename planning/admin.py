from django.contrib import admin

from planning.models import JobAsset, WorkPlan

admin.site.register([JobAsset, WorkPlan])
