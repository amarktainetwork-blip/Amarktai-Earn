from django.contrib import admin
from django.urls import include, path
from control import views, webhooks

urlpatterns = [
    path("admin/", admin.site.urls),
    path("healthz", views.healthz, name="healthz"),
    path("login/", views.login_page, name="login"),
    path("", views.overview_page, name="overview"),
    path("ops/<slug:section>/", views.ops_page, name="ops_page"),
    path("api/ops/<slug:section>", views.ops_api, name="ops_api"),
    path("api/auth/csrf", views.csrf_cookie, name="csrf"),
    path("api/auth/login", views.password_login, name="password_login"),
    path("api/auth/totp", views.verify_totp, name="verify_totp"),
    path("api/auth/refresh", views.refresh_tokens, name="refresh_tokens"),
    path("api/auth/logout", views.logout_view, name="logout"),
    path("api/overview", views.overview_api, name="overview_api"),
    path("webhooks/agentgigs/", webhooks.agentgigs_webhook, name="agentgigs_webhook"),
]
