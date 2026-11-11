/* Reporting limit = method limit x dilution, only for whole-number dilutions and only until the
   analyst edits the limit. Exact integer arithmetic; the server validates every value again. */
(function () {
  var grid = document.getElementById("entry-grid");
  if (!grid) return;
  grid.addEventListener("input", function (event) {
    var input = event.target, name = input.name || "";
    var row = input.closest("tr");
    if (!row) return;
    var limit = row.querySelector('[name$="-reporting_limit_applied"]');
    if (name.endsWith("-reporting_limit_applied")) { input.dataset.edited = "1"; return; }
    if (!name.endsWith("-dilution_factor") || !limit || limit.dataset.edited) return;
    var base = limit.dataset.baseLimit, factor = input.value.trim();
    if (!base || !/^[0-9]+$/.test(factor) || factor === "0") return;
    var parts = base.split("."), scale = (parts[1] || "").length;
    var scaled = (BigInt(parts[0] + (parts[1] || "")) * BigInt(factor)).toString();
    while (scaled.length <= scale) scaled = "0" + scaled;
    limit.value = scale ? scaled.slice(0, -scale) + "." + scaled.slice(-scale) : scaled;
  });
})();
