# Changelog

Remindly 的重要變更會記錄在這裡。

## Unreleased

### Added

- 到期提醒訊息加入延後按鈕，可選 10 分鐘後、1 小時後或明天同時間。
- `/list` 支援 inline filters：今天、本週、我的、全部。
- 中文 deterministic parser 支援常見時間片語，例如 `今晚`、`明早`、`半小時後`、`週五下午`。
- `/groupmode` 群組自然語言模式，允許管理員 opt-in 一般文字觸發提醒。

### Changed

- `/groupmode` 無參數或 `status` 會顯示狀態說明卡與 inline 切換按鈕。
- 群組建立提醒需要明確使用指令或 @bot；bot 追問後可直接輸入下一句回答。
- 到期提醒訊息會顯示 reminder short ID，讓 callback 操作更容易追蹤。

### Verified

- `make check` 通過 lint、unit tests 與 smoke flow。
