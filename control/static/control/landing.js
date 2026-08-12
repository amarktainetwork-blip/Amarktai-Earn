(function () {
  "use strict";
  const toggle = document.getElementById("navToggle");
  const navigation = document.getElementById("mainNav");
  if (!toggle || !navigation) return;
  function close() {
    navigation.classList.remove("open");
    toggle.setAttribute("aria-expanded", "false");
    toggle.setAttribute("aria-label", "Open site navigation");
  }
  toggle.addEventListener("click", () => {
    const open = !navigation.classList.contains("open");
    navigation.classList.toggle("open", open);
    toggle.setAttribute("aria-expanded", String(open));
    toggle.setAttribute("aria-label", open ? "Close site navigation" : "Open site navigation");
  });
  navigation.querySelectorAll("a").forEach((link) => link.addEventListener("click", close));
  document.addEventListener("keydown", (event) => { if (event.key === "Escape") close(); });
  window.addEventListener("resize", () => { if (window.innerWidth > 1180) close(); });
}());
