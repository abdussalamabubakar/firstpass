/* firstpass theme toggle — persisted, respects system preference.
   Loaded in <head> so the attribute is set before first paint (no flash). */
(function () {
  var KEY = "firstpass-theme";
  var saved = null;
  try { saved = localStorage.getItem(KEY); } catch (e) { /* private mode */ }
  var prefersDark = window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches;
  var theme = saved === "dark" || saved === "light" ? saved : (prefersDark ? "dark" : "light");
  document.documentElement.setAttribute("data-theme", theme);

  function label(btn) {
    btn.textContent = document.documentElement.getAttribute("data-theme") === "dark"
      ? "☀️ Light" : "🌙 Dark";
  }

  function toggle() {
    var next = document.documentElement.getAttribute("data-theme") === "dark" ? "light" : "dark";
    document.documentElement.setAttribute("data-theme", next);
    try { localStorage.setItem(KEY, next); } catch (e) { /* private mode */ }
    document.querySelectorAll("[data-theme-toggle]").forEach(function (btn) { label(btn); });
  }

  document.addEventListener("DOMContentLoaded", function () {
    document.querySelectorAll("[data-theme-toggle]").forEach(function (btn) {
      label(btn);
      btn.addEventListener("click", toggle);
    });
  });
})();
