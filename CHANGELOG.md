# Changelog

Remindly 的重要變更會記錄在這裡。

## Unreleased

### Added

- 預期結果為選填，最多 500 字，送出前可補充、修改或移除；後台詳情及單筆／多筆 Markdown 一起呈現，未填明示缺漏。schema v13 保留舊回報，先升級 Bot 再升級後台。
- 後台可填寫回報備註、標記未處理／處理中／已解決並篩選；管理者可批次移至回收區或還原。處理紀錄存於獨立權限資料庫，正式回報維持唯讀掛載，Markdown 包含狀態與備註。

- 後台支援批次修改狀態、關聯 GitHub 修復 PR／版本，以及原句、解析事項、預期結果與維護者備註的內容搜尋；搜尋條件會保留至分頁與詳情返回。修復關聯隨 Markdown 匯出，批次操作不覆蓋它。

### Changed

- 後台精簡常用與進階篩選，批次／匯出選單僅在選取回報後顯示；重整批次確認頁並統一 SVG 圖示，修正列表 Markdown 複製的網址取得。
- 新回報固定包含原句與解析結果，送出前顯示必要資料與身分資訊；移除附件選填開關。舊版未附資料的送出按鈕會先開啟新版預覽，重新確認後才保存。

## v0.6.0 - 2026-11-12

### Added

- **群組共用 AI**：管理員開放綁定，Key 持有人從群組按鈕進私聊選既有 Key，再由管理員同意啟用；不需手填群組 ID 或 Key ID。
- **群組授權管理**：授權綁定持有人，查核群組成員、Key 所有權及管理員身分；查詢失敗不變更。持有人可撤銷，管理員可關閉或解除綁定，刪 Key 立即清除引用，換綁需重新同意。
- **解析問題回報**：確認卡提供回報連結，私聊選原因、預覽選填附件與回報者資訊，確認送出才寫入 SQLite；依草稿去重。
- **選用維護後台**：Telegram OIDC 登入、即時篩選回報與詳情；回報者 ID／帳號僅在詳情呈現。
- **後台人員管理**：部署者以 `ADMIN_TELEGRAM_IDS` 指定管理者，可新增／移除維護者與編輯備註；人員名稱後方以藥丸顯示帳號與 ID，登入時更新身分資料。
- **Markdown 匯出**：詳情可複製單筆，列表可勾選多筆複製或下載 Markdown；最多 50 筆，保留解析當下與時區，排除回報者 ID／帳號欄位。

### Changed

- 群組只使用授權的單組 Key，不受個人 AI 開關影響，不改用任何人的其他備援 Key；觸發訊息與必要上下文送至該供應商，費用歸持有人，不傳全部群聊。
- 群組 AI 不可用時降級為內建解析，呼叫失敗會提示；私聊維持原有失敗處理。建立仍須發起者確認，確認卡顯示草稿的實際解析來源。
- 後台使用獨立權限 SQLite；每次請求檢查白名單，移除維護者後既有 session 的下次請求也失效。提醒資料庫唯讀掛載。
- README 精簡為功能、快速開始及文件入口；使用、部署、維護後台與開發說明分至 `docs/`。

### Fixed

- 群組另建提醒遇到 AI 失敗時隔離舊草稿，避免沿用上一筆事項與時間。
- Telegram OIDC 的數字字串 user ID 先正規化，再檢查原有白名單，避免合法管理者被誤拒。
- CSP 允許同源 Markdown 請求；瀏覽器拒絕自動複製時提供手動複製文字框。

### Upgrade notes

- 從 v0.5.0 升級時，提醒資料庫自動由 schema v9 升至 v12：v10 群組授權、v11 解析回報、v12 回報者 ID／帳號；既有提醒、設定與加密 Key 保留。
- 升級前備份提醒 SQLite，保留原本的 `AI_ENCRYPTION_KEY` 並分開備份；既有個人 Key 不需重新新增。群組 AI 預設關閉，不會自動共用任何個人 Key。
- 後台為選用的 Compose `admin` profile；需另設 HTTPS、Telegram OIDC 憑證、`ADMIN_SESSION_SECRET` 與初始 `ADMIN_TELEGRAM_IDS`。未啟用後台時 Bot 仍可獨立執行。
- 舊 `MAINTAINER_TELEGRAM_IDS` 已停用，既有維護者需由管理者重新加入。後台權限資料庫須與提醒資料庫分開，並備份 `remindly-admin-data` volume。
- 舊回報可能沒有回報者身分；Markdown 不含回報者欄位，但原句與解析結果中的人名仍保留。附件已截短的內容無法還原。
- 真實 Telegram OIDC、管理者身分與兩筆 Markdown 下載已在獨立假資料後台驗證。群組 AI 的真實 Telegram／供應商端到端驗收、多筆剪貼簿一致性與窄版版面仍未完成。

## v0.5.0 - 2026-11-05

### Added

- **選用個人 AI 解析**：支援 Gemini 3.5 Flash-Lite 與 Groq GPT-OSS 120B，理解中文口語、建立提醒、草稿追問／修正及修改下次時間；建立提醒仍須確認。
- **個人 Key 管理**：私聊 `/ai` 管理加密保存的 Key、供應商、開關及備援順序；平台不提供共用 Key。未設定 Key 或關閉 AI 時使用內建規則解析。
- **供應商備援與診斷**：最多嘗試三組 Key，共用 18 秒等待預算；依錯誤種類處理停用／冷卻，僅保存固定錯誤代碼，成功後清除。
- **解析來源標籤**：確認卡顯示實際解析紀錄中的模型名稱；名稱由供應商 metadata 取得，無名稱時顯示「使用 AI 解析」。
- **回歸與實測工具**：新增固定時間資料集、Gemini／Groq 錄製回應重播與手動 Groq API 評測；CI 不讀個人 Key 或呼叫 AI。
- **Docker 驗證**：CI 建置實際映像，在無網路容器驗證版本、加密依賴與 SQLite 初始化，不連線 Telegram 或 AI 供應商。

### Changed

- 建立、追問、編輯與 AI 約束共用時間文法，支援連字號日期與「這個／下個星期」等變形，統一無效、過去及歧義時間的處理。
- 新增 runtime `cryptography` 依賴；Docker 依有 hashes 的 `requirements.txt` 安裝。
- AI 編輯追問狀態持久化；補充輸入保留已確定的日期、事項、對象、週期及未解條件。

### Fixed

- 不完整時間片語不再以合法前綴覆寫模型完整時間；仍驗證明確日期、額外條件、多筆提醒與週期，防止漏掉條件或擅加重複提醒。
- 週期首次日期依週期計算；保留明確起始日期及「明早／明晚／後晚」，避免錯排日期或提前一天。
- 明確另建提醒時隔離舊草稿，避免模型誤標更新而沿用舊時間、週期或條件。
- 事項保留可可靠辨識的原文；消除已解決的多餘追問，保留必要條件與否定修正，對外追問使用本機文案。
- 草稿替換、確認建立及 migration 使用交易；寫入失敗可回滾，舊確認不能重複建立，確認時拒絕已過期時間。
- `/remind` 保存新草稿成功後才清理舊對話；週期首次時間檢查日光節約時間歧義。
- Groq 請求設定 User-Agent，避免預設 urllib 被拒絕；截斷回應轉為連線錯誤，沿用冷卻／備援。
- 固定週期排程測試時鐘，避免測試隨執行日期失敗。

### Upgrade notes

- SQLite 啟動時自動由 schema v5 升至 v9；部署前備份資料庫。升級保留既有提醒、草稿與設定，新增個人 AI Key、供應商、編輯追問及錯誤代碼欄位。
- 啟用 AI 須設定固定的 `AI_ENCRYPTION_KEY`，並在資料庫以外另外備份；遺失或更換後，原有 API Key 無法解密。未設定時仍可使用內建規則解析。
- 啟用 AI 後，一般私聊文字、群組觸發訊息及必要上下文會送到實際使用的供應商；費用與額度由個人帳號負擔。

## v0.4.0 - 2026-07-11

### Added

- **週期性提醒**：`每天 09:00`、`每週一、三、五 09:00`、`每個月 1, 18, 25 號 09:00`、`每年 12/25 08:00` 都能用一句自然語言建立，parser 產出結構化 `RecurrenceRule` 並在到期後自動排下一次（Phase 1-3）。
- **INTERVAL 週期**：`每 N 分鐘 / 小時 / 天 / 週`（含「日」/「週」/「周」別名）；下限 10 分鐘，低於此值直接單輪拒絕。首次觸發等於 `now + interval`（無 time-of-day 概念）。
- **週期到期新按鈕**：`[跳過下次]`（把 remind_at 推到 next_fire 的再下一次）與 `[取消整個系列]`。
- **週期性顯示**：確認卡 / `/list` / 詳情 / 到期訊息都會顯示 `重複：` 行；`/list` 對重複提醒加 `[重複]` 前綴。
- **無效 recurrence marker 單輪拒絕**：`每個月 45 號`、`每年 2/30`、`每 1 分鐘`、`每 0 分鐘`、超大 N（OverflowError）都會回具體錯誤原因（例：`『每年 2/30』日期無效，2 月最多 29 天。`）並要求重打，不建 draft、不追問。
- **啟動時 log 版本與關鍵設定**：Redeploy 後可從 log 立即確認版本已更新。

### Changed

- **週期性到期不再帶 snooze 按鈕**：只留 `[跳過下次][取消整個系列]`，避免與週期節奏語意重疊（每 15 分鐘 + snooze 10 分鐘只會造成節奏漂移）。一次性提醒的三顆延後按鈕維持不變。
- **`render_recurrence_error` 模板改為 reason 自帶語意**：monthly / yearly / interval 的錯誤訊息語氣一致（例：`『每個月 45 號』日期無效，需在 1-31 範圍。`、`『每 1 分鐘』太頻繁，最低支援 10 分鐘。`）。
- **`RecurrenceRule.hour / minute` 改為 Optional**：INTERVAL 不需要 time-of-day，non-interval 由 `next_fire / format_rule / serialize_rule` 三處早 raise 明確 ValueError 保護。

### Fixed

- **`command_body` 對 `bot_username=None` 不再 crash**（v0.3.1 的預存 bug）。
- **race condition**：`advance_pending / cancel / snooze` 的 SELECT-then-UPDATE 都加 `rowcount != 1` 檢查回 None，避免併發操作造成不一致狀態。
- **`snooze` 對 recurring reminder 支援**：原本 SQL 只接受 FIRING/FIRED，週期性提醒 scheduler tick 立刻轉回 PENDING 導致 snooze DOA；加 PENDING 到允許狀態。
- **cross-timezone `claim_due`**：改用 `julianday()` 做絕對時間比較，避免 lexicographic ISO 字串在跨時區時漏抓即將到期的提醒。
- **scheduler 用 reminder 自己的時區算 `next_fire`**：避免使用者「每天 09:00」設定漂到 scheduler 主機時區的 09:00。
- **`cancel` 回傳 fresh `updated_at`**：不再返回 stale timestamp。
- **`format_rule` 驗證比 `next_fire` 嚴格**：weekday 0-6、month_day 1-31、yearly (month, day) 必須實際存在（`is_valid_month_day` 允許 2/29），避免出現「每年 None/None」殘缺輸出。
- **`_format_interval_seconds` fail-fast**：非 60 秒倍數直接 raise ValueError，取代 silent truncate 造成 UI 與實際排程漂移。

### Persistence

- Schema v4：`reminders` 表新增 `recurrence` 欄位（JSON blob），存 `serialize_rule` 產出的字串。
- Schema v5：`reminder_drafts` 表新增 `recurrence` 欄位，確認前也能帶著規則。
- INTERVAL 新增不需 migration：直接沿用 `recurrence` JSON blob，`serialize_rule` 加分支寫 `interval_seconds`、`deserialize_rule` 反向即可。

## v0.3.1 - 2026-07-03

### Added

- 混合位置的自然語言時間：「在 7/30 21:00 提醒我 08/01 要去家樂福」等 9 種變體都能正確拆出時間與內容。
- 短日期 `M/D` 與中文日期 `M月D號 / M月D日`；`WEEKDAY_RE` 支援多個 `下` 前綴（`下下下下禮拜四` = 4 週後的週四）。
- 部分分鐘：`19 分要起立` = 當前小時 19 分，分鐘已過就滾到下一小時；不與 `分鐘` 撞。
- 相對時間新增 `週 / 周 / 星期 / 個星期 / 個月 / 个月` 單位；月份使用 calendar 加減，1/31 + 1 個月 clamp 到 2/28。
- Draft / EditSession 分等級 TTL：追問 10 分、確認 15 分（可用 `CONFIRMING_TTL_MINUTES` 調整）；scheduler 每次 tick 掃過期 draft / edit session，就地 editMessage 標為「已過期」+ 清按鈕。

### Changed

- Router 優先序反轉：新提醒意圖高於進行中 draft / edit session；覆蓋時舊 prompt 就地 editMessage 標為「已取消」+ 清按鈕，edit 失敗才退回訊息開頭 inline notice。
- 到期提醒延後按鈕文字改為動態的「明天 HH:MM」（帶入原提醒時段），移除「明天同時間」的語意歧義；後端 `1d` 延後改用 `tomorrow_from_now + original_time_of_day`，無論何時點都會兌現按鈕上寫的時間。
- `TelegramClient.send_message` / `edit_message_text` 回傳 `message_id | None`，供追問 / 修改流程綁定 message_id。

### Fixed

- Draft / EditSession 每次 save 都刷新 TTL，避免對話中途因初始時戳過期；draft 進入 confirming 狀態也能升級到較長 TTL。
- Router 對匿名管理員 / sender_chat（`from_user` 為 None）不再 crash。
- 延後按鈕使用後不再殘留（`show_snooze_result` 現在會清 inline keyboard），避免累積延後；`1d` 延後在拖延多天才點時不再產生過去時間。

### Persistence

- Schema v3：`reminder_drafts` 與 `edit_sessions` 新增 `prompt_message_id` 欄位，支援就地 editMessage 的 sweep / overwrite 流程。

## v0.3.0 - 2026-06-09

### Added

- `/groupmode` 群組自然語言模式，允許管理員 opt-in 一般文字觸發提醒。
- `/groupmode` 無參數或 `status` 會顯示狀態說明卡與 inline 切換按鈕。
- GitHub Actions CI，於 PR 與 `main` / `dev` push 時執行 `make check`。

### Changed

- 群組建立提醒需要明確使用指令或 @bot；bot 追問後可直接輸入下一句回答。
- 同步 package version 至 `0.3.0`，準備後續 release / package 發布流程。

### Fixed

- 選擇快捷時間後會移除原本的追問按鈕訊息。
- `今晚` 等時間片語若預設時間已過，會往下一個合理時間滾動。
- `週五下午` 使用較直覺的下午暫定時間，並保留精確時間追問。

### Docs

- README 補充部署檢查清單、SQLite 備份方式與 release flow。

## v0.2.0 - 2026-06-08

### Added

- 到期提醒訊息加入延後按鈕，可選 10 分鐘後、1 小時後或明天同時間。
- `/list` 支援 inline filters：今天、本週、我的、全部。
- 中文 deterministic parser 支援常見時間片語，例如 `今晚`、`明早`、`半小時後`、`週五下午`。

### Changed

- 到期提醒訊息會顯示 reminder short ID，讓 callback 操作更容易追蹤。

### Verified

- `make check` 通過 lint、unit tests 與 smoke flow。
