from pathlib import Path

from django.contrib.auth import get_user_model
from django.contrib.staticfiles import finders
from django.test import TestCase

from control.jwt_auth import issue_access
from control.models import OwnerSecurityProfile


class OwnerUIIntegrationTests(TestCase):
    def setUp(self):
        self.owner = get_user_model().objects.create_user(
            username="ui-owner",
            password="Strong-Owner-UI-Password-2026!",
            is_staff=True,
        )
        OwnerSecurityProfile.objects.create(user=self.owner)

    def authenticate(self):
        self.client.cookies["amarktai_access"] = issue_access(self.owner)

    def test_public_root_is_plain_language_landing_page_without_owner_runtime_payload(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "It finds the work.")
        self.assertContains(response, "Runs the job.")
        self.assertContains(response, "Tracks the money.")
        self.assertContains(response, "You set the rules. The system runs the loop.")
        self.assertContains(response, "owner-controlled AI earning system")
        self.assertContains(response, "Payout is not cash until it is proven")
        self.assertContains(response, "One system")
        self.assertContains(response, "Lots of ways to Earn")
        self.assertNotContains(response, "Lots of ways to make money")
        self.assertNotContains(response, "Find work.<br>Do it well.<br><em>Get paid.</em>")
        self.assertContains(response, 'href="/login/"')
        self.assertContains(response, 'href="/terms/"')
        self.assertContains(response, 'Part of the Amarkt<span class="brand-ai">AI</span> Network')
        self.assertContains(response, '© 2026 Amarkt<span class="brand-ai">AI</span> Earn. All rights reserved.')
        self.assertContains(response, 'AMARKT<span class="brand-ai">AI</span> <span class="brand-earn">EARN</span>')
        self.assertNotContains(response, 'data-section="overview"')
        self.assertNotContains(response, "/api/ops/")
        self.assertNotContains(response, "navJobs")
        self.assertNotContains(response, "CONTROL PLANE")
        self.assertNotContains(response, "BOUNDED AUTONOMY")
        self.assertNotContains(response, "economic evidence")

    def test_public_terms_are_accessible_and_do_not_promise_earnings(self):
        response = self.client.get("/terms/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "does not guarantee income, jobs, sales, profit, payouts, or any financial return")
        self.assertContains(response, "No statement on this website should be understood as a guarantee of daily income")
        self.assertContains(response, "laws of the Republic of South Africa")
        self.assertContains(response, "Nothing in these terms excludes rights or remedies that cannot lawfully be excluded")
        self.assertContains(response, "Protection of Personal Information Act")
        self.assertContains(response, "Third-party marketplaces and services")

    def test_authenticated_owner_root_redirects_directly_to_private_overview(self):
        self.authenticate()
        response = self.client.get("/")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, "/ops/overview/")

    def test_login_page_is_branded_step_by_step_and_recovery_remains_secondary(self):
        response = self.client.get("/login/")
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertContains(response, "Your earning operation.")
        self.assertContains(response, "One control centre.")
        self.assertContains(response, "what money has actually settled")
        self.assertNotContains(response, "Autonomous income.")
        self.assertContains(response, 'id="accountForm"')
        self.assertContains(response, 'id="verificationStep"')
        self.assertContains(response, "Two-factor authentication")
        self.assertContains(response, "Use a recovery code")
        self.assertIn('id="verificationStep"', html)
        self.assertIn("hidden", html[html.index('id="verificationStep"') - 80:html.index('id="verificationStep"') + 140])
        self.assertNotIn("localStorage", html)

    def test_authenticated_owner_login_redirects_to_private_overview(self):
        self.authenticate()
        response = self.client.get("/login/")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, "/ops/overview/")

    def test_login_javascript_preserves_existing_auth_endpoints_and_enters_dashboard_directly(self):
        script_path = Path(finders.find("control/login.js"))
        script = script_path.read_text(encoding="utf-8")
        self.assertIn('fetch("/api/auth/csrf"', script)
        self.assertIn('post("/api/auth/login"', script)
        self.assertIn('post("/api/auth/totp"', script)
        self.assertIn('window.location.replace("/ops/overview/")', script)
        self.assertNotIn('window.location.assign("/")', script)
        self.assertNotIn('window.location.replace("/")', script)
        self.assertIn("recovery: recoveryMode", script)
        self.assertNotIn("localStorage", script)
        self.assertNotIn("sessionStorage", script)

    def test_unauthenticated_owner_pages_redirect_to_login(self):
        for path in ("/ops/overview/", "/ops/jobs/", "/ops/agents/", "/ops/money/", "/ops/banking/", "/ops/markets/", "/ops/alerts/", "/ops/system/"):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 302)
                self.assertEqual(response.url, "/login/")

    def test_normal_and_advanced_owner_pages_render_for_authenticated_owner(self):
        self.authenticate()
        sections = (
            "jobs", "agents", "money", "alerts", "system", "genx", "nodes",
            "storage", "performance", "logs", "security", "settings", "live-work", "earnings", "treasury",
        )
        overview = self.client.get("/ops/overview/")
        self.assertEqual(overview.status_code, 200)
        self.assertContains(overview, 'data-section="overview"')
        self.assertContains(overview, "System &amp; Settings")
        self.assertContains(overview, "EARNING OPERATIONS")
        self.assertContains(overview, ">Work<")
        self.assertContains(overview, ">Earnings<")
        self.assertContains(overview, "Treasury &amp; Settlement")
        self.assertContains(overview, 'href="/ops/overview/"')
        for section in sections:
            with self.subTest(section=section):
                response = self.client.get(f"/ops/{section}/")
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, f'data-section="{section}"')

        markets = self.client.get("/ops/markets/")
        self.assertEqual(markets.status_code, 200)
        self.assertContains(markets, "Only owner-usable earning routes belong here")

        treasury = self.client.get("/ops/banking/")
        self.assertEqual(treasury.status_code, 200)
        self.assertContains(treasury, "Open accounts, prove payout routes")

    def test_ui_assets_are_discoverable_and_preserve_settled_cash_language(self):
        self.assertIsNotNone(finders.find("control/app.css"))
        self.assertIsNotNone(finders.find("control/app.js"))
        self.assertIsNotNone(finders.find("control/login.css"))
        self.assertIsNotNone(finders.find("control/landing.css"))
        self.assertIsNotNone(finders.find("control/terms.css"))
        app_styles = Path(finders.find("control/app.css")).read_text(encoding="utf-8")
        app_script = Path(finders.find("control/app.js")).read_text(encoding="utf-8")
        landing_styles = Path(finders.find("control/landing.css")).read_text(encoding="utf-8")
        self.assertIn("[hidden]{display:none!important}", app_styles)
        self.assertIn("Only bank or rail-confirmed, reconciled SETTLED payouts", app_script)
        self.assertIn("Contract exposure, not received cash", app_script)
        self.assertIn("Earned, but not received cash", app_script)
        self.assertIn("animation:ticker 12s linear infinite", landing_styles)
        self.assertIn(".login-link", landing_styles)
        self.assertIn("color:#fff", landing_styles)
        self.assertIn(".brand-ai{color:var(--blue)}", landing_styles)
        self.assertNotIn("fake", app_script.lower())

    def test_opportunity_activity_never_presents_all_time_awards_as_a_24_hour_funnel(self):
        app_script = Path(finders.find("control/app.js")).read_text(encoding="utf-8")
        self.assertIn("Opportunities seen · 24h", app_script)
        self.assertIn("Blocked preflights · 24h", app_script)
        self.assertIn("Applications · all time", app_script)
        self.assertIn("Awards · all time", app_script)
        self.assertNotIn("scanned - blocked", app_script)
        self.assertNotIn("Opportunity pipeline", app_script)

    def test_agent_activity_requires_executing_status_and_a_current_job(self):
        app_script = Path(finders.find("control/app.js")).read_text(encoding="utf-8")
        self.assertIn('toUpperCase() === "EXECUTING" && Boolean(agent && agent.current_job)', app_script)
        self.assertGreaterEqual(app_script.count("filter(isAgentActive)"), 2)
        self.assertIn("const active = isAgentActive(agent)", app_script)
        self.assertIn("Active work is waiting for runtime activity", app_script)
        self.assertIn("no agent has confirmed executing runtime evidence", app_script)
        self.assertNotIn('!["OFFLINE", "READY", "WAITING"].includes', app_script)

    def test_polling_preserves_active_interaction_and_badges_clear_at_zero(self):
        app_script = Path(finders.find("control/app.js")).read_text(encoding="utf-8")
        self.assertIn('.drawer:not([hidden])', app_script)
        self.assertIn('details[open]', app_script)
        self.assertIn("if (!manual && hasActiveInteraction()) pendingData = data", app_script)
        self.assertIn("function applyPendingData()", app_script)
        self.assertIn("window.setTimeout(applyPendingData, 0)", app_script)
        self.assertIn('element.textContent = count ? (count > 99 ? "99+" : String(count)) : ""', app_script)
        self.assertIn('element.classList.toggle("visible", count > 0)', app_script)
        self.assertIn("activeJobFilter", app_script)

    def test_polling_and_market_value_labels_do_not_overclaim_runtime_or_currency(self):
        app_script = Path(finders.find("control/app.js")).read_text(encoding="utf-8")
        self.assertIn("Your autonomous earning system at a glance.", app_script)
        self.assertIn('textContent = "Data live"', app_script)
        self.assertIn("SETTLED VALUE", app_script)
        self.assertNotIn('strong>$${esc(row.settled_net', app_script)
        self.assertNotIn("monitoring opportunities and managing", app_script)
