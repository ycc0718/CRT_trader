# CRT 訊號掃描器 — 給 Claude Code 的交接說明

使用者：Ycc（台灣，繁體中文）。這個 repo 是 TradingView 指標 `pine/CRT_Toolkit.pine`（Candle Range Theory，依「Yuki 的 CRT 教室」講義做的）的 **Python 移植版**，抓行情 → 跑同一套 CRT 邏輯 → 把掛單 / 進場 / 出場推到 Discord。目前用來做**模擬倉測試**。

## 檔案

| 檔案 | 內容 |
|---|---|
| `crt_engine.py` | CRT 邏輯，逐根 LTF K 執行，順序跟 Pine 一樣（改邏輯時兩邊要一起改，保持訊號一致） |
| `okx.py` | OKX 公開行情（不用 key）；`candles()`、`instruments()` |
| `notify.py` | Discord webhook（`?wait=true` 拿訊息 id，`edit()` 回頭在 TP 打勾）；也支援 Telegram |
| `scan.py` | 主程式：`python scan.py`（掃一次，用 `state/sent.json` 去重）、`--loop`（常駐，每根 5 分 K 收盤後 8 秒掃）、`--stateless`（排程準時的雲端用）、`--replay [--csv]`（列出最近 4 天訊號，跟 TradingView 對）、`--search sol`、`--hello`（連線測試訊息） |
| `config.json` | 商品、時間框架、各開關（`params` 對應 Pine 的 input，沒寫的用 `crt_engine.Params` 預設） |
| `.github/workflows/scan.yml` | 目前的部署：GitHub Actions（push 時發連線測試；cron-job.org 每 5 分鐘用 workflow_dispatch 觸發；state 用 actions/cache） |

環境變數：`DISCORD_WEBHOOK_URL`（必要）、`SYMBOLS`（例如 `BTC,ETH,SOL`，會自動補成 `-USDT-SWAP`）、`TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID`（選用）。

## 接下來要做（使用者的計畫）

1. **搬到使用者自己的主機**，跟 `fex_upsidedown` 專案放在一起。用 `python scan.py --loop` 常駐（systemd / Docker / pm2 擇一），`state/` 要放在持久的位置。搬好後把 GitHub Actions 的排程和 cron-job.org 停掉，避免重複發訊息。
2. **外匯 / 黃金 / 指數**（EURUSD、XAUUSD、US100、SPX500）：OKX 只有幣，要另外接資料來源，二選一（還沒決定）：
   - OANDA v20 API（模擬帳戶 token；`EUR_USD`、`XAU_USD`、`NAS100_USD`、`SPX500_USD`；TradingView 的 `OANDA:` 報價可直接對照）
   - MT5 Python 套件（要 Windows + MT5 開著；報價跟使用者實際下單的券商一致）
   - **引擎要先改**：`HTF.bucket()` / `HTF.prev()` 現在假設 24 小時、UTC 對齊（幣圈）。外匯是紐約 17:00 換日、週末休市、指數 / 黃金每天有休市時段 → 改成用 HTF K 線清單 bisect 找「包含這根 LTF 的 HTF K」與前一根，不要用時間除法。

## 對訊號時已知可能不一致的地方

- `ta.pivothigh/pivotlow` 遇到相同高 / 低點的判斷方式（這裡用左邊可相等、右邊要嚴格）
- 掃描器只看最近約 4 天的 5 分 K，很早以前形成的 KL 區塊可能不同
- TradingView 即時 K 會在收盤前觸發成交；掃描器只看收盤的 K

## 訊號規則重點（改程式前先看懂）

- 進場訊號 ①～⑧，全部同時監看，誰先觸發就用誰；① ④ ⑧ 是收盤市價，② ③ ⑤ ⑥ ⑦ 是限價單
- ① 只在 CISD「第一次」帶缺口突破那根進；如果那時不能進場，又收回 CISD 另一側、而且沒有 ② 掛單可以接 → ① 重新生效
- 掛單價出現時價格已經過了 → 不發掛單通知，下一根開盤直接成交（進場卡片會寫明）
- TP1 = 區間 50%、TP2 = 對側、TP3 / TP4 = 對側外面的流動性；預設各平 25%，到 TP1 停損移到保本
- TP1 已經在進場價後面 → 略過 TP1，部位併到 TP2，保本改在 TP2
- RR 預設不擋進場（`min_rr = 0`），只顯示

## 慣例

- 對使用者用繁體中文、白話；程式註解中文
- 改 `crt_engine.py` 的交易邏輯時，`pine/CRT_Toolkit.pine` 也要同步改（反過來也是）
