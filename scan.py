"""
CRT 雲端掃描器：抓 OKX K 線 → 跑 CRT 邏輯（跟 TradingView 指標同一套）→ 新訊號推到手機。

  python scan.py              掃一次，把新的訊號送出去（排程用）
  python scan.py --loop       一直跑，每根 LTF K 收盤後掃一次（VPS / Cloud Run 用）
  python scan.py --replay     不送通知，列出這段資料裡所有的訊號（拿來跟 TradingView 對）
  python scan.py --replay --csv out.csv
  python scan.py --config config.mt5.json ...   用 MT5 行情（外匯 / 黃金 / 指數；Windows + MT5 開著）

行情來源看 config 的 `source`："okx"（預設）或 "mt5"。
已送通知的紀錄放在 $CRT_DATA_DIR/sent.json（沒設就是 ./state/sent.json）。
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


def state_file() -> Path:
    return Path(os.environ.get("CRT_DATA_DIR") or ROOT / "state") / "sent.json"


def load_cfg(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def src(cfg: dict):
    """行情來源模組：okx 或 mt5src（介面相同：candles / tick_size）。"""
    if cfg.get("source", "okx") == "mt5":
        import mt5src
        mt5src.configure(cfg.get("mt5_path", ""))
        return mt5src
    return okx


def build_params(cfg: dict, tick: float) -> Params:
    p = Params(**{k: (tuple(v) if k == "w_tp" else v) for k, v in cfg.get("params", {}).items()})
    p.tick = tick
    return p


def resolve_symbols(names: list[str], cfg: dict | None = None) -> dict[str, float]:
    """BTC / sol / ETH-USDT-SWAP 都可以；找不到就在 log 印出相近的。MT5 用 MT5 上的名稱（例如 XAUUSDm）。"""
    if cfg is not None and cfg.get("source", "okx") == "mt5":
        return src(cfg).resolve(names)
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
    s = src(cfg)
    tick = tick if tick is not None else s.tick_size(sym)
    p = build_params(cfg, tick)
    ltf = s.candles(sym, cfg.get("ltf", "5m"), int(cfg.get("ltf_bars", 1200)))
    hn = int(cfg.get("htf_bars", 300))
    h = HTF(s.candles(sym, p.crt_tf, hn, closed_only=False), TF_MS[p.crt_tf])
    kl = None
    if TF_MS[p.kl_tf] > TF_MS[p.crt_tf] and p.kl_tf != "1D":
        kl = HTF(s.candles(sym, p.kl_tf, hn, closed_only=False), TF_MS[p.kl_tf])
    d = HTF(s.candles(sym, "1D", hn, closed_only=False), TF_MS["1D"])
    eng = Engine(p, sym, h, kl, d).run(ltf)
    eng.ltf_bars = ltf          # 給儀表板畫圖用
    return eng


def fmt_t(ms: int, tz: str) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).astimezone(ZoneInfo(tz)).strftime("%m/%d %H:%M")


def load_state() -> tuple[dict, dict]:
    """sent：已送過的事件；msgs：進場卡片的 Discord 訊息（之後在 TP 後面打勾）。"""
    try:
        js = json.loads(state_file().read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}, {}
    if "sent" not in js:  # 舊格式
        return js, {}
    return js["sent"], js.get("msgs", {})


def save_state(st: dict, msgs: dict) -> None:
    cutoff = time.time() * 1000 - 4 * 86_400_000
    st = {k: v for k, v in st.items() if v >= cutoff}
    msgs = {k: v for k, v in msgs.items() if v.get("t", 0) >= cutoff}
    f = state_file()
    f.parent.mkdir(parents=True, exist_ok=True)
    tmp = f.with_suffix(".tmp")
    tmp.write_text(json.dumps({"sent": st, "msgs": msgs}, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, f)


def mark_card(m: dict, d: dict) -> None:
    """在原本的進場卡片上打勾：TP 打到 ✅，出場加一行結果。"""
    lines = m["desc"].split("\n")
    for k, hit in enumerate(d.get("hits", [])):
        tag = f"🎯 TP{k + 1}:"
        lines = [ln + " ✅" if hit and ln.startswith(tag) and not ln.endswith("✅") else ln for ln in lines]
    if d.get("sl_tag"):  # 停損移動了：原本的 SL 行後面寫現在在哪（例如「→ 保本 7325.57」）
        lines = [f"{ln.split(' → ')[0]} → {d['sl_tag']} {d.get('sl_now_txt', '')}" if ln.startswith("🚫 SL:") else ln
                 for ln in lines]
    if d.get("done"):
        res = ("⚪ 保本出場" if d.get("be") else "❌ 停損出場") if d.get("stopped") else "🏁 全部出場"
        lines = [ln for ln in lines if not ln.startswith(("❌ 停損出場", "⚪ 保本出場", "🏁 全部出場"))]
        lines.insert(lines.index("") if "" in lines else len(lines), f"{res} · 合計 {d.get('rz', 0):+.2f}R")
    m["desc"] = "\n".join(lines)
    notify.edit(m["id"], m["title"], m["desc"], m["color"])


def _eid(sym: str, ev) -> str:
    return f"{sym}|{ev.key}|{ev.kind}|{ev.t}|{ev.data.get('sig', '')}|{ev.data.get('ev', '')}"


def scan_once(cfg: dict, stateless: bool = False, results: dict | None = None) -> int:
    """results：給儀表板用，每個商品放 Engine（失敗就放 Exception）。"""
    st, msgs = ({}, {}) if stateless else load_state()
    first_run = not st and not stateless
    now = time.time() * 1000
    ltf_ms = TF_MS[cfg.get("ltf", "5m")]
    # stateless：排程每 5 分鐘準時跑一次，只送「上一根 LTF K 收盤」產生的訊號（不用存檔）
    look = ltf_ms if stateless else (15 if first_run else int(cfg.get("lookback_min", 60))) * 60_000
    kinds = set(cfg.get("notify", ["limit", "entry", "exit", "cancel"]))
    tz = cfg.get("display_tz", "Asia/Taipei")
    sent = 0
    ticks = resolve_symbols(cfg["symbols"], cfg)
    if results is not None:
        for name in cfg["symbols"]:
            if name.strip() and name.strip() not in ticks and not any(k.startswith(name.strip().upper()) for k in ticks):
                results[name.strip()] = RuntimeError("找不到這個商品")
    for sym, tick in ticks.items():
        try:
            eng = run_symbol(sym, cfg, tick)
        except Exception as e:  # 一個商品失敗不影響其他
            print(f"[{sym}] 失敗：{e}", file=sys.stderr)
            if results is not None:
                results[sym] = e
            continue
        if results is not None:
            results[sym] = eng
        # 剛加進來的商品（紀錄裡沒有它）：跟第一次跑一樣，只送最近 15 分鐘的，舊的記成已送
        new_sym = not first_run and not stateless and not any(k.startswith(sym + "|") for k in st)
        look_s = 15 * 60_000 if new_sym else look
        todo = [ev for ev in eng.events
                if ev.kind in kinds and _eid(sym, ev) not in st and ev.t + ltf_ms >= now - look_s]
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
            try:
                info = notify.send(ev.kind, ev.data.get("dir", 0), text, fmt_t(close_t, tz) + "（K 棒收盤）")
            except Exception as e:  # Discord 暫時失敗：這則不記成已送，下次掃描再送；其他照常
                print(f"[{sym}] 通知失敗（下次再送）：{e}", file=sys.stderr)
                continue
            mk = f"{sym}|{ev.key}"
            if ev.kind == "entry" and info and info.get("id"):
                msgs[mk] = dict(info, t=close_t)
            elif ev.kind == "exit" and mk in msgs:
                try:
                    mark_card(msgs[mk], ev.data)
                except Exception as e:
                    print(f"改訊息失敗：{e}", file=sys.stderr)
            for e in g:
                st[_eid(sym, e)] = close_t
            sent += 1
        if first_run or new_sym:  # 第一次跑 / 新商品：把舊的都記成已送，避免一次灌一堆
            for ev in eng.events:
                st.setdefault(_eid(sym, ev), ev.t + ltf_ms)
    if not stateless:
        save_state(st, msgs)
    print(f"{datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S}Z 送出 {sent} 則")
    return sent


def replay(cfg: dict, csv_path: str | None) -> None:
    tz = cfg.get("display_tz", "Asia/Taipei")
    ltf_ms = TF_MS[cfg.get("ltf", "5m")]
    rows = []
    for sym, tick in resolve_symbols(cfg["symbols"], cfg).items():
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
    ap.add_argument("--hello", action="store_true", help="送一則連線測試訊息")
    ap.add_argument("--search", help="搜尋 OKX 永續合約代號，例如 --search sol")
    a = ap.parse_args()
    cfg = load_cfg(Path(a.config))
    if os.environ.get("SYMBOLS"):  # 雲端用環境變數改商品，例如 SYMBOLS=BTC,ETH,SOL
        cfg["symbols"] = os.environ["SYMBOLS"].split(",")
    if a.hello:
        lines = []
        for sym, tick in resolve_symbols(cfg["symbols"], cfg).items():
            b = src(cfg).candles(sym, cfg.get("ltf", "5m"), 1)[-1]
            lines.append(f"{sym}  最新收盤 {b.c}")
        notify.send("confirm", 0, "✅ CRT 掃描器連線正常\n" + "\n".join(lines) + "\n之後有新的掛單 / 進場 / 出場才會通知",
                    fmt_t(int(time.time() * 1000), cfg.get("display_tz", "Asia/Taipei")))
        return
    if a.search:
        q = a.search.upper()
        found = ([f"{x['name']}  {x['desc']}  [{x['group']}]" for x in src(cfg).search(q)] if cfg.get("source") == "mt5"
                 else [k for k in sorted(okx.instruments("SWAP")) if q in k])
        print("\n".join(found) or "沒有找到")
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
