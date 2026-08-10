from django.contrib import admin
from django.urls import include, path
from control import banking_views, channel_views, views, webhooks

urlpatterns = [
    path("admin/", admin.site.urls),
    path("healthz", views.healthz, name="healthz"),
    path("login/", views.login_page, name="login"),
    path("", views.overview_page, name="overview"),
    path("ops/banking/", banking_views.banking_page, name="banking"),
    path("ops/<slug:section>/", views.ops_page, name="ops_page"),
    path("api/banking/rails", banking_views.payment_rails_api, name="payment_rails_api"),
    path("api/banking/rails/<slug:slug>/proof", banking_views.payment_rail_proof_api, name="payment_rail_proof_api"),
    path("api/banking/routes/<slug:market_slug>/proof", banking_views.market_settlement_route_api, name="market_settlement_route_api"),
    path("api/channels/priority-launch", channel_views.priority_channel_launch_api, name="priority_channel_launch_api"),
    path("api/channels/publication-exports", channel_views.priority_channel_publication_exports_api, name="priority_channel_publication_exports_api"),
    path("api/ops/<slug:section>", views.ops_api, name="ops_api"),
    path("api/auth/csrf", views.csrf_cookie, name="csrf"),
    path("api/auth/login", views.password_login, name="password_login"),
    path("api/auth/totp", views.verify_totp, name="verify_totp"),
    path("api/auth/refresh", views.refresh_tokens, name="refresh_tokens"),
    path("api/auth/logout", views.logout_view, name="logout"),
    path("api/security/reauth", views.reauthenticate, name="reauthenticate"),
    path("api/security/reset", views.security_reset, name="security_reset"),
    path("api/overview", views.overview_api, name="overview_api"),
    path("webhooks/agentgigs/", webhooks.agentgigs_webhook, name="agentgigs_webhook"),
]
