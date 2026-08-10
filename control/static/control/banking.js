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

  function option(value, selected) {
    return `<option value="${esc(value)}" ${value === selected ? "selected" : ""}>${esc(human(value))}</option>`;
  }

  function railCard(row) {
    const proof = row.proof_reference || "No non-secret proof reference recorded";
    const verified = row.verified_at ? new Date(row.verified_at).toLocaleString() : "Not verified";
    const statuses = ["NOT_CONFIGURED", "ACCOUNT_PROOF_REQUIRED", "PENDING_EXTERNAL_APPROVAL", "VERIFIED", "BLOCKED", "PAUSED"];
    const slug = esc(row.slug);
    return `<article class="rail-card" data-rail="${slug}">
      <div class="rail-head">
        <div><small>${esc(human(row.category))}</small><h2>${esc(row.display_name)}</h2></div>
        ${stateBadge(row.status)}
      </div>
      <div class="rail-capabilities">${(row.candidate_capabilities || []).map((item) => `<span>${esc(human(item))}</span>`).join("")}</div>
      <div class="rail-checks">
        ${check("South Africa verified", row.south_africa_verified)}
        ${check("Checkout enabled", row.checkout_enabled)}
        ${check("Payout receipt enabled", row.payout_receive_enabled)}
        ${check("Final settlement enabled", row.final_settlement_enabled)}
      </div>
      <div class="rail-proof"><small>PROOF REFERENCE</small><strong>${esc(proof)}</strong><small>Verified: ${esc(verified)}</small></div>
      <p class="rail-action">${esc(row.owner_action || "Owner proof is required before activation.")}</p>
      ${row.notes ? `<p class="rail-action">${esc(row.notes)}</p>` : ""}
      <details class="proof-editor">
        <summary>Update owner proof</summary>
        <form class="proof-form" data-proof-form="${slug}">
          <label>Status<select name="status">${statuses.map((item) => option(item, row.status)).join("")}</select></label>
          <label>Proof reference<input name="proof_reference" maxlength="255" value="${esc(row.proof_reference || "")}" placeholder="Non-secret KYC/account proof reference"></label>
          <div class="toggle-row">
            <label><input type="checkbox" name="south_africa_verified" ${row.south_africa_verified ? "checked" : ""}> South Africa verified</label>
            <label><input type="checkbox" name="checkout_enabled" ${row.checkout_enabled ? "checked" : ""}> Checkout enabled</label>
            <label><input type="checkbox" name="payout_receive_enabled" ${row.payout_receive_enabled ? "checked" : ""}> Payout receipt enabled</label>
            <label><input type="checkbox" name="final_settlement_enabled" ${row.final_settlement_enabled ? "checked" : ""}> Final settlement enabled</label>
          </div>
          <label class="wide">Owner action<input name="owner_action" maxlength="500" value="${esc(row.owner_action || "")}"></label>
          <label class="wide">Notes<textarea name="notes" maxlength="1000">${esc(row.notes || "")}</textarea></label>
          <label>Password<input type="password" name="password" autocomplete="current-password" required></label>
          <label>TOTP code<input name="code" inputmode="numeric" autocomplete="one-time-code" required></label>
          <div class="proof-actions"><button type="submit">Save rail state</button></div>
        </form>
      </details>
    </article>`;
  }

  function routeCard(row, rails) {
    const statuses = ["UNMAPPED", "PROPOSED", "VERIFIED", "BLOCKED", "PAUSED"];
    const candidateSlugs = (row.candidate_rails || []).length ? row.candidate_rails : rails.map((rail) => rail.slug);
    const railOptions = ["", ...candidateSlugs].map((slug) => option(slug, row.selected_rail)).join("");
    const blockers = row.blockers || [];
    return `<article class="rail-card" data-route="${esc(row.market)}">
      <div class="rail-head">
        <div><small>MARKETPLACE SETTLEMENT</small><h2>${esc(row.market_display_name || human(row.market))}</h2></div>
        ${stateBadge(row.status)}
      </div>
      <div class="rail-checks">
        ${check("Marketplace payout ready", row.market_payout_ready)}
        ${check("Marketplace South Africa verified", row.market_south_africa_verified)}
        ${check("Mapped owner rail ready", row.ready)}
      </div>
      <div class="rail-proof"><small>SELECTED OWNER RAIL</small><strong>${esc(row.selected_rail ? human(row.selected_rail) : "Not mapped")}</strong><small>${esc(row.proof_reference || "No route proof recorded")}</small></div>
      ${blockers.length ? `<p class="rail-action">Blocked: ${esc(blockers.map(human).join(" · "))}</p>` : `<p class="rail-action">Verified marketplace-to-owner settlement route.</p>`}
      <details class="proof-editor">
        <summary>Plan or verify route</summary>
        <form class="proof-form" data-route-form="${esc(row.market)}">
          <label>Status<select name="status">${statuses.map((item) => option(item, row.status)).join("")}</select></label>
          <label>Owner rail<select name="selected_rail">${railOptions}</select></label>
          <label class="wide">Proof reference<input name="proof_reference" maxlength="255" value="${esc(row.proof_reference || "")}" placeholder="Non-secret payout-route proof reference"></label>
          <label class="wide">Notes<textarea name="notes" maxlength="1000">${esc(row.notes || "")}</textarea></label>
          <label>Password<input type="password" name="password" autocomplete="current-password" required></label>
          <label>TOTP code<input name="code" inputmode="numeric" autocomplete="one-time-code" required></label>
          <div class="proof-actions"><button type="submit">Save settlement route</button></div>
        </form>
      </details>
    </article>`;
  }

  function render(data) {
    const rows = data.rows || [];
    const meta = data.meta || {};
    const routes = data.settlement_routes || [];
    const routeMeta = data.settlement_route_meta || {};
    root.innerHTML = `<div class="banking-hero">
      <article class="banking-primary"><small>PAYMENT RAIL CONTROL</small><strong>${esc(meta.ready_rails || 0)} ready</strong><p>Rail readiness is independent from marketplace readiness. No candidate rail is treated as usable until owner proof and South Africa settlement truth are recorded.</p></article>
      <article><small>ACTION REQUIRED</small><strong>${esc(meta.action_required || 0)}</strong><p>Rails still missing verified owner evidence.</p></article>
      <article><small>PAYOUT RECEIPT READY</small><strong>${esc(meta.payout_receive_ready || 0)}</strong><p>Verified rails allowed to receive marketplace payouts.</p></article>
      <article><small>FINAL SETTLEMENT READY</small><strong>${esc(meta.final_settlement_ready || 0)}</strong><p>Verified final treasury destinations.</p></article>
    </div>
    <div class="rail-grid">${rows.map(railCard).join("")}</div>
    <div class="banking-truth"><strong>Payment rail truth:</strong> ${esc(meta.truth || "Payment and payout rail evidence remains fail-closed.")}</div>
    <div class="banking-truth"><strong>Settlement routing:</strong> ${esc(routeMeta.ready_routes || 0)} verified route(s). Marketplace payout readiness never inherits automatically from an owner payment rail.</div>
    <div class="rail-grid">${routes.map((row) => routeCard(row, rows)).join("")}</div>
    <div class="banking-truth"><strong>Settlement route truth:</strong> ${esc(routeMeta.truth || "Explicit marketplace-to-owner payout mapping remains fail-closed.")}</div>`;
    skeleton.hidden = true;
    root.hidden = false;
    bindForms();
  }

  async function load() {
    if (loading) return;
    loading = true;
    refreshButton && (refreshButton.disabled = true);
    try {
      const response = await fetch("/api/banking/rails", {credentials: "same-origin"});
      if (response.status === 401) {
        window.location.assign("/login/");
        return;
      }
      if (!response.ok) throw new Error("Banking data is unavailable");
      render(await response.json());
      if (liveState) {
        liveState.classList.add("connected");
        liveState.querySelector("span").textContent = "Data live";
      }
      if (lastUpdated) lastUpdated.textContent = new Date().toLocaleTimeString();
    } catch (error) {
      showToast(error.message || "Unable to load banking data", "error");
      if (liveState) liveState.querySelector("span").textContent = "Data unavailable";
    } finally {
      loading = false;
      refreshButton && (refreshButton.disabled = false);
    }
  }

  function bindForms() {
    root.querySelectorAll("form[data-proof-form]").forEach((form) => {
      form.addEventListener("submit", async (event) => {
        event.preventDefault();
        const button = form.querySelector("button[type=submit]");
        button.disabled = true;
        const fields = new FormData(form);
        const payload = {
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
        };
        try {
          await ensureCsrf();
          const slug = form.dataset.proofForm;
          const response = await fetch(`/api/banking/rails/${encodeURIComponent(slug)}/proof`, {
            method: "POST",
            credentials: "same-origin",
            headers: {"Content-Type": "application/json", "X-CSRFToken": cookie("csrftoken")},
            body: JSON.stringify(payload),
          });
          const body = await response.json();
          if (!response.ok) throw new Error(human(body.error || "payment_rail_update_failed"));
          showToast("Payment rail proof updated");
          await load();
        } catch (error) {
          showToast(error.message || "Payment rail update failed", "error");
        } finally {
          form.querySelector('input[name="password"]').value = "";
          form.querySelector('input[name="code"]').value = "";
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
        const payload = {
          status: fields.get("status"),
          selected_rail: fields.get("selected_rail"),
          proof_reference: fields.get("proof_reference"),
          notes: fields.get("notes"),
          password: fields.get("password"),
          code: fields.get("code"),
        };
        try {
          await ensureCsrf();
          const market = form.dataset.routeForm;
          const response = await fetch(`/api/banking/routes/${encodeURIComponent(market)}/proof`, {
            method: "POST",
            credentials: "same-origin",
            headers: {"Content-Type": "application/json", "X-CSRFToken": cookie("csrftoken")},
            body: JSON.stringify(payload),
          });
          const body = await response.json();
          if (!response.ok) throw new Error(human(body.error || "settlement_route_update_failed"));
          showToast("Settlement route updated");
          await load();
        } catch (error) {
          showToast(error.message || "Settlement route update failed", "error");
        } finally {
          form.querySelector('input[name="password"]').value = "";
          form.querySelector('input[name="code"]').value = "";
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

  refreshButton && refreshButton.addEventListener("click", load);
  logoutButton && logoutButton.addEventListener("click", logout);
  mobileMenu && mobileMenu.addEventListener("click", () => setMenu(!sidebar.classList.contains("open")));
  mobileOverlay && mobileOverlay.addEventListener("click", () => setMenu(false));

  load();
})();
