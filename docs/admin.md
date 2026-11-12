# 維護後台

[回到 README](../README.md) · [使用指南](usage.md) · [部署指南](deployment.md) · [維護後台](admin.md) · [開發指南](development.md)

**執行目錄：** 專案根目錄。

## 啟用後台

1. 在 BotFather 登記後台 HTTPS 網址與回呼 `https://<example.com>/auth/telegram`，保留 RS256 簽章。
2. 填入下列環境變數。

   | 變數 | 內容 |
   | --- | --- |
   | `ADMIN_TELEGRAM_CLIENT_ID` | BotFather 提供的 Client ID。 |
   | `ADMIN_TELEGRAM_CLIENT_SECRET` | BotFather 提供的 Client Secret。 |
   | `ADMIN_TELEGRAM_REDIRECT_URI` | 與 BotFather 登記完全相同的回呼網址。 |
   | `ADMIN_TELEGRAM_IDS` | 管理者的數字 Telegram user ID，以逗號分隔。 |
   | `ADMIN_SESSION_SECRET` | 由下列指令產生的 session 密鑰。 |

   ```sh
   uv run python -c "import secrets; print(secrets.token_urlsafe(32))"
   ```

3. 用 HTTPS 反向代理轉到部署主機的 `127.0.0.1:8081`，啟動後台：

   ```sh
   docker compose --profile admin up -d --build
   ```

首次啟用必須設定管理者，否則無人能新增維護者。

## 回報查閱與匯出

| 操作 | 方式 |
| --- | --- |
| 篩選 | 選原因、解析來源、模型或 Bot 版本，列表自動更新。 |
| 查看詳情 | 點回報原因。 |
| 複製單筆 | 詳情頁點「複製問題資料」。 |
| 複製／下載多筆 | 勾選回報或「全選本頁」，點「複製所選」或「下載 Markdown」。 |
| 手動複製 | 瀏覽器拒絕自動複製時，使用頁面提供的文字框。 |

- **格式：** 單筆與多筆共用 Markdown。
- **內容：** 草稿 ID、原因、Bot 版本、解析來源／模型、時區、解析當下、原句與解析結果。
- **缺漏：** 未提供的資料與使用者期望會明確標示，不推定正確結果。
- **回報者：** 不匯出 Telegram user ID 或帳號欄位；原句與解析結果中的人名保留。
- **範圍：** 一次最多 50 筆；換頁或變更篩選會清除選取。
- **截短：** 只能匯出回報送出時已保存的內容，無法還原截短部分。

## 人員權限

| 角色 | 查閱、複製與下載回報 | 管理維護者與備註 | 授予／撤銷方式 |
| --- | --- | --- | --- |
| 後台管理者 | 可以 | 可以 | 部署設定 `ADMIN_TELEGRAM_IDS`。 |
| 維護者 | 可以 | 不可以 | 管理者在「人員權限」新增／移除。 |

- 名稱與帳號於本人登入時更新；尚未登入時以管理者備註辨識。
- 移除維護者後，下次請求即失效，包含已登入的 session。
- 舊 `MAINTAINER_TELEGRAM_IDS` 已停用，維護者需由管理者重新加入。

變更管理者：更新 `ADMIN_TELEGRAM_IDS` 後重建後台容器。

```sh
docker compose --profile admin up -d --no-deps --force-recreate remindly-admin
```

- 新容器接手後，移除的管理者失去管理權限。
- 若該帳號仍列為維護者，仍可查閱回報。

## 資料保存

| 資料 | 路徑／儲存 | 權限 |
| --- | --- | --- |
| 回報與提醒 | 原提醒資料庫 | 後台唯讀，不能修改提醒或 AI Key。 |
| 維護者白名單、備註與登入身分 | `ADMIN_DATABASE_PATH`，預設 `data/admin/access.db` | 獨立 SQLite，需分開備份。 |
| Compose 後台資料 | `remindly-admin-data` volume，容器路徑 `/app/admin/access.db` | 持久化保存。 |
