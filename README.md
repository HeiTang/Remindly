<p align="center">
  <img src="assets/readme-hero.svg" alt="Remindly" width="100%" />
</p>

<p align="center">
  <a href="#功能">功能</a> ·
  <a href="#快速開始">快速開始</a> ·
  <a href="#使用方式">使用方式</a> ·
  <a href="#部署">部署</a> ·
  <a href="#維護後台">維護後台</a> ·
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
  Telegram 提醒機器人：支援私聊與群組、自然語言解析、確認建立與到期通知。
</p>

## 功能

| 分類 | 功能 | 說明 |
| --- | --- | --- |
| 建立 | 🔎 自然語言解析 | 從中文句子取得時間、事項與提醒對象。 |
| 建立 | 🧠 AI 解析（選用） | 自備 Gemini／Groq Key，支援口語與草稿修正；未設定時用內建規則。 |
| 建立 | 🔁 週期性提醒 | 支援每日、每週、每月、每年及固定間隔，到期後排下一次。 |
| 確認 | 💬 缺漏追問 | 缺少資訊時追問，確認後才建立；缺少時間可用快捷按鈕。 |
| 管理 | 🛠️ 提醒列表 | 依建立者分組，可篩選今天、本週、我的或全部。 |
| 管理 | ⏰ 延後與跳過 | 一次性提醒可延後；週期性提醒可跳過下次或取消系列。 |
| 群組 | 👥 多人提醒 | 到期時 mention 相關使用者。 |
| 群組 | 🧭 自然語言模式 | 預設需指令或 @bot；管理員可開啟一般文字觸發。 |
| 維護 | 🐛 解析回報 | 回報附原句與解析結果；後台可備註、標記狀態、刪除／還原與匯出。 |

## 快速開始

**需求：** 專案程式碼、Docker Compose、BotFather 提供的 Bot token。

**執行目錄：** 專案根目錄。

1. 首次部署時建立設定檔；已有 `.env` 可略過複製。

   ```sh
   cp .env.example .env
   ```

2. 編輯 `.env`，填入 `TELEGRAM_BOT_TOKEN` 與 `BOT_USERNAME`。
3. 啟動 Bot 並查看 logs：

   ```sh
   docker compose up -d --build
   docker compose logs -f remindly
   ```

4. 在 Telegram 私聊 Bot，輸入「明天下午三點提醒我倒垃圾」，核對確認卡後建立提醒。

- Bot 使用 long polling，不需要公開 webhook 網址。
- AI 與維護後台皆為選用；未設定 AI Key 時使用內建規則。

## 使用方式

- [Bot 指令](docs/usage.md#bot-指令)與[自然語言範例](docs/usage.md#自然語言範例)
- [個人 AI 設定](docs/usage.md#個人-ai-設定選用)：新增 Key、開關、備援順序、費用與資料傳送
- [群組使用](docs/usage.md#群組使用)：群組 AI 授權與自然語言模式
- [回報解析問題](docs/usage.md#回報解析問題)：必要資料、選填預期結果與回報者資訊告知

## 部署

- [部署檢查清單與環境變數](docs/deployment.md#部署檢查清單)
- [啟用個人 AI](docs/deployment.md#啟用個人-ai選用)：加密主金鑰的產生與保存
- [Docker Compose](docs/deployment.md#docker-compose)、[GHCR 映像](docs/deployment.md#ghcr-發布映像)與[SQLite 備份](docs/deployment.md#sqlite-備份)

## 維護後台

- [啟用後台](docs/admin.md#啟用後台)：Telegram 登入、HTTPS 與初始管理者
- [回報查閱與匯出](docs/admin.md#回報查閱與匯出)：篩選、詳情、單筆／多筆複製與 Markdown 下載
- [人員權限](docs/admin.md#人員權限)與[資料保存](docs/admin.md#資料保存)

## 開發

- [技術選型與處理流程](docs/development.md#技術)、[本機啟動](docs/development.md#本機啟動)與[測試](docs/development.md#測試)
- [專案分層](docs/development.md#專案分層)、[GitOps 流程](docs/development.md#gitops-流程)與[Release Flow](docs/development.md#release-flow)

## 版本紀錄

- [CHANGELOG.md](CHANGELOG.md)
