// Small progressive enhancements. Pages must work without this file.
(function () {
  "use strict";

  // Colour theme: "light" | "dark" | "system" in localStorage "vr-theme" (see theme-init.js).
  var THEME_KEY = "vr-theme";
  var systemDark = window.matchMedia ? window.matchMedia("(prefers-color-scheme: dark)") : null;

  function savedTheme() {
    try {
      return localStorage.getItem(THEME_KEY) || "system";
    } catch (error) {
      return "system";
    }
  }

  function applyTheme(choice) {
    var dark = choice === "dark" || (choice === "system" && systemDark && systemDark.matches);
    document.documentElement.setAttribute("data-bs-theme", dark ? "dark" : "light");
    document.documentElement.setAttribute("data-vr-theme-choice", choice);
    var label = choice.charAt(0).toUpperCase() + choice.slice(1);
    document.querySelectorAll("[data-vr-theme-value]").forEach(function (option) {
      option.setAttribute("aria-pressed", option.getAttribute("data-vr-theme-value") === choice ? "true" : "false");
    });
    document.querySelectorAll("[data-vr-theme-button]").forEach(function (button) {
      button.setAttribute("title", "Theme: " + label);
    });
  }

  document.addEventListener("click", function (event) {
    var option = event.target.closest("[data-vr-theme-value]");
    if (!option) {
      return;
    }
    var choice = option.getAttribute("data-vr-theme-value");
    try {
      localStorage.setItem(THEME_KEY, choice);
    } catch (error) {
      // Storage blocked: the choice applies to this page only.
    }
    applyTheme(choice);
    var toggle = option.closest(".dropdown").querySelector("[data-vr-theme-button]");
    if (toggle) {
      toggle.focus();
    }
  });

  if (systemDark) {
    systemDark.addEventListener("change", function () {
      if (savedTheme() === "system") {
        applyTheme("system");
      }
    });
  }

  // Another tab changed the theme.
  window.addEventListener("storage", function (event) {
    if (event.key === THEME_KEY) {
      applyTheme(savedTheme());
    }
  });

  document.addEventListener("DOMContentLoaded", function () {
    applyTheme(savedTheme());
  });

  // Page-bounded select-all: <input data-vr-select-all="name"> checks the enabled row checkboxes
  // named "name" in the same table; <span data-vr-select-count="name"> reads "3 of 18 selected".
  function selectionScope(control) {
    return control.form || control.closest("form, .vr-table-card") || document;
  }

  function rowBoxes(name, scope) {
    if (scope && scope.tagName === "FORM") {
      return Array.from(scope.elements).filter(function (control) {
        return control.type === "checkbox" && control.name === name;
      });
    }
    return Array.prototype.slice.call((scope || document).querySelectorAll('input[type="checkbox"][name="' + name + '"]'));
  }

  function syncSelection(name) {
    document.querySelectorAll('[data-vr-select-all="' + name + '"]').forEach(function (master) {
      var eligible = rowBoxes(name, master.closest("table")).filter(function (box) { return !box.disabled; });
      var checked = eligible.filter(function (box) { return box.checked; }).length;
      master.checked = eligible.length > 0 && checked === eligible.length;
      master.indeterminate = checked > 0 && checked < eligible.length;
      master.disabled = eligible.length === 0;
      selectionScope(master).querySelectorAll('[data-vr-select-count="' + name + '"]').forEach(function (counter) {
        counter.textContent = checked + " of " + eligible.length + " selected";
      });
    });
    // Optional "select only ready" shortcut (includes/select_all.html with ready_label).
    document.querySelectorAll('[data-vr-select-ready="' + name + '"]').forEach(function (button) {
      var ready = rowBoxes(name, selectionScope(button)).filter(function (box) { return !box.disabled && box.hasAttribute("data-ready"); });
      button.disabled = ready.length === 0;
    });
  }

  document.addEventListener("click", function (event) {
    var button = event.target.closest("[data-vr-select-ready]");
    if (!button) {
      return;
    }
    var name = button.getAttribute("data-vr-select-ready");
    var changedForms = new Set();
    rowBoxes(name, selectionScope(button)).forEach(function (box) {
      if (!box.disabled) {
        var checked = box.hasAttribute("data-ready");
        if (box.checked !== checked && box.form) {
          changedForms.add(box.form);
        }
        box.checked = checked;
      }
    });
    syncSelection(name);
    changedForms.forEach(function (form) {
      form.dispatchEvent(new Event("change", { bubbles: true }));
    });
  });

  document.addEventListener("change", function (event) {
    var master = event.target.closest("[data-vr-select-all]");
    if (master) {
      var name = master.getAttribute("data-vr-select-all");
      rowBoxes(name, master.closest("table")).forEach(function (box) {
        if (!box.disabled) {
          box.checked = master.checked;
        }
      });
      syncSelection(name);
      return;
    }
    if (event.target.matches('input[type="checkbox"][name]')) {
      if (document.querySelector('[data-vr-select-all="' + event.target.name + '"]')) {
        syncSelection(event.target.name);
      }
    }
  });

  document.addEventListener("DOMContentLoaded", function () {
    document.querySelectorAll("[data-vr-select-all]").forEach(function (master) {
      syncSelection(master.getAttribute("data-vr-select-all"));
    });
  });

  // Long checkbox/radio lists (more than ten options) get a filter box. Presentation only: hidden
  // options keep their checked state and are still submitted.
  function addChoiceFilter(list) {
    var options = Array.prototype.slice.call(list.querySelectorAll(".form-check"));
    if (options.length <= 10 || list.previousElementSibling && list.previousElementSibling.matches(".vr-choice-filter")) {
      return;
    }
    var legend = list.closest("fieldset") && list.closest("fieldset").querySelector("legend");
    var name = legend ? legend.textContent.replace(/\*/g, "").trim().toLowerCase() : "options";
    var wrap = document.createElement("div");
    wrap.className = "vr-choice-filter";
    var input = document.createElement("input");
    input.type = "search";
    input.className = "form-control form-control-sm";
    input.placeholder = "Filter " + name;
    input.setAttribute("aria-label", "Filter " + name);
    input.autocomplete = "off";
    var status = document.createElement("span");
    status.className = "form-text";
    status.setAttribute("role", "status");
    wrap.append(input, status);
    list.parentNode.insertBefore(wrap, list);
    input.addEventListener("input", function () {
      var terms = input.value.toLowerCase().split(/\s+/).filter(Boolean);
      var shown = 0;
      options.forEach(function (option) {
        var text = option.textContent.toLowerCase();
        var match = terms.every(function (term) { return text.indexOf(term) !== -1; });
        option.hidden = !match;
        shown += match ? 1 : 0;
      });
      status.textContent = terms.length ? shown + " of " + options.length + " shown; selections are kept." : "";
    });
  }

  document.addEventListener("DOMContentLoaded", function () {
    document.querySelectorAll(".vr-choices").forEach(addChoiceFilter);
  });

  // Native focus scrolling can place entry-grid fields behind a sticky row identifier.
  // Use its actual width, including enlarged text, rather than assuming a column size.
  document.addEventListener("focusin", function (event) {
    var control = event.target;
    var wrap = control.closest(".vr-table-wrap");
    var row = wrap && control.closest("tr");
    var identifier = row && row.querySelector('th[scope="row"]:first-child');
    if (!identifier || identifier.contains(control) || getComputedStyle(identifier).position !== "sticky") {
      return;
    }
    var overlap = identifier.getBoundingClientRect().right - control.getBoundingClientRect().left;
    if (overlap > 0) {
      wrap.scrollLeft -= overlap + 4;
    }
  });

  // After a failed submission, focus the first invalid field or the form-level error.
  document.addEventListener("DOMContentLoaded", function () {
    var invalid = document.querySelector("form [aria-invalid=true]");
    if (invalid && invalid.tagName === "FIELDSET") {
      invalid = invalid.querySelector("input, select, textarea");
    }
    if (invalid && invalid.tomselect) {
      invalid = invalid.tomselect.control_input; // Enhanced select: the native one is hidden.
    }
    if (invalid) {
      invalid.focus();
    } else {
      var error = document.querySelector(".vr-main .alert-danger[role=alert], .vr-entrance-panel .alert-danger[role=alert], .vr-messages [role=alert]");
      if (error) {
        error.setAttribute("tabindex", "-1");
        error.focus();
      }
    }
  });

  // Native POST forms: block double submission while the request is in flight.
  // Buttons are disabled after the submit event so their name/value is still sent.
  document.addEventListener("submit", function (event) {
    var form = event.target;
    var method = (form.getAttribute("method") || "get").toLowerCase();
    if (event.defaultPrevented || method !== "post" || form.hasAttribute("hx-post")) {
      return;
    }
    if (form.dataset.vrSubmitting === "true") {
      event.preventDefault();
      return;
    }
    form.dataset.vrSubmitting = "true";
    form.setAttribute("aria-busy", "true");
    var submitter = event.submitter;
    window.setTimeout(function () {
      form.querySelectorAll("button[type=submit], button:not([type])").forEach(function (button) {
        button.disabled = true;
      });
      // Release, import and export actions render or parse on the server and can take seconds:
      // show that the request is running, and announce it, instead of only greying the button.
      if (submitter && submitter.tagName === "BUTTON") {
        var spinner = document.createElement("span");
        spinner.className = "spinner-border spinner-border-sm me-2 vr-busy-mark";
        spinner.setAttribute("aria-hidden", "true");
        submitter.prepend(spinner);
        var status = document.createElement("span");
        status.className = "visually-hidden vr-busy-mark";
        status.setAttribute("role", "status");
        status.textContent = "Working. Please wait.";
        form.append(status);
      }
    }, 0);
  });

  // Back/forward cache restores the disabled state; re-enable on return.
  window.addEventListener("pageshow", function (event) {
    if (!event.persisted) {
      return;
    }
    document.querySelectorAll("form[data-vr-submitting]").forEach(function (form) {
      delete form.dataset.vrSubmitting;
      form.removeAttribute("aria-busy");
      document.querySelectorAll(".vr-busy-mark").forEach(function (mark) { mark.remove(); });
      form.querySelectorAll("button[type=submit], button:not([type])").forEach(function (button) {
        button.disabled = false;
      });
    });
  });

  // Django formsets: a button[data-formset-add="<prefix>"] appends a copy of
  // <template id="<prefix>-empty"> (the formset's empty_form) to #<prefix>-rows and keeps
  // TOTAL_FORMS accurate. "__num__" in the template becomes the 1-based row number.
  function initFormsetButton(button) {
    if (button.dataset.vrFormsetReady === "true") {
      return;
    }
    var prefix = button.dataset.formsetAdd;
    var total = document.querySelector("input[name='" + prefix + "-TOTAL_FORMS']");
    var max = document.querySelector("input[name='" + prefix + "-MAX_NUM_FORMS']");
    var template = document.getElementById(prefix + "-empty");
    var rows = document.getElementById(prefix + "-rows");
    if (!total || !template || !rows) {
      return;
    }
    button.dataset.vrFormsetReady = "true";
    function atLimit() {
      return max && parseInt(total.value, 10) >= parseInt(max.value, 10);
    }
    button.hidden = false;
    button.disabled = atLimit();
    button.addEventListener("click", function () {
      if (atLimit()) {
        return;
      }
      var index = parseInt(total.value, 10);
      var html = template.innerHTML.replace(/__prefix__/g, String(index)).replace(/__num__/g, String(index + 1));
      rows.insertAdjacentHTML("beforeend", html);
      if (window.htmx) {
        window.htmx.process(rows.lastElementChild);
        window.htmx.trigger(rows.lastElementChild, "htmx:load");
      }
      var form = button.form || button.closest("form");
      if (form) {
        form.dispatchEvent(new Event("change", { bubbles: true }));
      }
      total.value = String(index + 1);
      button.disabled = atLimit();
      var added = rows.lastElementChild;
      var first = added && added.querySelector("input:not([type=hidden]), select, textarea");
      if (first) {
        first.focus();
      }
    });
  }

  document.addEventListener("DOMContentLoaded", function () {
    document.querySelectorAll("[data-formset-add]").forEach(initFormsetButton);
  });

  document.addEventListener("htmx:load", function () {
    document.querySelectorAll("[data-vr-select-all]").forEach(function (master) {
      syncSelection(master.getAttribute("data-vr-select-all"));
    });
    document.querySelectorAll(".vr-choices").forEach(addChoiceFilter);
    document.querySelectorAll("[data-formset-add]").forEach(initFormsetButton);
  });

  // Print actions remain declarative so a strict script policy can stay enabled.
  document.addEventListener("click", function (event) {
    if (event.target.closest("[data-vr-print]")) {
      window.print();
    }
  });

  // HTMX transport and server errors: keep the page as it is and say what happened.
  function showRequestError(message) {
    var region = document.getElementById("vr-live");
    if (!region) {
      return;
    }
    region.replaceChildren();
    var alert = document.createElement("div");
    alert.className = "alert alert-danger alert-dismissible shadow-sm";
    alert.setAttribute("role", "alert");
    var text = document.createElement("div");
    text.textContent = message;
    var close = document.createElement("button");
    close.type = "button";
    close.className = "btn-close";
    close.setAttribute("data-bs-dismiss", "alert");
    close.setAttribute("aria-label", "Dismiss");
    alert.append(text, close);
    region.append(alert);
  }

  document.addEventListener("htmx:responseError", function (event) {
    if (event.detail.target && event.detail.target.closest("#vr-form-dialog")) {
      return; // The form dialog keeps its error beside the preserved inputs.
    }
    var status = event.detail.xhr ? event.detail.xhr.status : "";
    var message = status === 403
      ? "You do not have permission for that action. Nothing was changed."
      : "The server could not complete the request (" + status + "). Your input was kept; try again.";
    showRequestError(message);
  });

  document.addEventListener("htmx:sendError", function (event) {
    if (event.detail.target && event.detail.target.closest("#vr-form-dialog")) {
      return;
    }
    showRequestError("The server could not be reached. Check the connection and try again.");
  });
})();
