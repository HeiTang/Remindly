# Changelog

Remindly 的重要變更會記錄在這裡。

## Unreleased

- 尚未有未發布變更。

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
