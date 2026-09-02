(() => {
  const body = document.body;
  const toggle = document.querySelector(".sidebar-toggle");
  const backdrop = document.querySelector("[data-sidebar-close]");
  const sidebar = document.getElementById("platformSidebar");
  const themeToggle = document.getElementById("themeToggleSwitch");
  const isCalendarPage = Boolean(document.getElementById("calendarGrid"));
  const themeStorageKey = "taiwan_stock_market_theme";
  const previewRoleStorageKey = "taiwan_stock_preview_role";
  const authApiBase = "https://172-238-20-217.ip.linodeusercontent.com/api/v1";

  const headerActions = document.querySelector(".layout-header-actions");
  let previewControls = headerActions?.querySelector(".layout-preview-roles") || null;
  if (headerActions && !previewControls) {
    previewControls = document.createElement("div");
    previewControls.className = "layout-preview-roles";
    previewControls.setAttribute("aria-label", "權限預覽切換");
    previewControls.innerHTML = '<button type="button" data-preview-role="admin">管理者</button><button type="button" data-preview-role="general">一般</button>';
    headerActions.prepend(previewControls);
  }
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
  const previewGeneralPermissions = Object.freeze({
    calendar: true,
    prediction: true,
    market_overview: true,
    chips_analysis: true,
    stock_analysis: true,
  });
  const protectedItems = [...sidebar.querySelectorAll(".sidebar-item")]
    .filter((item) => !item.getAttribute("href")?.endsWith("index.html"));

  const renderAccountMenu = (username = "", token = "", isAdmin = false) => {
    document.querySelector(".layout-account-menu")?.remove();
    if (!loginButton) return;
    if (!username || !token) {
      loginButton.textContent = "登入";
      loginButton.href = "./login.html";
      loginButton.setAttribute("aria-label", "前往帳號登入頁");
      return;
    }
    loginButton.textContent = isAdmin ? "管理員" : username;
    loginButton.href = "#";
    loginButton.setAttribute("aria-label", "開啟帳號選單");
    loginButton.setAttribute("aria-expanded", "false");
    const menu = document.createElement("div");
    menu.className = "layout-account-menu";
    menu.hidden = true;
    if (isAdmin) {
      const managementLink = document.createElement("a");
      managementLink.href = "./user-management.html";
      managementLink.textContent = "使用者管理";
      menu.append(managementLink);
    }
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
        try { localStorage.removeItem(previewRoleStorageKey); } catch (error) {}
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
        label.textContent = "不可使用";
      }
    });
    renderAccountMenu(username, token, isAdmin);
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
    let previewRole = "";
    try { previewRole = localStorage.getItem(previewRoleStorageKey) || ""; } catch (error) {}
    if (previewRole === "admin") {
      applyNavigationAccess(true, "管理員", {}, "local-preview", true);
      previewControls?.querySelector('[data-preview-role="admin"]')?.classList.add("is-active");
      return;
    }
    if (previewRole === "general") {
      applyNavigationAccess(false, "一般使用者", previewGeneralPermissions, "local-preview", true);
      previewControls?.querySelector('[data-preview-role="general"]')?.classList.add("is-active");
      return;
    }
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

  previewControls?.addEventListener("click", (event) => {
    const button = event.target.closest("[data-preview-role]");
    if (!button) return;
    const role = button.dataset.previewRole;
    try { localStorage.setItem(previewRoleStorageKey, role); } catch (error) {}
    window.location.reload();
  });

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
