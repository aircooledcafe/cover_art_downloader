// Theme picker: switches the data-theme attribute on <html> and remembers the choice.
// Kept separate from app.js so the search/download logic is untouched.
(function () {
  "use strict";

  const STORAGE_KEY = "caa-theme";
  const THEMES = ["system", "light", "dark", "cyberpunk"];
  const root = document.documentElement;
  const select = document.getElementById("theme-select");

  function apply(theme) {
    if (theme === "system" || !THEMES.includes(theme)) {
      root.removeAttribute("data-theme"); // follow the OS light/dark preference
    } else {
      root.setAttribute("data-theme", theme);
    }
  }

  function saved() {
    try {
      const t = localStorage.getItem(STORAGE_KEY);
      return THEMES.includes(t) ? t : "system";
    } catch (_) {
      return "system";
    }
  }

  if (!select) return;
  select.value = saved();

  select.addEventListener("change", () => {
    const theme = select.value;
    apply(theme);
    try {
      if (theme === "system") localStorage.removeItem(STORAGE_KEY);
      else localStorage.setItem(STORAGE_KEY, theme);
    } catch (_) { /* storage unavailable: theme still applies for this page */ }
  });
})();
