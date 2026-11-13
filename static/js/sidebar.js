// Collapsible staff sidebar (from 992px): the menu folds into an icon rail. Each item's label then
// shows as a Bootstrap tooltip on hover and keyboard focus. The state is the user's preference
// "nav.collapsed" (static/js/preferences.js), so it follows the user, not the workstation.
(function () {
  "use strict";

  function shell() {
    return document.querySelector(".vr-shell");
  }

  function links() {
    return document.querySelectorAll("#vr-nav .vr-navlink");
  }

  function label(link) {
    var span = link.querySelector(":scope > span");
    return (span ? span.textContent : link.textContent).replace(/\s+/g, " ").trim();
  }

  // Tooltips only while collapsed: the expanded menu shows its labels already.
  function syncTooltips(collapsed) {
    if (!window.bootstrap) {
      return;
    }
    links().forEach(function (link) {
      var tip = window.bootstrap.Tooltip.getInstance(link);
      if (collapsed && !tip) {
        window.bootstrap.Tooltip.getOrCreateInstance(link, {
          title: label(link),
          placement: "right",
          container: "body", // the rail scrolls; a tooltip inside it would be clipped
          customClass: "vr-nav-tooltip",
        });
      } else if (!collapsed && tip) {
        tip.dispose();
      }
    });
  }

  function apply(collapsed, save) {
    var root = shell();
    var toggle = document.querySelector("[data-vr-nav-toggle]");
    if (!root || !toggle) {
      return;
    }
    root.classList.toggle("vr-nav-collapsed", collapsed);
    toggle.setAttribute("aria-expanded", collapsed ? "false" : "true");
    toggle.setAttribute("aria-label", collapsed ? "Expand menu" : "Collapse menu");
    syncTooltips(collapsed);
    if (save && window.vrPreferences) {
      window.vrPreferences.set("nav.collapsed", collapsed);
    }
  }

  document.addEventListener("click", function (event) {
    var toggle = event.target.closest("[data-vr-nav-toggle]");
    if (toggle) {
      apply(!shell().classList.contains("vr-nav-collapsed"), true);
    }
  });

  document.addEventListener("DOMContentLoaded", function () {
    var root = shell();
    if (root) {
      apply(root.classList.contains("vr-nav-collapsed"), false); // the server rendered the state
    }
  });
})();
