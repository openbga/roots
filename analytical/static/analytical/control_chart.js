// QC control chart: renders the JSON from analytical.charts.chart_payload with Chart.js.
// Presentation only. The exact values a person reads are the strings in the payload (tooltips) and
// the data table under the chart; the numbers drawn here are display positions. Colours come from
// the theme's CSS variables, so light, dark and system modes need no palette in this file.
(function () {
  "use strict";

  var dataElement = document.getElementById("control-chart-data");
  var canvas = document.getElementById("control-chart");
  if (!dataElement || !canvas) {
    return; // This page has no chart (nothing to plot).
  }
  var payload = JSON.parse(dataElement.textContent);
  var chart = null;

  function css(name) {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  }

  function theme() {
    return {
      text: css("--vr-text"),
      muted: css("--vr-text-muted"),
      grid: css("--vr-border"),
      danger: css("--vr-danger-solid"),
      warn: css("--vr-warning-text"),
    };
  }

  function lineStyle(kind, c) {
    return {
      config: { borderColor: c.danger, borderWidth: 2, borderDash: [] },
      mean: { borderColor: c.text, borderWidth: 1, borderDash: [] },
      warning: { borderColor: c.muted, borderWidth: 1.5, borderDash: [6, 4] },
      control: { borderColor: c.warn, borderWidth: 1.5, borderDash: [2, 3] },
    }[kind];
  }

  function pointColor(point, c) {
    if (point.status === "fail") return c.danger;
    if (point.band === "control") return c.warn;
    return point.band ? c.muted : c.text;
  }

  function build() {
    var c = theme();
    var count = payload.labels.length;
    var observed = {
      label: "Observed (" + payload.unit + ")",
      data: payload.points.map(function (p) { return p.y; }),
      borderColor: c.muted,
      borderWidth: 1,
      pointStyle: payload.points.map(function (p) { return p.shape; }),
      pointRadius: 5,
      pointHoverRadius: 7,
      pointBackgroundColor: payload.points.map(function (p) { return pointColor(p, c); }),
      pointBorderColor: payload.points.map(function (p) { return pointColor(p, c); }),
      order: 0,
    };
    var limits = payload.lines.map(function (line) {
      return Object.assign({
        label: line.label + " " + line.exact,
        data: new Array(count).fill(line.y),
        pointRadius: 0,
        pointHoverRadius: 0,
        fill: false,
        order: 1,
      }, lineStyle(line.kind, c));
    });
    Chart.defaults.color = c.text;
    Chart.defaults.borderColor = c.grid;
    chart = new Chart(canvas, {
      type: "line",
      data: { labels: payload.labels, datasets: [observed].concat(limits) },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        animation: false,
        interaction: { mode: "nearest", intersect: true },
        scales: {
          // Category axis in observation order; spacing is not proportional to elapsed time.
          x: {
            title: { display: true, text: "Observation order, oldest to newest (not to a time scale)" },
            ticks: { maxTicksLimit: 8, autoSkip: true },
          },
          y: { title: { display: true, text: payload.unit } },
        },
        plugins: {
          legend: {
            position: "bottom",
            labels: {
              usePointStyle: true,
              // Show what is drawn: observed results as points, each limit as its own line style.
              generateLabels: function (chart) {
                return chart.data.datasets.map(function (dataset, index) {
                  var isObserved = index === 0;
                  return {
                    text: dataset.label,
                    datasetIndex: index,
                    hidden: !chart.isDatasetVisible(index),
                    pointStyle: isObserved ? "circle" : "line",
                    fillStyle: isObserved ? c.text : "transparent",
                    strokeStyle: isObserved ? c.text : dataset.borderColor,
                    lineWidth: isObserved ? 1 : dataset.borderWidth,
                    lineDash: isObserved ? [] : dataset.borderDash,
                    fontColor: c.text,
                  };
                });
              },
            },
          },
          tooltip: {
            callbacks: {
              title: function (items) { return payload.labels[items[0].dataIndex]; },
              label: function (item) {
                if (item.datasetIndex !== 0) {
                  return payload.lines[item.datasetIndex - 1].label + ": " +
                    payload.lines[item.datasetIndex - 1].exact;
                }
                var p = payload.points[item.dataIndex];
                var note = p.status === "fail" ? "Outside configured limits"
                  : p.band === "control" ? "Beyond mean ± 3 SD (descriptive)"
                  : p.band === "warning" ? "Beyond mean ± 2 SD (descriptive)" : "";
                return [
                  p.exact + " " + payload.unit + " (observed " + p.observed + ")",
                  p.sample + " · " + p.batch,
                ].concat(note ? [note] : []);
              },
            },
          },
        },
      },
    });
  }

  build();
  // Light, dark and system modes switch data-bs-theme on <html>: redraw with the new variables.
  new MutationObserver(function () {
    chart.destroy();
    build();
  }).observe(document.documentElement, { attributes: true, attributeFilter: ["data-bs-theme"] });
})();
