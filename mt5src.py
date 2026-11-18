"""MetaTrader 5 行情（唯讀：只讀 K 線 / 報價 / 帳戶 / 持倉，絕不下單）。

Windows + MT5 開著並登入才能用。介面跟 okx.py 一樣（candles / tick_size / resolve / search），scan.py 用 config 的
`source: "mt5"` 切換。K 線時間是券商伺服器時間，用最新報價時間和本機時鐘的差（取整到小時）換成 UTC；
週末報價太舊量不出來時先當 0（Exness 伺服器就是 UTC）。
"""
from __future__ import annotations

import os
import threading
import time

from crt_engine import TF_MS, Bar

TF = {"1m": "TIMEFRAME_M1", "3m": "TIMEFRAME_M3", "5m": "TIMEFRAME_M5", "15m": "TIMEFRAME_M15",
      "1H": "TIMEFRAME_H1", "4H": "TIMEFRAME_H4", "1D": "TIMEFRAME_D1"}

_LOCK = threading.RLock()       # MetaTrader5 套件是整個 process 共用一條連線
_path = ""
_offset_s: int | None = None


def configure(path: str = "") -> None:
    """指定要連哪一個 MT5（同時開好幾個終端時一定要指定，不然可能連到別的帳號）。環境變數 MT5_PATH 優先。"""
    global _path
    _path = os.environ.get("MT5_PATH") or path or ""


def _same(a: str, b: str) -> bool:
    f = lambda x: os.path.normcase(os.path.abspath(x if x.lower().endswith(".exe") else os.path.join(x, "terminal64.exe")))
    return f(a) == f(b)


def _mt5():
    import MetaTrader5 as mt5
    if mt5.terminal_info() is None:
        if not (mt5.initialize(path=_path) if _path else mt5.initialize()):
            raise RuntimeError(f"連不到 MT5{f'（{_path}）' if _path else ''}，請確認 MT5 已開啟並登入：{mt5.last_error()}")
        ti = mt5.terminal_info()
        if _path and ti is not None and not _same(_path, ti.path):
            mt5.shutdown()
            raise RuntimeError(f"連到的 MT5 是 {ti.path}，不是設定的 {_path}")
    return mt5


def _offset(mt5) -> int:
    """伺服器時間 − UTC（秒）。從最新的報價量一次就記住；量不出來（週末）先回 0，下次再量。"""
    global _offset_s
    if _offset_s is not None:
        return _offset_s
    times = [s.time for s in (mt5.symbols_get() or []) if getattr(s, "visible", False) and getattr(s, "time", 0)]
    if not times:
        return 0
    diff = max(times) - time.time()
    off = round(diff / 3600) * 3600
    if abs(diff - off) > 120 or not -12 * 3600 <= off <= 14 * 3600:
        return 0
    _offset_s = int(off)
    return _offset_s


def candles(sym: str, tf: str, n: int, closed_only: bool = True) -> list[Bar]:
    """最近 n 根 K（舊 → 新，時間 ms UTC）。closed_only=True 時去掉還沒收盤的最後一根。"""
    with _LOCK:
        mt5 = _mt5()
        if not mt5.symbol_select(sym, True):
            raise RuntimeError(f"MT5 找不到商品 {sym}（名稱要跟 MT5「市場報價」一樣，例如 XAUUSDm）")
        rates = mt5.copy_rates_from_pos(sym, getattr(mt5, TF[tf]), 0, n + 1)
        if rates is None or len(rates) == 0:
            raise RuntimeError(f"{sym} {tf} 沒有資料：{mt5.last_error()}")
        off = _offset(mt5)
    bars = [Bar((int(r["time"]) - off) * 1000, float(r["open"]), float(r["high"]), float(r["low"]), float(r["close"]))
            for r in rates]
    if closed_only and bars and bars[-1].t + TF_MS[tf] > time.time() * 1000:
        bars.pop()                  # 還在跑的那根（用時間判斷：休市時最後一根其實已經收了）
    return bars[-n:]


def tick_size(sym: str) -> float:
    with _LOCK:
        mt5 = _mt5()
        mt5.symbol_select(sym, True)
        info = mt5.symbol_info(sym)
    if info is None:
        raise RuntimeError(f"MT5 找不到商品 {sym}")
    return float(info.trade_tick_size or info.point)


def resolve(names: list[str]) -> dict[str, float]:
    """商品名稱 → tick size；找不到的印出相近名稱（例如 XAUUSD → XAUUSDm）。MT5 本身連不到就直接丟錯。"""
    with _LOCK:
        _mt5()
    out = {}
    for n in names:
        n = n.strip()
        if not n:
            continue
        try:
            out[n] = tick_size(n)
        except Exception:
            print(f"MT5 找不到 {n}；相近的：{', '.join(x['name'] for x in search(n)[:10]) or '無'}")
    return out


# 常用叫法 → MT5 代號開頭（打 gold / 黃金 / nas 也找得到）
ALIAS = {"GOLD": "XAU", "黃金": "XAU", "SILVER": "XAG", "白銀": "XAG", "NASDAQ": "USTEC", "NAS100": "USTEC",
         "US100": "USTEC", "NDX": "USTEC", "那斯達克": "USTEC", "那指": "USTEC", "SPX": "US500", "SP500": "US500",
         "S&P": "US500", "標普": "US500", "DOW": "US30", "DJI": "US30", "道瓊": "US30", "BITCOIN": "BTC", "比特幣": "BTC",
         "ETHEREUM": "ETH", "以太": "ETH", "OIL": "USOIL", "WTI": "USOIL", "原油": "USOIL", "BRENT": "UKOIL",
         "DAX": "DE30", "GER": "DE30", "NIKKEI": "JP225", "日經": "JP225", "FTSE": "UK100", "美元指數": "DXY",
         "歐元": "EUR", "英鎊": "GBP", "日圓": "JPY", "日元": "JPY", "澳幣": "AUD", "紐幣": "NZD", "加幣": "CAD", "瑞郎": "CHF"}
GROUP = {"Forex": "外匯", "Indices": "指數", "Idx_Enlarge": "指數（放大合約）", "Crypto": "加密貨幣",
         "CryptoCross": "加密交叉", "Energies": "能源", "Metals": "金屬", "Stocks": "股票"}


def search(q: str, limit: int = 20) -> list[dict]:
    """打幾個字找商品：代號開頭 > 常用叫法 > 代號中間 > 英文說明；同一級裡「市場報價」有的、名稱短的排前面。
    不能交易的（trade_mode 0）不列。"""
    q = q.strip().upper().replace(" ", "")
    if not q:
        return []
    with _LOCK:
        syms = _mt5().symbols_get() or []
    keys = {v for k, v in ALIAS.items() if (q.isascii() and len(q) >= 2 and k.startswith(q)) or (not q.isascii() and q in k)}
    hits = []
    for s in syms:
        if s.trade_mode == 0:
            continue
        n, d = s.name.upper(), (s.description or "").upper().replace(" ", "")
        if n.startswith(q):
            sc = 0
        elif any(n.startswith(k) for k in keys):
            sc = 1
        elif q in n:
            sc = 2
        elif any(k in n for k in keys):
            sc = 3
        elif len(q) >= 3 and q in d:     # 兩個字比對說明太雜（de 會中 INDEX）
            sc = 4
        else:
            continue
        hits.append((sc, not s.visible, len(s.name), s.name, s))
    hits.sort(key=lambda x: x[:4])
    out = []
    for *_, s in hits[:limit]:
        parts = s.path.split("\\")
        g = parts[-2] if len(parts) >= 2 else ""
        out.append({"name": s.name, "desc": s.description, "group": GROUP.get(g, g), "visible": bool(s.visible),
                    "closeOnly": s.trade_mode == 3})
    return out


def account() -> dict:
    """帳戶 + 這個帳戶目前所有持倉 / 掛單（手動單也算），給儀表板顯示用。"""
    with _LOCK:
        mt5 = _mt5()
        a = mt5.account_info()
        pos = mt5.positions_get() or []
        ords = mt5.orders_get() or []
        off = _offset(mt5)
    if a is None:
        raise RuntimeError(f"MT5 沒有登入帳戶：{mt5.last_error()}")
    return {
        "login": a.login, "server": a.server, "currency": a.currency,
        "mode": {0: "模擬", 1: "比賽", 2: "真實"}.get(a.trade_mode, "?"),
        "balance": a.balance, "equity": a.equity, "margin_free": a.margin_free,
        "positions": [{"symbol": p.symbol, "side": "多" if p.type == 0 else "空", "volume": p.volume,
                       "price": p.price_open, "sl": p.sl, "tp": p.tp, "now": p.price_current, "profit": p.profit,
                       "t": (p.time - off) * 1000} for p in pos],
        "orders": [{"symbol": o.symbol, "type": o.type, "volume": o.volume_current, "price": o.price_open,
                    "sl": o.sl, "tp": o.tp} for o in ords],
    }


def quote(sym: str) -> dict | None:
    with _LOCK:
        mt5 = _mt5()
        t = mt5.symbol_info_tick(sym)
    return {"bid": t.bid, "ask": t.ask} if t else None
