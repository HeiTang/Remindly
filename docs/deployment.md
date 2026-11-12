# 部署指南

[回到 README](../README.md) · [使用指南](usage.md) · [部署指南](deployment.md) · [維護後台](admin.md) · [開發指南](development.md)

**執行目錄：** 專案根目錄。

## 部署檢查清單

1. 在 BotFather 建立 bot 並取得 `TELEGRAM_BOT_TOKEN`。

2. 設定 `BOT_USERNAME`，群組中的 `/command@bot` 與 `@bot` 觸發會用到。

3. 建立 `.env` 或在部署平台設定必要環境變數。

4. 執行 `docker compose up -d --build`。

5. 用 `docker compose logs -f remindly` 確認 long polling 和 scheduler 正常啟動。

6. 群組若要免 `/remind` / `@bot`，到 BotFather 將 Group Privacy 設為 `Disable`，再於群組使用 `/groupmode` 開啟。

7. 部署後先在私聊和目標群組各建立一筆測試提醒。

## 環境變數

```sh
cp .env.example .env
```

| 變數 | 必填 | 預設 | 說明 |
| --- | --- | --- | --- |
| `TELEGRAM_BOT_TOKEN` | 是 | 無 | Telegram Bot token。 |
| `BOT_USERNAME` | 否 | 無 | Bot username，群組 @bot 與 `/command@bot` 判斷用。 |
| `DATABASE_PATH` | 否 | `data/reminders.db` | SQLite DB 路徑。 |
| `DEFAULT_TIMEZONE` | 否 | `Asia/Taipei` | 預設時區。 |
| `POLL_TIMEOUT_SECONDS` | 否 | `25` | Telegram long polling timeout。 |
| `SCHEDULER_INTERVAL_SECONDS` | 否 | `10` | 到期提醒掃描間隔。 |
| `DRAFT_TTL_MINUTES` | 否 | `10` | 草稿與修改 session 過期時間。 |
| `LOG_LEVEL` | 否 | `INFO` | Logging level。 |
| `AI_ENCRYPTION_KEY` | AI 設定必填 | 無 | Fernet 主金鑰，加密個人 AI Key；需固定並另行備份。 |

## 啟用個人 AI（選用）

1. 產生伺服器加密主金鑰：

   ```sh
   uv run python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
   ```

2. 填入 `.env` 或部署環境的 `AI_ENCRYPTION_KEY`，再重啟 Bot。

- 這是伺服器加密已存 Key 的主金鑰，非供應商 API Key；一般使用者不需設定。
- 主金鑰須固定保存，並與資料庫分開備份。
- 更換或遺失後，舊 Key 無法解密；系統不會自動覆寫。
- 未設定時仍可用內建解析，但不能新增或使用 AI Key。

## Docker Compose

```sh
docker compose up -d --build
```

常用操作：

| 操作 | 指令 |
| --- | --- |
| 查看 logs | `docker compose logs -f remindly` |
| 重啟服務 | `docker compose restart remindly` |
| 停止服務 | `docker compose down` |
| 重新 build | `docker compose up -d --build` |

SQLite 會存在 named volume `remindly-data`，container 內 DB 路徑固定為 `/app/data/reminders.db`。

## 回報維護後台（選用）

後台為選用服務；Telegram 登入、管理者設定與維護者授權請見[維護後台指南](admin.md)。

## GHCR 發布映像

| 項目 | 規則 |
| --- | --- |
| 映像 | `ghcr.io/heitang/remindly`。 |
| 架構 | `linux/amd64`、`linux/arm64`；各自在原生 runner 建置並執行無網路自檢。 |
| 標籤 | 自檢通過後建立 `vX.Y.Z`、`sha-<完整來源提交>`；既有版本標籤不覆寫。 |
| Digest | 記錄在 Publish container workflow 摘要。 |
| 依賴更新 | 基底映像或依賴更新須發布新版本。 |
| 公開拉取 | 首次發布後，擁有者需在 package settings 設為 Public；不需 Portainer registry 憑證。 |
| 發布憑證 | 使用 `GITHUB_TOKEN`，不需個人 registry token。 |

正式部署改用摘要中的固定 digest，例如：

```yaml
services:
  remindly:
    image: ghcr.io/heitang/remindly@sha256:<digest>
```

- 替換既有 service 的 `image`，移除 `build` 與 `pull_policy: build`；保留環境變數與 `remindly-data`。
- 啟用後台時，`remindly-admin` 使用相同 digest，並移除 `pull_policy: never`。
- 開發用 Compose 仍可從原始碼建置；映像發布不會觸發部署。
- 升級前備份資料庫與 AI 主金鑰；回滾前確認 DB 相容性。

## SQLite 備份

執行備份：

```sh
make backup-db
```

- 備份位置：同一 volume 的 `/app/data/reminders.backup.db`。
- 使用 SQLite backup API，不需安裝 `sqlite3` CLI。
