// Per-user presentation settings, kept on the server (core/preferences.py): lab workstations are
// shared, so nothing here is stored in the browser. The page carries the user's saved values in
// <script id="vr-preferences"> ({% client_preferences %}); set() updates them and POSTs the change.
(function () {
  "use strict";

  if (window.vrPreferences) {
    return;
  }

  var cache = null;

  function all() {
    if (!cache) {
      var element = document.getElementById("vr-preferences");
      try {
        cache = element ? JSON.parse(element.textContent) || {} : {};
      } catch (error) {
        cache = {}; // Unreadable: every page falls back to its defaults.
      }
    }
    return cache;
  }

  function csrfToken() {
    try {
      return JSON.parse(document.body.getAttribute("hx-headers") || "{}")["X-CSRFToken"] || "";
    } catch (error) {
      return "";
    }
  }

  window.vrPreferences = {
    get: function (key) {
      return Object.prototype.hasOwnProperty.call(all(), key) ? all()[key] : null;
    },
    // value null removes the setting (back to the page's defaults).
    set: function (key, value) {
      if (value === null) {
        delete all()[key];
      } else {
        all()[key] = value;
      }
      if (!document.body.hasAttribute("data-vr-user")) {
        return Promise.resolve();
      }
      return fetch("/preferences/", {
        method: "POST",
        credentials: "same-origin",
        keepalive: true, // completes even if the user leaves the page right after the change
        headers: { "Content-Type": "application/json", "X-CSRFToken": csrfToken() },
        body: JSON.stringify({ key: key, value: value }),
      }).catch(function () {
        // Offline or refused: the setting applies to this page only.
      });
    },
  };

  // Earlier versions kept grid layouts in this browser; remove them, so nobody inherits another
  // person's layout on a shared workstation.
  try {
    Object.keys(localStorage).forEach(function (key) {
      if (key.indexOf("vr-grid:") === 0) {
        localStorage.removeItem(key);
      }
    });
  } catch (error) {
    // Storage blocked: nothing to clean.
  }
})();
