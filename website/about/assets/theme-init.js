// Applies the saved theme before CSS loads, so the page never flashes the other theme. Loaded
// synchronously in <head> (an external file, because the application's Content Security Policy
// blocks inline scripts). Key "vr-theme" is shared with the application: "light" | "dark" |
// "system" (default). The menu itself lives in site.js.
(function () {
  "use strict";
  var choice = "system";
  try {
    choice = localStorage.getItem("vr-theme") || "system";
  } catch (error) {
    // Storage blocked: follow the system setting.
  }
  var dark = choice === "dark" ||
    (choice !== "light" && window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches);
  document.documentElement.setAttribute("data-theme", dark ? "dark" : "light");
})();
