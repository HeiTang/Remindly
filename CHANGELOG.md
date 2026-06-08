# Changelog

Remindly 的重要變更會記錄在這裡。

## Unreleased

- 尚未有未發布變更。

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
