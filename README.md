# 台股投資分析平台

以原生 HTML、CSS、JavaScript 建立的台股分析平台，整合市場資料、事件、籌碼、個股基本面與研究型預測流程。網站可直接部署至 GitHub Pages；全市場個股資料由 Linode PostgreSQL 與 REST API 提供。

## 功能

### 平台介面

- 共用 Header、Sidebar、Page Framework 與 Widget 系統
- 支援深色／淺色模式與手機響應式版面
- 首頁以市場、股票、期貨、基金、債券、外匯、定存七大分類作為入口
- 首頁與七大分類 Hub 顯示一致的關鍵資料摘要、資料狀態、日期及來源
- Sidebar 採大分類與小功能的階層式導覽，各分類可進入獨立 Hub
- Hub 與功能頁使用一致的麵包屑、頁面標題、載入狀態與資料限制說明
- 支援鍵盤焦點、狀態朗讀及手機版表格水平操作等基本無障礙體驗
- 市場 Hub 彙整台股、法人、全球市場、事件與預測入口
- 股票 Hub 彙整股票搜尋、個股分析與條件選股入口
- 提供帳號登入、登出與功能權限管理入口

### 台股事件簿

- 顯示除權息、股東會與法說會等市場事件
- 提供月曆瀏覽與事件篩選

### 市場總覽

- 加權指數、櫃買指數、成交金額與上漲／下跌家數
- 三大法人每日買賣超
- 外資臺股期貨部位與台指期夜盤
- TSM ADR、SOX、S&P 500、NASDAQ 等國際市場資訊
- Market Score 與市場訊號因子

### 隔日行情分析

- 顯示 Logistic Regression Baseline Beta 的隔日方向與機率
- 使用 Platt Calibration 校準預測機率
- 顯示模型版本、歷史評估指標、因子權重與資料狀態
- 追蹤歷史預測結果與實際命中率

### 籌碼分析

- 三大法人、外資期貨與融資融券資料
- 5／20 日累計、連買連賣、部位變化與 Z-score
- 最近 20／60 個交易日趨勢
- 規則式 bullish／neutral／bearish 籌碼狀態

### 個股分析

- 搜尋 TWSE／TPEx 全市場股票、ETF、ETN 與其他可搜尋商品
- 行情、估值、月營收、財報、財務趨勢與籌碼資料
- 歷史 PE／PB／殖利率分位
- 成長股、地雷股、便宜股、績優股透明規則健檢
- 同產業排名、百分位與前十名排行
- 規則式綜合分析摘要、主要優勢、風險與觀察項目
- 個股內容統一由 Linode REST API 提供；服務無法使用時顯示明確狀態，不讀取過期的 GitHub 個股快取

### 條件選股

- 透過 Linode REST API 與 PostgreSQL 進行全市場 server-side 條件篩選
- 支援市場、產業、商品類型、行情、估值、基本面與籌碼條件
- 支援欄位排序、分頁與快速進入個股分析
- 產業分類沿用 TWSE／TPEx 官方月營收批次資料

### 期貨

- 顯示台指期 TX 最近交易日的一般交易時段完成行情
- 提供近月與次月契約、OHLC、漲跌幅、成交量、未平倉量及結算價比較
- 資料來自臺灣期貨交易所官方每日行情，每日自動更新並保留最近有效資料

### 基金／ETF

- 比較臺灣上市與上櫃 ETF 的基本資料、最新價格及 1M／3M／6M／1Y 期間報酬
- 支援名稱／代號搜尋、類型篩選、欄位排序及連結至既有個股分析頁
- 資料來自 TWSE／TPEx 官方批次行情，每日自動更新並保留最近有效資料

### 債券

- 比較台灣與美國政府公債的最新殖利率
- 提供市場切換、期限／殖利率排序、2Y–10Y 利差與殖利率曲線
- 資料來自證券櫃檯買賣中心與美國財政部官方每日資料

### 外匯

- 顯示 USD、JPY、EUR、CNY、GBP、AUD、CAD、SGD、HKD、KRW 對新台幣參考匯率
- 支援貨幣代碼／名稱搜尋、匯率排序及任意幣別換算
- 資料來自中央銀行官方每日匯率資料，並保留各貨幣實際資料日期

### 定存

- 比較臺灣各銀行目前的活期存款、定期存款與定期儲蓄存款牌告利率
- 支援銀行名稱搜尋、欄位排序及固定／機動利率切換
- 資料取自中央銀行牌告利率資訊網，每日自動更新並保留最近有效資料

### 全市場個股預測研究基礎

- 全市場歷史 OHLCV、技術、產業、籌碼、事件與全球市場特徵
- T+1／T+3／T+5 supervised targets
- 本機 Parquet Data Lake 與 DuckDB research query layer
- 已完成 50 檔股票的跨股票 Logistic Regression feasibility pilot
- 個股預測目前仍屬研究驗證階段，尚未 Production 化，也不會在前端顯示正式個股預測

## 資料架構

- GitHub Pages：前端、程式碼、設定、輕量搜尋 index 與必要文件
- Linode PostgreSQL：全市場個股正式資料與 REST API
- 本機 Data Lake：歷史研究資料，採 Parquet + ZSTD level 6 壓縮
- DuckDB：直接查詢 Parquet，利用欄位與條件下推避免載入完整資料集
- GitHub 不保存 Data Lake、Parquet 或 2,390 份個股 JSON

## 專案結構

- `scripts/`：Production 資料更新、模型、API 與資料庫入口
- `scripts/research/`：研究資料集、ablation、walk-forward、validation 與 pilot
- `scripts/research/common/`：共用 walk-forward 邊界與分類 metrics
- `scripts/maintenance/`：一次性 repair、cleanup、export 與 benchmark 工具
- `tests/frontend/`：前端 smoke／integration 測試
- `data/analysis/current/`：目前流程仍會讀取或更新的報告
- `data/analysis/archive/`：保留供追溯的歷史研究結果
- `scripts/database/migrations/`：Production migration；檔案與順序維持不變

## 資料來源

- 臺灣證券交易所（TWSE）：市場指數、成交統計、三大法人、融資融券、上市公司行情與估值
- 證券櫃檯買賣中心（TPEx）：上櫃市場、公司基本資料、行情與估值
- 臺灣期貨交易所（TAIFEX）：外資臺股期貨部位與台指期盤後交易
- 公開資訊觀測站（MOPS）：公司基本資料、月營收與財務報表
- Yahoo Finance：TSM ADR、SOX、美股指數、亞洲市場、商品、匯率與研究候選資料
- Cboe：VIX 官方歷史資料
- FRED：美國利率與總體經濟序列
- Coinbase：BTC／ETH 小時資料
- TWSE 年度官方交易日資訊：台股 Trading Calendar

## 使用限制

- 本平台僅供資料整理、研究與技術驗證，不構成投資建議。
- 市場資料可能受來源延遲、休市、格式調整或服務中斷影響。
- 使用者應自行核對官方資料並承擔投資決策風險。
