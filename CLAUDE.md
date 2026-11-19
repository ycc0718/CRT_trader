# CRT 訊號掃描器 — 給 Claude Code 的交接說明

使用者：Ycc（台灣，繁體中文）。這個 repo 是 TradingView 指標 `pine/CRT_Toolkit.pine`（Candle Range Theory，依「Yuki 的 CRT 教室」講義做的）的 **Python 移植版**，抓行情 → 跑同一套 CRT 邏輯 → 把掛單 / 進場 / 出場推到 Discord。目前用來做**模擬倉測試**。

## 檔案

| 檔案 | 內容 |
|---|---|
| `crt_engine.py` | CRT 邏輯，逐根 LTF K 執行，順序跟 Pine 一樣（改邏輯時兩邊要一起改，保持訊號一致） |
| `okx.py` | OKX 公開行情（不用 key）；`candles()`、`instruments()` |
| `mt5src.py` | MT5 行情（**唯讀**，不下單）：`candles()` / `tick_size()` / `resolve()` / `search()` 跟 okx.py 同介面，另有 `account()`（帳戶 + 所有持倉 / 掛單，含手動單）、`quote()`；伺服器時間用最新報價對本機時鐘換成 UTC |
| `notify.py` | Discord webhook（`?wait=true` 拿訊息 id，`edit()` 回頭在 TP 打勾）；也支援 Telegram |
| `scan.py` | 主程式：`python scan.py`（掃一次，用 `$CRT_DATA_DIR/sent.json` 去重，沒設就 `state/`）、`--loop`（常駐，每根 5 分 K 收盤後 8 秒掃）、`--stateless`（排程準時的雲端用）、`--replay [--csv]`（列出最近的訊號，跟 TradingView 對）、`--search sol`、`--hello`（連線測試訊息）；`--config config.mt5.json` 換成 MT5 行情（config 的 `source`：`okx` / `mt5`） |
| `dash.py` / `dash.html` | **本機 CRT 儀表板（現行部署）**（`python dash.py` → http://127.0.0.1:8011）：同一個 process 裡跑 `scan_once`（推 Discord、去重、卡片打勾），網頁顯示每個商品進行中的 CRT（掃蕩中 / 已確認 / 掛單 / 持倉）、K 線圖 + 價位線、訊號紀錄（點開是完整卡片）、回放統計、MT5 帳戶與手動持倉。商品選擇（打幾個字 → `/api/search` → `mt5src.search`：代號開頭 > 常用叫法 `ALIAS`（gold / 黃金 / nas…）> 代號中間 > 英文說明，市場報價有的排前面）和通知設定（事件開關、系統警示、Webhook、測試頻道 / MT5，仿 Stock_PY 8010 的設定視窗）存在 `~/crt-dash-data/settings.json`，蓋過 config 檔；Webhook 寫進 `.env`。POST 只收 `application/json`（擋跨站表單）。即時：掃描執行緒每秒 `refresh_live`（帳戶 / 持倉、報價、還沒收盤的那根 5 分 K），網頁每秒抓 `/api/live` 更新價格、浮動 R、圖上最後一根（訊號仍只看收盤 K）；`/api/state` 每 5 秒。停損顯示：`Engine.slTag`（進場後移到「保本 / TP1」）、`Engine.slWhy`（原始 SL 是 C2 掃點外、M3 新掃點、或 ④ 的 LTF OB 外）；出場事件帶 `sl_now / sl_tag`，Discord 進場卡片的 SL 行會改成「→ 保本 xxx」。新加的商品不補發舊訊號（`scan_once` 的 `new_sym`）；config 每次掃描重讀（不用重開）；資料 / log 在 `~/crt-dash-data` |
| `report.py` | **日報**：交易依進場時間歸到交易日（`report.time`，預設台灣 06:00 = 紐約收盤換日），每個商品進場幾筆、到 TP1–TP4 各幾筆、停損 / 保本 / 持倉中、累積 R（已實現，另列持倉浮動）；前一天進場、這段時間才結束的另外列。資料來自 `Engine.trades`（進場的 CRT；`crts` 結束就刪，所以另外留）與 `CRT.xT`（結束時間）。`dash.py` 的 `maybe_report` 照 8010：每天 06:00 後第一次掃描發、週日 / 週一早上不發（週二那份涵蓋上次到現在，最多 4 天）、狀態在 `~/crt-dash-data/report_state.json`；發到 `.env` 的 `DISCORD_WEBHOOK_URL_REPORT`（抄自 Stock_PY 同名，跟 8010 同一個日報頻道；沒設就發訊號頻道）。網頁「每日統計」可看今天 / 前 6 天，設定視窗可改時間、週末、日報 Webhook、立即發送今日日報。Discord embed 有 6000 字 / 欄位 1024 字限制，`embed()` 會分欄、超過就寫「看儀表板」 |
| `tg_collector/` | **給另一台電腦的 Telegram 收集器**（使用者要在另一台收集 TG、這台只放交易 / 訊號，2026-11-20）：整個資料夾複製過去就能跑，只要 Python（不用 MT5）。`collector.py`（`Collector` 類別：登入、抓訊息、合併編輯版本、寫 `raw.json` + `status.json`、`--loop`、`--autostart on/off` 在「啟動」資料夾放 `YukiCollector.cmd`）、`1_安裝.bat`～`5_取消開機自動.bat`（UTF-8 無 BOM、CRLF）、`操作說明.txt`（UTF-8 BOM）。輸出寫到 `OUTPUT_DIR`（雲端同步資料夾或這台的 Windows 共用資料夾）；登入檔在它自己的 `data\`（不放同步資料夾）。主專案的 `tg_collect.py` 也用同一支 `collector.py`（不要複製兩份）。這台在通知設定填「Yuki 訊息資料夾」→ `.env` 的 `YUKI_INBOX`，`yuki_loop` 進「遠端模式」：每分鐘看 `raw.json` 有沒有變（`tg_collect.pull_inbox`，解析得了才換），有變就重整；`status.json` 超過 20 分鐘沒更新 → 上方顯示「沒在更新」 |
| `tg_collect.py` / `tg_login.bat` | **收集 Yuki 的 Telegram 訊號**（這台自己收集的模式；另一台收集時用不到）（Yuki 在 TG 發，延遲、過 TP 才發，有激進 / 標準，只寫到 TP 幾、沒寫後續保本還停損）。用使用者自己的 Telegram 帳號唯讀（Telethon user API；bot 進不了別人的頻道）。第一次雙擊 `tg_login.bat`：api_id / api_hash（my.telegram.org）→ 手機驗證碼 → 選頻道 → 抓全部歷史。`.env`：`TG_API_ID`、`TG_API_HASH`、`TG_CHAT`、`TG_CHAT_NAME`。`dash.py` 的 `yuki_loop` 每 5 分鐘抓最近 300 則（新訊息 + 事後編輯，舊文字留在 versions），有變動就重跑 `yuki_report.build` |
| `yuki_parse.py` | 分類（signal / result / other）+ 抓欄位（#編號 → 商品 / TF / C2 時間、激進 / 標準、方向、掛單 or 進場價、SL、TP1–4（只認「TP1: 價格」）、觸發來源 → ①～⑧、KL、Model、TP 打到幾）。**規則是照我們卡片格式猜的，還沒看過 Yuki 實際訊息**：第一次抓到資料後先看 `unparsed.md`，照實際格式改 |
| `yuki_report.py` | 整理成 `yuki_vs_ours.md`（總覽、我們沒進場的原因 → 該調哪裡、依商品、Yuki 結果、逐筆不一致含原文、同一張但不一樣、我們多的）+ `signals.csv` + `unparsed.md`。對照：Yuki 的時間框架 / 激進或標準各跑一次引擎（`run_ours`，抓到最早訊號前 10 天），用 `Engine.history`（每個 CRT，含沒進場、被過濾的）以「同商品、同方向、C2 差 ≤ 1 根」對，沒時間就用發文前 3 天內 SL / 進場價最近的。自測：把我們自己的進場卡片當 Yuki 訊息餵進去 → 12/12 同一張、價差 0 |
| `config.json` | OKX 用：商品、時間框架、各開關（`params` 對應 Pine 的 input，沒寫的用 `crt_engine.Params` 預設） |
| `config.mt5.json` | 本機 MT5 用：`mt5_path`、預設商品（Exness 帶 `m`：XAUUSDm、EURUSDm、GBPUSDm、USDJPYm、USTECm、US500m；網頁上改的優先）、`ltf_bars` 3000、`htf_bars` 600、策略 `params` |

GitHub Actions 已停用（2026-11-19，使用者不用了，workflow 已刪；cron-job.org 那邊的觸發要使用者自己刪）。`--stateless` 是當時雲端排程用的，留著沒用到。

環境變數：`DISCORD_WEBHOOK_URL`（必要；本機放 `.env`，已 gitignore）、`SYMBOLS`（例如 `BTC,ETH,SOL`，會自動補成 `-USDT-SWAP`）、`TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID`（選用）、`CRT_DATA_DIR`、`CRT_PORT`、`MT5_PATH`（蓋過 config 的 `mt5_path`）。

## 本機部署（2026-11-18 起）

- 使用者的 Windows 電腦，跟 `Stock_PY`（`fex_upsidedown` 逆勢攤平移植過去的那套）放一起：`Stock_PY\start_local.ps1`（桌面捷徑「Nexus 儀表板」）會用 `CRT_trader\venv` 開 `dash.py` 在 **8011**——原本逆勢攤平的埠；逆勢攤平帳號爆倉，已停用。
- 行情來自 `C:\Users\user\claude\MT5\MT5_Strategy1`（Exness 模擬帳號，原逆勢攤平那個），**程式不下單**，使用者看訊號自己手動下在這個帳號。
- Discord 用原本逆勢攤平的頻道（`.env` 的 `DISCORD_WEBHOOK_URL`，值抄自 `Stock_PY/.env` 的 `DISCORD_WEBHOOK_URL_AVG`）。
- venv：`py -3.11 -m venv venv`、`venv\Scripts\pip install -r requirements.txt`（Windows 沒有 IANA 時區資料，`tzdata` 一定要裝）。

## 接下來要做（使用者的計畫）

0. **Yuki 對照**（2026-11-19 開始）：等使用者跑 `tg_login.bat` → 看 `~/crt-dash-data/yuki/unparsed.md` 修 `yuki_parse.py` → 看 `yuki_vs_ours.md` 找出我們跟 Yuki 不一樣的地方再調引擎。**Yuki 的訊息是別人的內容：只存本機 `~/crt-dash-data/yuki/`，不要 commit、不要貼進 repo / artifact（這個 repo 是公開的）**；`tg.session` 等於使用者 Telegram 帳號登入，絕不外流。
1. **訊號微調**：使用者說還有很多地方要調。調參數改 `config.mt5.json` 的 `params`（儀表板下一次掃描就套用）；改判斷邏輯時 `crt_engine.py` 和 Pine 要一起改，並用 `--replay` 前後比對。
2. **不要做自動下單**：使用者明講只要通知，看到訊息自己去 MT5 手操。`mt5src.py` 只能有讀的函式。
3. 外匯資料源已選 **MT5**（OANDA 沒做）。HTF 已改成用 K 線清單 bisect 找「包含這根 LTF 的 HTF K」和前一根（`HTF._find`），週末 / 每日休市都對；幣圈結果跟原本時間除法逐筆相同（BTC / ETH / SOL 1200 根 5 分 K 比過）。這是資料對齊，不是交易邏輯，Pine 不用改（`request.security` 本來就照實際 K 線）。
4. 注意：MT5（Exness）日線是 UTC 00:00 換日，TradingView 外匯日線是紐約 17:00；PDH / PDL、日線 OB / FVG 會跟 TradingView 外匯圖不一樣，以 MT5 為準（使用者在 MT5 下單）。

## 對訊號時已知可能不一致的地方

- `ta.pivothigh/pivotlow` 遇到相同高 / 低點的判斷方式（這裡用左邊可相等、右邊要嚴格）
- 掃描器只看最近的 5 分 K（OKX 約 4 天、MT5 3000 根約 2 週），很早以前形成的 KL 區塊可能不同
- TradingView 即時 K 會在收盤前觸發成交；掃描器只看收盤的 K

## 訊號規則重點（改程式前先看懂）

- 進場訊號 ①～⑧，全部同時監看，誰先觸發就用誰；① ④ ⑧ 是收盤市價，② ③ ⑤ ⑥ ⑦ 是限價單
- ① 只在 CISD「第一次」帶缺口突破那根進；如果那時不能進場，又收回 CISD 另一側、而且沒有 ② 掛單可以接 → ① 重新生效
- 掛單價出現時價格已經過了 → 不發掛單通知，下一根開盤直接成交（進場卡片會寫明）
- **掛單通知只發最近的那一張**（2026-11-19 使用者要求）：成交本來就是「這根 K 碰到的價位裡最近的」（`best`），遠的永遠輪不到（掛單不會單獨失效，只會整個 CRT 一起取消），所以 `syncOrder` 每根 K 最後只通知 `pendingLv` 的第一個（`actLv`），有更近的新價位才發「🔁 改掛單」；比較遠的寫在卡片「📎 比較遠的價位」。取消只針對 `actLv`，卡片帶 CRT 編號（`tagId`，含多 / 空）、原因、「接下來」（CRT 結束 / 同一個 CRT 還在等新訊號）。掛單卡片附「⛔ 沒成交前遇到這些就取消」（`cancelConds`：到對側、過掃點、HTF 收在區間外（M3）、逾時時間、被新 CRT 取代）。這是通知層的改動，進出場邏輯沒變（6 個 MT5 商品 3000 根 5 分 K 新舊引擎 entry / exit / stats 逐筆相同）；Pine 的 alert 還是每個價位各發一次，沒改
- TP1 = 區間 50%、TP2 = 對側、TP3 / TP4 = 對側外面的流動性；預設各平 25%，到 TP1 停損移到保本
- TP1 已經在進場價後面 → 略過 TP1，部位併到 TP2，保本改在 TP2
- RR 預設不擋進場（`min_rr = 0`），只顯示

## 慣例

- 對使用者用繁體中文、白話；程式註解中文
- 改 `crt_engine.py` 的交易邏輯時，`pine/CRT_Toolkit.pine` 也要同步改（反過來也是）
