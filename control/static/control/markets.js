(function () {
  "use strict";

  const root = document.getElementById("marketsRoot");
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

  function blockerText(blockers) {
    const values = blockers || [];
    return values.length ? values.map(human).join(" · ") : "No blockers";
  }

  function domain(label, ready, blockers, detail) {
    return `<div class="domain-card ${ready ? "ready" : "blocked"}">
      <small>${esc(label)}</small>
      <strong>${ready ? "READY" : "BLOCKED"}</strong>
      <div class="domain-blockers">${esc(detail || blockerText(blockers))}</div>
    </div>`;
  }

  function fact(label, enabled) {
    return `<span class="market-fact ${enabled ? "on" : ""}"><i></i>${esc(label)}</span>`;
  }

  function credentialsFields() {
    return `<label>Password<input type="password" name="password" autocomplete="current-password" required></label>
      <label>TOTP code<input name="code" inputmode="numeric" autocomplete="one-time-code" required></label>`;
  }

  function kyaPanel(row) {
    if (!row.kya_supported) return "";
    const verifiedAt = row.kya_verified_at ? new Date(row.kya_verified_at).toLocaleString() : "Not verified";
    return `<section class="control-panel">
      <h3>Dealwork KYA proof</h3>
      <p>KYA is work-readiness proof. It never changes Banking or marks cash settled.</p>
      <form class="market-form" data-proof-form="${esc(row.market)}">
        <label class="toggle wide"><input type="checkbox" name="verified" ${row.kya_verified ? "checked" : ""}> KYA verified</label>
        <label class="wide">Non-secret proof reference<input name="proof_reference" maxlength="255" value="${esc(row.kya_proof_reference || "")}" placeholder="Verification reference or public badge evidence"></label>
        ${credentialsFields()}
        <div class="market-actions"><button type="submit">Save KYA proof</button></div>
      </form>
      <div class="market-note">Recorded verification: ${esc(verifiedAt)}. Revoking this proof automatically disables Dealwork and disarms persisted acquisition.</div>
    </section>`;
  }

  function operatingPanel(row) {
    const enableDisabled = !row.enabled && !row.can_activate;
    const armDisabled = !row.profile_acquisition_enabled && !row.can_arm_persisted_acquisition;
    return `<section class="control-panel">
      <h3>Operating state</h3>
      <p>Persisted market state is owner-controlled. Runtime environment switches remain deployment-controlled and independent.</p>
      <form class="market-form" data-state-form="${esc(row.market)}">
        <label class="toggle wide"><input type="checkbox" name="enabled" ${row.enabled ? "checked" : ""} ${enableDisabled ? "disabled" : ""}> Market LIVE / enabled</label>
        <label class="toggle wide"><input type="checkbox" name="autonomous_acquisition_enabled" ${row.profile_acquisition_enabled ? "checked" : ""} ${armDisabled ? "disabled" : ""}> Persisted acquisition armed</label>
        ${credentialsFields()}
        <div class="market-actions"><button type="submit">Save operating state</button></div>
      </form>
      <div class="market-note"><strong>${esc(row.runtime_switch_name || "Runtime switch")}</strong>: ${row.runtime_switch_enabled ? "ON" : "OFF"} · global mode: ${esc(row.autonomy_mode || "OFF")}<br>${esc(row.runtime_configuration_note || "Runtime configuration is read-only here.")}</div>
    </section>`;
  }

  function marketCard(row) {
    return `<article class="market-card" data-market="${esc(row.market)}">
      <div class="market-head">
        <div>
          <small>${esc(row.market)}</small>
          <h2>${esc(row.display_name || human(row.market))}</h2>
          <div class="market-subtitle">Status ${esc(human(row.status))} · ${row.platform_wallet_proving ? "platform-wallet proving allowed" : "verified cash route required for live proving"}</div>
        </div>
        <div class="market-tags">
          <span class="market-tag ${row.enabled ? "live" : ""}">${row.enabled ? "LIVE" : "DISABLED"}</span>
          ${row.platform_wallet_proving ? '<span class="market-tag">PLATFORM WALLET</span>' : ""}
          ${row.kya_supported ? `<span class="market-tag ${row.kya_verified ? "live" : ""}">KYA ${row.kya_verified ? "VERIFIED" : "REQUIRED"}</span>` : ""}
        </div>
      </div>
      <div class="domain-grid">
        ${domain("WORK", row.work_ready, row.work_blockers)}
        ${domain("LIVE PROVING", row.live_test_ready, row.live_test_blockers)}
        ${domain("CASH", row.cash_ready, row.cash_blockers)}
        ${domain("AUTONOMY", row.autonomy_ready, row.autonomy_blockers)}
      </div>
      <div class="market-facts">
        ${fact("Connected", row.connected)}
        ${fact("Source wired", row.source_wired)}
        ${fact("Policy current", row.policy_current)}
        ${fact("Policy verified", row.policy_verified)}
        ${fact("Live-entry eligible", row.live_entry_ready)}
      </div>
      <div class="control-grid">${kyaPanel(row)}${operatingPanel(row)}</div>
    </article>`;
  }

  function render(data) {
    const rows = data.rows || [];
    const meta = data.meta || {};
    root.innerHTML = `<div class="markets-hero">
      <article class="markets-primary"><small>MARKET CONTROL PLANE</small><strong>${esc(rows.length)} markets</strong><p>Readiness is split by purpose. A market can be ready to do work while final settlement remains deliberately unresolved.</p></article>
      <article><small>WORK READY</small><strong>${esc(meta.work_ready || 0)}</strong><p>Can perform posted work.</p></article>
      <article><small>LIVE PROVING</small><strong>${esc(meta.live_test_ready || 0)}</strong><p>Currently enabled for bounded proving.</p></article>
      <article><small>CASH READY</small><strong>${esc(meta.cash_ready || 0)}</strong><p>Verified final cash route.</p></article>
      <article><small>AUTONOMY READY</small><strong>${esc(meta.autonomy_ready || 0)}</strong><p>All mutation gates armed.</p></article>
    </div>
    <div class="market-list">${rows.map(marketCard).join("")}</div>
    <div class="markets-truth"><strong>Readiness truth:</strong> ${esc(meta.truth || "Readiness domains remain independent and fail closed.")}</div>`;
    skeleton.hidden = true;
    root.hidden = false;
    bindForms();
  }

  async function postJson(url, payload) {
    await ensureCsrf();
    const response = await fetch(url, {
      method: "POST",
      credentials: "same-origin",
      headers: {"Content-Type": "application/json", "X-CSRFToken": cookie("csrftoken")},
      body: JSON.stringify(payload),
    });
    const body = await response.json();
    if (!response.ok) throw new Error(human(body.error || "market_update_failed"));
    return body;
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
          await postJson(`/api/markets/${encodeURIComponent(form.dataset.proofForm)}/proof`, {
            proof_type: "kya",
            verified: fields.get("verified") === "on",
            proof_reference: fields.get("proof_reference"),
            password: fields.get("password"),
            code: fields.get("code"),
          });
          showToast("Market proof updated");
          await load();
        } catch (error) {
          showToast(error.message || "Market proof update failed", "error");
        } finally {
          clearSecrets(form);
          button.disabled = false;
        }
      });
    });

    root.querySelectorAll("form[data-state-form]").forEach((form) => {
      form.addEventListener("submit", async (event) => {
        event.preventDefault();
        const button = form.querySelector("button[type=submit]");
        button.disabled = true;
        const fields = new FormData(form);
        try {
          await postJson(`/api/markets/${encodeURIComponent(form.dataset.stateForm)}/operating-state`, {
            enabled: fields.get("enabled") === "on",
            autonomous_acquisition_enabled: fields.get("autonomous_acquisition_enabled") === "on",
            password: fields.get("password"),
            code: fields.get("code"),
          });
          showToast("Market operating state updated");
          await load();
        } catch (error) {
          showToast(error.message || "Market operating state update failed", "error");
        } finally {
          clearSecrets(form);
          button.disabled = false;
        }
      });
    });
  }

  async function load() {
    if (loading) return;
    loading = true;
    if (refreshButton) refreshButton.disabled = true;
    try {
      const response = await fetch("/api/markets/controls", {credentials: "same-origin"});
      if (response.status === 401) {
        window.location.assign("/login/");
        return;
      }
      if (!response.ok) throw new Error("Markets data is unavailable");
      render(await response.json());
      if (liveState) {
        liveState.classList.add("connected");
        liveState.querySelector("span").textContent = "Data live";
      }
      if (lastUpdated) lastUpdated.textContent = new Date().toLocaleTimeString();
    } catch (error) {
      showToast(error.message || "Unable to load markets data", "error");
      if (liveState) liveState.querySelector("span").textContent = "Data unavailable";
    } finally {
      loading = false;
      if (refreshButton) refreshButton.disabled = false;
    }
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
