(function () {
  "use strict";
  const toggle = document.getElementById("navToggle");
  const navigation = document.getElementById("mainNav");
  let returnFocus = null;

  function closeMenu(restoreFocus) {
    if (!toggle || !navigation) return;
    navigation.classList.remove("open");
    toggle.setAttribute("aria-expanded", "false");
    toggle.setAttribute("aria-label", "Open site navigation");
    if (restoreFocus && returnFocus) returnFocus.focus();
  }

  if (toggle && navigation) {
    toggle.addEventListener("click", function () {
      const opening = !navigation.classList.contains("open");
      if (opening) {
        returnFocus = toggle;
        navigation.classList.add("open");
        toggle.setAttribute("aria-expanded", "true");
        toggle.setAttribute("aria-label", "Close site navigation");
        const first = navigation.querySelector("a");
        if (first) first.focus();
      } else closeMenu(true);
    });
    navigation.querySelectorAll("a").forEach(function (link) { link.addEventListener("click", function () { closeMenu(false); }); });
    document.addEventListener("keydown", function (event) { if (event.key === "Escape" && navigation.classList.contains("open")) closeMenu(true); });
    document.addEventListener("pointerdown", function (event) { if (navigation.classList.contains("open") && !navigation.contains(event.target) && !toggle.contains(event.target)) closeMenu(false); });
    window.addEventListener("resize", function () { if (window.innerWidth > 768) closeMenu(false); });
  }

  function eventId() {
    try {
      const key = "amarktai_funnel_session";
      let value = sessionStorage.getItem(key);
      if (!value) { value = crypto.randomUUID(); sessionStorage.setItem(key, value); }
      return value;
    } catch (_error) { return "anonymous"; }
  }
  let telemetryQueue = Promise.resolve();
  function record(type, element) {
    const product = (element && (element.dataset.product || element.closest("[data-product]")?.dataset.product)) || "";
    const body = JSON.stringify({event_type: type, anonymous_session: eventId(), product: product, source: document.referrer ? "referral" : "direct", metadata: {path: location.pathname}});
    telemetryQueue = telemetryQueue.then(function () {
      return fetch("/api/v1/telemetry/events", {method: "POST", headers: {"Content-Type": "application/json"}, body: body, keepalive: true});
    }).catch(function () {});
  }
  document.querySelectorAll("[data-funnel]").forEach(function (element) {
    if (element.dataset.funnel === "PRODUCT_IMPRESSION") record("PRODUCT_IMPRESSION", element);
    else element.addEventListener("click", function () { record(element.dataset.funnel, element); });
  });
  if (document.body.classList.contains("docs-page")) record("API_DOCUMENTATION_VIEW", document.body);
}());
