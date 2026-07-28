# Page Framework

目前啟用的頁面：

- `home`：`../index.html`
- `calendar`：`../calendar.html`
- `market-overview`：`../market-overview.html`

新頁面沿用以下唯一 Main Content 結構：

```html
<main class="layout-main" data-page="page-name">
  <div class="page-container">
    <header class="page-header">
      <p class="page-eyebrow">CATEGORY</p>
      <h1 class="page-title">頁面名稱</h1>
      <p class="page-description">頁面說明</p>
    </header>

    <div class="page-body">
      <section class="page-section">
        <div class="page-card"></div>
      </section>
    </div>
  </div>
</main>
```

共用樣式位於 `../page.css`。尚未實作的分析功能不建立頁面或內容。
