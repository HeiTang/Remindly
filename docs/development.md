# 開發指南

[回到 README](../README.md) · [使用指南](usage.md) · [部署指南](deployment.md) · [維護後台](admin.md) · [開發指南](development.md)

**執行目錄：** 專案根目錄。

## 技術

| 類別 | 選型 | 說明 |
| --- | --- | --- |
| Runtime | Python 3.12+ | 以標準函式庫為主。 |
| Package / Env | uv | 用 `uv run` 管理隔離環境與執行指令。 |
| Telegram | Bot HTTP API | 採 long polling，部署時不需要公開 webhook URL。 |
| Database | SQLite | 透過 volume 持久化資料。 |
| Scheduler | DB-driven worker | 固定掃描並 claim 到期提醒，避免重啟遺失。 |
| Deployment | Docker Compose | 同一份 `docker-compose.yml` 可供本機與伺服器部署使用。 |

```text
Telegram Update
  -> BotRouter
  -> CommandHandlers / CallbackHandlers
  -> ReminderService
  -> ReminderParser / SemanticParser / DraftStore / ReminderRepository
  -> SQLite
  -> ReminderScheduler
  -> TelegramClient
```

## 本機啟動

使用 `uv`：

```sh
uv sync
uv run remindly
```

或直接執行 module：

```sh
uv run python -m remindly
```

- 開發使用 `uv run`；`uvx --from . remindly` 可能使用 tool cache，無法立即反映本機變更。

## 測試

```sh
make check
```

- 固定回歸案例：`tests/fixtures/time_regressions.jsonl`，由 `make check` 與 CI 執行。
- 新增案例：[資料集說明](../tests/fixtures/README.md)。

等同於：

```sh
uv run ruff check . --no-cache
uv run python -B -m unittest discover -s tests
uv run python -B scripts/smoke_flow.py
```

Smoke test 會模擬：

```text
提醒我明天倒垃圾
-> 點 09:00
-> 確認建立
-> /list
-> 查看提醒
-> 修改內容
-> 刪除提醒
```

## 專案分層

| 路徑 | 職責 |
| --- | --- |
| `src/remindly/bot/` | Telegram update routing、指令、callback、訊息回覆。 |
| `src/remindly/ai/` | 供應商 adapter、Key 加密與備援、語意解析及模型顯示名稱。 |
| `src/remindly/reminders/` | 提醒 domain model、parser、service、renderer、scheduler。 |
| `src/remindly/storage/` | SQLite repository、session stores、versioned migrations。 |
| `src/remindly/telegram/` | Telegram Bot API client 與 DTO。 |
| `src/remindly/app.py` | Composition root，組裝 dependencies 並啟動 polling/scheduler。 |
| `src/remindly/admin.py` | 維護後台、Telegram 登入與回報查詢。 |

## GitOps 流程

```text
main -> dev -> feature/<name> -> PR -> dev -> PR -> main
```

- Feature branch → `dev`：必須開 PR。
- `dev` → `main`：驗證通過後開 release PR。

更新 runtime dependencies 後同步 Docker 安裝清單：

```sh
uv export --locked --no-dev --no-emit-project --format requirements-txt --output-file requirements.txt
```

- AI 測試：`tests/test_ai.py`、`tests/test_groq.py`、`tests/test_ai_flow.py`。
- 使用模擬供應商與暫存 SQLite，驗證 REST 請求、結構化資料、Key 備援與 Telegram 流程，不消耗真實額度。
- 模型中文準確率需另用真實 Key 評估。

## Release Flow

1. 更新 `pyproject.toml`、`src/remindly/__init__.py`、`uv.lock` 的版本號。

2. 將 `CHANGELOG.md` 的 `Unreleased` 內容移到新版本區段。

3. 跑 `make check`，確認 lint、unit tests、smoke flow 都通過。

4. 依 GitOps 流程合併到 `main`。

5. 在 `main` 建立與版本一致的 tag，例如 `v0.5.0`，再發布正式 GitHub Release。

6. Publish container workflow 驗證已發布的 release、tag 來源位於 `main` 歷史及版本一致性，再重跑測試、建置兩個架構並發布到 GHCR。

補發既有正式版本映像使用同一流程，不移動 tag：

```sh
gh workflow run publish.yml --ref main -f tag=v0.5.0
```

只接受 `vX.Y.Z` 正式 release，不發布草稿／預發布版，也不產生 `latest` 標籤。
