(function () {
  "use strict";
  var form = document.getElementById("refresh-form");
  var btn = document.getElementById("refresh-btn");
  var progress = document.getElementById("refresh-progress");
  var updated = document.getElementById("updated");
  var polling = false;
  var t = document.body.dataset;

  function setRunning(msg) {
    btn.disabled = true;
    btn.textContent = t.tRefreshing || "Refreshing…";
    progress.textContent = msg || t.tStarting || "Starting refresh";
    if (updated) updated.hidden = true;
  }

  function poll() {
    if (polling) return;
    polling = true;
    var tick = function () {
      fetch("/api/status", { headers: { Accept: "application/json" }, cache: "no-store" })
        .then(function (r) { return r.json(); })
        .then(function (s) {
          if (s.running) {
            setRunning(s.message);
            setTimeout(tick, 2000);
          } else {
            progress.textContent = t.tDone || "Done, reloading…";
            window.location.reload();
          }
        })
        .catch(function () { setTimeout(tick, 4000); });
    };
    setTimeout(tick, 2000);
  }

  if (form && btn) {
    form.addEventListener("submit", function (ev) {
      ev.preventDefault();
      if (btn.disabled) return;
      setRunning();
      fetch("/refresh", { method: "POST", headers: { Accept: "application/json" } })
        .then(function () { poll(); })
        .catch(function () { form.submit(); });
    });
    if (document.body.dataset.running === "1") { setRunning(progress.textContent); poll(); }
  }

  // translate button: show that the (slow) request is running
  document.querySelectorAll("form[data-translate]").forEach(function (f) {
    f.addEventListener("submit", function () {
      var b = f.querySelector("button");
      if (b) { b.disabled = true; b.textContent = b.dataset.busy || b.textContent; }
    });
  });

  // auto-submit filter selects / checkboxes
  document.querySelectorAll("[data-autosubmit]").forEach(function (el) {
    el.addEventListener("change", function () { el.form && el.form.submit(); });
  });
})();
