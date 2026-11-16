"""OKX public market data (no API key needed)."""
from __future__ import annotations

import time

import requests

from crt_engine import Bar

# OKX 有好幾個網域；美國機房（例如 GitHub Actions）連 www 被擋時，依序換下一個
BASES = ["https://www.okx.com", "https://aws.okx.com", "https://app.okx.com", "https://my.okx.com"]
# OKX 的 1D / 6H / 12H 是香港時間開盤；TradingView 的日線是 UTC，所以日線用 1Dutc
BAR = {"5m": "5m", "3m": "3m", "1m": "1m", "15m": "15m", "1H": "1H", "4H": "4H", "1D": "1Dutc"}

_session = requests.Session()
_session.headers["User-Agent"] = "crt-scanner/1.0"
_base_ok: str | None = None


def _get(path: str, params: dict) -> list:
    global _base_ok
    bases = [_base_ok] + [b for b in BASES if b != _base_ok] if _base_ok else BASES
    last = None
    for base in bases:
        for attempt in range(3):
            try:
                r = _session.get(base + path, params=params, timeout=10)
                if r.status_code == 429:
                    time.sleep(1.0 + attempt)
                    continue
                if r.status_code in (403, 451):
                    last = f"{base} HTTP {r.status_code}"
                    break
                r.raise_for_status()
                js = r.json()
                if js.get("code") != "0":
                    raise RuntimeError(f"OKX error {js.get('code')}: {js.get('msg')}")
                _base_ok = base
                return js["data"]
            except (requests.ConnectionError, requests.Timeout) as e:
                last = f"{base}: {e}"
                time.sleep(0.5 + attempt)
    raise RuntimeError(f"OKX 連不上（{last}）")


def tick_size(inst_id: str) -> float:
    inst_type = "SWAP" if inst_id.endswith("-SWAP") else "FUTURES" if inst_id.count("-") >= 2 and inst_id[-6:].isdigit() else "SPOT"
    data = _get("/api/v5/public/instruments", {"instType": inst_type, "instId": inst_id})
    return float(data[0]["tickSz"])


def candles(inst_id: str, tf: str, n: int, closed_only: bool = True) -> list[Bar]:
    """最近 n 根 K（舊 → 新）。closed_only=True 時去掉還沒收盤的最後一根。"""
    bar = BAR[tf]
    out: dict[int, Bar] = {}
    after = None
    # /market/candles 最多回最近 1440 根，每次 300；再舊的用 history-candles（每次 100）
    path, lim = "/api/v5/market/candles", 300
    while len(out) < n + 1:
        params = {"instId": inst_id, "bar": bar, "limit": str(lim)}
        if after:
            params["after"] = str(after)
        rows = _get(path, params)
        if not rows:
            if path.endswith("/candles"):
                path, lim = "/api/v5/market/history-candles", 100
                continue
            break
        for r in rows:
            t = int(r[0])
            if closed_only and r[8] == "0":
                continue
            out[t] = Bar(t, float(r[1]), float(r[2]), float(r[3]), float(r[4]))
        after = int(rows[-1][0])
        if len(rows) < lim and path.endswith("/candles"):
            path, lim = "/api/v5/market/history-candles", 100
    bars = sorted(out.values(), key=lambda b: b.t)
    return bars[-n:]


def instruments(inst_type: str = "SWAP") -> dict[str, float]:
    """所有商品代號 → tick size。"""
    return {d["instId"]: float(d["tickSz"]) for d in _get("/api/v5/public/instruments", {"instType": inst_type})}
