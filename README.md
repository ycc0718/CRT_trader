# CRT 雲端掃描器（OKX → 手機通知）

跟 TradingView 上的「CRT_Toolkit」指標同一套邏輯（Python 逐行移植），
每 5 分鐘抓一次 OKX 的 K 線，有新的 **掛單 / 進場 / 打到 TP / 保本 / 停損** 就推到 Discord（手機會跳通知）。
電腦不用開，也不用 TradingView 付費方案。

## 檔案

| 檔案 | 用途 |
|---|---|
| `crt_engine.py` | CRT 邏輯（C1/C2 掃蕩、Model 1–4、KL、CISD、①～⑧ 進場、TP1～TP4、分批 + 保本） |
| `okx.py` | 抓 OKX 公開行情（不用 API key） |
| `notify.py` | Discord webhook（也支援 Telegram） |
| `scan.py` | 主程式：掃描 / 回放 / 常駐 |
| `config.json` | 商品、時間框架、各種開關（對應指標的設定） |
| `.github/workflows/scan.yml` | GitHub Actions 排程（每 5 分鐘） |

## 設定（config.json）

- `symbols`：OKX 商品代號，例如 `BTC-USDT-SWAP`（永續）、`ETH-USDT-SWAP`、`SOL-USDT-SWAP`、`BTC-USDT`（現貨）
- `ltf`：進場用的時間框架（預設 `5m`），`params.crt_tf`：CRT 時間框架（預設 `1H`）
- `notify`：要通知哪些事件：`limit`（掛限價單，同一根 K 的多張會合成一則）、`entry`（進場）、`exit`（TP / 保本 / 停損）、`cancel`（掛單作廢，要去交易所取消）、`confirm`（CRT 確認）
- `params`：跟指標設定一一對應，沒寫的就用指標的預設值

## 對訊號（跟 TradingView 比）

在 TradingView 開 `OKX:BTCUSDT.P`、5 分鐘圖、指標設定跟 `config.json` 一樣，然後：

```
python scan.py --replay --csv signals.csv
```

會列出最近約 4 天每一個 CRT 確認、掛單、進場、出場的時間（台灣時間、K 棒收盤）。
GitHub 上也可以跑：Actions → CRT scan → Run workflow → 勾 replay，跑完下載 `replay` 檔。

已知可能對不上的地方：
- 擺動點（MSS 用的 pivot）遇到「兩根一樣高」時，TradingView 內建函式的判斷方式沒有公開，這裡用「左邊可以相等、右邊要更低」
- 很久以前形成的 KL 區塊（OB / FVG）：掃描器只看最近約 4 天的 5 分 K 來判斷區塊有沒有被收破
- TradingView 即時 K 棒還沒收盤時就可能觸發掛單成交；掃描器只看收盤的 K

## 本機試跑

```
pip install -r requirements.txt
python scan.py --replay
```

沒設定 `DISCORD_WEBHOOK_URL` 時，`python scan.py` 會把通知印在畫面上。
