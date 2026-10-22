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
  <img alt="CI" src="https://github.com/HeiTang/Remindly/actions/workflows/ci.yml/badge.svg" />
  <img alt="Python" src="https://img.shields.io/badge/Python-3.12%2B-3776AB?style=flat-square&logo=python&logoColor=white" />
  <img alt="uv" src="https://img.shields.io/badge/uv-ready-111827?style=flat-square" />
  <img alt="SQLite" src="https://img.shields.io/badge/SQLite-persistent-003B57?style=flat-square&logo=sqlite&logoColor=white" />
  <img alt="Docker" src="https://img.shields.io/badge/Docker-Compose-2496ED?style=flat-square&logo=docker&logoColor=white" />
  <img alt="Key encryption" src="https://img.shields.io/badge/key%20encryption-Fernet-22C55E?style=flat-square" />
</p>

<p align="center">
  Remindly 是一個 Telegram 提醒機器人，支援私聊與群組、自然語言建立提醒、缺少資訊時追問、確認後建立、到期主動通知與延後提醒。
</p>

## 功能

- 🔎 **自然語言建立提醒**：支援「明天下午三點提醒我倒垃圾」這類中文輸入，拆出提醒時間、提醒事項與參與者。

- 🧠 **自帶 Gemini 語意解析**：個人可設定多組 API Key，以 Gemini 3.5 Flash-Lite 理解口語、不同日期及草稿修正；不明確時追問，仍需確認才建立。未設定或關閉 AI 時使用原本規則解析。

- 🔁 **週期性提醒**：`每天 09:00`、`每週一 09:00`、`每個月 1, 18, 25 號 09:00`、`每年 12/25 08:00`、`每 15 分鐘 / 每 2 小時` 都能一句話建立，到期後自動排下一次。內建解析遇到無效語法會直接拒絕；AI 解析遇到缺漏或歧義則追問。

- 💬 **缺漏資訊追問**：缺少資訊時主動追問；內建解析在日期明確但缺少時間時，提供 09:00、12:00、15:00、18:00 快捷按鈕。

- 🛠️ **互動式提醒管理**：`/list` 依建立者分組，並支援今天、本週、我的、全部篩選。週期性提醒會以 `[重複]` 前綴標示。

- 👥 **群組與多人提醒**：支援私聊與群組，到期時可 mention 相關使用者。

- 🧭 **群組自然語言模式**：群組預設需用指令或 @bot；管理員可選擇開啟一般文字觸發。

- ⏰ **到期延後 / 跳過**：一次性提醒可一鍵延後 10 分鐘、1 小時或明天同時間；週期性提醒直接提供「跳過下次」與「取消整個系列」，避免和自然節奏語意重疊。


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
  -> ReminderParser / SemanticParser / DraftStore / ReminderRepository
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
| `/groupmode on\|off\|status` | 查看或切換群組自然語言模式。 |
| `/ai` | 私聊管理個人 Gemini Key、開關及備援順序。 |

### 個人 AI 設定（選用）

**不設定也能用。** 所有使用者功能相同，不分免費／付費；預設使用內建規則解析。
想用 AI 理解口語提醒，可自備 Gemini API Key，平台不提供 Key。

**開啟：** 與 Bot 私聊，輸入：

```text
/ai add <你的 Gemini API Key>
```

首次新增後自動開啟；若之前手動關閉過，請再輸入 `/ai on`。
接著直接說「明晚八點叫我拿包裹」，確認後才會建立提醒。
確認卡會標示「使用 … 解析」，方便核對本次使用的模型或內建規則。

| 想做什麼 | 指令 |
| --- | --- |
| 查看狀態與 Key ID | `/ai` |
| 關閉／重新開啟 AI | `/ai off` ／ `/ai on` |
| 新增備援 Key | `/ai add <另一組 Key>` |
| 將某組 Key 優先使用 | `/ai move <Key ID> 1` |
| 刪除 Key | `/ai delete <Key ID>` |

- **費用自己掌握：** 依你的 Google 專案計費；付費專案可能直接收費，不會先跳出付費確認。同專案多組 Key 共用額度。
- **資料會傳出：** Key 經 Telegram 傳至 Bot 加密保存；啟用後，一般私聊文字、群組觸發訊息及必要上下文會送至 Google。
- **AI 暫時不能用：** 草稿會保留；稍後重試，或 `/ai off` 後重送，改用內建解析。

### 自然語言範例

一次性：

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

週期性：

```text
每天 09:00 提醒我吃藥
每週一、三、五 09:00 提醒我運動
每個月 1, 18, 25 號 09:00 提醒我繳卡費
每年 12/25 08:00 提醒我聖誕節
每 15 分鐘提醒我喝水
每 2 小時提醒我看遠方
每 3 天提醒我澆花
```

### 群組使用

群組預設請用 `/remind@你的BotUsername 明天下午三點提醒我倒垃圾` 或 `@你的BotUsername 提醒我明天倒垃圾` 建立提醒；bot 追問時直接輸入下一句即可。

一般群組聊天預設不會被當成提醒，避免「提醒我...」這類對話誤觸 bot。

管理員可在群組輸入 `/groupmode` 查看狀態卡，卡片會寫清楚開關意義，並顯示「開啟」或「關閉」按鈕。

Telegram 的指令選單點選 `/groupmode` 時可能會直接送出，這是正常行為；送出後再用卡片按鈕切換即可。也可以直接輸入：

```text
/groupmode status
/groupmode on
/groupmode off
```

開啟後，群組才會接受較明確的提醒句型，例如：

```text
提醒我明天倒垃圾
明天提醒我倒垃圾
週五下午提醒我開會
明天下午三點提醒 @alice 倒垃圾
```

若 BotFather 的 Group Privacy 沒關，bot 仍收不到一般群組訊息。請到 `@BotFather` 執行 `/setprivacy`，選擇 bot 後設為 `Disable`；若原本已在群組，必要時把 bot 移除後重新加入。

## 部署

### 部署檢查清單

1. 在 BotFather 建立 bot 並取得 `TELEGRAM_BOT_TOKEN`。
2. 設定 `BOT_USERNAME`，群組中的 `/command@bot` 與 `@bot` 觸發會用到。
3. 建立 `.env` 或在部署平台設定必要環境變數。
4. 執行 `docker compose up -d --build`。
5. 用 `docker compose logs -f remindly` 確認 long polling 和 scheduler 正常啟動。
6. 群組若要免 `/remind` / `@bot`，到 BotFather 將 Group Privacy 設為 `Disable`，再於群組使用 `/groupmode` 開啟。
7. 部署後先在私聊和目標群組各建立一筆測試提醒。

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
| `AI_ENCRYPTION_KEY` | AI 設定必填 | 無 | Fernet 主金鑰，加密個人 Gemini Key；需固定並另行備份。 |

### 啟用個人 AI（選用）

若要讓使用者自行設定 AI Key，管理員需先產生加密主金鑰，填入 `.env` 或部署環境的
`AI_ENCRYPTION_KEY`，再重啟。這是伺服器用來保護已存 Key 的加密金鑰，並非供應商 API Key；
一般使用者不需要產生或設定它。

```sh
uv run python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

這把主金鑰要固定保存，並在資料庫以外另外備份。換掉或遺失後，原有 API Key 無法解密；
不會自動以新主金鑰覆蓋。未設定時 Bot 仍可使用內建規則解析，但不能新增或使用 AI Key。

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

### SQLite 備份

執行備份：

```sh
make backup-db
```

備份檔會寫到同一個 volume：`/app/data/reminders.backup.db`。這個 target 使用 Python 標準函式庫的 SQLite backup API，不需要在 image 裡額外安裝 `sqlite3` CLI。

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
| `src/remindly/ai/` | 供應商 adapter、Key 加密與備援、語意解析及模型顯示名稱。 |
| `src/remindly/reminders/` | 提醒 domain model、parser、service、renderer、scheduler。 |
| `src/remindly/storage/` | SQLite repository、session stores、versioned migrations。 |
| `src/remindly/telegram/` | Telegram Bot API client 與 DTO。 |
| `src/remindly/app.py` | Composition root，組裝 dependencies 並啟動 polling/scheduler。 |

### GitOps 流程

```text
main -> dev -> feature/<name> -> PR -> dev -> PR -> main
```

Feature branch 合併到 `dev` 必須開 PR；`dev` 驗證通過後再以 release PR 合併回 `main`。

Runtime 新增 `cryptography` 供 Key 加密。更新 runtime dependencies 後同步 Docker 安裝清單：

```sh
uv export --locked --no-dev --no-emit-project --format requirements-txt --output-file requirements.txt
```

`tests/test_ai.py` 與 `tests/test_ai_flow.py` 使用模擬 Gemini 回應及暫存 SQLite，檢查 REST
請求、結構化驗證、Key 備援與 Telegram 完整流程，不消耗真實 API 額度。這些檢查不代表
已量測模型中文準確率；實際模型表現需另用真實 Key 驗證。

### Release Flow

1. 更新 `pyproject.toml`、`src/remindly/__init__.py`、`uv.lock` 的版本號。
2. 將 `CHANGELOG.md` 的 `Unreleased` 內容移到新版本區段。
3. 跑 `make check`，確認 lint、unit tests、smoke flow 都通過。
4. 依 GitOps 流程合併到 `main`。
5. 在 `main` 建立 tag，例如 `v0.3.0`，再建立 GitHub Release。

## 版本紀錄

- [CHANGELOG.md](CHANGELOG.md)

