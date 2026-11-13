// Form controls (docs/ui-standards.md section 8): no control shows the browser's default UI.
// One initialiser per control; pages get them through Django widgets, never per-page code.
// The native element stays in the form as the no-JS fallback and as the submitted value.
//
//   select / select[multiple]   Tom Select (searchable above LONG_LIST options; remote with data-vr-lookup)
//   input[data-vr-suggest]      Tom Select free text with suggestions (core.widgets.SuggestInput)
//   input[data-vr-date]         typed date or date-time field with an inline calendar popover
//   [data-vr-file]              file drop zone with the chosen file's name
//   input[type=search]          one clear button
//   [title]                     Bootstrap tooltip (focus and hover) instead of the native tooltip
//   form                        novalidate: errors are server-rendered inline feedback
(function () {
  "use strict";

  if (window.vrControls) {
    window.vrControls.init(document);
    return; // Fragment extra_js may load this file again.
  }

  var LONG_LIST = 8; // more options than this: the menu gets a search box
  var GAP = 4; // px between a control and its floating menu
  var MARGIN = 8; // px a floating menu keeps from the viewport edge
  var floating = new Set(); // open menus and popovers, repositioned on scroll and resize

  // Bootstrap Icons only (static/img/icons.svg): never a library's own glyphs.
  function icon(name) {
    return window.vrUi ? window.vrUi.icon(name) : "";
  }

  function fragment(html) {
    return document.createRange().createContextualFragment(html);
  }

  function settling(element) {
    // HTMX must restore the server's classes before a control adds its own; the form dialog
    // initialises its content itself once it is ready (form-dialog.js finishReady).
    return element.closest(".htmx-added, .htmx-swapping, .htmx-settling, [aria-busy=true]");
  }

  function each(scope, selector, callback) {
    if (scope.matches && scope.matches(selector)) {
      callback(scope);
    }
    scope.querySelectorAll(selector).forEach(callback);
  }

  // ---------------------------------------------------------------- floating menus
  // Menus stay inside their control (focus trap, inert background and removal all just work) but
  // are position: fixed, so a modal body or scrolling table cannot clip them.

  function place(panel, anchor, matchWidth) {
    var rect = anchor.getBoundingClientRect();
    if (matchWidth) {
      panel.style.width = rect.width + "px";
    }
    var height = panel.offsetHeight;
    var width = panel.offsetWidth;
    var below = window.innerHeight - rect.bottom - GAP - MARGIN;
    var above = rect.top - GAP - MARGIN;
    var top = height > below && above > below ? rect.top - GAP - height : rect.bottom + GAP;
    var left = Math.min(rect.left, window.innerWidth - width - MARGIN);
    panel.style.top = Math.max(MARGIN, top) + "px";
    panel.style.left = Math.max(MARGIN, left) + "px";
  }

  function reposition() {
    floating.forEach(function (entry) {
      if (!entry.anchor.isConnected) {
        floating.delete(entry);
      } else {
        place(entry.panel, entry.anchor, entry.matchWidth);
      }
    });
  }

  document.addEventListener("scroll", reposition, true);
  window.addEventListener("resize", reposition);
  // A menu that grows or shrinks while open (filtering, loading more) is placed again, so it
  // can flip above its control when it no longer fits below.
  var resized = window.ResizeObserver ? new ResizeObserver(reposition) : null;

  // ---------------------------------------------------------------- Tom Select

  function copyState(native, picker) {
    var node = picker.focus_node;
    // A script may already have focused the native element (the first invalid field after a
    // failed submission): the control takes the focus over.
    if (document.activeElement === native) {
      node.focus({ preventScroll: true });
    }
    native.setAttribute("aria-hidden", "true"); // The labelled combobox is the accessible control.
    ["aria-describedby", "aria-invalid"].forEach(function (name) {
      if (native.hasAttribute(name)) {
        node.setAttribute(name, native.getAttribute(name));
      }
    });
    if (native.required) {
      node.setAttribute("aria-required", "true");
    }
    if (native.classList.contains("is-invalid")) {
      node.setAttribute("aria-invalid", "true"); // Styled by app.css from the attribute.
    }
    if (!node.hasAttribute("aria-labelledby") && native.getAttribute("aria-label")) {
      node.setAttribute("aria-label", native.getAttribute("aria-label"));
    }
  }

  // A script that sets the native element's value and fires "change" (a form reset, a page
  // script, a browser test) is mirrored in the control.
  function followNative(native, picker) {
    native.addEventListener("change", function () {
      var wanted = native.multiple ? Array.prototype.filter.call(native.options, function (option) {
        return option.selected;
      }).map(function (option) { return option.value; }) : native.value;
      if (String(wanted) !== String(picker.getValue())) {
        picker.setValue(wanted, true);
      }
    });
  }

  // A click opens the menu, as on a native select (openOnFocus is off so Tab does not open it;
  // Tom Select then only focuses on click). A click on an open control still closes it.
  function openOnClick(picker) {
    var wasOpen = false;
    picker.control.addEventListener("click", function () { wasOpen = picker.isOpen; }, true);
    picker.control.addEventListener("click", function () {
      if (!wasOpen && !picker.isOpen && !picker.isDisabled && !picker.isLocked) {
        picker.open();
      }
    });
  }

  function floatDropdown(picker) {
    var entry = { panel: picker.dropdown, anchor: picker.control, matchWidth: true };
    picker.dropdown.classList.add("vr-floating");
    picker.positionDropdown = function () {
      if (picker.isOpen) {
        place(picker.dropdown, picker.control, true);
      }
    };
    picker.on("dropdown_open", function () {
      floating.add(entry);
      place(picker.dropdown, picker.control, true);
      if (resized) {
        resized.observe(picker.dropdown);
      }
    });
    picker.on("dropdown_close", function () {
      floating.delete(entry);
      if (resized) {
        resized.unobserve(picker.dropdown);
      }
    });
  }

  // A short list has no search box, so give it what a native select has: Space or Enter opens
  // the menu, and typing jumps to the first option that starts with the typed letters.
  function typeAhead(picker) {
    var typed = "";
    var timer = null;
    picker.on("blur", function () { typed = ""; }); // a new visit starts a new search
    picker.control.addEventListener("keydown", function (event) {
      if (event.ctrlKey || event.metaKey || event.altKey || picker.isDisabled || picker.isLocked) {
        return;
      }
      if (event.key === " " && !typed || event.key === "Enter" && !picker.isOpen) {
        event.preventDefault();
        event.stopImmediatePropagation(); // Tom Select would act on the same key again.
        if (!picker.isOpen) {
          picker.open();
        } else if (picker.activeOption) {
          picker.onOptionSelect(event, picker.activeOption);
        }
        return;
      }
      if (event.key.length !== 1) {
        return; // A space while typing ("net 6") is part of the search.
      }
      window.clearTimeout(timer);
      timer = window.setTimeout(function () { typed = ""; }, 700);
      typed += event.key.toLowerCase();
      var options = Object.keys(picker.options).map(function (key) { return picker.options[key]; })
        .sort(function (a, b) { return a.$order - b.$order; });
      var current = picker.isOpen && picker.activeOption ? picker.activeOption.dataset.value : picker.getValue();
      var start = options.findIndex(function (option) { return String(option[picker.settings.valueField]) === current; });
      // Repeating one letter steps through the options that start with it, as a native select does.
      var repeat = typed.length > 1 && typed.split("").every(function (letter) { return letter === typed[0]; });
      var prefix = repeat ? typed[0] : typed;
      var ordered = repeat || typed.length === 1 ? options.slice(start + 1).concat(options.slice(0, start + 1)) : options;
      var match = ordered.find(function (option) {
        return String(option[picker.settings.labelField]).trim().toLowerCase().indexOf(prefix) === 0;
      });
      if (!match) {
        return;
      }
      event.preventDefault();
      event.stopImmediatePropagation();
      var value = match[picker.settings.valueField];
      if (picker.isOpen) {
        var element = picker.getOption(value);
        if (element) {
          picker.setActiveOption(element);
        }
      } else {
        picker.setValue(value);
      }
    }, true); // Capture: runs before Tom Select's own handler on the same element.
  }

  function remoteSettings(select) {
    var base = select.getAttribute("data-vr-lookup");
    function url(query, page) {
      return base + (base.indexOf("?") === -1 ? "?" : "&") + "q=" + encodeURIComponent(query) + "&page=" + page;
    }
    return {
      valueField: "value",
      labelField: "text",
      searchField: [],
      score: function () { return function () { return 1; }; }, // The server has filtered.
      loadThrottle: 250,
      shouldLoad: function () { return true; }, // An empty query lists the first page.
      firstUrl: function (query) { return url(query, 1); },
      load: function (query, callback) {
        var picker = this;
        var address = picker.getUrl(query);
        fetch(address, { credentials: "same-origin", headers: { Accept: "application/json" } })
          .then(function (response) { return response.ok ? response.json() : Promise.reject(response.status); })
          .then(function (data) {
            if (data.more) {
              var page = Number(new URL(address, window.location.href).searchParams.get("page")) || 1;
              picker.setNextUrl(query, url(query, page + 1));
            }
            callback(data.results);
          })
          .catch(function () {
            callback();
          });
      },
    };
  }

  // Django's blank choice is "---------". Name it from the field's label instead: "Any project"
  // in a filter (a GET form), "Choose a project" in a form. A form's own empty_label is kept.
  function labelText(select) {
    var label = select.labels && select.labels[0];
    var text = label ? label.cloneNode(true) : null;
    if (text) {
      text.querySelectorAll(".vr-req, .visually-hidden").forEach(function (node) { node.remove(); });
      text = text.textContent;
    } else {
      text = select.getAttribute("aria-label") || "";
    }
    text = text.replace(/\*/g, "").replace(/\s+/g, " ").trim();
    // Lower-case the first letter unless the word is an abbreviation ("QC type", "EDD format").
    return /^[A-Z][A-Z0-9]/.test(text) ? text : text.charAt(0).toLowerCase() + text.slice(1);
  }

  // "a" or "an" by sound: "an analyte", "a unit", "an hour", "an MDL study", "a QC type".
  function article(word) {
    if (/^(equipment|staff|evidence)\b/i.test(word)) {
      return ""; // uncountable: "Choose equipment"
    }
    if (/^[A-Z][A-Z0-9]/.test(word)) {
      return /^[AEFHILMNORSX]/.test(word) ? "an" : "a"; // abbreviations are spelled out
    }
    if (/^(uni|use|usu|eu|one|once)/i.test(word)) {
      return "a";
    }
    return /^([aeiou]|hour|honest)/i.test(word) ? "an" : "a";
  }

  function nameEmptyChoice(select) {
    var empty = Array.prototype.find.call(select.options, function (option) { return option.value === ""; });
    var thing = labelText(select);
    if (!empty || !/^-+$/.test(empty.text.trim()) || !thing) {
      return;
    }
    var form = select.form;
    var filter = form && (form.getAttribute("method") || "get").toLowerCase() === "get";
    empty.text = filter ? "Any " + thing : ["Choose", article(thing), thing].filter(Boolean).join(" ");
  }

  // A select that may be empty (an "Any …" filter, an optional field, a multi-select) gets a clear
  // button: a real button, shown by app.css while the control is hovered, focused or open (always
  // on touch screens), and only while there is a value. Backspace or Delete on a closed single
  // select does the same. Clearing fires the usual change event; nothing is submitted by it.
  function addClear(select, picker, multiple, label) {
    var empty = Array.prototype.some.call(select.options, function (option) { return option.value === ""; });
    if (select.required || !multiple && !empty) {
      return;
    }
    var button = document.createElement("button");
    button.type = "button";
    button.className = "vr-ts-clear";
    button.setAttribute("aria-label", "Clear " + (label || "selection"));
    button.append(fragment(icon("x")));
    picker.wrapper.classList.add("vr-clearable");
    picker.wrapper.append(button);

    function hasValue() {
      var value = picker.getValue();
      return multiple ? value.length > 0 : value !== "";
    }
    function sync() {
      button.hidden = !hasValue() || picker.isDisabled;
    }
    function clear() {
      picker.close();
      picker.setValue(multiple ? [] : ""); // not silent: the native select fires "change"
      picker.focus_node.focus();
    }
    button.addEventListener("mousedown", function (event) {
      event.preventDefault(); // keep Tom Select from treating it as a click outside
    });
    button.addEventListener("click", clear);
    picker.on("change", sync);
    if (!multiple) {
      picker.focus_node.addEventListener("keydown", function (event) {
        if ((event.key === "Backspace" || event.key === "Delete") && !picker.isOpen && hasValue()) {
          event.preventDefault();
          event.stopImmediatePropagation();
          clear();
        }
      }, true);
    }
    sync();
  }

  function initSelect(select) {
    if (select.tomselect || !window.TomSelect || select.hasAttribute("data-vr-native") ||
        select.hasAttribute("data-requested-tests") || settling(select)) {
      return;
    }
    nameEmptyChoice(select);
    var label = labelText(select); // before Tom Select points the <label> at its own control
    var multiple = select.multiple;
    var remote = select.hasAttribute("data-vr-lookup");
    var searchable = multiple || remote || select.options.length > LONG_LIST;
    var empty = Array.prototype.find.call(select.options, function (option) { return option.value === ""; });
    var plugins = {};
    if (multiple) {
      plugins.remove_button = {
        title: "",
        // Pointer shortcut only; keyboard removal is Backspace or Delete.
        html: function (data) {
          return '<span class="' + data.className + '" aria-hidden="true">' + icon("x") + "</span>";
        },
      };
    } else if (searchable) {
      plugins.dropdown_input = {};
    }
    if (remote) {
      plugins.virtual_scroll = {};
    }
    var settings = {
      plugins: plugins,
      maxOptions: null,
      openOnFocus: false, // Tabbing through a form must not pop every menu open.
      allowEmptyOption: !multiple, // "All", "Not recorded" and the like stay choosable, as natively.
      hideSelected: multiple,
      closeAfterSelect: !multiple,
      refreshThrottle: 0,
      placeholder: multiple ? "Type to search" : empty ? empty.text : "",
      render: {
        // The chosen option carries a tick (shown by CSS on .selected), as well as its weight.
        option: function (data, escape) {
          return '<div><span class="vr-ts-text">' + escape(data[this.settings.labelField]) + "</span>" +
            '<span class="vr-ts-check">' + icon("check2") + "</span></div>";
        },
        no_results: function (data, escape) {
          return '<div class="no-results">No matches for “' + escape(data.input) + "”.</div>";
        },
        loading_more: function () {
          return '<div class="loading-more-results">Loading more…</div>';
        },
        no_more_results: function () {
          return "";
        },
      },
    };
    if (!searchable) {
      settings.controlInput = null;
    }
    if (remote) {
      Object.assign(settings, remoteSettings(select));
    }
    var picker = new window.TomSelect(select, settings);
    if (remote) {
      // The first page loads when the menu opens, not on focus: a load that finishes while the
      // control has focus would open the menu by itself (a dialog's first field is focused).
      picker.on("dropdown_open", function () {
        if (!picker.vrLoaded) {
          picker.vrLoaded = true;
          picker.load("");
        }
      });
    }
    copyState(select, picker);
    followNative(select, picker);
    openOnClick(picker);
    floatDropdown(picker);
    if (!multiple) {
      var caret = document.createElement("span");
      caret.className = "vr-ts-caret";
      caret.setAttribute("aria-hidden", "true");
      caret.append(fragment(icon("chevron-down")));
      picker.wrapper.append(caret);
    }
    addClear(select, picker, multiple, label);
    if (!searchable) {
      typeAhead(picker);
    } else if (!multiple) {
      picker.control_input.setAttribute("placeholder", "Type to filter");
      picker.control_input.setAttribute("aria-label", "Filter options");
      picker.control_input.classList.add("form-control", "form-control-sm");
      picker.control_input.before(fragment(icon("search")));
    }
  }

  // Free text with suggestions: SuggestInput renders <input data-vr-suggest list=…><datalist>.
  // The typed text is the value; a suggestion only fills it in.
  function initSuggest(input) {
    if (input.tomselect || !window.TomSelect || settling(input)) {
      return;
    }
    var list = input.list || document.getElementById(input.getAttribute("list"));
    var options = list ? Array.prototype.map.call(list.options, function (option) {
      var label = option.textContent.trim();
      return { value: option.value, text: option.value, note: label && label !== option.value ? label : "" };
    }) : [];
    // Passing options replaces the ones Tom Select reads from the input, so keep the current value.
    var current = input.value.trim();
    if (current && !options.some(function (option) { return option.value === current; })) {
      options.unshift({ value: current, text: current, note: "" });
    }
    input.removeAttribute("list"); // No native suggestion popup on the hidden input.
    var picker = new window.TomSelect(input, {
      options: options,
      create: true,
      createOnBlur: true,
      persist: false,
      maxItems: 1,
      maxOptions: null,
      openOnFocus: false,
      delimiter: "\u001f", // Unit separator: free text cannot contain it, so it is never split.
      plugins: ["restore_on_backspace"],
      refreshThrottle: 0,
      render: {
        option: function (data, escape) {
          return "<div>" + escape(data.text) + (data.note ? ' <span class="vr-ts-meta">· ' + escape(data.note) + "</span>" : "") + "</div>";
        },
        option_create: function (data, escape) {
          return '<div class="create">Use “' + escape(data.input) + "”</div>";
        },
        no_results: null,
      },
    });
    copyState(input, picker);
    openOnClick(picker);
    floatDropdown(picker);
  }

  // ---------------------------------------------------------------- dates
  // The native input (type=date or datetime-local, rendered by the DateInput/DateTimeInput widget)
  // becomes the hidden submitted value, in the exact format the Django field already parses:
  // YYYY-MM-DD, or YYYY-MM-DDTHH:MM[:SS]. The visible field is typed text in a fixed format, so no
  // time-zone conversion ever happens here: times are the laboratory's local wall-clock time.

  var DATE_FORMAT = "YYYY-MM-DD";
  var ISO_DATE = /^(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})$/;
  var COMPACT_DATE = /^(\d{4})(\d{2})(\d{2})$/;
  var US_DATE = /^(\d{1,2})\/(\d{1,2})\/(\d{4})$/;
  var TIME = /^(\d{1,2}):?(\d{2})(?::?(\d{2}))?$/;
  var NATIVE_DATETIME = /^(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2}(?::\d{2})?)/;

  function pad(number) {
    return String(number).padStart(2, "0");
  }

  function clockParts(zone) {
    var options = { year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23" };
    try {
      return new Intl.DateTimeFormat("en-US", Object.assign({ timeZone: zone || undefined }, options)).formatToParts(new Date());
    } catch (error) {
      return new Intl.DateTimeFormat("en-US", options).formatToParts(new Date());
    }
  }

  // The laboratory's wall clock now (the time zone Django parses naive input in).
  function labClock(zone) {
    var parts = {};
    clockParts(zone).forEach(function (part) { parts[part.type] = part.value; });
    return { date: parts.year + "-" + parts.month + "-" + parts.day, time: parts.hour + ":" + parts.minute, second: parts.second };
  }

  function zoneLabel(zone) {
    try {
      var now = new Date();
      var short = new Intl.DateTimeFormat("en-US", { timeZone: zone, timeZoneName: "short" }).formatToParts(now)
        .find(function (part) { return part.type === "timeZoneName"; }).value;
      var offset = new Intl.DateTimeFormat("en-US", { timeZone: zone, timeZoneName: "longOffset" }).formatToParts(now)
        .find(function (part) { return part.type === "timeZoneName"; }).value.replace("GMT", "UTC").replace("-", "−");
      if (offset === "UTC") {
        offset = "UTC±00:00";
      }
      return "Laboratory time, " + short + (offset.indexOf(short) === -1 ? " (" + offset + ")" : "");
    } catch (error) {
      return "Laboratory time";
    }
  }

  function shiftDate(iso, days) {
    var parts = iso.split("-").map(Number);
    var date = new Date(Date.UTC(parts[0], parts[1] - 1, parts[2] + days));
    return date.getUTCFullYear() + "-" + pad(date.getUTCMonth() + 1) + "-" + pad(date.getUTCDate());
  }

  // YYYY-MM-DD (also with / or . and without leading zeros), YYYYMMDD, or US MM/DD/YYYY.
  function parseDate(text) {
    var match = text.match(ISO_DATE) || text.match(COMPACT_DATE);
    var y;
    var m;
    var d;
    if (match) {
      y = +match[1]; m = +match[2]; d = +match[3];
    } else if ((match = text.match(US_DATE))) {
      y = +match[3]; m = +match[1]; d = +match[2];
    } else {
      return null;
    }
    var date = new Date(Date.UTC(y, m - 1, d));
    if (y < 1000 || date.getUTCFullYear() !== y || date.getUTCMonth() !== m - 1 || date.getUTCDate() !== d) {
      return null;
    }
    return y + "-" + pad(m) + "-" + pad(d);
  }

  // HH:MM (or HHMM, H:MM); HH:MM:SS only where the field records seconds.
  function parseTime(text, seconds) {
    var match = text.match(TIME);
    if (!match || +match[1] > 23 || +match[2] > 59 || match[3] && (!seconds || +match[3] > 59)) {
      return null;
    }
    return pad(+match[1]) + ":" + match[2] + (seconds ? ":" + (match[3] || "00") : "");
  }

  // The native input itself becomes the visible typed field: it keeps its id, label, help and
  // error links, and anything already holding it (a script, a browser test) still has the field.
  // Its name moves to a new hidden input that submits the value in the field's own format.
  function DateField(native) {
    var self = this;
    var datetime = native.getAttribute("data-vr-date") === "datetime";
    var seconds = datetime && (native.getAttribute("step") === "1" || /T\d{2}:\d{2}:\d{2}$/.test(native.value));
    var format = datetime ? DATE_FORMAT + " " + (seconds ? "HH:MM:SS" : "HH:MM") : DATE_FORMAT;
    var id = native.id || native.name;
    this.datetime = datetime;
    this.seconds = seconds;
    this.zone = native.getAttribute("data-vr-time-zone") || "";
    this.min = native.getAttribute("min") || "";
    this.max = native.getAttribute("max") || "";
    this.format = format;

    var value = native.value;
    var hidden = document.createElement("input");
    hidden.type = "hidden";
    hidden.name = native.name;
    hidden.value = value;
    native.removeAttribute("name");
    ["min", "max", "step"].forEach(function (name) { native.removeAttribute(name); });
    native.type = "text";
    native.classList.add("vr-date-text");
    native.autocomplete = "off";
    native.spellcheck = false;
    native.placeholder = format;
    native.setAttribute("inputmode", "numeric");
    native.setAttribute("aria-haspopup", "dialog");
    native.setAttribute("aria-expanded", "false");
    if (native.required) {
      native.setAttribute("aria-required", "true");
    }

    var wrapper = document.createElement("div");
    wrapper.className = "vr-date-input";
    var button = document.createElement("button");
    button.type = "button";
    button.className = "vr-date-icon";
    button.tabIndex = -1; // Keyboard: Alt+Down, or Enter in an empty field, opens the calendar.
    button.setAttribute("aria-hidden", "true");
    button.append(fragment(icon("calendar3")));
    button.disabled = native.disabled || native.readOnly;

    // Format and keys for assistive technology (the placeholder shows the format); a date-time
    // field also states, visibly, that it is the laboratory's local time.
    var hint = document.createElement("div");
    hint.className = datetime ? "form-text vr-date-hint" : "visually-hidden";
    hint.id = id + "_format";
    var keys = document.createElement("span");
    keys.className = "visually-hidden";
    keys.textContent = "Format " + format + (datetime ? ", 24-hour. " : ". ") + "Alt+Down opens the calendar. ";
    hint.append(keys, datetime ? zoneLabel(this.zone) : "");
    var error = document.createElement("div");
    error.className = "invalid-feedback vr-date-error";
    error.id = id + "_format_error";
    native.setAttribute("aria-describedby", [native.getAttribute("aria-describedby"), hint.id].filter(Boolean).join(" "));

    native.parentNode.insertBefore(wrapper, native);
    wrapper.append(native, button, hidden);
    wrapper.after(hint, error);

    native.value = this.display(value);
    this.input = native;
    this.hidden = hidden;
    this.wrapper = wrapper;
    this.error = error;
    this.popover = null;
    this.calendar = null;
    this.time = null;

    native.addEventListener("change", function () { self.commit(); });
    native.addEventListener("keydown", function (event) {
      if (event.key === "ArrowDown" && event.altKey || event.key === "Enter" && !native.value.trim()) {
        event.preventDefault();
        self.open(true);
      } else if (event.key === "Enter") {
        self.commit(); // Commit before an implicit submission reads the hidden value.
      } else if (event.key === "Escape" && self.isOpen()) {
        event.preventDefault();
        event.stopPropagation(); // Close the calendar, not the dialog around it.
        self.close(true);
      }
    });
    button.addEventListener("click", function () {
      if (self.isOpen()) {
        self.close(true);
      } else {
        self.open(false);
      }
    });
    native.vrDateField = this;
  }

  DateField.prototype.isOpen = function () {
    return !!this.popover && !this.popover.hidden;
  };

  DateField.prototype.display = function (value) {
    if (!value || !this.datetime) {
      return value || "";
    }
    var match = value.match(NATIVE_DATETIME);
    return match ? match[1] + " " + match[2] : value; // An unparsed server echo shows as typed.
  };

  DateField.prototype.parse = function (text) {
    text = text.trim().replace(/\s+/g, " ");
    if (!this.datetime) {
      return parseDate(text);
    }
    var parts = text.split(/[ T]/);
    if (parts.length !== 2) {
      return null;
    }
    var date = parseDate(parts[0]);
    var time = parseTime(parts[1], this.seconds);
    return date && time ? date + "T" + time : null;
  };

  DateField.prototype.setNative = function (value) {
    if (this.hidden.value !== value) {
      this.hidden.value = value;
      this.hidden.dispatchEvent(new Event("change", { bubbles: true })); // Form dirty tracking.
    }
  };

  DateField.prototype.setError = function (message) {
    var errorId = this.error.id;
    var ids = (this.input.getAttribute("aria-describedby") || "").split(" ").filter(function (part) {
      return part && part !== errorId;
    });
    this.error.textContent = message;
    this.input.classList.toggle("is-invalid", !!message);
    if (message) {
      ids.push(errorId);
      this.input.setAttribute("aria-invalid", "true");
      this.checked = true;
    } else if (this.checked) {
      this.input.removeAttribute("aria-invalid"); // Clears only what this script flagged.
    }
    this.input.setAttribute("aria-describedby", ids.join(" "));
  };

  // Parse what was typed and write the server format to the hidden input. Text that does not
  // parse is still submitted as typed, so the server rejects it with its own message.
  DateField.prototype.commit = function () {
    var text = this.input.value.trim();
    if (!text) {
      this.setNative("");
      this.setError("");
      return true;
    }
    var value = this.parse(text);
    if (!value) {
      this.setNative(text);
      this.setError("Enter " + (this.datetime ? "a date and time" : "a date") + " as " + this.format + ".");
      return false;
    }
    this.setNative(value);
    this.input.value = this.display(value);
    if (this.min && value < this.min) {
      this.setError("Enter " + this.display(this.min) + " or later.");
      return false;
    }
    if (this.max && value > this.max) {
      this.setError("Enter " + this.display(this.max) + " or earlier.");
      return false;
    }
    this.setError("");
    return true;
  };

  DateField.prototype.set = function (date, time) {
    this.input.value = date ? date + (this.datetime ? " " + (time || "") : "") : "";
    this.input.value = this.input.value.trim();
    this.commit();
  };

  DateField.prototype.parts = function () {
    var value = this.parse(this.input.value) || "";
    return { date: value.slice(0, 10), time: value.slice(11) };
  };

  DateField.prototype.buildPopover = function () {
    var self = this;
    var popover = document.createElement("div");
    popover.className = "vr-popover vr-datepicker vr-floating" + (this.datetime ? " vr-datetime" : "");
    popover.setAttribute("role", "dialog");
    popover.setAttribute("aria-label", this.datetime ? "Choose a date and time" : "Choose a date");
    popover.hidden = true;
    var host = document.createElement("div");
    host.className = "vr-cal-host";
    popover.append(host);
    if (this.datetime) {
      var row = document.createElement("div");
      row.className = "vr-time-row";
      var label = document.createElement("label");
      var time = document.createElement("input");
      time.type = "text";
      time.className = "form-control form-control-sm vr-time";
      time.id = this.input.id + "_time";
      time.placeholder = this.seconds ? "HH:MM:SS" : "HH:MM";
      time.autocomplete = "off";
      time.setAttribute("inputmode", "numeric");
      label.htmlFor = time.id;
      label.textContent = "Time";
      var zone = document.createElement("span");
      zone.className = "vr-tz";
      zone.textContent = zoneLabel(this.zone);
      row.append(label, time, zone);
      popover.append(row);
      time.addEventListener("keydown", function (event) {
        if (event.key === "Enter") {
          event.preventDefault();
          self.applyTime(true);
        }
      });
      time.addEventListener("change", function () { self.applyTime(false); });
      this.time = time;
    }
    var shortcuts = document.createElement("div");
    shortcuts.className = "vr-cal-shortcuts";
    function shortcut(label, role, action) {
      var button = window.vrUi.button(label, role, true);
      button.addEventListener("click", action);
      shortcuts.append(button);
      return button;
    }
    if (this.datetime) {
      shortcut("Now", "secondary", function () {
        var now = labClock(self.zone);
        self.set(now.date, now.time + (self.seconds ? ":" + now.second : ""));
        self.close(true);
      });
      shortcut("Today", "secondary", function () {
        self.pickDate(labClock(self.zone).date, self.parts().time);
      });
    } else {
      shortcut("Today", "secondary", function () {
        self.set(labClock(self.zone).date);
        self.close(true);
      });
      shortcut("Yesterday", "secondary", function () {
        self.set(shiftDate(labClock(self.zone).date, -1));
        self.close(true);
      });
    }
    shortcut("Clear", "quiet", function () {
      self.set("");
      self.close(true);
    }).classList.add("ms-auto");
    if (this.datetime) {
      shortcut("Done", "primary", function () { self.applyTime(true); });
    }
    popover.append(shortcuts);
    popover.addEventListener("keydown", function (event) {
      if (event.key === "Escape") {
        event.preventDefault();
        event.stopPropagation(); // Close the calendar, not the dialog around it.
        self.close(true);
      }
    });
    this.wrapper.addEventListener("focusout", function (event) {
      if (self.isOpen() && event.relatedTarget && !self.wrapper.contains(event.relatedTarget)) {
        self.close(false);
      }
    });
    this.wrapper.append(popover);
    this.popover = popover;
    this.host = host;
    this.entry = { panel: popover, anchor: this.input, matchWidth: false };
  };

  // The time box (date-time fields): apply it to the chosen date, and close when asked.
  DateField.prototype.applyTime = function (close) {
    var date = this.parts().date || this.pending || "";
    var text = this.time.value.trim();
    var time = parseTime(text, this.seconds);
    if (text && !time) {
      this.time.setAttribute("aria-invalid", "true");
      this.time.focus();
      return;
    }
    this.time.removeAttribute("aria-invalid");
    if (date) {
      this.set(date, time || "");
    }
    if (close) {
      this.close(true);
    }
  };

  DateField.prototype.pickDate = function (date, time) {
    if (!this.datetime) {
      this.set(date);
      this.close(true);
      return;
    }
    this.pending = date;
    if (time) {
      this.set(date, time);
    } else {
      this.input.value = date + " "; // The time comes next; it is checked when the calendar closes.
      this.setError("");
    }
    if (this.calendar) {
      this.calendar.set({ selectedDates: [date], selectedMonth: +date.slice(5, 7) - 1, selectedYear: +date.slice(0, 4) });
    }
    this.time.focus();
    this.time.select();
  };

  // The calendar is Vanilla Calendar Pro in inline mode, created on open and destroyed on close:
  // its own popup mode appends to <body>, which breaks a Bootstrap modal's focus trap.
  DateField.prototype.open = function (focusCalendar) {
    var self = this;
    if (!window.VanillaCalendarPro || this.input.disabled || this.input.readOnly || this.isOpen()) {
      return;
    }
    if (!this.popover) {
      this.buildPopover();
    }
    var parts = this.parts();
    var today = labClock(this.zone).date;
    var shown = parts.date || today;
    this.pending = parts.date;
    this.calendar = new window.VanillaCalendarPro.Calendar(this.host, {
      firstWeekday: 0, // United States: weeks start on Sunday.
      locale: "en-US",
      dateToday: today,
      dateMin: this.min ? this.min.slice(0, 10) : "1900-01-01",
      dateMax: this.max ? this.max.slice(0, 10) : "2099-12-31",
      selectedDates: parts.date ? [parts.date] : [],
      selectedMonth: +shown.slice(5, 7) - 1,
      selectedYear: +shown.slice(0, 4),
      enableDateToggle: false,
      themeAttrDetect: "html[data-bs-theme]",
      onInit: arrowIcons,
      onUpdate: arrowIcons,
      onClickDate: function (calendar) {
        var date = calendar.context.selectedDates[0];
        if (date) {
          var typed = self.time ? parseTime(self.time.value.trim(), self.seconds) : "";
          self.pickDate(date, typed || self.parts().time);
        }
      },
    });
    this.calendar.init();
    if (this.time) {
      this.time.value = parts.time;
      this.time.removeAttribute("aria-invalid");
    }
    this.popover.hidden = false;
    this.input.setAttribute("aria-expanded", "true");
    floating.add(this.entry);
    place(this.popover, this.input, false);
    if (focusCalendar) {
      var target = this.host.querySelector("[data-vc-date-selected] [data-vc-date-btn]") ||
        this.host.querySelector("[data-vc-date-today] [data-vc-date-btn]") ||
        this.host.querySelector("[data-vc-date-btn]");
      if (target) {
        target.focus();
      }
    }
  };

  DateField.prototype.close = function (returnFocus) {
    if (!this.isOpen()) {
      return;
    }
    this.popover.hidden = true;
    this.input.setAttribute("aria-expanded", "false");
    floating.delete(this.entry);
    this.destroy();
    if (this.input.value.trim() || this.hidden.value) {
      this.commit();
    }
    if (returnFocus) {
      this.input.focus();
    }
  };

  DateField.prototype.destroy = function () {
    if (this.calendar) {
      this.calendar.destroy();
      this.calendar = null;
      this.host.replaceChildren();
    }
  };

  // The calendar's month arrows carry Bootstrap Icons chevrons instead of its own images.
  function arrowIcons(calendar) {
    calendar.context.mainElement.querySelectorAll("[data-vc-arrow]").forEach(function (arrow) {
      if (!arrow.querySelector(".vr-icon")) {
        arrow.append(fragment(icon(arrow.getAttribute("data-vc-arrow") === "prev" ? "chevron-left" : "chevron-right")));
      }
    });
  }

  function initDate(native) {
    if (native.vrDateField || settling(native)) {
      return;
    }
    new DateField(native);
  }

  document.addEventListener("mousedown", function (event) {
    document.querySelectorAll(".vr-datepicker:not([hidden])").forEach(function (popover) {
      var wrapper = popover.closest(".vr-date-input");
      if (!wrapper.contains(event.target)) {
        wrapper.querySelector("input[data-vr-date]").vrDateField.close(false);
      }
    });
  });

  // A date that is unreadable or out of range stops the submission, as the browser's own date
  // input did, with the message beside the field. formnovalidate submitters (pagers) still go.
  document.addEventListener("submit", function (event) {
    if (event.submitter && event.submitter.formNoValidate) {
      return;
    }
    var invalid = Array.prototype.filter.call(event.target.querySelectorAll("input[data-vr-date]"), function (native) {
      return native.vrDateField && !native.vrDateField.commit();
    });
    if (invalid.length) {
      event.preventDefault();
      event.stopImmediatePropagation();
      invalid[0].vrDateField.input.focus();
    }
  }, true);

  // ---------------------------------------------------------------- files

  function sizeText(bytes) {
    if (bytes < 1024) {
      return bytes + " bytes";
    }
    return bytes < 1048576 ? Math.round(bytes / 1024) + " KB" : (bytes / 1048576).toFixed(1) + " MB";
  }

  var FILE_TYPES = { "application/pdf": "PDF", "image/png": "PNG", "image/jpeg": "JPEG", "text/csv": "CSV", "image/*": "images" };

  // "image/png,image/jpeg" -> "PNG or JPEG"; the drop zone's hint and the input's description.
  function acceptText(accept) {
    var names = [];
    accept.split(",").forEach(function (part) {
      part = part.trim().toLowerCase();
      var name = FILE_TYPES[part] || (part.charAt(0) === "." ? part.slice(1).toUpperCase() : part);
      if (name && names.indexOf(name) === -1) {
        names.push(name);
      }
    });
    return names.length > 1 ? names.slice(0, -1).join(", ") + " or " + names[names.length - 1] : names[0] || "";
  }

  function initFile(zone) {
    if (zone.vrFileReady || settling(zone)) {
      return;
    }
    zone.vrFileReady = true;
    var input = zone.querySelector("input[type=file]");
    var name = zone.querySelector("[data-vr-file-name]");
    var drop = zone.querySelector(".vr-drop");
    function show() {
      var files = Array.prototype.slice.call(input.files || []);
      name.textContent = files.length ? files.map(function (file) {
        return file.name + " (" + sizeText(file.size) + ")";
      }).join(", ") : name.getAttribute("data-vr-file-empty");
      zone.classList.toggle("vr-file-chosen", files.length > 0);
    }
    var hint = zone.querySelector(".vr-drop-hint");
    if (hint && input.accept) {
      hint.textContent = acceptText(input.accept) + " files";
      hint.id = input.id + "_accept";
      input.setAttribute("aria-describedby", [input.getAttribute("aria-describedby"), hint.id].filter(Boolean).join(" "));
    }
    input.addEventListener("change", show);
    ["dragenter", "dragover"].forEach(function (type) {
      drop.addEventListener(type, function (event) {
        event.preventDefault();
        drop.classList.add("vr-drop-over");
      });
    });
    ["dragleave", "drop"].forEach(function (type) {
      drop.addEventListener(type, function () { drop.classList.remove("vr-drop-over"); });
    });
    drop.addEventListener("drop", function (event) {
      event.preventDefault();
      if (input.disabled || !event.dataTransfer || !event.dataTransfer.files.length) {
        return;
      }
      var transfer = new DataTransfer();
      Array.prototype.slice.call(event.dataTransfer.files, 0, input.multiple ? undefined : 1).forEach(function (file) {
        transfer.items.add(file);
      });
      input.files = transfer.files;
      input.dispatchEvent(new Event("change", { bubbles: true }));
    });
    zone.classList.add("vr-file-ready");
    show();
  }

  // ---------------------------------------------------------------- search boxes

  function initSearch(input) {
    if (input.vrSearchReady || input.closest(".ts-wrapper")) {
      return;
    }
    input.vrSearchReady = true;
    var button = document.createElement("button");
    button.type = "button";
    button.className = "vr-search-clear";
    button.setAttribute("aria-label", "Clear text");
    button.append(fragment(icon("x")));
    function sync() {
      button.hidden = !input.value || input.disabled;
    }
    button.addEventListener("click", function () {
      input.value = "";
      input.dispatchEvent(new Event("input", { bubbles: true }));
      input.dispatchEvent(new Event("change", { bubbles: true }));
      sync();
      input.focus();
    });
    input.addEventListener("input", sync);
    input.classList.add("vr-search-input");
    input.after(button);
    sync();
  }

  // ---------------------------------------------------------------- tooltips
  // A title becomes a Bootstrap tooltip, shown on hover and keyboard focus. A non-interactive
  // hint (a status, a note) becomes focusable so keyboard users reach it too; a <time>'s title
  // only adds precision to the visible date, so it stays out of the tab order. Kept native:
  // iframe titles (accessible names) and abbr (expansion semantics).

  var FOCUSABLE = "a[href], button, input, select, textarea, summary, [tabindex]";

  function initTooltip(element) {
    // Bootstrap allows one component per element, so a dropdown or collapse toggle keeps its title.
    if (!window.bootstrap || element.matches("iframe, abbr, link, style, [data-bs-toggle]") ||
        element.closest("svg, .ts-wrapper, [data-vr-native-title]") || settling(element)) {
      return;
    }
    if (!element.getAttribute("title").trim()) {
      element.removeAttribute("title");
      return;
    }
    if (!element.matches(FOCUSABLE) && !element.matches("time")) {
      element.tabIndex = 0;
    }
    window.bootstrap.Tooltip.getOrCreateInstance(element, { container: element.closest(".modal") || "body" });
  }

  // A script may set a new title later: move it into the tooltip.
  document.addEventListener("show.bs.tooltip", function (event) {
    var element = event.target;
    var title = element.getAttribute("title");
    var tip = window.bootstrap.Tooltip.getInstance(element);
    if (title && tip) {
      element.setAttribute("data-bs-original-title", title);
      element.removeAttribute("title");
      tip.setContent({ ".tooltip-inner": title });
    }
  });

  // ---------------------------------------------------------------- teardown

  // Before HTMX removes content: release enhancements that hold listeners or open popups.
  document.addEventListener("htmx:beforeCleanupElement", function (event) {
    var element = event.target;
    if (element.tomselect) {
      element.tomselect.destroy();
    }
    if (element.vrDateField) {
      element.vrDateField.close(false);
    }
    var tip = window.bootstrap && window.bootstrap.Tooltip.getInstance(element);
    if (tip) {
      tip.dispose();
    }
  });

  // ---------------------------------------------------------------- init

  function init(scope) {
    scope = scope || document;
    each(scope, "form", function (form) { form.noValidate = true; });
    each(scope, "select", initSelect);
    each(scope, "input[data-vr-suggest]", initSuggest);
    each(scope, "input[data-vr-date]", initDate);
    each(scope, "[data-vr-file]", initFile);
    each(scope, "input[type=search]", initSearch);
    each(scope, "[title]", initTooltip);
  }

  window.vrControls = { init: init };

  ["htmx:load", "htmx:afterSettle"].forEach(function (name) {
    document.addEventListener(name, function (event) {
      init(event.detail.elt || document);
    });
  });
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", function () { init(document); });
  } else {
    init(document);
  }
})();
