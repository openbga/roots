// One Bootstrap dialog for dedicated form pages and explicitly marked inline forms.
// Links and native forms remain the no-JavaScript fallback; the server owns every check.
(function () {
  "use strict";

  function init() {
    var element = document.getElementById("vr-form-dialog");
    var registry = document.getElementById("vr-form-routes");
    if (!element || !registry || !window.bootstrap || !window.htmx) {
      return;
    }
    var routes;
    try {
      routes = JSON.parse(registry.textContent).map(function (route) {
        return { pattern: new RegExp(route.pattern), size: route.size };
      });
    } catch (error) {
      return; // Invalid configuration: ordinary page navigation remains available.
    }
    var content = document.getElementById("vr-form-content");
    var title = document.getElementById("vr-form-title");
    var errorRegion = document.getElementById("vr-form-error");
    // The discard confirmation is its own small modal (includes/form_dialog.html), stacked over the
    // form dialog while it asks.
    var discard = document.getElementById("vr-form-discard");
    var confirming = false;
    var dialog = element.querySelector(".modal-dialog");
    var maximize = element.querySelector("[data-vr-form-maximize]");
    var tools = element.querySelector("[data-vr-form-tools]");
    var modal = window.bootstrap.Modal.getOrCreateInstance(element, { backdrop: "static", keyboard: false });
    var active = false;
    var shown = false;
    var settling = false;
    var needsFocus = false;
    var dirty = false;
    var allowedHide = false;
    var nativeLeaving = false;
    var opener = null;
    var pending = null;
    var confirmationFocus = null;
    var directDestination = null;
    var currentUrl = "";
    var retryUrl = null;
    var localForm = null;
    var requests = new Set();
    var disabledControls = new Map();
    var background = new Map();
    var backgroundIds = new Map();

    function namespaceBackground(root) {
      [root].concat(Array.from(root.querySelectorAll("[id]"))).forEach(function (node) {
        if (!node.id || node.id === "main" || node.id === "vr-nav" || node.closest("main[data-vr-form-page]")) {
          return;
        }
        // Background controls are inert until restored. Give the active form its original
        // IDs, so native labels and page-specific scripts never find a background filter.
        backgroundIds.set(node, node.id);
        node.id = "vr-background-" + backgroundIds.size + "-" + node.id;
      });
    }

    function setBackgroundInert(inert) {
      if (inert) {
        var branch = element;
        while (branch.parentElement) {
          Array.from(branch.parentElement.children).forEach(function (sibling) {
            // The discard confirmation opens over the form dialog: it is never background.
            if (sibling !== branch && sibling !== discard && !background.has(sibling) && !sibling.matches("script, style, link, template, .modal-backdrop")) {
              background.set(sibling, sibling.inert);
              sibling.inert = true;
              namespaceBackground(sibling);
            }
          });
          if (branch.parentElement === document.body) {
            break;
          }
          branch = branch.parentElement;
        }
      } else {
        backgroundIds.forEach(function (id, node) { node.id = id; });
        backgroundIds.clear();
        background.forEach(function (value, sibling) { sibling.inert = value; });
        background.clear();
      }
    }

    function routeFor(url) {
      return url.origin === window.location.origin && routes.find(function (route) {
        return route.pattern.test(url.pathname);
      });
    }

    function isBusy() {
      return requests.size > 0 || settling || active && !shown;
    }

    function focusForm() {
      if (!active || isBusy() || confirming) {
        return;
      }
      var field = content.querySelector("[aria-invalid=true], .is-invalid");
      var focused = document.activeElement;
      if (!field && content.contains(focused) && focused.matches("input, select, textarea, [contenteditable=true]") && !focused.matches(":disabled")) {
        return; // A ready form must not take focus away from an edit already in progress.
      }
      if (field && field.tagName === "FIELDSET") {
        field = field.querySelector("input, select, textarea");
      }
      field = field || content.querySelector("input:not([type=hidden]):not([disabled]), select:not([disabled]), textarea:not([disabled])");
      if (field && field.tomselect) {
        field = field.tomselect.focus_node;
      }
      if (field && field.getClientRects().length) {
        field.focus();
      } else {
        title.setAttribute("tabindex", "-1");
        title.focus();
      }
    }

    function setSize(size) {
      dialog.classList.toggle("modal-lg", size !== "xl");
      dialog.classList.toggle("modal-xl", size === "xl");
    }

    function setBusy() {
      var busy = isBusy();
      content.setAttribute("aria-busy", busy ? "true" : "false");
      content.inert = busy;
      if (busy) {
        content.querySelectorAll("button, input, select, textarea").forEach(function (button) {
          if (!disabledControls.has(button)) {
            disabledControls.set(button, button.disabled);
          }
          button.disabled = true;
        });
      } else {
        disabledControls.forEach(function (disabled, button) { button.disabled = disabled; });
        disabledControls.clear();
      }
      element.querySelectorAll("[data-vr-form-close], [data-vr-form-discard]").forEach(function (button) {
        button.disabled = busy;
      });
    }

    function finishReady() {
      setBusy();
      if (isBusy()) {
        return;
      }
      if (window.vrControls) {
        window.vrControls.init(content); // After setBusy: controls must not initialise disabled.
      }
      if (window.vrInitTestPickers) {
        window.vrInitTestPickers(content);
      }
      if (needsFocus) {
        needsFocus = false;
        focusForm(); // Enable and focus in this callback, before another edit can begin.
      }
    }

    function showError(message, retry) {
      // The region is a danger notice (includes/form_dialog.html): keep its stamp, fill its text.
      var text = errorRegion.querySelector("[data-vr-notice-text]");
      var oldRetry = errorRegion.querySelector("[data-vr-form-retry]");
      if (oldRetry) {
        oldRetry.remove();
      }
      text.replaceChildren(document.createTextNode(message));
      errorRegion.hidden = false;
      if (retry) {
        var button = window.vrUi.button("Try again", "secondary", true);
        button.classList.add("ms-2");
        button.setAttribute("data-vr-form-retry", "");
        text.after(button);
      }
    }

    function discardModal() {
      return window.bootstrap.Modal.getOrCreateInstance(discard, { backdrop: "static", keyboard: false });
    }

    // Bootstrap does not stack modals: the form dialog's focus trap would pull focus back from the
    // confirmation, so it pauses while the confirmation is open.
    function pauseFocusTrap(paused) {
      var trap = modal._focustrap;
      if (trap) {
        if (paused) {
          trap.deactivate();
        } else if (active) {
          trap.activate();
        }
      }
    }

    function clearConfirmation(then) {
      pending = null;
      if (!confirming) {
        if (then) {
          then();
        }
        return;
      }
      discard.addEventListener("hidden.bs.modal", function () {
        confirming = false;
        pauseFocusTrap(false);
        if (then) {
          then();
        }
      }, { once: true });
      discardModal().hide();
    }

    function confirmLeave(action) {
      if (isBusy() || confirming) {
        return;
      }
      if (!dirty) {
        action();
        return;
      }
      pending = action;
      confirmationFocus = document.activeElement;
      confirming = true;
      pauseFocusTrap(true);
      discard.addEventListener("shown.bs.modal", function () {
        (discard.querySelector("[data-vr-form-keep][autofocus]") || discard.querySelector("[data-vr-form-keep]")).focus();
      }, { once: true });
      discardModal().show();
    }

    function keepEditing() {
      clearConfirmation(function () {
        if (confirmationFocus && confirmationFocus.isConnected) {
          confirmationFocus.focus();
        }
      });
    }

    function discardChanges() {
      var action = pending;
      dirty = false;
      clearConfirmation(function () {
        if (action) {
          action();
        }
      });
    }

    discard.addEventListener("click", function (event) {
      var target = event.target.closest("button");
      if (target && target.hasAttribute("data-vr-form-keep")) {
        keepEditing();
      } else if (target && target.hasAttribute("data-vr-form-discard")) {
        discardChanges();
      }
    });

    // Escape in the confirmation means Keep editing.
    discard.addEventListener("keydown", function (event) {
      if (event.key === "Escape") {
        event.preventDefault();
        event.stopPropagation();
        keepEditing();
      }
    });

    function restoreLocal() {
      if (localForm) {
        localForm.form.reset();
        localForm.form.hidden = true;
        localForm.form.remove();
        localForm = null;
      }
    }

    function close() {
      dirty = false;
      clearConfirmation();
      if (directDestination) {
        window.location.assign(directDestination);
        return;
      }
      allowedHide = true;
      modal.hide();
    }

    function begin(source) {
      if (!active) {
        opener = source || document.activeElement;
        active = true;
        needsFocus = true;
        dialog.classList.remove("modal-fullscreen");
        maximize.textContent = "Maximize";
        maximize.setAttribute("aria-pressed", "false");
        modal.show(source);
        setBusy();
        setBackgroundInert(true);
      }
      clearConfirmation();
      errorRegion.hidden = true;
    }

    function load(url, source) {
      restoreLocal();
      begin(source);
      dirty = false;
      retryUrl = url;
      currentUrl = url;
      clearPageActions();
      var route = routeFor(new URL(url, window.location.href));
      setSize(route && route.size);
      if (!content.querySelector("[data-vr-form-page]")) {
        content.replaceChildren();
        var status = document.createElement("p");
        status.setAttribute("role", "status");
        status.textContent = "Loading form…";
        content.append(status);
        title.textContent = "Loading form";
      }
      // HTMX owns transport, CSRF inheritance, submitter values, and script execution.
      window.htmx.ajax("GET", url, {
        source: element, target: content, swap: "innerHTML", headers: { "X-VR-Form": "1" },
      }).catch(function () {
        // htmx:sendError / responseError provides the visible recovery message.
      });
    }

    function enhancePage() {
      var page = content.querySelector("[data-vr-form-page]");
      if (!page) {
        return;
      }
      currentUrl = page.getAttribute("data-vr-form-url") || currentUrl;
      retryUrl = null;
      var header = page.querySelector(".vr-page-header");
      if (header) {
        var facts = header.querySelectorAll(".vr-page-meta, .vr-page-intro");
        var destination = header;
        facts.forEach(function (part) {
          destination.after(part);
          destination = part;
        });
      }
      var heading = page.querySelector(".vr-page-header h1, h1");
      title.textContent = heading ? heading.textContent.trim() : "Form";
      setSize(page.getAttribute("data-vr-form-size"));
      // Query-only sort/pager links belong to the form URL, while the document stays
      // on its background record. Resolve them once so modified clicks work too.
      var pageUrl = new URL(currentUrl, window.location.href);
      page.querySelectorAll("a[href]").forEach(function (link) {
        var href = link.getAttribute("href");
        if (href.charAt(0) !== "#") {
          link.href = new URL(href, pageUrl).href;
        }
      });
      page.querySelectorAll("[formaction]").forEach(function (button) {
        button.setAttribute("formaction", new URL(button.getAttribute("formaction") || currentUrl, pageUrl).href);
      });
      page.querySelectorAll("form").forEach(function (form) {
        var method = (form.getAttribute("method") || "get").toLowerCase();
        var action = form.getAttribute("action") || currentUrl;
        action = new URL(action, new URL(currentUrl, window.location.href)).href;
        form.action = action;
        form.setAttribute(method === "post" ? "hx-post" : "hx-get", action);
        form.setAttribute("hx-target", "#vr-form-content");
        form.setAttribute("hx-swap", "innerHTML");
        form.setAttribute("hx-headers", '{"X-VR-Form":"1"}');
        form.setAttribute("hx-push-url", "false");
        form.setAttribute("hx-replace-url", "false");
        form.setAttribute("hx-sync", "this:drop");
        if (form.enctype === "multipart/form-data") {
          form.setAttribute("hx-encoding", "multipart/form-data");
        }
      });
      showPageActions(page);
      window.htmx.process(page);
    }

    // The page header is hidden in the dialog, so its actions (a method's New version) move to
    // the dialog header, before Help, in page order. Links stay links: a form route opens here.
    function showPageActions(page) {
      clearPageActions();
      var region = page && page.querySelector(".vr-page-actions");
      var first = tools.firstElementChild;
      Array.from(region ? region.children : []).forEach(function (action) {
        action.setAttribute("data-vr-page-action", "");
        tools.insertBefore(action, first);
      });
    }

    function clearPageActions() {
      tools.querySelectorAll("[data-vr-page-action]").forEach(function (action) {
        action.remove();
      });
    }

    function localButton(form) {
      if (form.dataset.vrLocalReady) {
        return null;
      }
      form.dataset.vrLocalReady = "true";
      form.action = form.getAttribute("action") ? new URL(form.getAttribute("action"), window.location.href).href : window.location.href;
      var button = window.vrUi.button(form.getAttribute("data-vr-local-form") || "Open form", "secondary");
      form.before(button);
      form.hidden = true;
      form.remove(); // Keep field IDs out of the background while another form is open.
      if (!form.querySelector("[data-vr-form-close], .vr-form-actions .btn-link")) {
        var cancel = window.vrUi.button("Cancel", "quiet");
        cancel.setAttribute("data-vr-form-close", "");
        var actions = form.querySelector(".vr-form-actions") || form;
        actions.insertBefore(cancel, actions.querySelector("button[type=submit]"));
      }
      function open() {
        restoreLocal();
        clearPageActions();
        begin(button);
        dirty = false;
        currentUrl = form.action;
        localForm = { form: form };
        title.textContent = form.getAttribute("data-vr-local-title") || button.textContent;
        setSize(form.getAttribute("data-vr-local-size"));
        content.replaceChildren(form);
        form.hidden = false;
        needsFocus = true;
        finishReady();
      }
      button.addEventListener("click", function () { confirmLeave(open); });
      return form.querySelector("[aria-invalid=true], .vr-error-summary, .vr-form-errors, .errorlist, .alert-danger[role=alert]") ? open : null;
    }

    // Capture links before HTMX's delegated handlers, retaining native modified/download links.
    document.addEventListener("click", function (event) {
      if (event.defaultPrevented || event.button !== 0 || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) {
        return;
      }
      var link = event.target.closest("a[href]");
      if (!link || link.hasAttribute("download") || link.target && link.target !== "_self" || link.getAttribute("href").charAt(0) === "#") {
        return;
      }
      var url = new URL(link.href, window.location.href);
      var inside = active && content.contains(link);
      var route = routeFor(url);
      if (!route && !inside) {
        return;
      }
      if (active && !inside && !element.contains(link)) {
        return; // Bootstrap keeps the background inert to pointer and keyboard interaction.
      }
      event.preventDefault();
      event.stopPropagation();
      var cancel = inside && (link.hasAttribute("data-vr-form-close") || link.textContent.trim() === "Cancel");
      confirmLeave(function () {
        dirty = false;
        if (cancel) {
          if (directDestination) {
            directDestination = link.href;
          }
          close();
        } else if (route) {
          load(url.href, link);
        } else {
          window.location.assign(url.href);
        }
      });
    }, true);

    element.addEventListener("click", function (event) {
      var target = event.target.closest("button");
      if (!target) {
        return;
      }
      if (target.hasAttribute("data-vr-form-close") || target.getAttribute("data-bs-dismiss") === "modal") {
        event.preventDefault();
        event.stopPropagation();
        confirmLeave(close);
      } else if (target.hasAttribute("data-vr-form-maximize")) {
        var full = dialog.classList.toggle("modal-fullscreen");
        target.textContent = full ? "Restore size" : "Maximize";
        target.setAttribute("aria-pressed", full ? "true" : "false");
        modal.handleUpdate();
      } else if (target.hasAttribute("data-vr-form-retry") && retryUrl) {
        load(retryUrl);
      }
    });

    // Escape belongs to an open control first (a picker's or a menu's list): it closes that list,
    // not the dialog. Recorded in the capture phase, before the control closes its list.
    var escapeForControl = false;
    element.addEventListener("keydown", function (event) {
      if (event.key === "Escape") {
        escapeForControl = Boolean(event.target.closest &&
          event.target.closest('[aria-expanded="true"], .ts-wrapper.dropdown-active, .dropdown-menu.show'));
      }
    }, true);

    element.addEventListener("keydown", function (event) {
      // With an inert background, Chromium may move directly to browser chrome at the
      // boundary, without the focusin event Bootstrap normally uses to return focus.
      if (event.key === "Tab") {
        var controls = Array.from(element.querySelectorAll("a[href], button, input, select, textarea, summary, [tabindex], [contenteditable=true]")).filter(function (control) {
          return control.tabIndex >= 0 && !control.matches(":disabled") && !control.closest("[inert]") &&
            control.getClientRects().length && window.getComputedStyle(control).visibility !== "hidden";
        });
        var first = controls[0];
        var last = controls[controls.length - 1];
        if (first && !event.shiftKey && document.activeElement === last) {
          event.preventDefault();
          first.focus();
        } else if (last && event.shiftKey && (document.activeElement === first || document.activeElement === title || document.activeElement === element)) {
          event.preventDefault();
          last.focus();
        }
      }
      if (event.key === "Escape") {
        if (escapeForControl && !confirming) {
          return;
        }
        event.preventDefault();
        event.stopPropagation();
        if (confirming) {
          keepEditing();
        } else {
          confirmLeave(close);
        }
      }
    });
    ["input", "change"].forEach(function (name) {
      content.addEventListener(name, function (event) {
        var form = event.target.closest("form");
        if (active && form && (form.getAttribute("method") || "get").toLowerCase() !== "get") {
          dirty = true;
        }
      });
    });
    document.addEventListener("submit", function (event) {
      if (!active || !content.contains(event.target)) {
        return;
      }
      if (isBusy()) {
        event.preventDefault();
        event.stopPropagation();
      } else if ((event.target.getAttribute("method") || "get").toLowerCase() === "get" && dirty) {
        event.preventDefault();
        event.stopPropagation();
        confirmLeave(function () {
          dirty = false;
          event.target.requestSubmit(event.submitter || undefined);
        });
      } else if (localForm && event.target === localForm.form) {
        nativeLeaving = true;
      }
    }, true);
    window.addEventListener("pageshow", function (event) {
      nativeLeaving = false;
      if (event.persisted) {
        requests.clear();
        setBusy();
      }
    });
    window.addEventListener("beforeunload", function (event) {
      if (active && !nativeLeaving && (dirty || requests.size)) {
        event.preventDefault();
        event.returnValue = "";
      }
    });
    element.addEventListener("hide.bs.modal", function (event) {
      if (!allowedHide) {
        event.preventDefault();
        confirmLeave(close);
      }
    });
    element.addEventListener("hidden.bs.modal", function () {
      active = false;
      shown = false;
      settling = false;
      needsFocus = false;
      allowedHide = false;
      restoreLocal();
      content.replaceChildren();
      clearPageActions();
      setBackgroundInert(false);
      if (opener && opener.isConnected) {
        opener.focus();
      }
    });
    element.addEventListener("shown.bs.modal", function () {
      shown = true;
      finishReady();
    });

    function isDialogRequest(event) {
      return event.detail.target === content;
    }
    document.addEventListener("htmx:beforeRequest", function (event) {
      if (!isDialogRequest(event)) {
        return;
      }
      errorRegion.hidden = true;
      requests.add(event.detail.xhr);
      setBusy();
    });
    document.addEventListener("htmx:beforeOnLoad", function (event) {
      if (!isDialogRequest(event)) {
        return;
      }
      if (event.detail.xhr.getResponseHeader("HX-Redirect")) {
        nativeLeaving = true;
        dirty = false;
      } else if (event.detail.xhr.getResponseHeader("HX-Location")) {
        dirty = false;
      }
    });
    document.addEventListener("htmx:beforeSwap", function (event) {
      if (!isDialogRequest(event)) {
        return;
      }
      var detail = event.detail;
      var status = detail.xhr.status;
      if (status === 204) {
        return;
      }
      var response = new DOMParser().parseFromString(detail.serverResponse, "text/html");
      var page = response.querySelector("[data-vr-form-page]");
      if (page && (status < 400 || status === 409 || status === 422)) {
        settling = true;
        needsFocus = true;
        detail.shouldSwap = true;
        detail.isError = false;
      } else {
        detail.shouldSwap = false;
        showError(status === 403 ? "You do not have permission for this action. Your input was kept." :
          "The form could not be loaded or saved. Your input was kept; try again.", Boolean(retryUrl));
      }
    });
    document.addEventListener("htmx:afterSwap", function (event) {
      if (isDialogRequest(event)) {
        settling = true;
        needsFocus = true;
        enhancePage();
        setBusy(); // Swapped controls must inherit any request still in flight.
      }
    });
    document.addEventListener("htmx:afterSettle", function (event) {
      if (isDialogRequest(event)) {
        settling = false;
        finishReady();
      }
    });
    document.addEventListener("htmx:afterRequest", function (event) {
      if (!requests.delete(event.detail.xhr)) {
        return;
      }
      finishReady();
    });
    ["htmx:sendError", "htmx:timeout", "htmx:responseError"].forEach(function (name) {
      document.addEventListener(name, function (event) {
        if (isDialogRequest(event)) {
          showError(name === "htmx:responseError" && event.detail.xhr.status === 403 ?
            "You do not have permission for this action. Your input was kept." :
            "The request could not be completed. Your input was kept; check the connection and try again.", Boolean(retryUrl));
        }
      });
    });

    var dedicated = document.querySelector("main[data-vr-form-page]");
    if (dedicated) {
      var cancel = Array.from(dedicated.querySelectorAll("a[href]")).find(function (link) { return link.textContent.trim() === "Cancel"; });
      var crumbs = Array.from(dedicated.querySelectorAll(".breadcrumb a[href]"));
      directDestination = cancel ? cancel.href : (crumbs.length ? crumbs[crumbs.length - 1].href : "/");
      begin();
      // The dedicated dialog is the page's main content; the empty shell stays hidden.
      var page = document.createElement("section");
      Array.from(dedicated.attributes).forEach(function (attribute) {
        if (attribute.name !== "id" && attribute.name !== "class" && attribute.name !== "tabindex") {
          page.setAttribute(attribute.name, attribute.value);
        }
      });
      while (dedicated.firstChild) {
        page.append(dedicated.firstChild);
      }
      dedicated.hidden = true;
      page.setAttribute("role", "main");
      var pageTitle = document.createElement("h1");
      Array.from(title.attributes).forEach(function (attribute) {
        pageTitle.setAttribute(attribute.name, attribute.value);
      });
      title.replaceWith(pageTitle);
      title = pageTitle;
      content.replaceChildren(page);
      enhancePage();
      needsFocus = true;
      finishReady();
    }
    var errorForm = null;
    document.querySelectorAll("form[data-vr-local-form]").forEach(function (form) {
      var open = localButton(form);
      errorForm = errorForm || open;
    });
    if (errorForm && !dedicated) {
      errorForm();
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
