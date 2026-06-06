<p align="center">
  <img src="assets/readme-hero.svg" alt="Remindly" width="100%" />
</p>

<p align="center">
  <a href="#功能">功能</a> ·
  <a href="#技術">技術</a> ·
  <a href="#使用方式">使用方式</a> ·
  <a href="#部署">部署</a> ·
  <a href="#開發">開發</a> ·
  <a href="#版本紀錄">版本紀錄</a>
</p>

<p align="center">
  <img alt="Python" src="https://img.shields.io/badge/Python-3.12%2B-3776AB?style=flat-square&logo=python&logoColor=white" />
  <img alt="uv" src="https://img.shields.io/badge/uv-ready-111827?style=flat-square" />
  <img alt="SQLite" src="https://img.shields.io/badge/SQLite-persistent-003B57?style=flat-square&logo=sqlite&logoColor=white" />
  <img alt="Docker" src="https://img.shields.io/badge/Docker-Compose-2496ED?style=flat-square&logo=docker&logoColor=white" />
  <img alt="Runtime dependencies" src="https://img.shields.io/badge/runtime%20deps-stdlib%20only-22C55E?style=flat-square" />
</p>

<p align="center">
  Remindly 是一個 Telegram 提醒機器人，支援私聊與群組、自然語言建立提醒、缺少資訊時追問、確認後建立、到期主動通知與延後提醒。
</p>

## 功能

- 🔎 **自然語言建立提醒**：支援「明天下午三點提醒我倒垃圾」這類中文輸入，拆出提醒時間、提醒事項與參與者。

- 💬 **缺漏資訊追問**：時間不足時主動追問，並提供 09:00、12:00、15:00、18:00 快捷按鈕。

- 🛠️ **互動式提醒管理**：`/list` 依建立者分組，並支援今天、本週、我的、全部篩選。

- 👥 **群組與多人提醒**：支援私聊與群組，到期時可 mention 相關使用者。

- ⏰ **到期延後提醒**：提醒送出後可一鍵延後 10 分鐘、1 小時或明天同時間。


## 技術

| 類別 | 選型 | 說明 |
| --- | --- | --- |
| Runtime | Python 3.12+ | 使用標準函式庫為主，降低部署與維護成本。 |
| Package / Env | uv | 用 `uv run` 管理隔離環境與執行指令。 |
| Telegram | Bot HTTP API | 採 long polling，部署時不需要公開 webhook URL。 |
| Database | SQLite | 單機 MVP 友善，透過 volume 持久化資料。 |
| Scheduler | DB-driven worker | 固定掃描並 claim 到期提醒，避免重啟遺失。 |
| Deployment | Docker Compose | 同一份 `docker-compose.yml` 可供本機與伺服器部署使用。 |

```text
Telegram Update
  -> BotRouter
  -> CommandHandlers / CallbackHandlers
  -> ReminderService
  -> ReminderParser / DraftStore / ReminderRepository
  -> SQLite
  -> ReminderScheduler
  -> TelegramClient
```

## 使用方式

### Bot 指令

| 指令 | 用途 |
| --- | --- |
| `/start` | 開始使用。 |
| `/help` | 查看說明。 |
| `/remind 明天下午三點提醒我倒垃圾` | 建立提醒。 |
| `/list` | 列出未到期提醒。 |
| `/cancel R-8F3K` | 取消提醒。 |
| `/timezone Asia/Taipei` | 設定時區。 |

### 自然語言範例

```text
明天下午三點提醒我倒垃圾
明天 15:00 提醒我倒垃圾
3 小時後提醒我喝水
半小時後提醒我喝水
今晚提醒我洗衣服
明早提醒我買早餐
週五下午提醒我開會
提醒我明天下午三點倒垃圾
明天下午三點提醒我和 @alice 倒垃圾
```

### 群組使用

群組請用 `/remind@你的BotUsername 明天下午三點提醒我倒垃圾` 或 `@你的BotUsername 提醒我明天倒垃圾` 建立提醒；bot 追問時直接輸入下一句即可。

一般群組聊天不會被當成提醒，避免「提醒我...」這類對話誤觸 bot。

## 部署

### 環境變數

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

### Docker Compose

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

## 開發

### 本機啟動

建議使用 `uv`：

```sh
uv sync
uv run remindly
```

直接跑 module 也可以：

```sh
uv run python -m remindly
```

日常開發建議用 `uv run`。`uvx --from . remindly` 可能使用 tool cache，本機改動不一定立即反映。

### 測試

```sh
make check
```

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

### 專案分層

| 路徑 | 職責 |
| --- | --- |
| `src/remindly/bot/` | Telegram update routing、指令、callback、訊息回覆。 |
| `src/remindly/reminders/` | 提醒 domain model、parser、service、renderer、scheduler。 |
| `src/remindly/storage/` | SQLite repository、session stores、versioned migrations。 |
| `src/remindly/telegram/` | Telegram Bot API client 與 DTO。 |
| `src/remindly/app.py` | Composition root，組裝 dependencies 並啟動 polling/scheduler。 |

### GitOps 流程

```text
main -> dev -> feature/<name> -> PR -> dev -> PR -> main
```

Feature branch 合併到 `dev` 必須開 PR；`dev` 驗證通過後再以 release PR 合併回 `main`。

## 版本紀錄

- [CHANGELOG.md](CHANGELOG.md)

