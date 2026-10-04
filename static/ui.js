/*
 * Shared UI behaviour, loaded on every page (app and sign-in pages).
 *   1. Toast messages: close button, auto-dismiss, pause while hovered.
 *   2. Password fields: eye icon to show or hide the typed password.
 */
(function () {
  "use strict";

  /* ---------- 1. Toasts ---------- */

  // How long each kind of message stays before fading out (milliseconds).
  // Errors stay longer because they usually need reading; "persistent"
  // messages (e.g. the development-only reset link) stay until closed.
  var TOAST_DURATION_BY_CATEGORY = { success: 5000, error: 9000, persistent: 0 };
  var TOAST_EXIT_ANIMATION_MS = 250;

  function dismissToast(toastElement) {
    if (!toastElement || toastElement.dataset.dismissing === "true") return;
    toastElement.dataset.dismissing = "true";
    window.clearTimeout(Number(toastElement.dataset.timerId));
    toastElement.classList.add("toast-leaving");
    window.setTimeout(function () { toastElement.remove(); }, TOAST_EXIT_ANIMATION_MS);
  }

  function startToastTimer(toastElement, remainingMilliseconds) {
    if (remainingMilliseconds <= 0) return;
    toastElement.dataset.timerStartedAt = String(Date.now());
    toastElement.dataset.remainingMs = String(remainingMilliseconds);
    var timerId = window.setTimeout(function () { dismissToast(toastElement); }, remainingMilliseconds);
    toastElement.dataset.timerId = String(timerId);
    var progressBar = toastElement.querySelector(".toast-progress");
    if (progressBar) {
      progressBar.style.transition = "none";
      progressBar.style.transform = "scaleX(" + (remainingMilliseconds / Number(toastElement.dataset.totalMs)) + ")";
      void progressBar.offsetWidth; // restart the CSS transition
      progressBar.style.transition = "transform " + remainingMilliseconds + "ms linear";
      progressBar.style.transform = "scaleX(0)";
    }
  }

  function pauseToastTimer(toastElement) {
    if (!toastElement.dataset.timerId) return;
    window.clearTimeout(Number(toastElement.dataset.timerId));
    var elapsed = Date.now() - Number(toastElement.dataset.timerStartedAt);
    toastElement.dataset.remainingMs = String(Math.max(0, Number(toastElement.dataset.remainingMs) - elapsed));
    var progressBar = toastElement.querySelector(".toast-progress");
    if (progressBar) {
      var currentScale = window.getComputedStyle(progressBar).transform;
      progressBar.style.transition = "none";
      progressBar.style.transform = currentScale === "none" ? "scaleX(1)" : currentScale;
    }
  }

  function setUpToast(toastElement) {
    var category = toastElement.dataset.category || "error";
    var totalMilliseconds = TOAST_DURATION_BY_CATEGORY.hasOwnProperty(category)
      ? TOAST_DURATION_BY_CATEGORY[category]
      : TOAST_DURATION_BY_CATEGORY.error;
    toastElement.dataset.totalMs = String(totalMilliseconds);

    var closeButton = toastElement.querySelector(".toast-close");
    if (closeButton) closeButton.addEventListener("click", function () { dismissToast(toastElement); });

    if (totalMilliseconds > 0) {
      startToastTimer(toastElement, totalMilliseconds);
      // Pause while the user is reading (hover) or tabbing through it (focus)
      toastElement.addEventListener("mouseenter", function () { pauseToastTimer(toastElement); });
      toastElement.addEventListener("focusin", function () { pauseToastTimer(toastElement); });
      toastElement.addEventListener("mouseleave", function () {
        startToastTimer(toastElement, Number(toastElement.dataset.remainingMs));
      });
      toastElement.addEventListener("focusout", function () {
        startToastTimer(toastElement, Number(toastElement.dataset.remainingMs));
      });
    } else {
      var progressBar = toastElement.querySelector(".toast-progress");
      if (progressBar) progressBar.remove();
    }
  }

  /* ---------- 2. Password visibility ---------- */

  // Inline SVG so the icon works even if the icon font fails to load
  var EYE_OPEN_ICON = '<svg viewBox="0 0 24 24" width="18" height="18" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12z"/><circle cx="12" cy="12" r="3"/></svg>';
  var EYE_CLOSED_ICON = '<svg viewBox="0 0 24 24" width="18" height="18" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M10.6 5.1A10.6 10.6 0 0 1 12 5c6.5 0 10 7 10 7a17.4 17.4 0 0 1-3.2 4.2M6.6 6.6C3.9 8.4 2 12 2 12s3.5 7 10 7a9.7 9.7 0 0 0 5.4-1.6"/><path d="M9.9 9.9a3 3 0 0 0 4.2 4.2"/><line x1="3" y1="3" x2="21" y2="21"/></svg>';

  function addPasswordToggle(passwordInput) {
    if (passwordInput.dataset.visibilityToggle === "added") return;
    passwordInput.dataset.visibilityToggle = "added";

    var wrapper = document.createElement("div");
    wrapper.className = "password-field";
    passwordInput.parentNode.insertBefore(wrapper, passwordInput);
    wrapper.appendChild(passwordInput);

    var toggleButton = document.createElement("button");
    toggleButton.type = "button";
    toggleButton.className = "password-visibility-toggle";
    toggleButton.setAttribute("aria-label", "Show password");
    toggleButton.setAttribute("aria-pressed", "false");
    toggleButton.innerHTML = EYE_OPEN_ICON;
    wrapper.appendChild(toggleButton);

    toggleButton.addEventListener("click", function () {
      var isNowVisible = passwordInput.type === "password";
      passwordInput.type = isNowVisible ? "text" : "password";
      toggleButton.setAttribute("aria-pressed", String(isNowVisible));
      toggleButton.setAttribute("aria-label", isNowVisible ? "Hide password" : "Show password");
      toggleButton.innerHTML = isNowVisible ? EYE_CLOSED_ICON : EYE_OPEN_ICON;
      passwordInput.focus();
    });

    // Never submit a form with the password field left as plain text, so
    // browsers still offer to save it as a password.
    if (passwordInput.form) {
      passwordInput.form.addEventListener("submit", function () { passwordInput.type = "password"; });
    }
  }

  document.addEventListener("DOMContentLoaded", function () {
    document.querySelectorAll(".toast").forEach(setUpToast);
    document.querySelectorAll('input[type="password"]').forEach(addPasswordToggle);
  });
})();
