(() => {
  const body = document.body;
  const toggle = document.querySelector(".sidebar-toggle");
  const backdrop = document.querySelector("[data-sidebar-close]");
  const sidebar = document.getElementById("platformSidebar");
  const themeToggle = document.getElementById("themeToggleSwitch");
  const isCalendarPage = Boolean(document.getElementById("calendarGrid"));
  const themeStorageKey = "taiwan_stock_market_theme";
  const authApiBase = "https://172-238-20-217.ip.linodeusercontent.com/api/v1";

  const headerActions = document.querySelector(".layout-header-actions");
  let loginButton = headerActions?.querySelector(".layout-login-button") || null;
  if (headerActions && !headerActions.querySelector(".layout-login-button")) {
    loginButton = document.createElement("a");
    loginButton.className = "layout-login-button";
    loginButton.href = "./login.html";
    loginButton.textContent = "登入";
    loginButton.setAttribute("aria-label", "前往帳號登入頁");
    headerActions.append(loginButton);
  }

  if (!toggle || !backdrop || !sidebar) return;

  const protectedItems = [...sidebar.querySelectorAll(".sidebar-item")]
    .filter((item) => !item.getAttribute("href")?.endsWith("index.html"));

  const applyNavigationAccess = (isAdmin, username = "") => {
    protectedItems.forEach((item) => {
      if (!item.dataset.accessHref && item.hasAttribute("href")) {
        item.dataset.accessHref = item.getAttribute("href");
      }
      let label = item.querySelector("small[data-access-label]");
      if (isAdmin) {
        if (item.dataset.accessHref) item.setAttribute("href", item.dataset.accessHref);
        item.classList.remove("is-disabled");
        item.removeAttribute("aria-disabled");
        item.removeAttribute("tabindex");
        label?.remove();
      } else {
        item.removeAttribute("href");
        item.classList.add("is-disabled");
        item.setAttribute("aria-disabled", "true");
        item.setAttribute("tabindex", "-1");
        if (!label) {
          label = document.createElement("small");
          label.dataset.accessLabel = "";
          item.append(label);
        }
        label.textContent = "付費解鎖";
      }
    });
    if (loginButton && isAdmin) {
      loginButton.textContent = username || "admin";
      loginButton.setAttribute("aria-label", "目前登入的管理者帳號");
    }
    body.dataset.accessRole = isAdmin ? "admin" : "restricted";
    window.dispatchEvent(new CustomEvent("platform-access-change", {
      detail: { isAdmin, username },
    }));
  };

  const verifyAccess = async () => {
    applyNavigationAccess(false);
    let token = "";
    try { token = sessionStorage.getItem("taiwan_stock_access_token") || ""; } catch (error) {}
    if (!token) return;
    try {
      const response = await fetch(`${authApiBase}/auth/me`, {
        headers: { Authorization: `Bearer ${token}`, Accept: "application/json" },
        cache: "no-store",
      });
      if (!response.ok) throw new Error("invalid session");
      const account = await response.json();
      applyNavigationAccess(account.username === "admin" && account.role === "admin", account.username);
    } catch (error) {
      try {
        sessionStorage.removeItem("taiwan_stock_access_token");
        sessionStorage.removeItem("taiwan_stock_account");
      } catch (storageError) {}
      applyNavigationAccess(false);
    }
  };

  verifyAccess();

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
