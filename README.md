# CRT 訊號儀表板（MT5 → Discord 通知，手動下單）

跟 TradingView 上的「CRT_Toolkit」指標（`pine/CRT_Toolkit.pine`）同一套邏輯（Python 逐行移植）。
在自己的 Windows 電腦上跑，讀 MT5 的 K 線，每根 5 分 K 收盤後掃一次，有新的
**掛單 / 進場 / 打到 TP / 保本 / 停損 / 掛單取消** 就推到 Discord（手機會跳通知）。
**只讀行情、不會自動下單**——看到通知自己到 MT5 下單。

## 開起來

Windows + MT5 開著並登入。

```
py -3.11 -m venv venv
venv\Scripts\pip install -r requirements.txt
venv\Scripts\python dash.py          # http://127.0.0.1:8011
```

網頁上可以：
- **選商品**：在「加商品」打幾個字（`xau`、`gold`、`黃金`、`nas`、`spx`、`eu`…）就會列出 MT5 上相關的商品，點一下或按 Enter 加入，× 移除
- **⚙ 通知設定**：要通知哪些事件、Discord Webhook、測試頻道 / 測試 MT5
- 看每個商品進行中的 CRT、K 線圖上的進場 / SL / TP 線、訊號紀錄（點開是完整卡片）、回放統計、MT5 帳戶的手動持倉

## 跟 Yuki 的 Telegram 訊號對照

雙擊 `tg_login.bat`（第一次：Telegram api_id / api_hash、手機驗證碼、選頻道）。之後儀表板每 5 分鐘自動抓新訊息，
整理成 `~/crt-dash-data/yuki/yuki_vs_ours.md`（跟我們的訊號逐筆對照）、`signals.csv`、`unparsed.md`。
只用你的帳號讀取、不發訊息；資料只存在本機。手動重整：`venv\Scripts\python yuki_report.py`。

## 檔案

| 檔案 | 用途 |
|---|---|
| `crt_engine.py` | CRT 邏輯（C1/C2 掃蕩、Model 1–4、KL、CISD、①～⑧ 進場、TP1～TP4、分批 + 保本） |
| `mt5src.py` | MT5 行情（唯讀）、商品搜尋 |
| `okx.py` | OKX 公開行情（幣圈，不用 API key） |
| `notify.py` | Discord webhook（也支援 Telegram） |
| `scan.py` | 掃描 / 回放 / 常駐（命令列） |
| `dash.py` / `dash.html` | 本機儀表板 |
| `config.mt5.json` | MT5 用：`mt5_path`（哪一個 MT5）、預設商品、時間框架、策略參數 |
| `config.json` | OKX 用 |

## 設定

- 商品、要通知的事件：網頁上改（存在 `~/crt-dash-data/settings.json`，蓋過 config 檔）
- 策略參數 `params`：改 `config.mt5.json`，下一次掃描自動套用；跟指標設定一一對應，沒寫的就用指標的預設值
- `notify` 事件：`limit`（掛限價單，同一根 K 的多張會合成一則）、`entry`（進場）、`exit`（TP / 保本 / 停損）、`cancel`（掛單作廢，要去 MT5 刪單）、`confirm`（CRT 確認）
- `.env`：`DISCORD_WEBHOOK_URL`（沒設就只顯示、不推播；網頁上也能填）

## 對訊號

```
venv\Scripts\python scan.py --config config.mt5.json --replay --csv signals.csv
venv\Scripts\python scan.py --replay                      # OKX（BTC / ETH）
venv\Scripts\python scan.py --config config.mt5.json --search gold
```

會列出最近每一個 CRT 確認、掛單、進場、出場的時間（台灣時間、K 棒收盤）。

已知可能跟 TradingView 對不上的地方：
- 擺動點（MSS 用的 pivot）遇到「兩根一樣高」時，TradingView 內建函式的判斷方式沒有公開，這裡用「左邊可以相等、右邊要更低」
- 很久以前形成的 KL 區塊（OB / FVG）：只看最近的 5 分 K（MT5 3000 根約 2 週）來判斷區塊有沒有被收破
- TradingView 即時 K 棒還沒收盤時就可能觸發掛單成交；這裡只看收盤的 K
- MT5（Exness）日線是 UTC 00:00 換日，TradingView 外匯日線是紐約 17:00，PDH / PDL 會不一樣
