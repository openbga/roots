// Server-rendered page help: no requests, record reads, or HTML assembled from stored text.
(function () {
  "use strict";

  function init() {
    var panel = document.getElementById("vr-documentation");
    if (!panel) { return; }
    var destination = panel.querySelector("[data-vr-documentation-content]");
    var separator = panel.querySelector("[role=separator]");
    var baseSource = document.querySelector("template[data-vr-documentation-source]");
    var formDialog = document.getElementById("vr-form-dialog");
    var storageKey = "vr-documentation:" + (panel.dataset.vrUser || "anonymous");
    var width = 440;
    var open = false;
    var inForm = false;
    var lastTrigger = null;
    var currentId = null;
    var currentSource = null;
    var home = document.createComment("Documentation panel home");
    panel.before(home);

    try {
      var saved = JSON.parse(window.localStorage.getItem(storageKey));
      if (saved && typeof saved.open === "boolean") { open = saved.open; }
      if (saved && typeof saved.width === "number" && Number.isFinite(saved.width)) {
        width = Math.min(680, Math.max(320, saved.width));
      }
    } catch (error) { /* Private browsing or unavailable storage keeps session controls working. */ }

    function save() {
      try { window.localStorage.setItem(storageKey, JSON.stringify({ open: open, width: width })); }
      catch (error) { /* The panel remains usable without persistent browser storage. */ }
    }

    function bounds() {
      var max = window.innerWidth >= 992 ? Math.min(680, window.innerWidth - 620) : Math.min(680, window.innerWidth - 32);
      return { min: Math.min(320, max), max: max };
    }

    function setWidth(value, persist) {
      var limits = bounds();
      var actual = Math.round(Math.max(limits.min, Math.min(limits.max, value)));
      document.documentElement.style.setProperty("--vr-documentation-width", actual + "px");
      separator.setAttribute("aria-valuemin", String(limits.min));
      separator.setAttribute("aria-valuemax", String(limits.max));
      separator.setAttribute("aria-valuenow", String(actual));
      separator.setAttribute("aria-valuetext", actual + " pixels");
      if (persist) { width = actual; save(); }
    }

    function refresh() {
      if (!open) { return; }
      var source = inForm && formDialog.querySelector("#vr-form-content template[data-vr-documentation-source]") || baseSource;
      if (!source) { return; }
      var article = source.content.querySelector("[data-vr-documentation-id]");
      var id = article && article.dataset.vrDocumentationId;
      if (source === currentSource && id === currentId) { return; }
      var wasReading = destination.contains(document.activeElement);
      destination.replaceChildren(source.content.cloneNode(true));
      destination.scrollTop = 0;
      currentId = id;
      currentSource = source;
      if (wasReading) { destination.querySelector("h2").focus(); }
    }

    function render() {
      panel.hidden = !open;
      document.body.classList.toggle("vr-documentation-open", open);
      document.querySelectorAll("[data-vr-documentation-toggle]").forEach(function (button) {
        button.hidden = false;
        button.setAttribute("aria-expanded", String(open));
      });
      setWidth(width, false);
    }

    function close() {
      open = false;
      destination.replaceChildren();
      currentSource = null;
      currentId = null;
      render();
      save();
      // A form may have replaced the original opener, or made the page's toolbar inert.
      var target = lastTrigger;
      if (!target || !target.isConnected || target.closest("[inert]") || !target.getClientRects().length) {
        target = (inForm ? formDialog : document).querySelector("[data-vr-documentation-toggle]");
      }
      if (target) { target.focus(); }
    }

    document.addEventListener("click", function (event) {
      var button = event.target.closest("[data-vr-documentation-toggle]");
      if (!button) { return; }
      if (open) { lastTrigger = button; close(); return; }
      lastTrigger = button;
      open = true;
      refresh();
      render();
      save();
      var heading = destination.querySelector("h2");
      if (heading) { heading.focus(); }
    });
    panel.querySelector("[data-vr-documentation-close]").addEventListener("click", close);
    panel.addEventListener("keydown", function (event) {
      if (event.key === "Escape") {
        event.preventDefault();
        event.stopPropagation();
        close();
      }
    });
    separator.addEventListener("keydown", function (event) {
      var step = event.shiftKey ? 80 : 16;
      var actual = Number(separator.getAttribute("aria-valuenow"));
      var limits = bounds();
      var next = { ArrowLeft: actual + step, ArrowRight: actual - step, Home: limits.min, End: limits.max }[event.key];
      if (next === undefined) { return; }
      event.preventDefault();
      setWidth(next, true);
    });
    var pointerId = null;
    separator.addEventListener("pointerdown", function (event) {
      if (event.button !== 0) { return; }
      event.preventDefault();
      pointerId = event.pointerId;
      separator.setPointerCapture(pointerId);
      separator.focus();
      document.body.classList.add("vr-documentation-resizing");
    });
    separator.addEventListener("pointermove", function (event) {
      if (pointerId === event.pointerId) { setWidth(window.innerWidth - event.clientX, true); }
    });
    function endResize() {
      pointerId = null;
      document.body.classList.remove("vr-documentation-resizing");
    }
    separator.addEventListener("lostpointercapture", endResize);
    separator.addEventListener("pointerup", endResize);
    separator.addEventListener("pointercancel", endResize);
    window.addEventListener("resize", function () { setWidth(width, false); });
    if (formDialog) {
      // Keep this nonmodal sheet inside Bootstrap's existing form focus boundary.
      // Dock before form-dialog.js marks the other body branches inert.
      formDialog.addEventListener("show.bs.modal", function () {
        inForm = true;
        formDialog.append(panel);
        refresh();
      });
      formDialog.addEventListener("hidden.bs.modal", function () {
        inForm = false;
        home.after(panel);
        refresh();
      });
      new MutationObserver(refresh).observe(formDialog.querySelector("#vr-form-content"), { childList: true, subtree: true });
    }
    var topbar = document.querySelector(".vr-topbar");
    if (topbar && window.ResizeObserver) {
      new ResizeObserver(function () {
        document.documentElement.style.setProperty("--vr-documentation-top", topbar.getBoundingClientRect().height + "px");
      }).observe(topbar);
    }
    // Jump links scroll the drawer, never the page or its address.
    destination.addEventListener("click", function (event) {
      var link = event.target.closest("[data-vr-help-jump]");
      if (!link) { return; }
      var target = destination.querySelector(link.getAttribute("href"));
      if (!target) { return; }
      event.preventDefault();
      var heading = target.querySelector("h3") || target;
      heading.setAttribute("tabindex", "-1");
      target.scrollIntoView({ block: "start" });
      heading.focus({ preventScroll: true });
    });

    // "Find a field or action": hides reference items that don't match, opens the sections that
    // do, and marks the matches. Built from text nodes only; help text never becomes markup.
    var originals = new WeakMap();
    function plain(element) {
      if (!originals.has(element)) { originals.set(element, element.textContent); }
      return originals.get(element);
    }
    function mark(element, query) {
      var text = plain(element);
      element.replaceChildren();
      if (!query) { element.append(text); return false; }
      var lower = text.toLowerCase();
      var at = 0;
      var found = false;
      var index = lower.indexOf(query);
      while (index !== -1) {
        found = true;
        element.append(text.slice(at, index));
        var hit = document.createElement("mark");
        hit.textContent = text.slice(index, index + query.length);
        element.append(hit);
        at = index + query.length;
        index = lower.indexOf(query, at);
      }
      element.append(text.slice(at));
      return found;
    }
    destination.addEventListener("input", function (event) {
      if (event.target.id !== "vr-help-filter") { return; }
      var query = event.target.value.trim().toLowerCase();
      var reference = destination.querySelector("[data-vr-help-reference]");
      var any = false;
      reference.querySelectorAll("[data-vr-help-section]").forEach(function (section) {
        if (!section.hasAttribute("data-vr-help-default")) {
          section.setAttribute("data-vr-help-default", section.open ? "open" : "closed");
        }
        var matches = 0;
        section.querySelectorAll("[data-vr-help-text]").forEach(function (paragraph) {
          var hit = mark(paragraph, query);
          paragraph.hidden = Boolean(query) && !hit;
          if (hit) { matches += 1; }
        });
        section.querySelectorAll("dl > div").forEach(function (item) {
          var hits = Array.prototype.map.call(item.querySelectorAll("dt, dd"), function (part) {
            return mark(part, query);
          });
          var hit = hits.indexOf(true) !== -1;
          item.hidden = Boolean(query) && !hit;
          if (hit) { matches += 1; }
        });
        section.hidden = Boolean(query) && !matches;
        section.open = query ? matches > 0 : section.getAttribute("data-vr-help-default") === "open";
        if (matches) { any = true; }
      });
      reference.querySelector("[data-vr-help-none]").hidden = !query || any;
    });

    refresh();
    render();
  }
  if (document.readyState === "loading") { document.addEventListener("DOMContentLoaded", init); }
  else { init(); }
})();
