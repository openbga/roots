// Applies the saved colour theme before first paint. Loaded synchronously in <head>, before CSS.
// Storage key "vr-theme": "light" | "dark" | "system" (default). The theme menu lives in app.js.
(function () {
  "use strict";
  var choice = "system";
  try {
    choice = localStorage.getItem("vr-theme") || "system";
  } catch (error) {
    // Storage blocked: follow the system preference.
  }
  var dark = choice === "dark" ||
    (choice !== "light" && window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches);
  document.documentElement.setAttribute("data-bs-theme", dark ? "dark" : "light");
  document.documentElement.setAttribute("data-vr-theme-choice", choice);
})();
