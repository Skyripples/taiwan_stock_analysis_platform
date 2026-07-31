# 台股投資分析平台

整合台股事件月曆與市場資訊的投資分析平台，支援響應式版面與深色模式。

## 最新版本：V2.2
- Market Analysis Engine 新增可擴充的 Market Score 系統
- 每個市場訊號提供 `value`、`status` 與統一規則產生的 `score`
- 自動加總所有訊號，換算市場分數百分比與五段市場狀態
- 維持既有市場訊號 JSON 結構相容

## 網址
- GitHub Pages: https://skyripples.github.io/taiwan_stock_analysis_platform/

## 目前功能
- 平台首頁與功能入口
- 共用 Header、Sidebar、Page Framework 與 Widget 系統
- 深色模式切換與響應式手機版導覽
- 台股事件簿：
  - 月曆顯示台股事件（可依股票代號篩選）
  - 除權息
  - 股利發放日
  - 法說會
  - 股東會
  - 終止掛牌
- 市場總覽：
  - 市場指標卡片
  - 三大法人買賣超（外資及陸資、投信、自營商與合計）
  - 外資台指期未平倉（多空口數、淨部位與淨契約金額）
  - 市場方向狀態區
  - 趨勢圖表預留區（尚未接入資料）

## 資料來源
- Yahoo 台股行事曆（主要來源）
- TWSE 三大法人買賣金額統計表（市場資料，官方公開來源）
- TAIFEX 三大法人區分各期貨契約統計（外資臺股期貨未平倉部位，官方公開來源）
- 事件簿中的其他 TWSE API、MOPS 資料流程（保留，現階段停用）

## Data Provider Framework
市場資料層位於 `scripts/providers/`，以 `BaseProvider` 統一 `fetch()`、`normalize()`、`validate()` 與 `export()` 介面。目前 TWSE 與 TAIFEX Provider 已啟用；Yahoo 與 MOPS Provider 保留可擴充骨架，尚未啟用或呼叫外部 API。

```text
scripts/
├─ providers/
│  ├─ base_provider.py
│  ├─ registry.py
│  ├─ twse_provider.py
│  ├─ taifex_provider.py
│  ├─ yahoo_provider.py
│  └─ mops_provider.py
├─ config.py
└─ update_market_data.py

data/
└─ market/
```

從專案根目錄執行：

```bash
python scripts/update_market_data.py
```

## Market Data Schema
所有 Provider 最終都必須透過 `export()` 輸出統一 Schema，固定包含 `updated_at`、`provider`、`dataset`、`version` 與可擴充的 `data`。完整規格與空白範例位於 `data/market/schema.md` 及 `data/market/sample_market.json`。

### TWSE 三大法人資料
`TwseProvider` 使用臺灣證券交易所官方「三大法人買賣金額統計表」取得最近可用交易日資料，金額以整數輸出，幣別為 TWD、單位為元。執行成功後寫入：

```text
data/market/institutional_investors.json
```

從專案根目錄執行：

```bash
python scripts/update_market_data.py
```

若官方來源無資料或請求失敗，程式會輸出錯誤日誌並保留既有有效 JSON。

### Provider Registry
市場資料更新入口透過 `scripts/providers/registry.py` 自動探索 `scripts/providers/*_provider.py` 中的 Provider，不直接匯入個別實作。每個 Provider 以 `name`、`dataset`、`enabled` 與 `output_filename` 宣告執行設定；`enabled` 預設為 `True`，尚未完成的 Provider 會明確設為停用。

Registry 會依序對所有啟用的 Provider 執行 `fetch()`、`normalize()`、`validate()`、`export()` 與共用 `write_json()`。單一 Provider 失敗不會中止其他 Provider，執行結束會輸出成功、失敗、略過數量與總耗時。

### TAIFEX 外資臺股期貨部位
`TaifexProvider` 使用臺灣期貨交易所官方「三大法人－區分各期貨契約－依日期」，查詢商品代碼 `TXF`（臺股期貨）的外資未平倉部位。資料包含多方、空方與淨部位的未平倉口數及契約金額；口數單位為口，契約金額單位為新臺幣千元。

執行成功後寫入：

```text
data/market/foreign_futures_position.json
```

若指定日期休市或尚無資料，Provider 會向前查找最近可取得交易日；請求失敗、無資料或格式驗證失敗時，不會覆蓋既有有效 JSON，也不會中止其他 Provider。

## Market Analysis Engine
Provider 負責取得並正規化市場資料，Analysis 負責讀取多個 Provider JSON 並產生統一市場訊號。Dashboard 未來只需讀取 Analysis 輸出，不需要各自解析不同來源格式。

目前 `MarketSignalEngine` 讀取三大法人買賣超與外資臺股期貨部位，依外資現貨買賣超及期貨淨未平倉口數的正、負、零，分別產生 `bullish`、`bearish`、`neutral` 狀態；這些規則只描述目前資料方向，不是行情預測。

每個 signal 同時依集中管理的規則取得分數：`bullish` 為 `+1`、`neutral` 為 `0`、`bearish` 為 `-1`。Engine 會加總所有 signal，將 `-max_score～+max_score` 換算為 `0～100%` 的 Market Score，並標示 `Strong Bullish`、`Bullish`、`Neutral`、`Bearish` 或 `Strong Bearish`。既有的 `value` 與 `status` 欄位維持不變。

```text
scripts/
├─ analysis/
│  ├─ __init__.py
│  ├─ base_analysis.py
│  └─ market_signal_engine.py
└─ update_market_signals.py

data/
└─ market/
   └─ market_signals.json
```

從專案根目錄執行：

```bash
python scripts/update_market_signals.py
```
