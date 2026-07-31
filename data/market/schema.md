# Market Data JSON Schema

所有市場資料 Provider 最終都必須輸出相同的頂層 JSON 結構。Provider 可以依資料集需求擴充 `data`，但不可變更或省略共用欄位。

## 共用結構

```json
{
  "updated_at": "2026-01-01T00:00:00Z",
  "provider": "provider_name",
  "dataset": "dataset_name",
  "version": "1.0",
  "data": {}
}
```

以上內容只說明格式，不代表任何真實市場資料。

## 欄位定義

| 欄位 | 型別 | 必填 | 說明 |
| --- | --- | --- | --- |
| `updated_at` | string | 是 | 資料更新時間，使用 ISO 8601 格式；正式輸出建議使用 UTC 與 `Z` 時區標記。 |
| `provider` | string | 是 | 資料提供者的穩定識別名稱，例如 Provider 類別的 `name`。 |
| `dataset` | string | 是 | 資料集識別名稱，建議使用小寫 snake_case。 |
| `version` | string | 是 | Schema 版本，目前基礎版本為 `1.0`。 |
| `data` | object、array 或 null | 是 | 資料內容，可依資料集擴充；即使沒有內容也必須保留。 |

## 規則

- 所有五個頂層欄位都必須存在。
- `version` 表示資料格式版本，不代表資料版本或交易日期。
- `data` 是唯一可依資料類型自由擴充的欄位。
- Provider 的 `export()` 只建立可序列化的結構；檔案命名與寫入由未來的統一更新流程處理。
- 尚未取得資料時，可使用 `null`、空物件或空陣列，不可填入推測或模擬的市場數值。

## 空白範例

請參考 [`sample_market.json`](./sample_market.json)。該檔案只用於 Provider 與前端開發時確認欄位結構，不包含真實市場資料。
