(function () {
  "use strict";

  const root = document.getElementById("bankingRoot");
  const skeleton = document.getElementById("pageSkeleton");
  const liveState = document.getElementById("liveState");
  const lastUpdated = document.getElementById("lastUpdated");
  const refreshButton = document.getElementById("refreshButton");
  const toast = document.getElementById("toast");
  const sidebar = document.getElementById("sidebar");
  const mobileMenu = document.getElementById("mobileMenu");
  const mobileOverlay = document.getElementById("mobileOverlay");
  const logoutButton = document.getElementById("logoutButton");

  let csrfReady = false;
  let loading = false;

  function esc(value) {
    return String(value === null || value === undefined ? "" : value).replace(/[&<>"']/g, (character) => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", "\"": "&quot;", "'": "&#39;"}[character]));
  }

  function human(value) {
    return String(value || "").replace(/[_-]+/g, " ").toLowerCase().replace(/\b\w/g, (letter) => letter.toUpperCase());
  }

  function cookie(name) {
    const match = document.cookie.split("; ").find((row) => row.startsWith(`${name}=`));
    return match ? decodeURIComponent(match.split("=").slice(1).join("=")) : "";
  }

  async function ensureCsrf() {
    if (csrfReady && cookie("csrftoken")) return;
    const response = await fetch("/api/auth/csrf", {credentials: "same-origin"});
    if (!response.ok) throw new Error("Unable to initialize secure form protection");
    csrfReady = true;
  }

  function showToast(message, kind) {
    if (!toast) return;
    toast.textContent = message;
    toast.classList.toggle("error", kind === "error");
    toast.classList.add("show");
    window.setTimeout(() => toast.classList.remove("show"), 3500);
  }

  function stateBadge(value) {
    const raw = String(value || "NOT_CONFIGURED").toLowerCase();
    return `<span class="state-badge state-${esc(raw)}">${esc(human(value || "NOT_CONFIGURED"))}</span>`;
  }

  function check(label, enabled) {
    return `<div class="rail-check ${enabled ? "on" : ""}"><i></i><span>${esc(label)}</span></div>`;
  }

  function option(value, selected, label) {
    return `<option value="${esc(value)}" ${value === selected ? "selected" : ""}>${esc(label || human(value))}</option>`;
  }

  function accountList(title, rows, tone) {
    const items = rows || [];
    return `<section class="account-column ${esc(tone || "")}">
      <div class="account-heading"><small>${esc(title)}</small><strong>${esc(items.length)}</strong></div>
      ${items.map((row, index) => `<div class="account-row"><span>${esc(index + 1)}</span><div><strong>${esc(row.display_name)}</strong><p>${esc(row.purpose)}</p></div></div>`).join("") || '<p class="rail-action">None.</p>'}
    </section>`;
  }

  function railCard(row) {
    const proof = row.proof_reference || "No non-secret proof reference recorded";
    const verified = row.verified_at ? new Date(row.verified_at).toLocaleString() : "Not verified";
    const statuses = ["NOT_CONFIGURED", "ACCOUNT_PROOF_REQUIRED", "PENDING_EXTERNAL_APPROVAL", "VERIFIED", "BLOCKED", "PAUSED"];
    const slug = esc(row.slug);
    return `<article class="rail-card" data-rail="${slug}">
      <div class="rail-head">
        <div><small>${esc(human(row.category))}</small><h2>${esc(row.display_name)}</h2></div>
        <div class="rail-badges">${stateBadge(row.status)}<span class="withdrawal-badge ${row.human_withdrawal_required ? "human" : "auto"}">${row.human_withdrawal_required ? "HUMAN WITHDRAWAL" : "PROVIDER MANAGED"}</span></div>
      </div>
      <div class="rail-capabilities">${(row.candidate_capabilities || []).map((item) => `<span>${esc(human(item))}</span>`).join("")}</div>
      <div class="receipt-strip"><small>RECEIPT</small><strong>${esc(human(row.receipt_mode))}</strong><small>AFTER RECEIPT</small><strong>${esc(human(row.withdrawal_mode))}</strong></div>
      <div class="rail-checks">
        ${check("South Africa / owner use verified", row.south_africa_verified)}
        ${check("Checkout enabled", row.checkout_enabled)}
        ${check("Payout receipt enabled", row.payout_receive_enabled)}
        ${check("External settlement proof", row.final_settlement_enabled)}
      </div>
      <div class="security-note"><strong>No financial secrets stored</strong><span>${esc(row.external_configuration_note || "Configure sensitive destination details in the provider account only.")}</span></div>
      <div class="rail-proof"><small>NON-SECRET PROOF REFERENCE</small><strong>${esc(proof)}</strong><small>Verified: ${esc(verified)}</small></div>
      <p class="rail-action">${esc(row.owner_action || "Owner proof is required before activation.")}</p>
      ${row.notes ? `<p class="rail-action">${esc(row.notes)}</p>` : ""}
      <details class="proof-editor">
        <summary>Update account proof</summary>
        <form class="proof-form" data-proof-form="${slug}">
          <label>Status<select name="status">${statuses.map((item) => option(item, row.status)).join("")}</select></label>
          <label>Proof reference<input name="proof_reference" maxlength="255" value="${esc(row.proof_reference || "")}" placeholder="Account/KYC/public proof reference only"></label>
          <div class="toggle-row">
            <label><input type="checkbox" name="south_africa_verified" ${row.south_africa_verified ? "checked" : ""}> Owner/SA use verified</label>
            <label><input type="checkbox" name="checkout_enabled" ${row.checkout_enabled ? "checked" : ""}> Checkout enabled</label>
            <label><input type="checkbox" name="payout_receive_enabled" ${row.payout_receive_enabled ? "checked" : ""}> Payout receipt enabled</label>
            <label><input type="checkbox" name="final_settlement_enabled" ${row.final_settlement_enabled ? "checked" : ""}> External settlement observed</label>
          </div>
          <label class="wide">Next owner action<input name="owner_action" maxlength="500" value="${esc(row.owner_action || "")}"></label>
          <label class="wide">Notes<textarea name="notes" maxlength="1000">${esc(row.notes || "")}</textarea></label>
          <label>Password<input type="password" name="password" autocomplete="current-password" required></label>
          <label>TOTP code<input name="code" inputmode="numeric" autocomplete="one-time-code" required></label>
          <div class="proof-actions"><button type="submit">Save account state</button></div>
        </form>
      </details>
    </article>`;
  }

  function routeCard(row, railsBySlug) {
    const statuses = ["UNMAPPED", "PROPOSED", "VERIFIED", "BLOCKED", "PAUSED"];
    const candidateSlugs = row.candidate_rails || [];
    const railOptions = [option("", row.selected_rail, candidateSlugs.length ? "Choose receipt rail" : "No supported receipt rail proven"), ...candidateSlugs.map((slug) => option(slug, row.selected_rail, (railsBySlug[slug] || {}).display_name || human(slug)))].join("");
    const blockers = row.blockers || [];
    return `<article class="rail-card route-card" data-route="${esc(row.market)}">
      <div class="rail-head">
        <div><small>#${esc(row.priority_rank)} · ${esc(human(row.priority_tier))}</small><h2>${esc(row.market_display_name || human(row.market))}</h2></div>
        ${stateBadge(row.status)}
      </div>
      <div class="rail-checks">
        ${check("Marketplace payout ready", row.market_payout_ready)}
        ${check("Owner receipt route ready", row.ready)}
        ${check("Automatic receipt ready", row.receipt_ready)}
        ${check("Human withdrawal allowed", row.human_withdrawal_required)}
      </div>
      <div class="rail-proof"><small>OWNER RECEIPT RAIL</small><strong>${esc(row.selected_rail ? ((railsBySlug[row.selected_rail] || {}).display_name || human(row.selected_rail)) : "Not mapped")}</strong><small>${esc(row.proof_reference || "No route proof recorded")}</small></div>
      ${candidateSlugs.length ? "" : '<p class="rail-action blocked-copy">No usable owner receipt rail is currently proven for this market. Do not spend activation effort until that changes.</p>'}
      ${blockers.length ? `<p class="rail-action">Blocked: ${esc(blockers.map(human).join(" · "))}</p>` : `<p class="rail-action">Verified marketplace-to-owner receipt route. Any later personal bank withdrawal may be performed by a human outside AmarktAI.</p>`}
      <details class="proof-editor">
        <summary>Plan or verify receipt route</summary>
        <form class="proof-form" data-route-form="${esc(row.market)}">
          <label>Status<select name="status">${statuses.map((item) => option(item, row.status)).join("")}</select></label>
          <label>Receipt rail<select name="selected_rail">${railOptions}</select></label>
          <label class="wide">Proof reference<input name="proof_reference" maxlength="255" value="${esc(row.proof_reference || "")}" placeholder="Non-secret payout-route evidence"></label>
          <label class="wide">Notes<textarea name="notes" maxlength="1000">${esc(row.notes || "")}</textarea></label>
          <label>Password<input type="password" name="password" autocomplete="current-password" required></label>
          <label>TOTP code<input name="code" inputmode="numeric" autocomplete="one-time-code" required></label>
          <div class="proof-actions"><button type="submit">Save receipt route</button></div>
        </form>
      </details>
    </article>`;
  }

  function render(data) {
    const rows = data.rows || [];
    const meta = data.meta || {};
    const accounts = data.account_setup || {};
    const routes = data.settlement_routes || [];
    const routeMeta = data.settlement_route_meta || {};
    const railsBySlug = Object.fromEntries(rows.map((row) => [row.slug, row]));

    root.innerHTML = `<div class="banking-hero">
      <article class="banking-primary"><small>TREASURY CONTROL</small><strong>${esc(meta.ready_rails || 0)} receipt rail(s) ready</strong><p>AmarktAI automates earning and payout receipt. Personal bank withdrawals can remain human. No bank account number, FNB profile, wallet private key or exchange withdrawal secret is stored here.</p></article>
      <article><small>OPEN NOW</small><strong>${esc(meta.accounts_open_now || 0)}</strong><p>Core accounts needed for the first earning wave.</p></article>
      <article><small>PAYOUT RECEIPT READY</small><strong>${esc(meta.payout_receive_ready || 0)}</strong><p>Verified rails able to receive marketplace money.</p></article>
      <article><small>HUMAN WITHDRAWAL RAILS</small><strong>${esc(meta.human_withdrawal_rails || 0)}</strong><p>Money can arrive automatically; you withdraw later when convenient.</p></article>
    </div>

    <section class="section-heading"><div><small>ACCOUNT OPENING PLAN</small><h2>What you need to open</h2></div><p>Opening an account does not mark it ready. We still record KYC/API/payout proof before automation is enabled.</p></section>
    <div class="account-grid">
      ${accountList("OPEN NOW", accounts.open_now, "primary")}
      ${accountList("OPEN NEXT", accounts.open_next, "secondary")}
      ${accountList("OPTIONAL", accounts.optional, "optional")}
    </div>

    <section class="section-heading"><div><small>OWNER RECEIPT RAILS</small><h2>Where earnings can land</h2></div><p>Sensitive destination setup stays inside each provider. The dashboard records status and non-secret proof only.</p></section>
    <div class="rail-grid">${rows.map(railCard).join("")}</div>
    <div class="banking-truth"><strong>Treasury truth:</strong> ${esc(meta.truth || "Payment and payout evidence remains fail-closed.")}</div>

    <section class="section-heading"><div><small>MARKET → TREASURY</small><h2>Receipt routing</h2></div><p>Only active earning candidates are shown. Stripe-only or otherwise unsupported routes remain visibly blocked rather than pretending they are usable.</p></section>
    <div class="route-summary"><strong>${esc(routeMeta.ready_routes || 0)} verified</strong><span>${esc(routeMeta.blocked_routes || 0)} blocked/unmapped</span><span>${esc(routeMeta.human_withdrawal_routes || 0)} may end in a human withdrawal</span></div>
    <div class="rail-grid">${routes.map((row) => routeCard(row, railsBySlug)).join("")}</div>
    <div class="banking-truth"><strong>Receipt route truth:</strong> ${esc(routeMeta.truth || "Explicit marketplace-to-owner receipt mapping remains fail-closed.")}</div>`;

    skeleton.hidden = true;
    root.hidden = false;
    bindForms();
  }

  async function load() {
    if (loading) return;
    loading = true;
    if (refreshButton) refreshButton.disabled = true;
    try {
      const response = await fetch("/api/banking/rails", {credentials: "same-origin"});
      if (response.status === 401) {
        window.location.assign("/login/");
        return;
      }
      if (!response.ok) throw new Error("Treasury data is unavailable");
      render(await response.json());
      if (liveState) {
        liveState.classList.add("connected");
        liveState.querySelector("span").textContent = "Data live";
      }
      if (lastUpdated) lastUpdated.textContent = new Date().toLocaleTimeString();
    } catch (error) {
      showToast(error.message || "Unable to load treasury data", "error");
      if (liveState) liveState.querySelector("span").textContent = "Data unavailable";
    } finally {
      loading = false;
      if (refreshButton) refreshButton.disabled = false;
    }
  }

  function clearSecrets(form) {
    const password = form.querySelector('input[name="password"]');
    const code = form.querySelector('input[name="code"]');
    if (password) password.value = "";
    if (code) code.value = "";
  }

  function bindForms() {
    root.querySelectorAll("form[data-proof-form]").forEach((form) => {
      form.addEventListener("submit", async (event) => {
        event.preventDefault();
        const button = form.querySelector("button[type=submit]");
        button.disabled = true;
        const fields = new FormData(form);
        try {
          await ensureCsrf();
          const response = await fetch(`/api/banking/rails/${encodeURIComponent(form.dataset.proofForm)}/proof`, {
            method: "POST",
            credentials: "same-origin",
            headers: {"Content-Type": "application/json", "X-CSRFToken": cookie("csrftoken")},
            body: JSON.stringify({
              status: fields.get("status"),
              proof_reference: fields.get("proof_reference"),
              south_africa_verified: fields.get("south_africa_verified") === "on",
              checkout_enabled: fields.get("checkout_enabled") === "on",
              payout_receive_enabled: fields.get("payout_receive_enabled") === "on",
              final_settlement_enabled: fields.get("final_settlement_enabled") === "on",
              owner_action: fields.get("owner_action"),
              notes: fields.get("notes"),
              password: fields.get("password"),
              code: fields.get("code"),
            }),
          });
          const body = await response.json();
          if (!response.ok) throw new Error(human(body.error || "payment_rail_update_failed"));
          showToast("Treasury account proof updated");
          await load();
        } catch (error) {
          showToast(error.message || "Treasury account update failed", "error");
        } finally {
          clearSecrets(form);
          button.disabled = false;
        }
      });
    });

    root.querySelectorAll("form[data-route-form]").forEach((form) => {
      form.addEventListener("submit", async (event) => {
        event.preventDefault();
        const button = form.querySelector("button[type=submit]");
        button.disabled = true;
        const fields = new FormData(form);
        try {
          await ensureCsrf();
          const response = await fetch(`/api/banking/routes/${encodeURIComponent(form.dataset.routeForm)}/proof`, {
            method: "POST",
            credentials: "same-origin",
            headers: {"Content-Type": "application/json", "X-CSRFToken": cookie("csrftoken")},
            body: JSON.stringify({
              status: fields.get("status"),
              selected_rail: fields.get("selected_rail"),
              proof_reference: fields.get("proof_reference"),
              notes: fields.get("notes"),
              password: fields.get("password"),
              code: fields.get("code"),
            }),
          });
          const body = await response.json();
          if (!response.ok) throw new Error(human(body.error || "settlement_route_update_failed"));
          showToast("Market receipt route updated");
          await load();
        } catch (error) {
          showToast(error.message || "Market receipt route update failed", "error");
        } finally {
          clearSecrets(form);
          button.disabled = false;
        }
      });
    });
  }

  async function logout() {
    try {
      await ensureCsrf();
      await fetch("/api/auth/logout", {method: "POST", credentials: "same-origin", headers: {"X-CSRFToken": cookie("csrftoken")}});
    } finally {
      window.location.assign("/login/");
    }
  }

  function setMenu(open) {
    if (!sidebar || !mobileOverlay || !mobileMenu) return;
    sidebar.classList.toggle("open", open);
    mobileOverlay.hidden = !open;
    mobileMenu.setAttribute("aria-expanded", open ? "true" : "false");
  }

  if (refreshButton) refreshButton.addEventListener("click", load);
  if (logoutButton) logoutButton.addEventListener("click", logout);
  if (mobileMenu) mobileMenu.addEventListener("click", () => setMenu(!sidebar.classList.contains("open")));
  if (mobileOverlay) mobileOverlay.addEventListener("click", () => setMenu(false));

  load();
})();
