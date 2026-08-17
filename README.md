# 台股投資分析平台 V3.4

## 功能

- 平台首頁、共用 Header、Sidebar、Page Framework 與 Widget 系統
- 台股事件簿：除權息、股東會、法說會等事件月曆
- 市場總覽：台股市場指標、國際市場、三大法人、外資台指期與 Market Score
- 隔日行情預測 Beta：
  - 使用 15 個正式市場特徵的 Logistic Regression Baseline Model
  - 依時間序列進行 Walk-forward Validation，避免未來資料洩漏
  - 使用 Platt Calibration 校準上漲與下跌機率
  - 顯示模型指標、Feature 清單、預測方向、機率與信心度
- V3.4 Feature Engineering 與模型定版：
  - 全球市場、VIX、KOSPI 與半導體產業候選因子分析
  - Feature Correlation、Ablation、Robustness 與 Pruning Validation
  - Temporal Leakage Audit 與台股 Trading Calendar
  - 正式模型移除冗餘的平盤家數，定版為 15 Features
- 每日市場資料、歷史資料、監督式訓練資料與預測驗證紀錄自動更新
- 深色／淺色模式與桌面、平板、手機響應式版面

## 資料來源

- 臺灣證券交易所（TWSE）：加權指數、成交金額、漲跌家數、三大法人與交易日資訊
- 證券櫃檯買賣中心（TPEx）：櫃買指數、上櫃成交金額與漲跌家數
- 臺灣期貨交易所（TAIFEX）：外資臺股期貨未平倉與臺股期貨夜盤
- Yahoo Finance：TSM ADR、SOX、S&P 500、NASDAQ、VIX、KOSPI，以及研究用國際與半導體候選資料
- Federal Reserve Economic Data（FRED）：研究用美國公債殖利率候選資料
- 公開資訊觀測站（MOPS）：台股事件簿公開資訊
