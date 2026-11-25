(function () {
  "use strict";
  function draw() {
    var el = document.getElementById("price-chart");
    var dataEl = document.getElementById("chart-data");
    if (!el || !dataEl || typeof Chart === "undefined") return;
    var d = JSON.parse(dataEl.textContent);
    var css = getComputedStyle(document.documentElement);
    var v = function (n) { return css.getPropertyValue(n).trim(); };
    var money = function (x) { if (x == null) return "n/a"; if (x === 0) return "$0"; return "$" + (x < 0.1 ? x.toFixed(3) : x.toFixed(2)); };
    Chart.defaults.font.family = v("--font");
    Chart.defaults.color = v("--muted");
    new Chart(el, {
      type: "line",
      data: {
        labels: d.labels,
        datasets: [
          { label: (d.names && d.names.input) || "Input", data: d.input, stepped: true, borderColor: v("--t0"), backgroundColor: v("--t0"), borderWidth: 2.5, pointRadius: 3, spanGaps: true },
          { label: (d.names && d.names.output) || "Output", data: d.output, stepped: true, borderColor: v("--t4"), backgroundColor: v("--t4"), borderWidth: 2.5, pointRadius: 3, spanGaps: true }
        ]
      },
      options: {
        maintainAspectRatio: false,
        interaction: { mode: "index", intersect: false },
        scales: {
          y: { beginAtZero: true, grid: { color: v("--border") }, ticks: { callback: function (x) { return money(x); } } },
          x: { grid: { display: false } }
        },
        plugins: {
          legend: { position: "top", align: "end", labels: { usePointStyle: true, pointStyle: "circle", boxWidth: 8, boxHeight: 8 } },
          tooltip: { callbacks: { label: function (c) { return c.dataset.label + ": " + money(c.parsed.y) + " / 1M"; } } }
        }
      }
    });
  }
  if (document.readyState === "complete") draw(); else window.addEventListener("load", draw);
})();
