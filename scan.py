"""
CRT 雲端掃描器：抓 OKX K 線 → 跑 CRT 邏輯（跟 TradingView 指標同一套）→ 新訊號推到手機。

  python scan.py              掃一次，把新的訊號送出去（排程用）
  python scan.py --loop       一直跑，每根 LTF K 收盤後掃一次（VPS / Cloud Run 用）
  python scan.py --replay     不送通知，列出這段資料裡所有的訊號（拿來跟 TradingView 對）
  python scan.py --replay --csv out.csv
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import notify
import okx
from crt_engine import HTF, TF_MS, Engine, Params

ROOT = Path(__file__).resolve().parent
STATE = ROOT / "state" / "sent.json"


def load_cfg(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def build_params(cfg: dict, tick: float) -> Params:
    p = Params(**{k: (tuple(v) if k == "w_tp" else v) for k, v in cfg.get("params", {}).items()})
    p.tick = tick
    return p


def resolve_symbols(names: list[str]) -> dict[str, float]:
    """BTC / sol / ETH-USDT-SWAP 都可以；找不到就在 log 印出相近的。"""
    allx = okx.instruments("SWAP")
    out = {}
    for n in names:
        n = n.strip().upper()
        if not n:
            continue
        cand = [n, f"{n}-USDT-SWAP", f"{n}-USD-SWAP"]
        hit = next((c for c in cand if c in allx), None)
        if hit:
            out[hit] = allx[hit]
        else:
            near = [k for k in allx if n in k][:10]
            print(f"找不到 {n}；相近的：{', '.join(near) or '無'}", file=sys.stderr)
    return out


def run_symbol(sym: str, cfg: dict, tick: float | None = None) -> Engine:
    tick = tick if tick is not None else okx.tick_size(sym)
    p = build_params(cfg, tick)
    ltf = okx.candles(sym, cfg.get("ltf", "5m"), int(cfg.get("ltf_bars", 1200)))
    h = HTF(okx.candles(sym, p.crt_tf, 300, closed_only=False), TF_MS[p.crt_tf])
    kl = None
    if TF_MS[p.kl_tf] > TF_MS[p.crt_tf] and p.kl_tf != "1D":
        kl = HTF(okx.candles(sym, p.kl_tf, 300, closed_only=False), TF_MS[p.kl_tf])
    d = HTF(okx.candles(sym, "1D", 300, closed_only=False), TF_MS["1D"])
    return Engine(p, sym, h, kl, d).run(ltf)


def fmt_t(ms: int, tz: str) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).astimezone(ZoneInfo(tz)).strftime("%m/%d %H:%M")


def load_state() -> dict:
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(st: dict) -> None:
    cutoff = time.time() * 1000 - 4 * 86_400_000
    st = {k: v for k, v in st.items() if v >= cutoff}
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(st), encoding="utf-8")


def _eid(sym: str, ev) -> str:
    return f"{sym}|{ev.key}|{ev.kind}|{ev.t}|{ev.data.get('sig', '')}|{ev.data.get('ev', '')}"


def scan_once(cfg: dict, stateless: bool = False) -> int:
    st = {} if stateless else load_state()
    first_run = not st and not stateless
    now = time.time() * 1000
    ltf_ms = TF_MS[cfg.get("ltf", "5m")]
    # stateless：排程每 5 分鐘準時跑一次，只送「上一根 LTF K 收盤」產生的訊號（不用存檔）
    look = ltf_ms if stateless else (15 if first_run else int(cfg.get("lookback_min", 60))) * 60_000
    kinds = set(cfg.get("notify", ["limit", "entry", "exit", "cancel"]))
    tz = cfg.get("display_tz", "Asia/Taipei")
    sent = 0
    ticks = resolve_symbols(cfg["symbols"])
    for sym, tick in ticks.items():
        try:
            eng = run_symbol(sym, cfg, tick)
        except Exception as e:  # 一個商品失敗不影響其他
            print(f"[{sym}] 失敗：{e}", file=sys.stderr)
            continue
        todo = [ev for ev in eng.events
                if ev.kind in kinds and _eid(sym, ev) not in st and ev.t + ltf_ms >= now - look]
        # 同一根 K、同一個 CRT 的多張掛單合成一則
        groups: list[list] = []
        for ev in todo:
            if ev.kind == "limit" and groups and groups[-1][0].kind == "limit" \
                    and groups[-1][0].key == ev.key and groups[-1][0].t == ev.t:
                groups[-1].append(ev)
            else:
                groups.append([ev])
        for g in groups:
            ev = g[0]
            text = eng.limitGroupText(g) if len(g) > 1 else ev.text
            close_t = ev.t + ltf_ms
            notify.send(ev.kind, ev.data.get("dir", 0), text, fmt_t(close_t, tz) + "（K 棒收盤）")
            for e in g:
                st[_eid(sym, e)] = close_t
            sent += 1
        if first_run:  # 第一次跑：把舊的都記成已送，避免一次灌一堆
            for ev in eng.events:
                st.setdefault(_eid(sym, ev), ev.t + ltf_ms)
    if not stateless:
        save_state(st)
    print(f"{datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S}Z 送出 {sent} 則")
    return sent


def replay(cfg: dict, csv_path: str | None) -> None:
    tz = cfg.get("display_tz", "Asia/Taipei")
    ltf_ms = TF_MS[cfg.get("ltf", "5m")]
    rows = []
    for sym, tick in resolve_symbols(cfg["symbols"]).items():
        eng = run_symbol(sym, cfg, tick)
        print(f"\n===== {sym} =====")
        for ev in eng.events:
            first = ev.text.split("\n")[0].split(" — ", 1)[-1]
            extra = ""
            if ev.kind in ("entry", "limit"):
                extra = f" @ {eng.px(ev.data.get('price'))} SL {eng.px(ev.data.get('sl'))}"
            print(f"{fmt_t(ev.t + ltf_ms, tz)}  {ev.kind:7s}  {first}{extra}")
            rows.append([sym, fmt_t(ev.t + ltf_ms, tz), ev.kind, ev.key, first, ev.data.get("price"), ev.data.get("sl")])
        s = eng.stats
        n = s[0]
        if n:
            print(f"已結算 {int(n)} 筆：TP1 {s[1] / n:.0%} · TP2 {s[2] / n:.0%} · TP3 {s[3] / n:.0%} · TP4 {s[13] / n:.0%}"
                  f" · 先停損 {s[4] / n:.0%} · 分批+保本 {s[15] / n:+.2f}R/筆")
    if csv_path:
        with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(["商品", "時間（收盤）", "事件", "CRT", "內容", "價格", "SL"])
            w.writerows(rows)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(ROOT / "config.json"))
    ap.add_argument("--loop", action="store_true")
    ap.add_argument("--replay", action="store_true")
    ap.add_argument("--csv")
    ap.add_argument("--stateless", action="store_true")
    ap.add_argument("--search", help="搜尋 OKX 永續合約代號，例如 --search sol")
    a = ap.parse_args()
    cfg = load_cfg(Path(a.config))
    if os.environ.get("SYMBOLS"):  # 雲端用環境變數改商品，例如 SYMBOLS=BTC,ETH,SOL
        cfg["symbols"] = os.environ["SYMBOLS"].split(",")
    if a.search:
        q = a.search.upper()
        print("\n".join(k for k in sorted(okx.instruments("SWAP")) if q in k) or "沒有找到")
        return
    if a.replay:
        replay(cfg, a.csv)
        return
    if not a.loop:
        scan_once(cfg, a.stateless)
        return
    ltf_ms = TF_MS[cfg.get("ltf", "5m")]
    while True:
        now = time.time() * 1000
        nxt = (now // ltf_ms + 1) * ltf_ms + 8_000  # K 收盤後 8 秒（等 OKX 確認收盤）
        time.sleep(max(1.0, (nxt - now) / 1000))
        try:
            scan_once(cfg)
        except Exception as e:
            print(f"掃描失敗：{e}", file=sys.stderr)


if __name__ == "__main__":
    os.chdir(ROOT)
    main()
