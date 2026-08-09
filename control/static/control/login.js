(function () {
  "use strict";

  const accountForm = document.getElementById("accountForm");
  const verificationForm = document.getElementById("verificationForm");
  const accountStep = document.getElementById("accountStep");
  const verificationStep = document.getElementById("verificationStep");
  const successStep = document.getElementById("successStep");
  const accountProgress = document.getElementById("accountProgress");
  const verificationProgress = document.getElementById("verificationProgress");
  const progressFill = document.getElementById("progressFill");
  const username = document.getElementById("username");
  const password = document.getElementById("password");
  const code = document.getElementById("verificationCode");
  const error = document.getElementById("authError");
  const continueButton = document.getElementById("continueButton");
  const verifyButton = document.getElementById("verifyButton");
  const recoveryButton = document.getElementById("recoveryButton");
  const togglePassword = document.getElementById("togglePassword");
  let recoveryMode = false;

  function cookie(name) {
    const item = document.cookie.split("; ").find((entry) => entry.startsWith(name + "="));
    return item ? decodeURIComponent(item.split("=").slice(1).join("=")) : "";
  }

  async function post(url, body) {
    return fetch(url, {
      method: "POST",
      credentials: "same-origin",
      headers: {"Content-Type": "application/json", "X-CSRFToken": cookie("csrftoken")},
      body: JSON.stringify(body),
    });
  }

  function setBusy(button, busy) {
    button.disabled = busy;
    button.classList.toggle("loading", busy);
  }

  function showError(message) {
    error.textContent = message || "We couldn’t verify those details. Check them and try again.";
    error.hidden = false;
  }

  function clearError() {
    error.textContent = "";
    error.hidden = true;
  }

  function showVerification() {
    accountStep.hidden = true;
    accountStep.classList.remove("active");
    verificationStep.hidden = false;
    verificationStep.classList.add("active");
    accountProgress.classList.remove("active");
    accountProgress.classList.add("complete");
    verificationProgress.classList.add("active");
    progressFill.classList.add("complete");
    password.value = "";
    window.setTimeout(() => code.focus(), 80);
  }

  accountForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    clearError();
    if (!username.value.trim() || !password.value) {
      showError("Enter your username and password to continue.");
      return;
    }
    setBusy(continueButton, true);
    try {
      await fetch("/api/auth/csrf", {credentials: "same-origin"});
      const response = await post("/api/auth/login", {username: username.value.trim(), password: password.value});
      if (!response.ok) {
        showError("We couldn’t verify those details. Check them and try again.");
        return;
      }
      const payload = await response.json();
      if (!payload.requires_2fa) {
        showError("Secure verification is currently unavailable. Please try again.");
        return;
      }
      showVerification();
    } catch (_requestError) {
      showError("Secure sign-in is temporarily unavailable. Please try again shortly.");
    } finally {
      setBusy(continueButton, false);
    }
  });

  verificationForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    clearError();
    const value = code.value.replace(/\s/g, "");
    if (!value || (!recoveryMode && !/^\d{6}$/.test(value))) {
      showError(recoveryMode ? "Enter a valid recovery code." : "Enter the 6-digit code from your authenticator.");
      return;
    }
    setBusy(verifyButton, true);
    try {
      const response = await post("/api/auth/totp", {code: value, recovery: recoveryMode});
      if (!response.ok) {
        showError("That verification code wasn’t accepted. Check it and try again.");
        code.select();
        return;
      }
      verificationStep.hidden = true;
      successStep.hidden = false;
      code.value = "";
      window.setTimeout(() => window.location.assign("/"), 650);
    } catch (_requestError) {
      showError("Verification is temporarily unavailable. Please try again shortly.");
    } finally {
      setBusy(verifyButton, false);
    }
  });

  code.addEventListener("input", () => {
    if (!recoveryMode) code.value = code.value.replace(/\D/g, "").slice(0, 6);
    clearError();
  });

  recoveryButton.addEventListener("click", () => {
    recoveryMode = !recoveryMode;
    clearError();
    code.value = "";
    code.inputMode = recoveryMode ? "text" : "numeric";
    code.maxLength = recoveryMode ? 128 : 6;
    code.classList.toggle("recovery-code", recoveryMode);
    document.getElementById("codeLabel").textContent = recoveryMode ? "Recovery code" : "Authentication code";
    document.getElementById("verificationHelp").textContent = recoveryMode
      ? "Enter one of your unused owner recovery codes. It can only be used once."
      : "Enter the 6-digit code from your authenticator app.";
    verifyButton.querySelector("span").textContent = recoveryMode ? "Verify recovery code" : "Verify & enter dashboard";
    recoveryButton.innerHTML = recoveryMode
      ? "Have your authenticator? <span>Use a 6-digit code</span>"
      : "Can’t access your authenticator? <span>Use a recovery code</span>";
    code.focus();
  });

  togglePassword.addEventListener("click", () => {
    const reveal = password.type === "password";
    password.type = reveal ? "text" : "password";
    togglePassword.setAttribute("aria-label", reveal ? "Hide password" : "Show password");
  });

  username.focus();
}());
