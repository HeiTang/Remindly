# 時間解析回歸資料集

`time_regressions.jsonl` 每行是一個獨立 JSON 案例，可直接新增一行；預期結果由人核對，
不要執行解析器後把輸出自動存成預期值。實際使用遇到誤解析時，先補一個會失敗的案例再修正。

資料集由 `tests/test_time_corpus.py` 執行，既有 `make test`、`make check` 與 CI 會自動納入。
全部使用固定時間與臨時 SQLite，不讀正式資料庫、不連 Telegram 或 AI，也不需要 Key。

只跑資料集：

```sh
uv run python -B -m unittest discover -s tests -p 'test_time_corpus.py' -v
```

失敗訊息會顯示案例 `id`，以及解析入口、來源句或對話輪次。

## 案例格式

每筆必填 `kind`、唯一的 `id`、含 UTC offset 的 `now` 與 IANA `timezone`。
`at` 使用含 offset 的 ISO 日期時間；無法解析則填 `null`。

| kind | 用途與必要欄位 |
| --- | --- |
| `time` | `text`、`at`、`missing_time`、`past`、`existing_date`（可為 `null`）、`cross_entry_points`。檢查單獨補時間；開啟跨入口時，再檢查「時間提醒我買湯」、「提醒我在時間買湯」、「時間買湯」。有既有日期或倒裝時間的案例可關閉跨入口。 |
| `reminder` | 完整句子的 `text`、`title`、`at`、`missing`、`recurrence`、`error`、`participants`。事項必須完全相符；週期規則與參與者也要相符。無週期填 `null`，`error` 表示是否應拒絕無效週期。 |
| `conversation` | `turns` 陣列，各輪包含 `text`、`state`（`prompt`／`confirmation`）、`title`、`at`、`missing`、`question`（確認卡填 `null`）。逐輪檢查草稿保存，最後確認並檢查資料庫；確認前不得建立提醒。 |
| `edit` | `initial`（建立原提醒的完整句子）、修改輸入 `text`、`state`（`updated`／`prompt`）、修改後資料庫的 `at` 與 `title`。被拒絕的修改必須保留整筆原提醒。 |

測試使用者固定為 `@orange`，私聊 ID 為 `100`。對話各輪固定在該案例的 `now`，
用途是驗證解析與狀態合併；TTL 與時間經過由原有 session 測試負責。

日期追問案例包含 `9/17 23:55`、`09/17 23:55`、`09-17` → `23:55`、
`2026/09/17` → `23:55`，以及只回答 `23:55`。預期規則如下：

- 未指定年份的月日若已過，沿用下一年的規則；有指定年份時不改年份。
- 只給日期時繼續追問時分，下一輪保留該日期。
- 只給時分且沒有草稿日期時，取下一次該時刻；已有草稿日期則保留日期。
- 明確的過去日期不能進入確認建立流程，必須重新提供有效時間。

新增時間案例的範例：

```json
{"kind":"time","id":"tomorrow-afternoon-clock","now":"2026-10-23T12:10:00+08:00","timezone":"Asia/Taipei","text":"明天下午三點","at":"2026-10-24T15:00:00+08:00","missing_time":false,"past":false,"existing_date":null,"cross_entry_points":true}
```

## 維護原則

- 保留真實回報的完整句子與對話，不只抽出日期時間。
- 正常格式、缺漏追問、非法輸入、事項中的日期／時段文字都要涵蓋。
- 日期與時區要明寫，避免依賴執行測試當天或作業系統時區。
- 只有產品規則確定改變時才調整預期值，並說明理由；不能為了讓測試通過而改答案。
- 這份資料集驗證內建規則；不代表任意口語或 Gemini 回應的準確率。

`time_regressions.required_ids.txt` 保存原本 107 筆案例的 ID；測試會檢查它們全部存在，
避免刪除舊案例後用等量新案例取代。新增案例直接附加至 JSONL，不需改這份基準清單。

時間格式必須完整有效；超長數字、不完整冒號及目前不支援的秒數都需重新輸入，
不能只取合法前綴。早上／上午十二點是 00:00，中午十二點是 12:00；晚上十二點
需追問單一日期與 24 小時制時間。時間片語間以「或／或者／還是」列出選項時需追問，
事項中的日期與「或」則保留。DST 不存在或重複的牆上時間需追問；
明確經過分鐘／小時的相對時間以 UTC 計算，仍可跨越 DST。

`tests/test_time_boundaries.py` 另外以共用案例交叉驗證建立、追問、編輯與 AI 約束，
並覆蓋跨午夜、草稿重載、確認時已過期與快捷時間。AI 使用刻意不一致的模型回應替身，
驗證本機約束與確認流程；這些測試不呼叫 Gemini。
