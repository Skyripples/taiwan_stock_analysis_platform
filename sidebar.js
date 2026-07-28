(() => {
  const body = document.body;
  const toggle = document.querySelector(".sidebar-toggle");
  const backdrop = document.querySelector("[data-sidebar-close]");
  const sidebar = document.getElementById("platformSidebar");
  const themeToggle = document.getElementById("themeToggleSwitch");
  const isCalendarPage = Boolean(document.getElementById("calendarGrid"));
  const themeStorageKey = "taiwan_stock_market_theme";

  if (!toggle || !backdrop || !sidebar) return;

  if (themeToggle && !isCalendarPage) {
    let preferredTheme = "dark";
    try {
      const savedTheme = localStorage.getItem(themeStorageKey);
      if (savedTheme === "light" || savedTheme === "dark") {
        preferredTheme = savedTheme;
      } else if (window.matchMedia("(prefers-color-scheme: light)").matches) {
        preferredTheme = "light";
      }
    } catch (error) {
      // Keep the dark default if storage is unavailable.
    }

    const applyTheme = (theme) => {
      const isDark = theme === "dark";
      body.classList.toggle("theme-dark", isDark);
      body.classList.toggle("theme-light", !isDark);
      themeToggle.checked = isDark;
      themeToggle.setAttribute("aria-checked", String(isDark));
    };

    applyTheme(preferredTheme);

    themeToggle.addEventListener("change", () => {
      const theme = themeToggle.checked ? "dark" : "light";
      applyTheme(theme);
      try {
        localStorage.setItem(themeStorageKey, theme);
      } catch (error) {
        // Theme still applies for this visit if storage is unavailable.
      }
    });
  }

  function setSidebarOpen(isOpen) {
    body.classList.toggle("sidebar-open", isOpen);
    toggle.setAttribute("aria-expanded", String(isOpen));
    toggle.setAttribute("aria-label", isOpen ? "關閉導覽列" : "開啟導覽列");
  }

  toggle.addEventListener("click", () => {
    setSidebarOpen(!body.classList.contains("sidebar-open"));
  });

  backdrop.addEventListener("click", () => setSidebarOpen(false));

  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") setSidebarOpen(false);
  });

  sidebar.addEventListener("click", (event) => {
    if (event.target.closest("a")) setSidebarOpen(false);
  });

  const desktopQuery = window.matchMedia("(min-width: 901px)");
  const handleBreakpointChange = (event) => {
    if (event.matches) setSidebarOpen(false);
  };

  if (desktopQuery.addEventListener) {
    desktopQuery.addEventListener("change", handleBreakpointChange);
  } else {
    desktopQuery.addListener(handleBreakpointChange);
  }
})();
