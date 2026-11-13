// Public site behaviour. Theme menu: Light, Dark or System, stored in localStorage "vr-theme", the
// application's key, so a choice made in the app carries over on the same origin. The inline script
// in <head> applies the saved choice before CSS loads; this file handles the menu, the hero picture
// and changes made in another tab or in the system setting. Without JavaScript the menu stays hidden
// and the page follows the system setting.
(function () {
  "use strict";
  var KEY = "vr-theme";
  var root = document.documentElement;
  var systemDark = window.matchMedia ? window.matchMedia("(prefers-color-scheme: dark)") : null;

  function saved() {
    try {
      return localStorage.getItem(KEY) || "system";
    } catch (error) {
      return "system";
    }
  }

  function apply(choice) {
    if (choice !== "light" && choice !== "dark") {
      choice = "system";
    }
    var dark = choice === "dark" || (choice === "system" && !!systemDark && systemDark.matches);
    root.setAttribute("data-theme", dark ? "dark" : "light");
    root.setAttribute("data-theme-choice", choice);
    // A <source data-theme-dark> carries the dark art; pin it to the chosen theme.
    document.querySelectorAll("source[data-theme-dark]").forEach(function (source) {
      source.media = dark ? "all" : "not all";
    });
    var label = choice.charAt(0).toUpperCase() + choice.slice(1);
    document.querySelectorAll("[data-theme-label]").forEach(function (text) {
      text.textContent = label;
    });
    document.querySelectorAll("[data-theme-value]").forEach(function (option) {
      option.setAttribute("aria-pressed", option.getAttribute("data-theme-value") === choice ? "true" : "false");
    });
  }

  function setOpen(menu, open, focusButton) {
    var button = menu.querySelector("[data-theme-button]");
    var options = menu.querySelector("[data-theme-options]");
    button.setAttribute("aria-expanded", open ? "true" : "false");
    options.hidden = !open;
    if (open) {
      (options.querySelector('[aria-pressed="true"]') || options.querySelector("button")).focus();
    } else if (focusButton) {
      button.focus();
    }
  }

  document.querySelectorAll("[data-theme-menu]").forEach(function (menu) {
    var button = menu.querySelector("[data-theme-button]");
    var options = menu.querySelector("[data-theme-options]");
    var items = Array.prototype.slice.call(options.querySelectorAll("[data-theme-value]"));

    button.addEventListener("click", function () {
      setOpen(menu, options.hidden, false);
    });
    button.addEventListener("keydown", function (event) {
      if (event.key === "ArrowDown" || event.key === "ArrowUp") {
        event.preventDefault();
        setOpen(menu, true, false);
      }
    });
    items.forEach(function (item, index) {
      item.addEventListener("click", function () {
        var choice = item.getAttribute("data-theme-value");
        try {
          localStorage.setItem(KEY, choice);
        } catch (error) {
          // Storage blocked: the choice applies to this page only.
        }
        apply(choice);
        setOpen(menu, false, true);
      });
      item.addEventListener("keydown", function (event) {
        var next = null;
        if (event.key === "ArrowDown") next = items[(index + 1) % items.length];
        if (event.key === "ArrowUp") next = items[(index - 1 + items.length) % items.length];
        if (event.key === "Home") next = items[0];
        if (event.key === "End") next = items[items.length - 1];
        if (next) {
          event.preventDefault();
          next.focus();
        }
      });
    });
    menu.addEventListener("keydown", function (event) {
      if (event.key === "Escape" && !options.hidden) {
        event.preventDefault();
        setOpen(menu, false, true);
      }
    });
    // Close when focus or a click moves outside the menu.
    menu.addEventListener("focusout", function (event) {
      if (!options.hidden && event.relatedTarget && !menu.contains(event.relatedTarget)) {
        setOpen(menu, false, false);
      }
    });
    document.addEventListener("click", function (event) {
      if (!options.hidden && !menu.contains(event.target)) {
        setOpen(menu, false, false);
      }
    });
  });

  if (systemDark) {
    systemDark.addEventListener("change", function () {
      if (saved() === "system") {
        apply("system");
      }
    });
  }
  window.addEventListener("storage", function (event) {
    if (event.key === KEY) {
      apply(saved());
    }
  });

  apply(saved());

  // Roadmap graphic: one tooltip layer, last in the graphic so it sits above every node, label and
  // branch. It opens above the hovered or focused node (below when there is no room), stays inside
  // the graphic and points at the node's icon. The inline .tip spans remain the links' descriptions.
  var route = document.querySelector("[data-route]");
  if (route) {
    var layer = document.createElement("div");
    layer.className = "route-tip";
    layer.setAttribute("aria-hidden", "true");
    layer.hidden = true;
    var text = document.createElement("span");
    var arrow = document.createElement("span");
    arrow.className = "route-tip-arrow";
    layer.appendChild(text);
    layer.appendChild(arrow);
    route.appendChild(layer);
    route.classList.add("has-tip-layer");
    var current = null;
    var hideTimer = null;
    var GAP = 10;
    var EDGE = 8;

    function show(node) {
      var tip = node.querySelector(".tip");
      var art = route.querySelector(".route-art");
      if (!tip || !art || getComputedStyle(art).display === "none") {
        return;
      }
      clearTimeout(hideTimer);
      current = node;
      text.textContent = tip.textContent;
      layer.hidden = false;
      var box = route.getBoundingClientRect();
      var around = node.getBoundingClientRect();
      var icon = (node.querySelector(".node-icon") || node).getBoundingClientRect();
      var width = layer.offsetWidth;
      var height = layer.offsetHeight;
      var side = "top";
      var top = around.top - box.top - GAP - height;
      if (top < EDGE) {
        side = "bottom";
        top = around.bottom - box.top + GAP;
      }
      var centre = icon.left + icon.width / 2 - box.left;
      var left = Math.min(Math.max(centre - width / 2, EDGE), box.width - width - EDGE);
      layer.setAttribute("data-side", side);
      layer.style.transform = "translate(" + Math.round(left) + "px, " + Math.round(top) + "px)";
      arrow.style.left = Math.min(Math.max(centre - left, 14), width - 14) + "px";
    }

    function hide(now) {
      clearTimeout(hideTimer);
      if (now) {
        layer.hidden = true;
        current = null;
        return;
      }
      hideTimer = setTimeout(function () { hide(true); }, 120);
    }

    route.querySelectorAll(".node").forEach(function (node) {
      node.addEventListener("mouseenter", function () { show(node); });
      node.addEventListener("mouseleave", function () { hide(false); });
      node.addEventListener("focus", function () { show(node); });
      node.addEventListener("blur", function () { hide(true); });
    });
    // The pointer may move onto the tooltip to read it.
    layer.addEventListener("mouseenter", function () { clearTimeout(hideTimer); });
    layer.addEventListener("mouseleave", function () { hide(false); });
    document.addEventListener("keydown", function (event) {
      if (event.key === "Escape" && current) {
        hide(true);
      }
    });
    window.addEventListener("resize", function () {
      if (current) {
        show(current);
      }
    });
  }

  // Arriving from the graphic, focus follows the jump: to the card's heading (made focusable for
  // this), or back to the node from the card's "Back to the roadmap" link. The browser has already
  // scrolled; focus must not scroll again.
  function focusTarget(id) {
    var target = id && document.getElementById(id);
    if (!target) {
      return;
    }
    var heading = target.closest("[data-route]") ? target : target.querySelector("h3, th, dt");
    if (!heading) {
      return;
    }
    if (!heading.hasAttribute("href") && !heading.hasAttribute("tabindex")) {
      heading.setAttribute("tabindex", "-1");
    }
    heading.focus({preventScroll: true});
  }
  document.addEventListener("click", function (event) {
    var link = event.target.closest("[data-route] .node, .back-to-map");
    if (!link) {
      return;
    }
    var id = link.getAttribute("href").slice(1);
    // Wait for the navigation (and :target) before moving focus.
    setTimeout(function () { focusTarget(id); }, 0);
  });
})();
