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

  const navigationGroups = [
    {
      key: "market", label: "市場", href: "./market.html",
      children: [
        ["市場總覽", "./market-overview.html", "market_overview"],
        ["籌碼／法人", "./chips-analysis.html", "chips_analysis"],
        ["全球市場", "./market-overview.html#internationalMarket", "market_overview"],
        ["市場事件", "./calendar.html", "calendar"],
        ["行情預測", "./prediction.html", "prediction"],
      ],
    },
    {
      key: "stocks", label: "股票", href: "./stocks.html",
      children: [
        ["股票搜尋／個股分析", "./stock-analysis.html", "stock_analysis"],
        ["條件選股", "./screener.html", "stock_analysis"],
      ],
    },
    { key: "futures", label: "期貨", href: "./futures.html", children: [] },
    { key: "funds", label: "基金", href: "./funds.html", children: [] },
    { key: "bonds", label: "債券", href: "./bonds.html", children: [] },
    { key: "forex", label: "外匯", href: "./forex.html", children: [] },
    { key: "deposits", label: "定存", href: "./deposits.html", children: [] },
  ];

  const currentPage = window.location.pathname.split("/").pop() || "index.html";
  const renderNavigation = () => {
    const nav = sidebar.querySelector(".sidebar-nav");
    if (!nav) return;
    const homeActive = currentPage === "index.html";
    nav.innerHTML = `<a class="sidebar-item${homeActive ? " is-active" : ""}" href="./index.html"${homeActive ? ' aria-current="page"' : ""}>首頁</a>`;
    navigationGroups.forEach((group) => {
      const groupPage = group.href.slice(2);
      const childActive = group.children.some(([, href]) => href.slice(2) === currentPage);
      const groupActive = groupPage === currentPage;
      const wrapper = document.createElement("section");
      wrapper.className = `sidebar-group${childActive || groupActive ? " is-open" : ""}`;
      const heading = document.createElement("div");
      heading.className = "sidebar-group-heading";
      heading.innerHTML = `<a class="sidebar-item sidebar-category${groupActive ? " is-active" : ""}" href="${group.href}"${groupActive ? ' aria-current="page"' : ""}>${group.label}</a>`;
      if (group.children.length) {
        const toggleButton = document.createElement("button");
        toggleButton.type = "button";
        toggleButton.className = "sidebar-group-toggle";
        toggleButton.setAttribute("aria-label", `展開或收合${group.label}功能`);
        toggleButton.setAttribute("aria-expanded", String(childActive || groupActive));
        toggleButton.textContent = "⌄";
        toggleButton.addEventListener("click", () => {
          const open = wrapper.classList.toggle("is-open");
          toggleButton.setAttribute("aria-expanded", String(open));
        });
        heading.append(toggleButton);
      }
      wrapper.append(heading);
      if (group.children.length) {
        const children = document.createElement("div");
        children.className = "sidebar-subnav";
        group.children.forEach(([label, href, feature]) => {
          const active = href.slice(2) === currentPage;
          children.insertAdjacentHTML("beforeend", `<a class="sidebar-item sidebar-subitem${active ? " is-active" : ""}" href="${href}" data-feature="${feature}"${active ? ' aria-current="page"' : ""}>${label}</a>`);
        });
        wrapper.append(children);
      }
      nav.append(wrapper);
    });
  };
  renderNavigation();

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
    "stock-analysis.html": "stock_analysis", "screener.html": "stock_analysis",
  };
  const previewGeneralPermissions = Object.freeze({
    calendar: true,
    prediction: true,
    market_overview: true,
    chips_analysis: true,
    stock_analysis: true,
  });
  const protectedItems = [...sidebar.querySelectorAll(".sidebar-item[data-feature]")];

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
      const featureKey = item.dataset.feature || featureByPath[filename];
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
