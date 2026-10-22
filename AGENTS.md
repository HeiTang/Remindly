# Remindly 開發規範

遵循上層工作區規範；以下為本專案 AI 擴充的約束。

## AI 產品邊界

- 不區分免費／付費用戶，AI 為選用；未提供 Key 或關閉 AI 時使用內建規則解析。
- 平台不提供共用 Key，也不代付費用；只使用請求者的個人 Key，設定僅限私聊。
- AI 啟用後，一般私聊文字、群組觸發訊息及必要上下文會送至供應商；說明不得宣稱只傳已辨識的提醒。
- 建立提醒仍須使用者確認；確認卡從草稿解析紀錄顯示「使用 … 解析」，不依當下開關推測來源。

## 擴充 AI 供應商

目前正式啟用 Gemini。供應商透過 `ai/contracts.py` 的 `AiProvider` Protocol 接入，
不繼承 Gemini 實作。新增時只需實作 adapter 並在 `app.py` 註冊：

1. 提供 `id`、`name`、`model`，實作 `generate(key, system, prompt, schema, *, timeout)`。
   另實作 `model_display_name(key, *, timeout)`，由供應商 metadata 取得可讀名稱並快取；
   查詢失敗回傳 `None`，不得中斷已成功的解析。沒有查詢介面時也回傳 `None`。
   API 模型 ID 僅供請求與內部紀錄，不用作使用者標籤；renderer 不維護模型名稱對照表。
   名稱沿 `GeneratedResponse.model_display_name` 存入解析紀錄；缺少名稱時顯示「使用 AI 解析」。
2. 接收共用 JSON Schema（小寫 `type`、含 `null` 的型別陣列），自行轉成供應商 API 格式。
   成功回傳解析後的 JSON object；上游錯誤轉成固定的 `ProviderError` 類別，禁止帶入
   Key、請求 URL 或上游錯誤原文。
3. 在 composition root 的供應商 mapping 加入新實例。之後 `/ai add <供應商ID> <Key>`
   及設定畫面會使用同一份 mapping，不需另外維護供應商清單。

備援、冷卻、Key 加密、中文 prompt、schema、驗證及確認流程均共用。
`AiGateway` 回傳實際使用的供應商、模型 ID 與顯示名稱，語意解析器依賴 `SemanticGenerator` Protocol，
提醒 service 依賴 `SemanticParsing` Protocol；Bot 設定處理器由入口注入，不從解析器取出 Key store。

每組 Key 都綁定供應商；未知或未註冊的供應商不會收到請求，Key 也不會送給其他供應商。
同一個使用者不能把同一組 Key 重複綁定；要改所屬供應商須刪除後重加。
Schema v7 只新增供應商欄位，原有 v6 Key 標記為 `gemini`，加密內容、排序及冷卻狀態保留。
Schema v8 保存 AI 編輯時間的追問狀態；schema 與版本紀錄必須在同一交易內提交。
第二供應商的隔離與備援透過測試替身驗證；不代表已正式支援另一個雲端服務。
