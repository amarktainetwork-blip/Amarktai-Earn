from django.contrib import admin

from planning.models import JobAsset, JobAssetManifest, WorkPlan, WorkPlanStep, WorkPlanStepDependency

admin.site.register([JobAsset, JobAssetManifest, WorkPlan, WorkPlanStep, WorkPlanStepDependency])
