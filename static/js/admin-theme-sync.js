// Keeps Django admin's own theme (localStorage "theme": light | dark | auto) in step with the
// application theme (localStorage "vr-theme": light | dark | system). Loaded before admin/js/theme.js.
(function () {
  "use strict";
  var toAdmin = { light: "light", dark: "dark", system: "auto" };
  var toApp = { light: "light", dark: "dark", auto: "system" };
  try {
    var app = localStorage.getItem("vr-theme");
    if (app && toAdmin[app]) {
      localStorage.setItem("theme", toAdmin[app]);
    }
  } catch (error) {
    return;
  }
  // Admin's own toggle changes html[data-theme]; mirror it back to the application choice.
  new MutationObserver(function () {
    var mode = document.documentElement.dataset.theme;
    if (toApp[mode]) {
      try {
        localStorage.setItem("vr-theme", toApp[mode]);
      } catch (error) {
        // Storage blocked: nothing to mirror.
      }
    }
  }).observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
})();
