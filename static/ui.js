/*
 * Shared UI behaviour, loaded on every page (app and sign-in pages).
 *   1. Toast messages: close button, auto-dismiss, pause while hovered.
 *   2. Password fields: eye icon to show or hide the typed password.
 *   3. Loading states: buttons show a spinner while a form submits or a
 *      download is prepared, and cannot be clicked twice.
 *   4. Field validation: messages under each field, matching the server's.
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

  /* ---------- 3. Loading states ---------- */

  var SPINNER_HTML = '<span class="button-spinner" aria-hidden="true"></span>';

  // Exposed so page scripts (e.g. the delete dialog) can use the same look
  window.setButtonBusy = function (button, loadingText) {
    if (!button || button.dataset.busy === "true") return;
    button.dataset.busy = "true";
    button.dataset.originalHtml = button.innerHTML;
    button.setAttribute("aria-busy", "true");
    button.classList.add("is-busy");
    if (button.tagName === "BUTTON") button.disabled = true;
    var text = loadingText || button.dataset.loadingText || "Please wait...";
    button.innerHTML = SPINNER_HTML + '<span class="button-busy-text">' + text + "</span>";
  };

  window.clearButtonBusy = function (button) {
    if (!button || button.dataset.busy !== "true") return;
    button.innerHTML = button.dataset.originalHtml;
    button.dataset.busy = "false";
    button.removeAttribute("aria-busy");
    button.classList.remove("is-busy");
    if (button.tagName === "BUTTON") button.disabled = false;
  };

  // Any form submit: busy the button that was pressed. Runs last (bubbling at
  // document level), so forms whose own scripts cancel the submit are skipped.
  document.addEventListener("submit", function (event) {
    var form = event.target;
    if (event.defaultPrevented || form.hasAttribute("data-no-busy")) return;
    var submitButton = event.submitter || form.querySelector('[type="submit"]');
    if (submitButton && submitButton.dataset.busy === "true") {
      event.preventDefault(); // already submitting: ignore the second click
      return;
    }
    window.setButtonBusy(submitButton);
  });

  // Back/forward cache can restore a page with buttons still spinning
  window.addEventListener("pageshow", function (event) {
    if (!event.persisted) return;
    document.querySelectorAll('[data-busy="true"]').forEach(window.clearButtonBusy);
  });

  // Download links: the server echoes ?download_token=X back as a cookie
  // together with the file; the spinner stops when that cookie appears.
  var DOWNLOAD_WAIT_LIMIT_MS = 90000;

  function readCookie(name) {
    var match = document.cookie.match(new RegExp("(?:^|; )" + name + "=([^;]*)"));
    return match ? decodeURIComponent(match[1]) : null;
  }

  function setUpDownloadLink(link) {
    link.addEventListener("click", function (event) {
      if (link.dataset.busy === "true") { event.preventDefault(); return; }
      var downloadToken = Math.random().toString(36).slice(2, 12) + Date.now().toString(36);
      var url = new URL(link.href, window.location.href);
      url.searchParams.set("download_token", downloadToken);
      link.href = url.toString();
      window.setButtonBusy(link);
      var startedAt = Date.now();
      var poll = window.setInterval(function () {
        var finished = readCookie("download_token") === downloadToken;
        if (finished || Date.now() - startedAt > DOWNLOAD_WAIT_LIMIT_MS) {
          window.clearInterval(poll);
          window.clearButtonBusy(link);
          document.cookie = "download_token=; Max-Age=0; path=/";
        }
      }, 300);
    });
  }

  /* ---------- 4. Field validation ---------- */
  // Same rules and wording as the server (app.py). The server always checks
  // again; this only saves a round trip and points at the exact field.

  var EMAIL_SHAPE = /^[A-Za-z0-9._%+'-]+@[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)*\.[A-Za-z]{2,}$/;

  var PASSWORD_CHECKS = {
    length: function (value) { return value.length >= 8 && value.length <= 128; },
    letter: function (value) { return /[A-Za-z]/.test(value); },
    number: function (value) { return /\d/.test(value); },
  };

  function fieldProblem(input) {
    var value = input.value;
    var trimmed = value.trim();
    switch (input.dataset.rule) {
      case "email":
        if (!trimmed) return "Enter your email address.";
        if (trimmed.length > 254 || !EMAIL_SHAPE.test(trimmed) || trimmed.indexOf("..") !== -1) {
          return "Enter a valid email address, like name@example.com.";
        }
        return "";
      case "name":
        if (!trimmed) return "Enter your full name.";
        if (trimmed.length < 2 || !/\p{L}/u.test(trimmed)) return "Enter your name using letters.";
        if (trimmed.length > 100) return "Name must be at most 100 characters.";
        return "";
      case "new-password":
        if (!value) return "Enter a password.";
        if (value.length < 8) return "Password must be at least 8 characters.";
        if (value.length > 128) return "Password must be at most 128 characters.";
        if (!PASSWORD_CHECKS.letter(value) || !PASSWORD_CHECKS.number(value)) {
          return "Password must include at least one letter and one number.";
        }
        var emailInput = input.form && input.form.querySelector('[data-rule="email"]');
        var email = emailInput ? emailInput.value.trim().toLowerCase() : (input.dataset.accountEmail || "");
        if (email && (value.toLowerCase() === email || value.toLowerCase() === email.split("@")[0])) {
          return "Password must not be your email address.";
        }
        return "";
      case "confirm-password":
        var original = document.getElementById(input.dataset.match);
        if (!value) return "Re-enter the password.";
        if (original && value !== original.value) return "Passwords do not match.";
        return "";
      case "required":
        return value ? "" : (input.dataset.requiredMessage || "This field is required.");
      default:
        return "";
    }
  }

  function errorElementFor(input) {
    var errorId = input.id + "-error";
    var errorElement = document.getElementById(errorId);
    if (!errorElement) {
      errorElement = document.createElement("p");
      errorElement.className = "field-error";
      errorElement.id = errorId;
      errorElement.setAttribute("aria-live", "polite");
      var anchor = input.closest(".password-field") || input;
      anchor.insertAdjacentElement("afterend", errorElement);
    }
    var describedBy = (input.getAttribute("aria-describedby") || "").split(" ").filter(Boolean);
    if (describedBy.indexOf(errorId) === -1) {
      describedBy.push(errorId);
      input.setAttribute("aria-describedby", describedBy.join(" "));
    }
    return errorElement;
  }

  function showFieldProblem(input) {
    var problem = fieldProblem(input);
    errorElementFor(input).textContent = problem;
    if (problem) {
      input.setAttribute("aria-invalid", "true");
    } else {
      input.removeAttribute("aria-invalid");
    }
    return problem;
  }

  function updatePasswordRules(input) {
    var rulesList = document.querySelector('[data-rules-for="' + input.id + '"]');
    if (!rulesList) return;
    rulesList.querySelectorAll("[data-check]").forEach(function (ruleItem) {
      var met = PASSWORD_CHECKS[ruleItem.dataset.check](input.value);
      ruleItem.classList.toggle("met", met);
      ruleItem.setAttribute("aria-label", ruleItem.textContent.trim() + (met ? ": done" : ": not yet"));
    });
  }

  function setUpValidatedForm(form) {
    form.setAttribute("novalidate", ""); // our messages replace the browser's bubbles
    var inputs = Array.prototype.slice.call(form.querySelectorAll("[data-rule]"));

    inputs.forEach(function (input) {
      // Message appears when leaving a field, then updates live while fixing it
      input.addEventListener("blur", function () {
        if (input.value || input.dataset.touched) {
          input.dataset.touched = "true";
          showFieldProblem(input);
        }
      });
      input.addEventListener("input", function () {
        if (input.dataset.rule === "new-password") updatePasswordRules(input);
        if (input.getAttribute("aria-invalid") === "true") showFieldProblem(input);
        // Changing the password re-checks an already-filled confirmation
        if (input.dataset.rule === "new-password") {
          var confirmInput = form.querySelector('[data-rule="confirm-password"][data-match="' + input.id + '"]');
          if (confirmInput && confirmInput.dataset.touched) showFieldProblem(confirmInput);
        }
      });
      if (input.dataset.rule === "new-password") updatePasswordRules(input);
    });

    form.addEventListener("submit", function (event) {
      var firstInvalid = null;
      inputs.forEach(function (input) {
        input.dataset.touched = "true";
        if (showFieldProblem(input) && !firstInvalid) firstInvalid = input;
      });
      if (firstInvalid) {
        event.preventDefault();
        firstInvalid.focus();
      }
    });
  }

  document.addEventListener("DOMContentLoaded", function () {
    document.querySelectorAll(".toast").forEach(setUpToast);
    // Password toggles first, so error messages are placed after the eye button's wrapper
    document.querySelectorAll('input[type="password"]').forEach(addPasswordToggle);
    document.querySelectorAll("form[data-validate]").forEach(setUpValidatedForm);
    document.querySelectorAll("a[data-download-link]").forEach(setUpDownloadLink);
  });
})();
