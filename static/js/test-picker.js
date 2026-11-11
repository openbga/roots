// Requested-test picker: Tom Select on the native <select multiple data-requested-tests>.
// The native select stays the no-JS fallback and the form value;
// the server stays authoritative for matrix and catalog checks.
//
// Only tests for the sample matrix (the form's "matrix" field) can be ADDED. Changing the matrix
// never removes a selection: tests for another matrix stay selected, are marked, listed and submitted
// until the user removes them or corrects the matrix. Option metadata comes from each option's
// data-test JSON; labels are never parsed.
(function () {
  "use strict";

  if (window.vrInitTestPickers) {
    window.vrInitTestPickers(document);
    return; // Fragment extra_js may load this file again.
  }

  function iconMarkup(name) {
    var sprite = document.body.getAttribute("data-vr-icons") || "";
    return '<svg class="vr-icon" aria-hidden="true" focusable="false"><use href="' + sprite + "#i-" + name + '"></use></svg>';
  }

  function detail(test) {
    return test.method_code + " (" + test.method_version + ") · " + test.unit;
  }

  function fullLabel(test) {
    return test.analyte + " · " + detail(test) + " · " + test.matrix_label;
  }

  function plural(count, word) {
    return count + " " + word + (count === 1 ? "" : "s");
  }

  function init(select) {
    if (select.tomselect || !window.TomSelect || select.closest(".htmx-added, .htmx-swapping, .htmx-settling, [aria-busy=true]")) {
      return; // HTMX must restore the server's classes before the picker adds its own.
    }
    var registry = [];
    for (var i = 0; i < select.options.length; i++) {
      try {
        registry.push(Object.freeze(JSON.parse(select.options[i].getAttribute("data-test"))));
      } catch (error) {
        return; // Missing or unreadable metadata: keep the native control.
      }
    }
    var form = select.form;
    var matrix = form && form.elements.namedItem("matrix");
    var byValue = {};
    registry.forEach(function (test) { byValue[test.value] = test; });

    function matrixValue() {
      return matrix && matrix.value ? matrix.value : "";
    }

    function matrixLabel() {
      return matrix && matrix.value ? matrix.options[matrix.selectedIndex].text : "";
    }

    function mismatched(test) {
      return matrixValue() !== "" && test.matrix !== matrixValue();
    }

    // Visible summary, a polite announcer and the mismatch notice, placed before help and errors.
    var summary = document.createElement("div");
    summary.className = "form-text vr-test-summary";
    summary.id = select.id + "_summary";
    var announcer = document.createElement("div");
    announcer.className = "visually-hidden";
    announcer.setAttribute("role", "status");
    var hint = document.createElement("span");
    hint.className = "visually-hidden";
    hint.id = select.id + "_keys";
    hint.textContent = "Type to search. Enter adds the highlighted test. Backspace removes the last selected test; Left arrow moves to earlier ones.";
    var notice = document.createElement("div");
    notice.className = "vr-test-mismatch";
    notice.hidden = true;

    var picker = new window.TomSelect(select, {
      plugins: {
        remove_button: {
          title: "Remove test",
          // Pointer shortcut only; keyboard removal is Backspace/Delete or the mismatch action below.
          html: function (data) {
            var el = document.createElement("span");
            el.className = data.className;
            el.title = data.title;
            el.setAttribute("aria-hidden", "true");
            el.innerHTML = iconMarkup("x");
            return el;
          },
        },
      },
      dataAttr: "data-test",
      valueField: "value",
      labelField: "text",
      searchField: ["analyte", "method_code", "method_version", "unit"],
      create: false,
      hideSelected: true,
      closeAfterSelect: false,
      clearAfterSelect: true, // Next typing starts a new search; Backspace then removes items.
      maxOptions: null,
      refreshThrottle: 0, // Local registry: filter at once; a 300 ms timer could reopen the list after Escape.
      placeholder: "Search analyte, method or unit",
      render: {
        option: function (test, escape) {
          return '<div><span class="vr-ts-analyte">' + escape(test.analyte) + '</span> <span class="vr-ts-meta">· ' +
            escape(detail(test) + " · " + test.matrix_label) + "</span></div>";
        },
        item: function (test, escape) {
          if (!mismatched(test)) {
            return "<div>" + escape(test.analyte + " · " + detail(test)) + "</div>";
          }
          return '<div class="vr-ts-mismatch">' + iconMarkup("exclamation-triangle") + escape(fullLabel(test)) +
            '<span class="visually-hidden">, not for ' + escape(matrixLabel()) + " samples</span></div>";
        },
        no_results: function (data, escape) {
          var scope = matrixValue() ? matrixLabel() + " tests" : "tests";
          return '<div class="no-results">No ' + escape(scope) + " match “" + escape(data.input) + "”.</div>";
        },
      },
    });

    var control = picker.control_input;
    var bulk = false;
    picker.wrapper.after(summary, notice, announcer, hint);
    var describedBy = [select.getAttribute("aria-describedby"), summary.id, hint.id].filter(Boolean).join(" ");
    control.setAttribute("aria-describedby", describedBy);
    if (select.getAttribute("aria-invalid") === "true") {
      picker.wrapper.classList.add("is-invalid");
      control.setAttribute("aria-invalid", "true");
    }

    function announce(text) {
      announcer.textContent = "";
      window.setTimeout(function () { announcer.textContent = text; }, 30);
    }

    function selectedTests() {
      return picker.items.map(function (value) { return byValue[value]; }).filter(Boolean);
    }

    function addableCount() {
      return registry.filter(function (test) { return !mismatched(test); }).length;
    }

    function renderNotice(wrong) {
      notice.replaceChildren();
      notice.hidden = wrong.length === 0;
      if (!wrong.length) {
        return;
      }
      var text = document.createElement("p");
      text.innerHTML = iconMarkup("exclamation-triangle");
      text.append(plural(wrong.length, "selected test") + (wrong.length === 1 ? " is" : " are") +
        " not for " + matrixLabel() + " samples. Remove " + (wrong.length === 1 ? "it" : "them") +
        ", or correct the sample matrix.");
      var list = document.createElement("ul");
      wrong.forEach(function (test) {
        var item = document.createElement("li");
        item.textContent = fullLabel(test);
        list.append(item);
      });
      var button = document.createElement("button");
      button.type = "button";
      button.className = "btn btn-outline-secondary btn-sm";
      button.textContent = wrong.length === 1 ? "Remove this test" : "Remove these " + wrong.length + " tests";
      button.addEventListener("click", function () {
        var label = matrixLabel();
        bulk = true;
        wrong.forEach(function (test) { picker.removeItem(test.value); });
        bulk = false;
        refresh();
        control.focus();
        announce("Removed " + plural(wrong.length, "test") + " not for " + label + " samples.");
      });
      notice.append(text, list, button);
    }

    // Re-scope addable options to the matrix, re-render selected items and update the summary.
    function refresh() {
      picker.clearOptions(); // Keeps selected options (Tom Select clearFilter).
      picker.addOptions(registry.filter(function (test) { return !mismatched(test); }).map(function (test, index) {
        return Object.assign({ $order: index + 1 }, test);
      }));
      selectedTests().forEach(function (test) {
        picker.updateOption(test.value, Object.assign({}, test));
      });
      picker.refreshOptions(false);
      var wrong = selectedTests().filter(mismatched);
      var scope = matrixValue() ? plural(addableCount(), matrixLabel() + " test") + " available" :
        "Choose the sample matrix to limit the list to its tests";
      summary.textContent = plural(picker.items.length, "test") + " selected · " + scope + ".";
      renderNotice(wrong);
      return wrong;
    }

    picker.on("item_add", function (value) {
      var test = byValue[value];
      announce("Added " + (test ? fullLabel(test) : "test") + ". " + plural(picker.items.length, "test") + " selected.");
      refresh();
    });

    picker.on("item_remove", function (value) {
      if (bulk) {
        return; // The mismatch action announces once and refreshes after the last removal.
      }
      var test = byValue[value];
      announce("Removed " + (test ? fullLabel(test) : "test") + ". " + plural(picker.items.length, "test") + " selected.");
      refresh();
    });

    if (matrix) {
      matrix.addEventListener("change", function () {
        var wrong = refresh();
        var text = matrixValue() ? "Showing " + plural(addableCount(), matrixLabel() + " test") + "." : "Showing tests for every matrix.";
        if (wrong.length) {
          text += " " + plural(wrong.length, "selected test") + " not for " + matrixLabel() + " samples; kept for review.";
        }
        announce(text);
      });
    }

    refresh();
  }

  window.vrInitTestPickers = function (scope) {
    if (scope.matches && scope.matches("select[data-requested-tests]")) {
      init(scope);
    }
    scope.querySelectorAll("select[data-requested-tests]").forEach(init);
  };
  ["htmx:load", "htmx:afterSettle"].forEach(function (name) {
    document.addEventListener(name, function (event) {
      window.vrInitTestPickers(event.detail.elt || document);
    });
  });
  window.vrInitTestPickers(document);
})();
