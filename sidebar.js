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

  const featureByPath = {
    "calendar.html": "calendar", "prediction.html": "prediction",
    "market-overview.html": "market_overview", "chips-analysis.html": "chips_analysis",
    "stock-analysis.html": "stock_analysis",
  };
  const protectedItems = [...sidebar.querySelectorAll(".sidebar-item")]
    .filter((item) => !item.getAttribute("href")?.endsWith("index.html"));

  const ensureAdminNavigation = (isAdmin) => {
    let item = sidebar.querySelector("[data-admin-navigation]");
    if (!isAdmin) { item?.remove(); return; }
    if (!item) {
      item = document.createElement("a");
      item.className = "sidebar-item";
      item.href = "./user-management.html";
      item.dataset.adminNavigation = "";
      item.textContent = "使用者管理";
      if (window.location.pathname.endsWith("/user-management.html")) item.classList.add("is-active");
      sidebar.querySelector(".sidebar-nav")?.append(item);
    }
  };

  const renderAccountMenu = (username = "", token = "") => {
    document.querySelector(".layout-account-menu")?.remove();
    if (!loginButton) return;
    if (!username || !token) {
      loginButton.textContent = "登入";
      loginButton.href = "./login.html";
      loginButton.setAttribute("aria-label", "前往帳號登入頁");
      return;
    }
    loginButton.textContent = username;
    loginButton.href = "#";
    loginButton.setAttribute("aria-label", "開啟帳號選單");
    loginButton.setAttribute("aria-expanded", "false");
    const menu = document.createElement("div");
    menu.className = "layout-account-menu";
    menu.hidden = true;
    const logoutButton = document.createElement("button");
    logoutButton.type = "button";
    logoutButton.textContent = "登出";
    menu.append(logoutButton);
    headerActions.append(menu);
    loginButton.onclick = (event) => {
      event.preventDefault();
      menu.hidden = !menu.hidden;
      loginButton.setAttribute("aria-expanded", String(!menu.hidden));
    };
    logoutButton.addEventListener("click", async () => {
      logoutButton.disabled = true;
      try {
        await fetch(`${authApiBase}/auth/logout`, { method: "POST", headers: { Authorization: `Bearer ${token}` }, cache: "no-store" });
      } catch (error) {} finally {
        try { sessionStorage.removeItem("taiwan_stock_access_token"); sessionStorage.removeItem("taiwan_stock_account"); } catch (error) {}
        window.location.replace("./index.html");
      }
    });
    document.addEventListener("click", (event) => {
      if (!event.target.closest(".layout-header-actions")) { menu.hidden = true; loginButton.setAttribute("aria-expanded", "false"); }
    });
  };

  const applyNavigationAccess = (isAdmin, username = "", permissions = {}, token = "", resolved = false) => {
    protectedItems.forEach((item) => {
      if (!item.dataset.accessHref && item.hasAttribute("href")) {
        item.dataset.accessHref = item.getAttribute("href");
      }
      const filename = (item.dataset.accessHref || "").split("/").pop();
      const featureKey = featureByPath[filename];
      const allowed = isAdmin || Boolean(featureKey && permissions[featureKey]);
      let label = item.querySelector("small[data-access-label]");
      if (allowed) {
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
    ensureAdminNavigation(isAdmin);
    renderAccountMenu(username, token);
    body.dataset.accessRole = isAdmin ? "admin" : "restricted";
    window.dispatchEvent(new CustomEvent("platform-access-change", {
      detail: { isAdmin, username, permissions },
    }));
    const currentFeature = featureByPath[window.location.pathname.split("/").pop()];
    if (resolved && currentFeature && !(isAdmin || permissions[currentFeature])) {
      window.location.replace("./index.html");
    }
  };

  const verifyAccess = async () => {
    applyNavigationAccess(false);
    let token = "";
    try { token = sessionStorage.getItem("taiwan_stock_access_token") || ""; } catch (error) {}
    if (!token) { applyNavigationAccess(false, "", {}, "", true); return; }
    try {
      const response = await fetch(`${authApiBase}/auth/me`, {
        headers: { Authorization: `Bearer ${token}`, Accept: "application/json" },
        cache: "no-store",
      });
      if (!response.ok) throw new Error("invalid session");
      const account = await response.json();
      applyNavigationAccess(account.role === "admin", account.username, account.permissions || {}, token, true);
    } catch (error) {
      try {
        sessionStorage.removeItem("taiwan_stock_access_token");
        sessionStorage.removeItem("taiwan_stock_account");
      } catch (storageError) {}
      applyNavigationAccess(false, "", {}, "", true);
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
