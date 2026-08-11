(function () {
  "use strict";

  const root = document.getElementById("pageRoot");
  const skeleton = document.getElementById("pageSkeleton");
  const section = document.getElementById("pageContent").dataset.section;
  const refreshButton = document.getElementById("refreshButton");
  const liveState = document.getElementById("liveState");
  const lastUpdated = document.getElementById("lastUpdated");
  const toast = document.getElementById("toast");
  const sourceMap = {
    overview: ["overview", "live-work", "agents", "markets", "alerts", "earnings"],
    jobs: ["live-work"], "live-work": ["live-work"],
    agents: ["agents", "live-work"],
    money: ["overview", "earnings", "treasury", "markets"],
    treasury: ["overview", "earnings", "treasury", "accounts"],
    markets: ["markets", "accounts"], alerts: ["alerts"],
    settings: ["overview", "settings", "security", "nodes", "storage", "performance", "genx", "logs", "factory"],
    system: ["nodes", "storage", "performance", "genx", "security"],
  };
  const advanced = new Set(["genx", "nodes", "storage", "performance", "logs", "security", "settings", "earnings", "treasury"]);
  const jobStages = ["CLAIMED", "EXECUTING", "QA", "SUBMITTED", "ACCEPTED", "PAYOUT_PENDING", "SETTLED"];
  const messages = {
    MARKET_DISABLED: "Marketplace is disabled", MARKET_NOT_LIVE: "Marketplace is not live",
    PAYOUT_NOT_READY: "Complete payout onboarding", SOUTH_AFRICA_NOT_VERIFIED: "South Africa payout eligibility is not verified",
    AUTOMATION_POLICY_NOT_APPROVED: "Automation policy approval is required", AUTONOMY_OFF: "Autonomous work is switched off",
    AUTONOMY_SHADOW_ONLY: "Monitoring only while autonomy is in Shadow mode", GENX_NOT_CONFIGURED: "AI service is not configured",
    GENX_CAPABILITY_UNAVAILABLE: "A required AI capability is unavailable", CODING_SANDBOX_DISABLED: "Coding runtime is not started",
    WORKER_DISABLED: "This agent is disabled", RUNTIME_UNAVAILABLE: "Required runtime is unavailable",
    PUBLIC_WEB_DATA_DISABLED: "Public web access is disabled", SAFETY_BOUNTY_EXECUTION_DISABLED: "Safety research execution is disabled",
    SANDBOX_BROKER_SECRET_INVALID: "Sandbox broker is not configured", SANDBOX_TOKEN_SECRET_INVALID: "Sandbox access is not configured",
    UNKNOWN_REMOTE_STATE: "Remote status needs safe reconciliation", RESOURCE_CONSTRAINT: "A resource constraint needs attention",
  };
  let firstRender = true;
  let refreshing = false;
  let currentData = {};
  let pendingData = null;
  let activeJobFilter = "active";

  function esc(value) {
    return String(value === null || value === undefined ? "" : value).replace(/[&<>"']/g, (character) => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", "\"": "&quot;", "'": "&#39;"}[character]));
  }
  function human(value) { return String(value || "").replace(/[_-]+/g, " ").toLowerCase().replace(/\b\w/g, (letter) => letter.toUpperCase()); }
  function reason(value) { return messages[value] || human(value); }
  function titleForWorker(value) { return human(value).replace("Ci ", "CI ").replace("Seo ", "SEO ").replace("Ai ", "AI "); }
  function shortId(value) { const string = String(value || ""); return string.length > 16 ? string.slice(0, 8) + "…" : string || "—"; }
  function statusClass(value) {
    const status = String(value || "").toUpperCase();
    if (["SETTLED", "ACCEPTED", "READY", "PASS", "OK", "LIVE", "COMPLETED", "ENROLLED"].includes(status)) return "status-good";
    if (["EXECUTING", "WORKING", "SEARCHING", "REVIEWING", "CLAIMED", "AWARDED", "ALLOWED"].includes(status)) return "status-active";
    if (["FAILED", "ERROR", "CRITICAL", "BLOCKED", "REJECTED"].includes(status)) return "status-bad";
    if (["PAYOUT_PENDING", "SUBMITTED", "WARNING", "PENDING", "SHADOW"].includes(status)) return "status-warn";
    return "";
  }
  function stateBadge(value) { return `<span class="state-badge state-${esc(String(value || "unknown").toLowerCase().replaceAll("_", "-"))} ${statusClass(value)}">${esc(human(value || "Unknown"))}</span>`; }
  function timeAgo(value) {
    if (!value) return "Not recorded";
    const date = new Date(value); if (Number.isNaN(date.getTime())) return esc(value);
    const seconds = Math.max(0, Math.round((Date.now() - date.getTime()) / 1000));
    if (seconds < 60) return "Just now"; if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
    if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`; return `${Math.floor(seconds / 86400)}d ago`;
  }
  function cardValue(cards, label, fallback) { const card = (cards || []).find((item) => item.label === label); return card ? card.value : (fallback || "—"); }
  function modeClass(mode) { return `mode-${String(mode || "off").toLowerCase().replaceAll("_", "-")}`; }
  function lifecycle(row) {
    let current = jobStages.indexOf(row.state);
    if (row.state === "AWARDED") current = 0;
    if (row.state === "EXECUTING" && row.qa && row.qa !== "—") current = Math.max(current, 2);
    return `<div class="lifecycle" aria-label="Job lifecycle">${jobStages.map((_stage, index) => `<i class="${index < current ? "done" : index === current ? "current" : ""}"></i>`).join("")}</div>`;
  }
  function readableBlockers(row) {
    return [...new Set([...(row.blockers || []), ...(row.latest_preflight_reasons || [])])].map(reason);
  }
  function marketReady(row) {
    return row.ready_for_verified_work === true;
  }
  function firstBlocker(row) { return row.next_action_code ? reason(row.next_action_code) : readableBlockers(row)[0] || (marketReady(row) ? "Ready for verified work" : "Channel readiness evidence is incomplete"); }
  function isAgentActive(agent) {
    return String(agent && agent.status || "").toUpperCase() === "EXECUTING" && Boolean(agent && agent.current_job);
  }
  function hasActiveInteraction() {
    const focused = document.activeElement;
    const visibleFocus = focused && root.contains(focused) && focused !== root && focused.getClientRects().length > 0;
    return Boolean(root.querySelector(".drawer:not([hidden])") || root.querySelector("details[open]") || visibleFocus);
  }
  function applyPendingData() {
    if (!pendingData || hasActiveInteraction()) return;
    const data = pendingData;
    pendingData = null;
    render(data);
  }
  function panel(title, subtitle, body, link) {
    return `<section class="panel"><div class="panel-head"><div><h2>${esc(title)}</h2>${subtitle ? `<p>${esc(subtitle)}</p>` : ""}</div>${link ? `<a class="panel-link" href="${esc(link.href)}">${esc(link.label)} →</a>` : ""}</div>${body}</section>`;
  }
  function empty(title, copy) { return `<div class="empty-state"><h2>${esc(title)}</h2><p>${esc(copy)}</p></div>`; }
  function friendlyAlertTitle(row) {
    if (row.type === "PAYOUT_BLOCKER") return "Payout setup required";
    if (row.type === "MARKET_AUTH_FAILURE") return "Marketplace authentication failed";
    if (row.type === "RESOURCE_CONSTRAINT") return "Operating capacity is constrained";
    if (row.type === "AVOIDABLE_IDLE") return "Profitable work is waiting";
    if (row.type === "GROWTH_TARGET_BEHIND") return "Growth target needs review";
    return human(row.type || "Operating alert");
  }

  async function loadSources() {
    const sources = sourceMap[section] || (advanced.has(section) ? [section] : [section]);
    const entries = await Promise.all(sources.map(async (name) => {
      const response = await fetch(`/api/ops/${name}`, {credentials: "same-origin"});
      if (response.status === 401) { window.location.assign("/login/"); throw new Error("unauthorized"); }
      if (!response.ok) throw new Error(`Dashboard source ${name} is unavailable`);
      return [name, await response.json()];
    }));
    return Object.fromEntries(entries);
  }

  function renderMetrics(overview) {
    const cards = overview.cards || [];
    const metrics = [
      ["SETTLED TODAY", cardValue(cards, "SETTLED TODAY", "$0.00"), "Received and reconciled cash only", "primary settled"],
      ["PENDING PAYOUT", cardValue(cards, "PENDING PAYOUT", "$0.00"), "Earned, but not received cash", "pending"],
      ["SETTLED LAST 30 DAYS", cardValue(cards, "SETTLED 30D", "$0.00"), "Reconciled received cash", "settled"],
      ["ACTIVE JOB VALUE", cardValue(cards, "AWARDED/ACCEPTED EXPOSURE", "$0.00"), "Contract exposure, not received cash", "exposure"],
    ];
    return `<div class="metric-grid">${metrics.map(([label, value, truth, classes]) => `<article class="metric-card ${classes}"><div class="metric-label"><span>${esc(label)}</span><i></i></div><div class="metric-value">${esc(value)}</div><div class="metric-truth">${esc(truth)}</div></article>`).join("")}</div>`;
  }

  function renderAgentMini(agents, jobs) {
    const jobMap = new Map(jobs.map((job) => [job.job, job]));
    const ranked = [...agents].sort((a, b) => Number(isAgentActive(b)) - Number(isAgentActive(a))).slice(0, 3);
    if (!ranked.length) return "";
    return `<div class="agent-mini-list">${ranked.map((agent) => {
      const active = isAgentActive(agent);
      const runtimeStatus = String(agent.status || "OFFLINE").toUpperCase();
      const job = jobMap.get(agent.current_job);
      const copy = active && job ? job.title : active ? "Executing recorded work" : runtimeStatus === "READY" ? "Ready for work" : runtimeStatus === "OFFLINE" ? "Runtime not started" : human(runtimeStatus);
      return `<div class="agent-mini"><span class="agent-mini-icon">${esc(titleForWorker(agent.worker_class).charAt(0))}</span><span><strong>${esc(titleForWorker(agent.worker_class))}</strong><small>${esc(copy)}</small></span><i class="agent-mini-state ${active ? "active" : ""}"></i></div>`;
    }).join("")}</div>`;
  }

  function renderAttention(rows, limit) {
    const actionable = (rows || []).filter((row) => !["RESOLVED", "ACKNOWLEDGED"].includes(row.status)).slice(0, limit || 5);
    if (!actionable.length) return `<div class="attention-clear"><div class="clear-mark">✓</div><strong>Nothing needs your attention</strong><p>Every owner-action item is currently clear.</p></div>`;
    return `<div class="attention-list">${actionable.map((row) => `<div class="attention-item ${row.severity === "ERROR" || row.severity === "CRITICAL" ? "error" : ""}"><span class="attention-icon">${row.severity === "ERROR" || row.severity === "CRITICAL" ? "!" : "•"}</span><span><strong>${esc(friendlyAlertTitle(row))}</strong><small>${esc(row.message || reason(row.type))}</small></span></div>`).join("")}</div>`;
  }

  function jobRows(rows, limit) {
    const active = rows.filter((row) => ["CLAIMED", "AWARDED", "EXECUTING", "SUBMITTED", "ACCEPTED", "PAYOUT_PENDING"].includes(row.state)).slice(0, limit || 5);
    if (!active.length) return empty("No active jobs", "The system will show real funded work here when it exists.");
    return `<div class="panel-flush"><div class="table-header"><span>Job</span><span>Reward</span><span>Status</span><span>Progress</span><span></span></div><div class="job-list">${active.map((row) => `<div class="job-row" tabindex="0" role="button" data-job="${esc(row.job)}"><div class="job-title"><strong>${esc(row.title)}</strong><small>${esc(human(row.market))} · ${esc(human(row.task_class))}</small></div><div class="reward">${esc(row.reward)}</div><div>${stateBadge(row.state)}</div><div>${lifecycle(row)}</div><span class="row-chevron">›</span></div>`).join("")}</div></div>`;
  }

  function renderMarketStrip(rows) {
    if (!rows.length) return empty("No marketplaces configured", "Connect a marketplace to begin readiness checks.");
    return `<div class="market-strip">${rows.slice(0, 4).map((row) => { const ready = marketReady(row); return `<a class="market-mini" href="/ops/markets/"><div class="market-mini-top"><span class="market-logo">${esc(row.market.slice(0, 2))}</span><strong>${esc(human(row.market))}</strong><i class="readiness-dot ${ready ? "ready" : "blocked"}"></i></div><small>${esc(ready ? "Ready for verified work" : firstBlocker(row))}</small></a>`; }).join("")}</div>`;
  }

  function buildChart(rows) {
    if (!rows.length) return `<div class="chart-empty"><strong>Not enough settlement history yet</strong><p>Real settled and pending payouts will form this chart over time.</p></div>`;
    const byDay = new Map();
    rows.forEach((row) => {
      const source = row.settled || row.pending || row.earned;
      if (!source) return;
      const key = source.slice(0, 10); const current = byDay.get(key) || {settled: 0, pending: 0};
      const amount = Number(String(row.net || "").replace(/[^0-9.-]/g, "")) || 0;
      if (row.state === "SETTLED") current.settled += amount; else if (row.state === "PAYOUT_PENDING") current.pending += amount;
      byDay.set(key, current);
    });
    const days = [...byDay.entries()].sort(([a], [b]) => a.localeCompare(b)).slice(-14);
    if (!days.length) return `<div class="chart-empty"><strong>No dated payout history</strong><p>Charting begins when a real payout enters its lifecycle.</p></div>`;
    const max = Math.max(1, ...days.flatMap(([, values]) => [values.settled, values.pending]));
    return `<div class="bar-chart">${days.map(([day, values]) => `<div class="bar-column" title="${esc(day)}"><i class="settled-bar" style="height:${Math.max(2, values.settled / max * 100)}%"></i><i class="pending-bar" style="height:${Math.max(2, values.pending / max * 100)}%"></i><small>${esc(day.slice(5))}</small></div>`).join("")}</div><div class="chart-legend"><span><i class="settled-key"></i>Settled cash</span><span><i class="pending-key"></i>Pending payout</span></div>`;
  }

  function renderOverview(data) {
    const overview = data.overview || {cards: [], meta: {}};
    const jobs = (data["live-work"] || {}).rows || [];
    const agents = (data.agents || {}).rows || [];
    const markets = (data.markets || {}).rows || [];
    const alerts = (data.alerts || {}).rows || [];
    const earnings = (data.earnings || {}).rows || [];
    const mode = (overview.meta || {}).autonomous_mode || "OFF";
    const activeAgents = agents.filter(isAgentActive);
    const activeJobs = jobs.filter((row) => ["CLAIMED", "AWARDED", "EXECUTING", "SUBMITTED", "ACCEPTED", "PAYOUT_PENDING"].includes(row.state));
    const scanned = markets.reduce((total, row) => total + Number(row.opportunities_seen_24h || 0), 0);
    const blocked = Number(String(cardValue(overview.cards, "BLOCKED ACQUISITIONS 24H", "0")).replace(/\D/g, "")) || 0;
    const applications = markets.reduce((total, row) => total + Number(row.applications_total || 0), 0);
    const awards = markets.reduce((total, row) => total + Number(row.awards_total || 0), 0);
    const agentsExecuting = activeAgents.length > 0;
    const jobsActive = activeJobs.length > 0;
    const workTitle = agentsExecuting ? "Your digital workforce is active" : jobsActive ? "Active work is waiting for runtime activity" : "Agents are standing by";
    const workCopy = agentsExecuting
      ? `${activeAgents.length} agent${activeAgents.length === 1 ? " has" : "s have"} confirmed EXECUTING status and a current job.`
      : jobsActive
        ? `${activeJobs.length} active job${activeJobs.length === 1 ? " is" : "s are"} recorded, but no agent has confirmed executing runtime evidence.`
        : `No active jobs or executing agents are currently recorded. Autonomy is ${human(mode)}.`;
    const workSignal = agentsExecuting ? "AGENT EXECUTING" : jobsActive ? "WORK WAITING" : "STANDING BY";
    const greeting = new Date().getHours() < 12 ? "Good morning" : new Date().getHours() < 18 ? "Good afternoon" : "Good evening";
    root.innerHTML = `<section class="overview-welcome"><div><span class="eyebrow">OWNER OVERVIEW</span><h2>${greeting}</h2><p>Your autonomous earning system at a glance.</p></div><div class="autonomy-pill ${modeClass(mode)}"><small>AUTONOMY STATE</small><strong>${esc(human(mode))}</strong></div></section>
      ${renderMetrics(overview)}
      <div class="dashboard-grid"><section class="panel system-working"><div class="working-header"><div><span class="working-kicker">RUNTIME ACTIVITY</span><h2 class="working-title">${workTitle}</h2><p class="working-copy">${workCopy}</p></div><span class="working-signal ${agentsExecuting ? "active" : ""}"><i></i>${workSignal}</span></div>${renderAgentMini(agents, jobs)}</section>
      ${panel("Needs your attention", "Only meaningful owner actions", `<div class="panel-body">${renderAttention(alerts, 4)}</div>`, {href: "/ops/alerts/", label: "View alerts"})}</div>
      ${panel("Jobs in progress", "Real work moving through delivery and settlement", jobRows(jobs, 5), {href: "/ops/jobs/", label: "View all jobs"})}
      <div class="section-grid"><section class="panel"><div class="panel-head"><div><h2>Opportunity activity</h2><p>Independent persisted counts with their real reporting windows</p></div></div><div class="panel-body"><div class="pipeline"><div class="pipeline-step"><strong>${scanned}</strong><small>Opportunities seen · 24h</small></div><div class="pipeline-step"><strong>${blocked}</strong><small>Blocked preflights · 24h</small></div><div class="pipeline-step"><strong>${applications}</strong><small>Applications · all time</small></div><div class="pipeline-step"><strong>${awards}</strong><small>Awards · all time</small></div></div></div></section>
      ${panel("Earnings movement", "Settled and pending remain visually distinct", `<div class="panel-body chart-shell">${buildChart(earnings)}</div>`, {href: "/ops/money/", label: "Open money"})}</div>
      ${panel("Marketplace readiness", "Connection, policy, and payout evidence", `<div class="panel-body">${renderMarketStrip(markets)}</div>`, {href: "/ops/markets/", label: "Manage markets"})}
      ${jobDrawerShell()}`;
    bindJobDrawers(jobs);
  }

  function jobMatches(row, filter) {
    if (filter === "active") return ["CLAIMED", "AWARDED", "EXECUTING", "SUBMITTED", "ACCEPTED"].includes(row.state);
    if (filter === "payout") return row.state === "PAYOUT_PENDING";
    if (filter === "completed") return ["SETTLED", "FAILED"].includes(row.state);
    return true;
  }
  function renderJobCards(rows) {
    const filtered = rows.filter((row) => jobMatches(row, activeJobFilter));
    if (!filtered.length) return empty(activeJobFilter === "all" ? "No jobs recorded" : "Nothing in this view", "Only real persisted jobs appear here.");
    return `<div class="jobs-board">${filtered.map((row) => `<article class="job-card" tabindex="0" role="button" data-job="${esc(row.job)}"><div class="job-card-main"><div class="job-card-title"><h3>${esc(row.title)}</h3><p>${esc(human(row.market))} · ${esc(human(row.task_class))}</p></div><div><div class="job-detail-label">REWARD</div><div class="job-detail-value reward">${esc(row.reward)}</div></div><div><div class="job-detail-label">STATE</div>${stateBadge(row.state)}</div><div><div class="job-detail-label">CURRENT AGENT</div><div class="job-detail-value">${esc(row.worker && row.worker !== "—" ? titleForWorker(row.worker) : "Not assigned")}</div></div><span class="row-chevron">›</span></div><div class="job-progress">${lifecycle(row)}<div class="job-progress-labels">${jobStages.map((stage) => `<span class="${stage === row.state ? "current" : ""}">${esc(stage === "PAYOUT_PENDING" ? "Payout" : human(stage))}</span>`).join("")}</div></div></article>`).join("")}</div>`;
  }
  function renderJobs(data) {
    const rows = (data["live-work"] || {}).rows || [];
    root.innerHTML = `<div class="page-toolbar"><div class="page-toolbar-copy"><h2>${rows.length} recorded job${rows.length === 1 ? "" : "s"}</h2><p>Every card is backed by persisted marketplace and execution state.</p></div><div class="filter-tabs" role="tablist" aria-label="Job filters"><button data-filter="active" class="${activeJobFilter === "active" ? "active" : ""}">Active</button><button data-filter="payout" class="${activeJobFilter === "payout" ? "active" : ""}">Awaiting payout</button><button data-filter="completed" class="${activeJobFilter === "completed" ? "active" : ""}">Completed</button><button data-filter="all" class="${activeJobFilter === "all" ? "active" : ""}">All</button></div></div><div id="jobCards">${renderJobCards(rows)}</div>${jobDrawerShell()}`;
    root.querySelectorAll("[data-filter]").forEach((button) => button.addEventListener("click", () => { activeJobFilter = button.dataset.filter; renderJobs(data); }));
    bindJobDrawers(rows);
  }

  function jobDrawerShell() { return `<div class="drawer" id="jobDrawer" hidden><aside class="drawer-panel" role="dialog" aria-modal="true" aria-labelledby="drawerTitle"><div id="drawerContent"></div></aside></div>`; }
  function bindJobDrawers(rows) {
    root.querySelectorAll("[data-job]").forEach((element) => {
      const open = () => openJobDrawer(rows.find((row) => row.job === element.dataset.job));
      element.addEventListener("click", open); element.addEventListener("keydown", (event) => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); open(); } });
    });
  }
  function openJobDrawer(row) {
    if (!row) return; const drawer = document.getElementById("jobDrawer"); const content = document.getElementById("drawerContent");
    const opener = document.activeElement;
    const fields = [["Marketplace", human(row.market)], ["Reward", row.reward], ["Deadline", row.deadline ? new Date(row.deadline).toLocaleString() : "No deadline recorded"], ["State", human(row.state)], ["Current agent", row.worker && row.worker !== "—" ? titleForWorker(row.worker) : "Not assigned"], ["Execution", human(row.execution || "Not started")], ["Work plan", human(row.plan || "Not created")], ["Operation", human(row.operation || "Not selected")], ["QA", human(row.qa || "Not run")], ["Acceptance contract", `${human(row.acceptance_contract || "Not compiled")}${row.acceptance_contract_version ? ` · v${row.acceptance_contract_version}` : ""}`], ["Semantic acceptance", human(row.semantic_acceptance || "Not evaluated")], ["Submission ready", row.submission_ready ? "Yes" : "No"], ["Submission", human(row.submission || "Not submitted")], ["Revisions", String(row.open_revisions || 0)], ["Artifacts", String(row.artifacts || 0)]];
    const acceptance = (row.acceptance_results || []).map((item) => `<div class="readiness-item ${item.status === "PASS" ? "yes" : ""}"><i>${item.status === "PASS" ? "✓" : "!"}</i><span>${esc(human(item.id))} · ${esc(human(item.status))}</span></div>`).join("");
    const failures = (row.acceptance_failures || []).map((item) => `<span class="reason-chip">${esc(reason(item))}</span>`).join("");
    content.innerHTML = `<div class="drawer-head"><div><span class="step-kicker">JOB DETAIL</span><h2 id="drawerTitle">${esc(row.title)}</h2></div><button class="drawer-close" type="button" aria-label="Close job detail">×</button></div><div class="drawer-grid">${fields.map(([label, value]) => `<div class="drawer-field"><small>${esc(label.toUpperCase())}</small><strong>${esc(value)}</strong></div>`).join("")}</div><div class="drawer-section"><h3>Delivery lifecycle</h3>${lifecycle(row)}</div><div class="drawer-section"><h3>Acceptance &amp; submission gate</h3><div class="readiness-list">${acceptance || `<div class="readiness-item"><i>·</i><span>Not evaluated</span></div>`}</div>${failures ? `<div class="reason-list">${failures}</div>` : ""}</div><details class="ledger-disclosure"><summary>Advanced technical detail</summary><div class="panel-body"><div class="drawer-field"><small>JOB ID</small><strong>${esc(row.job)}</strong></div><div class="drawer-field"><small>COMPILER</small><strong>${esc(row.acceptance_compiler || "Not compiled")}</strong></div>${row.last_error ? `<div class="drawer-field"><small>LAST ERROR</small><strong>${esc(reason(row.last_error))}</strong></div>` : ""}</div></details>`;
    drawer.hidden = false; document.body.style.overflow = "hidden"; content.querySelector(".drawer-close").focus();
    let closed = false;
    const escapeDrawer = (event) => { if (event.key === "Escape") close(); };
    const close = () => { if (closed) return; closed = true; drawer.hidden = true; document.body.style.overflow = ""; document.removeEventListener("keydown", escapeDrawer); if (opener && opener.focus) opener.focus(); window.setTimeout(applyPendingData, 0); };
    content.querySelector(".drawer-close").addEventListener("click", close); drawer.addEventListener("click", (event) => { if (event.target === drawer) close(); });
    document.addEventListener("keydown", escapeDrawer);
  }

  function renderAgents(data) {
    const agents = (data.agents || {}).rows || []; const jobs = (data["live-work"] || {}).rows || []; const jobMap = new Map(jobs.map((job) => [job.job, job]));
    const activeCount = agents.filter(isAgentActive).length;
    root.innerHTML = `<div class="page-toolbar"><div class="page-toolbar-copy"><h2>Your digital workforce</h2><p>${activeCount} executing · ${agents.length} registered capabilities · no simulated activity</p></div></div>${agents.length ? `<div class="agent-grid">${agents.map((agent) => { const job = jobMap.get(agent.current_job); const active = isAgentActive(agent); const runtimeStatus = String(agent.status || "OFFLINE").toUpperCase(); const displayState = active ? runtimeStatus : ["ERROR", "REPAIRING"].includes(runtimeStatus) ? runtimeStatus : agent.production_enabled ? runtimeStatus : "BLOCKED"; const reasons = (agent.enablement_reason_codes || []).map(reason); const taskCopy = active && job ? job.title : job ? `${human(runtimeStatus)} · ${job.title}` : runtimeStatus === "READY" ? "Waiting for runtime work" : runtimeStatus === "OFFLINE" ? "Runtime not started" : human(runtimeStatus); return `<article class="agent-card"><div class="agent-card-top"><span class="agent-icon">${esc(titleForWorker(agent.worker_class).charAt(0))}</span><div class="agent-card-heading"><h3>${esc(titleForWorker(agent.worker_class))} Agent</h3>${stateBadge(displayState)}</div></div><p class="agent-card-copy">${esc(agent.description || "Registered autonomous worker capability")}</p><div class="agent-current"><small>CURRENT TASK</small><strong>${esc(taskCopy)}</strong></div>${reasons.length ? `<div class="reason-list">${reasons.slice(0, 3).map((item) => `<span class="reason-chip">${esc(item)}</span>`).join("")}</div>` : ""}<div class="agent-foot"><span>${Number((agent.operations || []).length)} operation${Number((agent.operations || []).length) === 1 ? "" : "s"}</span><span>${agent.last_heartbeat ? `Heartbeat ${timeAgo(agent.last_heartbeat)}` : "Runtime not started"}</span></div></article>`; }).join("")}</div>` : empty("No agents registered", "Worker registry entries will appear here when available.")}`;
  }

  function renderMoney(data) {
    const overview = data.overview || {cards: []}; const earnings = (data.earnings || {}).rows || []; const treasury = data.treasury || {rows: [], secondary_rows: []};
    const settled = cardValue(overview.cards, "SETTLED 30D", "$0.00"); const pending = cardValue(overview.cards, "PENDING PAYOUT", "$0.00"); const inbound = cardValue(overview.cards, "INBOUND SERVICE EXPOSURE", "$0.00"); const costCard = (overview.cards || []).find((row) => String(row.label || "").includes("PAID EXECUTION COST 30D")); const cost = costCard ? costCard.value : "$0.00"; const coverageComplete = overview.meta && overview.meta.settled_profit_cost_coverage_complete === true; const profitCard = (overview.cards || []).find((row) => String(row.label || "").includes("NET SETTLED PROFIT 30D")); const profit = profitCard ? profitCard.value : "$0.00"; const profitLabel = coverageComplete ? "TRUE RECORDED NET SETTLED PROFIT · 30D" : "RECORDED NET SETTLED PROFIT · COST COVERAGE INCOMPLETE";
    const payoutBody = earnings.length ? `<div class="payout-list">${earnings.map((row) => `<div class="payout-row"><div><strong>Job ${esc(shortId(row.job))}</strong><small>${esc(row.reference || "No external reference")}</small></div><strong>${esc(row.net)}</strong><div>${stateBadge(row.state)}</div><small>${esc(row.settled ? new Date(row.settled).toLocaleDateString() : row.pending ? `Pending since ${new Date(row.pending).toLocaleDateString()}` : "Earned, not settled")}</small></div>`).join("")}</div>` : empty("No payout history yet", "Real job payouts will appear here without being promoted to cash before settlement.");
    root.innerHTML = `<div class="money-hero"><article class="money-primary"><small>SETTLED CASH · LAST 30 DAYS</small><strong>${esc(settled)}</strong><p>Only bank or rail-confirmed, reconciled SETTLED payouts are presented as received cash.</p></article><article class="money-side"><span>PENDING PAYOUT</span><strong>${esc(pending)}</strong><small>Earned, not received cash</small></article><article class="money-side"><span>INBOUND SERVICE EXPOSURE</span><strong>${esc(inbound)}</strong><small>Seller-side orders in flight; never cash before settlement</small></article><article class="money-side"><span>${esc(profitLabel)}</span><strong>${esc(profit)}</strong><small>Settled payout net less recorded attributable paid execution cost (${esc(cost)}). Missing monetary cost truth remains explicit. Targets are objectives, never caps.</small></article></div><div class="section-grid"><section class="panel"><div class="panel-head"><div><h2>Earnings movement</h2><p>Real payout history only</p></div></div><div class="panel-body chart-shell">${buildChart(earnings)}</div></section><section class="panel"><div class="panel-head"><div><h2>Treasury balances</h2><p>Earned, pending, and settled remain separate</p></div></div>${treasury.rows.length ? genericTable(treasury.rows) : empty("No treasury balances", "Balances appear after real financial lifecycle records exist.")}</section></div>${panel("Payout history", "Job-to-payout linkage and settlement state", payoutBody)}<details class="ledger-disclosure"><summary>Advanced ledger · accounting evidence</summary>${treasury.secondary_rows && treasury.secondary_rows.length ? genericTable(treasury.secondary_rows) : empty("No advanced ledger activity has been recorded.")}</details>`;
  }

  function renderMarkets(data) {
    const markets = (data.markets || {}).rows || [];
    const accounts = (data.accounts || {}).rows || [];
    const meta = (data.accounts || {}).meta || {};
    const accountCard = (row) => {
      const checks = [["Credentials", ["CONFIGURED", "VERIFIED", "NOT_REQUIRED"].includes(row.credential_state)], ["Connection", row.connected], ["Work capability", row.work_ready], ["Payout receipt", row.cash_ready], ["Bounded live proof", row.live_entry_ready]];
      return `<article class="market-card account-card"><div class="market-card-head"><span class="market-logo">${esc(row.display_name.slice(0, 2))}</span><div class="market-card-title"><small>${esc(human(row.category))}</small><h3>${esc(row.display_name)}</h3><p>${esc(row.purpose)}</p></div>${stateBadge(row.setup_state)}</div><div class="market-verdict ${row.live_entry_ready ? "ready" : ""}">${esc(row.live_entry_ready ? "READY FOR BOUNDED LIVE PROOF" : row.owner_action_required)}</div><div class="readiness-list">${checks.map(([label, yes]) => `<div class="readiness-item ${yes ? "yes" : ""}"><i>${yes ? "✓" : "·"}</i>${esc(label)}</div>`).join("")}</div><button class="account-manage" type="button" data-account="${esc(row.slug)}">Manage secure setup</button></article>`;
    };
    const accountCards = [...new Set(accounts.map((row) => row.category))].map((category, index) => { const rows = accounts.filter((row) => row.category === category); return `<details class="ledger-disclosure account-group" ${index === 0 ? "open" : ""}><summary>${esc(human(category))} · ${rows.length} account${rows.length === 1 ? "" : "s"}</summary><div class="market-grid panel-body">${rows.map(accountCard).join("")}</div></details>`; }).join("");
    const marketRows = markets.length ? `<details class="ledger-disclosure"><summary>Market operating evidence · ${markets.length} canonical records</summary><div class="market-grid panel-body">${markets.map((row) => `<article class="market-card"><div class="market-card-head"><span class="market-logo">${esc(row.market.slice(0, 2))}</span><div class="market-card-title"><small>${esc(row.category)}</small><h3>${esc(human(row.market))}</h3></div>${stateBadge(row.status)}</div><div class="market-verdict ${marketReady(row) ? "ready" : ""}">${esc(marketReady(row) ? "READY FOR VERIFIED WORK" : firstBlocker(row))}</div><div class="market-stats"><div><strong>${Number(row.opportunities_seen_24h || 0)}</strong><small>24H SEEN</small></div><div><strong>${Number(row.applications_total || 0)}</strong><small>APPLICATIONS</small></div><div><strong>${Number((row.service_listings || {}).PUBLISHED || 0)}</strong><small>LIVE LISTINGS</small></div><div><strong>${esc(row.settled_net || "0.00")}</strong><small>SETTLED VALUE</small></div></div></article>`).join("")}</div></details>` : "";
    root.innerHTML = `<div class="page-toolbar"><div class="page-toolbar-copy"><h2>Markets &amp; Accounts</h2><p>One canonical place for account setup, connection, capability, payout proof, and live-entry readiness.</p></div></div><div class="health-grid"><article class="health-card"><small>ACCOUNTS</small><strong>${Number(meta.total || accounts.length)}</strong><p>Canonical catalogue</p></article><article class="health-card"><small>CONNECTED</small><strong>${Number(meta.connections_verified || 0)}</strong><p>Authoritatively verified</p></article><article class="health-card"><small>PAYOUT ROUTES PROVEN</small><strong>${Number(meta.payout_routes_proven || 0)}</strong><p>Owner receipt evidence</p></article><article class="health-card"><small>AUTONOMY</small><strong>${esc(human(meta.autonomy_state || "OFF"))}</strong><p>Never inferred from connection</p></article></div>${accounts.length ? `<div class="account-groups">${accountCards}</div>` : empty("No integration accounts", "Bootstrap the canonical disabled account catalogue.")}${marketRows}<div class="drawer" id="accountDrawer" hidden><aside class="drawer-panel" role="dialog" aria-modal="true" aria-labelledby="accountDrawerTitle"><div id="accountDrawerContent"></div></aside></div>`;
    root.querySelectorAll("[data-account]").forEach((button) => button.addEventListener("click", () => openAccountDrawer(accounts.find((row) => row.slug === button.dataset.account), button)));
  }

  function renderTreasury(data) {
    const overview = data.overview || {cards: []}; const treasury = data.treasury || {rows: [], secondary_rows: []}; const earnings = (data.earnings || {}).rows || []; const accounts = (data.accounts || {}).rows || [];
    const receiptRoutes = accounts.filter((row) => row.payout_route && row.payout_route !== "NONE");
    root.innerHTML = `<div class="page-toolbar"><div class="page-toolbar-copy"><h2>Treasury</h2><p>Payment routes, payout state, settlement, reconciliation, and owner receipt remain separate canonical stages.</p></div></div><div class="health-grid"><article class="health-card"><small>PENDING PAYOUT</small><strong>${esc(cardValue(overview.cards, "PENDING PAYOUT", "$0.00"))}</strong><p>Earned, not received cash</p></article><article class="health-card"><small>SETTLED · 30D</small><strong>${esc(cardValue(overview.cards, "SETTLED 30D", "$0.00"))}</strong><p>Reconciled received cash</p></article><article class="health-card"><small>PAYOUT ROUTES</small><strong>${receiptRoutes.length}</strong><p>Configured catalogue routes</p></article><article class="health-card"><small>RECEIPT PROVEN</small><strong>${receiptRoutes.filter((row) => row.cash_ready).length}</strong><p>Authoritative owner receipt evidence</p></article></div><div class="section-grid"><section class="panel"><div class="panel-head"><div><h2>Balances</h2><p>Earned, pending, and settled are never collapsed</p></div></div>${treasury.rows.length ? genericTable(treasury.rows) : empty("No treasury balances", "Balances appear after real financial lifecycle records exist.")}</section><section class="panel"><div class="panel-head"><div><h2>Settlement movement</h2><p>Real payout history only</p></div></div><div class="panel-body chart-shell">${buildChart(earnings)}</div></section></div>${panel("Owner receipt routes", "Withdrawal remains a human action where the provider requires it", receiptRoutes.length ? `<div class="payout-list">${receiptRoutes.map((row) => `<div class="payout-row"><div><strong>${esc(row.display_name)}</strong><small>${esc(human(row.payout_route))}</small></div><strong>${esc(human(row.payout_receipt_proof_state))}</strong><div>${stateBadge(row.live_proving_state)}</div><small>${esc(row.human_withdrawal_required ? "Human withdrawal required" : "Provider payout route")}</small></div>`).join("")}</div>` : empty("No payout routes recorded", "Canonical account setup will identify supported owner receipt routes."))}<details class="ledger-disclosure"><summary>Advanced accounting ledger</summary>${treasury.secondary_rows && treasury.secondary_rows.length ? genericTable(treasury.secondary_rows) : empty("No ledger activity", "Entries appear only from persisted financial events.")}</details>`;
  }

  function renderSettings(data) {
    const overview = data.overview || {meta: {}}; const settings = data.settings || {rows: []}; const security = data.security || {cards: [], rows: []}; const nodes = data.nodes || {rows: [], secondary_rows: []}; const storage = data.storage || {rows: [], meta: {}}; const performance = data.performance || {cards: [], rows: []}; const genx = data.genx || {rows: [], meta: {}}; const logs = data.logs || {rows: []}; const factory = data.factory || {policy: {}, products: []};
    const sections = [
      ["connections", "Connections", "Integration credentials are managed from Markets & Accounts; secret values remain write-only.", `<a class="account-manage inline-action" href="/ops/markets/">Open Markets &amp; Accounts</a>`],
      ["limits", "Autonomy & Limits", "Persisted operating configuration and safety bounds.", `<div class="health-grid"><article class="health-card"><small>AUTONOMY</small><strong>${esc(human((overview.meta || {}).autonomous_mode || "OFF"))}</strong><p>Canonical runtime mode</p></article><article class="health-card"><small>PRODUCT FACTORY</small><strong>${factory.policy && factory.policy.enabled === true ? "Enabled" : "Disabled"}</strong><p>Disabled unless explicitly owner-authorized</p></article><article class="health-card"><small>READY TO PUBLISH</small><strong>${Number(factory.ready_to_publish || 0)}</strong><p>Inventory is not revenue</p></article><article class="health-card"><small>DAILY INTERNAL CREDITS</small><strong>${esc(factory.daily_internal_credits || "0")}</strong><p>Persisted production usage</p></article></div>${genericTable(settings.rows || [])}`],
      ["ai", "AI / GenX", "Live catalogue, credits, calls, and truthful unconfigured state.", genericTable(genx.rows || [])],
      ["security", "Security", "Owner access, protected secret state, and active sessions.", `${security.cards && security.cards.length ? `<div class="health-grid">${security.cards.map((card) => `<article class="health-card"><small>${esc(card.label)}</small><strong>${valueMarkup(card.value)}</strong></article>`).join("")}</div>` : ""}${genericTable(security.rows || [])}`],
      ["health", "System Health", "Nodes, storage admission, quality, and resource evidence.", `${genericTable(nodes.rows || [])}${genericTable(storage.rows || [])}${performance.cards && performance.cards.length ? `<div class="health-grid">${performance.cards.slice(0, 4).map((card) => `<article class="health-card"><small>${esc(card.label)}</small><strong>${esc(card.value)}</strong><p>${esc(card.truth || "Persisted evidence")}</p></article>`).join("")}</div>` : ""}`],
      ["audit", "Audit", "Append-only technical events remain available without occupying primary navigation.", genericTable((logs.rows || []).slice(0, 100))],
    ];
    root.innerHTML = `<div class="page-toolbar"><div class="page-toolbar-copy"><h2>Settings</h2><p>Connections, limits, AI, security, health, and audit evidence in one bounded surface.</p></div></div><div class="settings-sections">${sections.map(([id, title, copy, body], index) => `<details class="ledger-disclosure" id="${id}" ${index === 0 ? "open" : ""}><summary>${esc(title)}</summary><div class="panel-body"><p class="settings-copy">${esc(copy)}</p>${body}</div></details>`).join("")}</div>`;
    const requested = new URLSearchParams(window.location.search).get("view"); const map = {system: "health", nodes: "health", storage: "health", performance: "health", genx: "ai", security: "security", logs: "audit"}; const target = document.getElementById(map[requested] || requested || ""); if (target) { target.open = true; target.scrollIntoView({block: "start"}); }
  }

  function secureFields() {
    return `<div class="secure-action"><p>Saving, testing, or recording proof requires fresh owner verification.</p><label>Owner password<input name="password" type="password" autocomplete="current-password" required></label><label>Current TOTP code<input name="code" inputmode="numeric" autocomplete="one-time-code" required></label></div>`;
  }

  async function mutateAccount(form, url, extra) {
    const formData = new FormData(form); const payload = {...extra, password: formData.get("password"), code: formData.get("code")};
    if (extra.credentials) payload.credentials = Object.fromEntries([...formData.entries()].filter(([key, value]) => key.startsWith("credential:") && value).map(([key, value]) => [key.slice(11), value]));
    const response = await fetch(url, {method: "POST", credentials: "same-origin", headers: {"Content-Type": "application/json", "X-CSRFToken": cookie("csrftoken")}, body: JSON.stringify(payload)});
    const result = await response.json(); if (!response.ok) throw new Error(reason(result.error || "REQUEST_FAILED")); return result;
  }

  function openAccountDrawer(row, opener) {
    if (!row) return; const drawer = document.getElementById("accountDrawer"); const content = document.getElementById("accountDrawerContent");
    const credentialInputs = (row.credential_fields || []).map((field) => `<label>${esc(field.label)}${field.required ? " *" : ""}<input name="credential:${esc(field.name)}" type="password" autocomplete="new-password" placeholder="Write-only · leave blank to keep current"></label>`).join("");
    content.innerHTML = `<div class="drawer-head"><div><span class="step-kicker">SECURE ACCOUNT SETUP</span><h2 id="accountDrawerTitle">${esc(row.display_name)}</h2></div><button class="drawer-close" type="button" aria-label="Close account setup">×</button></div><div class="drawer-grid"><div class="drawer-field"><small>SETUP</small><strong>${esc(human(row.setup_state))}</strong></div><div class="drawer-field"><small>CONNECTION</small><strong>${esc(human(row.api_connection_state))}</strong></div><div class="drawer-field"><small>WORK CAPABILITY</small><strong>${esc(human(row.work_capability_state))}</strong></div><div class="drawer-field"><small>PAYOUT RECEIPT</small><strong>${esc(human(row.payout_receipt_proof_state))}</strong></div></div><div class="account-next"><strong>Next owner action</strong><p>${esc(row.owner_action_required)}</p></div>${credentialInputs ? `<form class="account-form" id="credentialForm"><h3>Write-only credentials</h3>${credentialInputs}${secureFields()}<button type="submit">Save credentials</button></form>` : ""}<form class="account-form" id="testForm"><h3>Authoritative connection test</h3>${secureFields()}<button type="submit">Run connection test</button></form><form class="account-form" id="proofForm"><h3>Submit external proof reference</h3><label>Proof type<select name="proof_type"><option>ACCOUNT</option><option>KYC</option><option>PUBLICATION</option><option>PAYOUT_CONFIGURATION</option><option>PAYOUT_RECEIPT</option></select></label><label>Provider reference<input name="proof_reference" required></label>${secureFields()}<button type="submit">Record proof for verification</button></form>`;
    drawer.hidden = false; document.body.style.overflow = "hidden"; content.querySelector(".drawer-close").focus(); let closed = false;
    const escapeDrawer = (event) => { if (event.key === "Escape") close(); };
    const close = () => { if (closed) return; closed = true; drawer.hidden = true; document.body.style.overflow = ""; document.removeEventListener("keydown", escapeDrawer); if (opener) opener.focus(); window.setTimeout(applyPendingData, 0); };
    content.querySelector(".drawer-close").addEventListener("click", close); drawer.addEventListener("click", (event) => { if (event.target === drawer) close(); }); document.addEventListener("keydown", escapeDrawer);
    const bind = (id, url, extraFactory) => { const form = content.querySelector(id); if (!form) return; form.addEventListener("submit", async (event) => { event.preventDefault(); const button = form.querySelector("button"); button.disabled = true; try { const extra = extraFactory ? extraFactory(new FormData(form)) : {}; await mutateAccount(form, url, extra); close(); await refresh(true); } catch (error) { showToast(error.message); } finally { button.disabled = false; } }); };
    bind("#credentialForm", `/api/integrations/${encodeURIComponent(row.slug)}/credentials`, () => ({credentials: true}));
    bind("#testForm", `/api/integrations/${encodeURIComponent(row.slug)}/test`);
    bind("#proofForm", `/api/integrations/${encodeURIComponent(row.slug)}/proof`, (data) => ({proof_type: data.get("proof_type"), proof_reference: data.get("proof_reference")}));
  }

  function renderAlerts(data) {
    const rows = (data.alerts || {}).rows || []; const attention = rows.filter((row) => !["RESOLVED", "ACKNOWLEDGED"].includes(row.status)); const handled = rows.filter((row) => row.status === "ACKNOWLEDGED"); const resolved = rows.filter((row) => row.status === "RESOLVED");
    function group(title, values, type) { return `<section><div class="alert-group-head"><h2>${esc(title)}</h2><span>${values.length}</span></div>${values.length ? `<div class="alert-list">${values.map((row) => `<article class="alert-card ${row.severity === "ERROR" || row.severity === "CRITICAL" ? "error" : type === "resolved" ? "resolved" : ""}"><span class="alert-symbol">${type === "resolved" ? "✓" : row.severity === "ERROR" || row.severity === "CRITICAL" ? "!" : "•"}</span><div><h3>${esc(friendlyAlertTitle(row))}</h3><p>${esc(row.message || reason(row.type))}</p><details class="ledger-disclosure"><summary>Technical detail</summary><div class="panel-body"><code>${esc(row.type || "")}</code>${row.evidence ? `<pre class="raw-object">${esc(JSON.stringify(row.evidence, null, 2))}</pre>` : ""}</div></details></div><time class="alert-time">${esc(timeAgo(row.created))}</time></article>`).join("")}</div>` : empty(`No ${title.toLowerCase()}`, type === "attention" ? "Everything requiring owner attention is clear." : "No records in this group.")}</section>`; }
    root.innerHTML = `<div class="alert-groups">${group("Needs your attention", attention, "attention")}${group("Handled automatically", handled, "handled")}${group("Resolved", resolved, "resolved")}</div>`;
  }

  function renderSystem(data) {
    const nodes = data.nodes || {rows: [], secondary_rows: []}; const storage = data.storage || {rows: []}; const performance = data.performance || {cards: []}; const genx = data.genx || {rows: [], meta: {}}; const security = data.security || {cards: []};
    const healthyNodes = nodes.rows.filter((row) => ["OK", "HEALTHY", "LIVE"].includes(row.health)).length;
    const storageOk = storage.rows.filter((row) => row.status === "OK").length; const securityTotp = cardValue(security.cards, "TOTP", "UNKNOWN");
    const health = [["SYSTEM NODES", nodes.rows.length ? `${healthyNodes}/${nodes.rows.length} healthy` : "No snapshot", "Persisted controller and worker nodes"], ["STORAGE", storage.rows.length ? `${storageOk}/${storage.rows.length} clear` : "No snapshot", "Persistent volume capacity"], ["AI / GENX", genx.meta && genx.meta.available_credits !== null && genx.meta.available_credits !== undefined ? `${genx.meta.available_credits} credits` : "Not configured", "Live account balance evidence"], ["OWNER SECURITY", securityTotp, "TOTP and secure owner sessions"]];
    const links = [["✦", "AI / GenX", "Model catalog, calls, and credit reconciliation", "/ops/genx/"], ["⌁", "Infrastructure", "Nodes, services, and heartbeats", "/ops/nodes/"], ["▰", "Storage", "Volumes and admission capacity", "/ops/storage/"], ["↗", "Performance", "Quality, utilization, and growth", "/ops/performance/"], ["≡", "Audit logs", "Technical event evidence", "/ops/logs/"], ["◇", "Security", "Owner access and hidden secret state", "/ops/security/"], ["⚙", "Settings", "Persisted operating configuration", "/ops/settings/"]];
    root.innerHTML = `<div class="health-grid">${health.map(([label, value, copy]) => `<article class="health-card"><small>${esc(label)}</small><strong>${esc(value)}</strong><p>${esc(copy)}</p></article>`).join("")}</div><div class="system-links">${links.map(([icon, title, copy, href]) => `<a class="system-link" href="${href}"><span>${icon}</span><strong>${esc(title)}</strong><small>${esc(copy)}</small></a>`).join("")}</div>${performance.cards && performance.cards.length ? `<div style="margin-top:18px">${panel("Operating evidence", "Advanced runtime summaries", `<div class="panel-body"><div class="market-strip">${performance.cards.slice(0, 4).map((card) => `<div class="market-mini"><strong>${esc(card.label)}</strong><small>${esc(card.value)}${card.truth ? ` · ${esc(card.truth)}` : ""}</small></div>`).join("")}</div></div>`)}</div>` : ""}`;
  }

  function valueMarkup(value) {
    if (value === null || value === undefined || value === "") return "—";
    if (typeof value === "object") return `<pre class="raw-object">${esc(JSON.stringify(value, null, 2))}</pre>`;
    const string = String(value); if (/^(CONFIGURED — HIDDEN|CONFIGURED â€” HIDDEN)$/.test(string)) return `<span class="status-badge status-good">Configured · hidden</span>`;
    if (/^(OK|LIVE|READY|ENROLLED|PASS|SETTLED|COMPLETED|HEALTHY)$/.test(string)) return `<span class="status-badge status-good">${esc(human(string))}</span>`;
    if (/^(ERROR|FAILED|CRITICAL|BLOCKED)$/.test(string)) return `<span class="status-badge status-bad">${esc(human(string))}</span>`;
    return esc(string);
  }
  function genericTable(rows) {
    if (!rows || !rows.length) return empty("No records yet", "No real persisted records are available for this view.");
    const keys = [...new Set(rows.flatMap((row) => Object.keys(row)))];
    return `<div class="table-scroll"><table class="modern-table"><thead><tr>${keys.map((key) => `<th>${esc(human(key))}</th>`).join("")}</tr></thead><tbody>${rows.map((row) => `<tr>${keys.map((key) => `<td>${valueMarkup(row[key])}</td>`).join("")}</tr>`).join("")}</tbody></table></div>`;
  }
  function renderAdvanced(data) {
    const payload = data[section] || {}; const cards = payload.cards || [];
    root.innerHTML = `${cards.length ? `<div class="health-grid">${cards.map((card) => `<article class="health-card"><small>${esc(card.label)}</small><strong>${valueMarkup(card.value)}</strong>${card.truth ? `<p>${esc(card.truth)}</p>` : ""}</article>`).join("")}</div>` : ""}${payload.meta && Object.keys(payload.meta).length ? `<details class="ledger-disclosure"><summary>Runtime summary</summary><div class="panel-body">${genericTable([payload.meta])}</div></details>` : ""}<div style="margin-top:18px">${panel(human(section), "Persisted advanced operating evidence", genericTable(payload.rows || []))}</div>${payload.secondary_rows ? `<details class="ledger-disclosure"><summary>Additional technical detail</summary>${genericTable(payload.secondary_rows)}</details>` : ""}`;
  }

  function render(data) {
    currentData = data;
    if (section === "overview") renderOverview(data);
    else if (section === "jobs" || section === "live-work") renderJobs(data);
    else if (section === "agents") renderAgents(data);
    else if (section === "money") renderMoney(data);
    else if (section === "treasury") renderTreasury(data);
    else if (section === "markets") renderMarkets(data);
    else if (section === "alerts") renderAlerts(data);
    else if (section === "system") renderSystem(data);
    else if (section === "settings") renderSettings(data);
    else renderAdvanced(data);
  }

  function updateNavBadges(data) {
    const jobs = data["live-work"] ? (data["live-work"].rows || []).filter((row) => ["CLAIMED", "AWARDED", "EXECUTING", "SUBMITTED", "ACCEPTED", "PAYOUT_PENDING"].includes(row.state)).length : null;
    const alerts = data.alerts ? (data.alerts.rows || []).filter((row) => !["RESOLVED", "ACKNOWLEDGED"].includes(row.status)).length : null;
    [["navJobs", jobs], ["navAlerts", alerts]].forEach(([id, count]) => { const element = document.getElementById(id); if (element && count !== null) { element.textContent = count ? (count > 99 ? "99+" : String(count)) : ""; element.classList.toggle("visible", count > 0); } });
  }

  async function refresh(manual) {
    if (refreshing) return; refreshing = true; refreshButton.classList.add("spinning");
    try {
      const data = await loadSources();
      currentData = data;
      updateNavBadges(data);
      if (!manual && hasActiveInteraction()) pendingData = data;
      else { render(data); pendingData = null; }
      skeleton.hidden = true; root.hidden = false; firstRender = false;
      const now = new Date(); lastUpdated.textContent = now.toLocaleTimeString([], {hour: "2-digit", minute: "2-digit"}); liveState.className = "live-state live"; liveState.querySelector("span").textContent = "Data live";
      if (manual) showToast("Dashboard refreshed with persisted data.");
    } catch (loadError) {
      liveState.className = "live-state stale"; liveState.querySelector("span").textContent = "Data stale";
      if (firstRender && loadError.message !== "unauthorized") { skeleton.hidden = true; root.hidden = false; root.innerHTML = empty("Dashboard data is temporarily unavailable", "The current screen was not replaced with invented information. Try again shortly."); }
      else if (loadError.message !== "unauthorized") showToast("Refresh failed. Existing data remains on screen.");
    } finally { refreshing = false; refreshButton.classList.remove("spinning"); }
  }
  function showToast(message) { toast.textContent = message; toast.classList.add("show"); window.setTimeout(() => toast.classList.remove("show"), 2600); }
  function cookie(name) { const item = document.cookie.split("; ").find((entry) => entry.startsWith(name + "=")); return item ? decodeURIComponent(item.split("=").slice(1).join("=")) : ""; }

  refreshButton.addEventListener("click", () => refresh(true));
  document.getElementById("logoutButton").addEventListener("click", async () => {
    try { await fetch("/api/auth/logout", {method: "POST", credentials: "same-origin", headers: {"X-CSRFToken": cookie("csrftoken")}}); } finally { window.location.assign("/login/"); }
  });
  const sidebar = document.getElementById("sidebar"); const mobileMenu = document.getElementById("mobileMenu"); const overlay = document.getElementById("mobileOverlay");
  function closeNav() { sidebar.classList.remove("open"); overlay.hidden = true; mobileMenu.setAttribute("aria-expanded", "false"); }
  mobileMenu.addEventListener("click", () => { const open = !sidebar.classList.contains("open"); sidebar.classList.toggle("open", open); overlay.hidden = !open; mobileMenu.setAttribute("aria-expanded", String(open)); });
  overlay.addEventListener("click", closeNav);
  root.addEventListener("toggle", () => window.setTimeout(applyPendingData, 0), true);
  root.addEventListener("focusout", () => window.setTimeout(applyPendingData, 0));

  refresh(false);
  window.setInterval(() => { if (!document.hidden) refresh(false); }, 20000);
}());
