"""Yuki 訊號 × 我們的引擎 對照 → 一份報告（給使用者和 Claude 看了再調引擎）。

  python yuki_report.py                    最近 YUKI_DAYS 天（預設 30）重新整理；MT5 要開著，幣圈用 OKX
  python yuki_report.py --days 60
  python yuki_report.py --set kl_only=false --set e8=true --out loose
                                           試別的參數（蓋過 config.mt5.json 的 params），寫成 yuki_vs_ours_loose.md，不動主檔
  python yuki_report.py --no-compare       只整理 Yuki 的訊號

輸出（$CRT_DATA_DIR/yuki/，只在本機；Yuki 的訊息是別人的內容，不進 repo）：
  yuki_vs_ours.md  主檔　signals.csv  每張 Yuki 訊號 + 對到的我們那張　unparsed.md  認不出來的訊息
  summary.json     給儀表板的數字　cache/  OKX K 線快取（下次只補最新的）

Yuki 的頻道「到 TP2 才發卡片」：卡片全部是贏的單，停損的看不到；真正的勝率看每日結算（報告第 2 節）。
對照方法：
  行情　幣圈（xxxUSDT.P）→ OKX 永續（1000PEPE 的價格 ÷1000）；外匯 / 金屬 / 指數 → MT5，4H 和日線用 1H 照紐約 17:00 組
        （TradingView 外匯的切法：Yuki 外匯 4H 的編號是台灣 01 / 05 / 09 / 13 / 17 / 21 點，冬令時間晚 1 小時）
  參數　Yuki 1H → 5m 進場、4H → 15m；標準對 aggressive=False、激進對 aggressive=True；其他照 config.mt5.json（或 --set）
  配對　同商品、同方向、我們 CRT 的 C2 開盤 = Yuki 編號的時間（差 ≤ 1 根）；再用 TP 反推 Yuki 的區間，看 C1 是不是同一根
"""
from __future__ import annotations

import argparse
import bisect
import csv
import json
import os
import re
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import report  # noqa: E402
import tg_collect  # noqa: E402
import yuki_parse as yp  # noqa: E402
from crt_engine import K_NAME, NY, TF_MS, Bar  # noqa: E402

LTF_OF = {"15m": "1m", "1H": "5m", "4H": "15m", "1D": "1H"}      # Pine 建議的 CRT → 進場時間框架
OUT = tg_collect.DIR
CACHE = OUT / "cache"
DAY = 86_400_000
WARM = 7 * DAY            # 對照期間前面先多跑幾天（KL 區塊、前高前低、還沒結束的 CRT）
MARKET = {"okx": "加密", "mt5": "外匯黃金"}


# ───────────── 行情（對照專用：快取、紐約 17:00 的 4H / 日線） ─────────────
def ny_resample(bars: list[Bar], tf: str) -> list[Bar]:
    """1H K → 紐約 17:00 起算的 4H / 日 K（TradingView 外匯、OANDA 的切法）。"""
    per = 4 if tf == "4H" else 24
    out: dict[int, list] = {}
    for b in bars:
        d = datetime.fromtimestamp(b.t / 1000, NY).replace(minute=0, second=0, microsecond=0)
        start = (d.replace(tzinfo=None) - timedelta(hours=(d.hour - 17) % per)).replace(tzinfo=NY)
        k = int(start.timestamp() * 1000)
        x = out.get(k)
        if x is None:
            out[k] = [k, b.o, b.h, b.l, b.c]
        else:
            x[2], x[3], x[4] = max(x[2], b.h), min(x[3], b.l), b.c
    return [Bar(*v) for _, v in sorted(out.items())]


class Feed:
    """span：這次要涵蓋多久（ms）；HTF 根數會被截到 span 再多 300 根，不用抓幾年的日線。"""
    span = 60 * DAY

    def cap(self, tf: str, n: int) -> int:
        return min(n, int(self.span / TF_MS[tf]) + 300)


class OkxFeed(Feed):
    """OKX 永續 + 磁碟快取：收盤的 K 存起來，下次只補最新的幾根（OKX 舊資料一次只給 100 根，很慢）。"""
    name = "OKX"

    def __init__(self):
        import okx
        self.okx = okx
        self.mem: dict[tuple, list[Bar]] = {}
        self.short: set = set()        # 交易所就只有這麼多（新上市），不用每次重抓
        self._ins = None

    def instruments(self) -> dict:
        if self._ins is None:
            self._ins = self.okx.instruments("SWAP")
        return self._ins

    def tick_size(self, inst: str) -> float:
        return self.instruments().get(inst) or self.okx.tick_size(inst)

    def _load(self, inst: str, tf: str, n: int) -> list[Bar]:
        key, ms, now = (inst, tf), TF_MS[tf], time.time() * 1000
        have = self.mem.get(key)
        if have is not None and (len(have) >= n + 1 or key in self.short):
            return have
        f = CACHE / f"okx_{inst}_{tf}.json"
        try:
            old = [Bar(*x) for x in json.loads(f.read_text(encoding="utf-8"))]
        except (FileNotFoundError, json.JSONDecodeError, TypeError):
            old = []
        if old and old[0].t <= now - (n + 1) * ms:
            new = self.okx.candles(inst, tf, int((now - old[-1].t) // ms) + 3, closed_only=False)
        else:
            new = self.okx.candles(inst, tf, n + 2, closed_only=False)
            if len(new) < n + 1:
                self.short.add(key)
        m = {b.t: b for b in old}
        m.update({b.t: b for b in new})
        bars = sorted(m.values(), key=lambda b: b.t)
        closed = [b for b in bars if b.t + ms <= now]
        CACHE.mkdir(parents=True, exist_ok=True)
        tmp = f.with_suffix(f".{os.getpid()}.tmp")         # 儀表板和手動跑的可能同時寫
        tmp.write_text(json.dumps([[b.t, b.o, b.h, b.l, b.c] for b in closed[-150_000:]]), encoding="utf-8")
        os.replace(tmp, f)
        self.mem[key] = bars
        return bars

    def candles(self, inst: str, tf: str, n: int, closed_only: bool = True) -> list[Bar]:
        n = self.cap(tf, n)
        bars = self._load(inst, tf, n)
        if closed_only:
            now = time.time() * 1000
            bars = [b for b in bars if b.t + TF_MS[tf] <= now]
        return bars[-n:]


class Mt5Feed(Feed):
    """MT5（唯讀）；4H / 日線改用 1H 照紐約 17:00 組，跟 Yuki（TradingView 外匯）同一種切法。"""
    name = "MT5"

    def __init__(self, cfg: dict):
        import mt5src
        mt5src.configure(cfg.get("mt5_path", ""))
        self.m = mt5src

    def tick_size(self, sym: str) -> float:
        return self.m.tick_size(sym)

    def candles(self, sym: str, tf: str, n: int, closed_only: bool = True) -> list[Bar]:
        n = self.cap(tf, n)
        if tf not in ("4H", "1D"):
            return self.m.candles(sym, tf, n, closed_only)
        per = 4 if tf == "4H" else 24
        bars = ny_resample(self.m.candles(sym, "1H", min(n * per + per, 99_000), closed_only=False), tf)
        if closed_only:
            now = time.time() * 1000
            bars = [b for b in bars if b.t + TF_MS[tf] <= now]
        return bars[-n:]


def route(sym: str | None, okx: OkxFeed, mt5: Mt5Feed | None) -> tuple:
    """Yuki 的商品名 → (行情, 代號, 價格倍數)：1000PEPEUSDT.P → (OKX, PEPE-USDT-SWAP, 0.001)、NAS100USD → (MT5, USTECm, 1)。"""
    if not sym:
        return None, None, 1.0
    s = sym.upper().replace(".P", "")
    m = re.match(r"^(1000+)?([A-Z0-9]+?)USDTP?$", s)
    if m:
        inst = f"{m.group(2)}-USDT-SWAP"
        return (okx, inst, 1.0 / int(m.group(1)) if m.group(1) else 1.0) if inst in okx.instruments() else (None, None, 1.0)
    if mt5 is None:
        return None, None, 1.0
    tries = [s] + ([s[:-3]] if s.endswith("USD") and len(s) > 6 else []) + [s[:6]]
    for q in tries:
        items = mt5.m.search(q, 5)
        if items:
            return mt5, items[0]["name"], 1.0
    return None, None, 1.0


# ───────────── 我們這邊 ─────────────
def run_group(feed: Feed, inst: str, tf: str, aggressive: bool, cfg: dict, since: int, over: dict):
    import scan
    ltf = LTF_OF.get(tf, "5m")
    span = time.time() * 1000 - since
    feed.span = span
    params = dict(cfg.get("params", {}), **over)
    params.update(crt_tf=tf, aggressive=aggressive)
    c2 = dict(cfg, ltf=ltf, ltf_bars=int(span / TF_MS[ltf]) + 50, htf_bars=int(span / TF_MS[tf]) + 300, params=params)
    return scan.run_symbol(inst, c2, feed.tick_size(inst), source=feed)


def our_max(c) -> int:
    """最高到 TP 幾（TP1 在進場價後面 = 一進場就算過 TP1，跟 Yuki 的 ✅ 一樣）。"""
    h = 4 if c.tp4Hit else 3 if c.tp3Hit else 2 if c.tp2 else 1 if c.tp1 else 0
    return max(h, 1) if c.t1Skip else h


def why_not(c) -> tuple[str, str]:
    """我們這個 CRT 為什麼沒進場 → (原因分類, 細節)。"""
    if c.skipTxt:
        return c.skipTxt.replace("跳過：", ""), ""
    if c.result:
        r = c.result.replace("未進場：", "")
        m = re.match(r"^(.*?)（(.*)）$", r)
        return (m.group(1), m.group(2)) if m else (r, "")
    if not c.done:
        return "還在等（還沒結束）", c.note
    if not c.confirmed:
        return "C2 沒收回區間（我們沒確認）", ""
    return "其他", c.note


HINT = {
    "附近沒有 KL": "kl_only（Yuki 沒 KL 也發）/ klCheck、kl_tol_pct",
    "TS 沒間隔 1 根 K": "ts_gap",
    "逾時": "進場訊號（①～⑦）沒出現；Yuki 標準是 C2 收盤直接進 = 我們的 ⑧（預設關）",
    "已到對側": "進場訊號太慢（CISD / IFVG / BB 偵測）",
    "被新 CRT 取代": "retire 規則",
    "C2 沒收回區間": "行情不同（C2 收盤價差一點）；Yuki 激進是 C2 還沒收盤就進",
    "收在區間外": "Model 3 規則（m3_max）",
    "沒有收回": "Model 3 規則（m3_max）",
    "Model 2": "C2 自己掃完又打到對側：我們不做",
    "不是 TBS": "tbs_only", "不是時段 CRT": "sess_only",
}


def y_range(s: dict, sc: float):
    """用 Yuki 的 TP 反推區間（TP1 = 50%、TP2 = 對側、TP3 / TP4 = 對側再過 0.5 / 1.5 個區間）→ (高, 低)。"""
    t = [v * sc if v is not None else None for v in s["tp"]]
    d = s["dir"]
    if t[2] is not None and t[3] is not None and t[3] != t[2]:
        rng = abs(t[3] - t[2])
        opp = t[2] - d * 0.5 * rng
    elif t[0] is not None and t[1] is not None and not s["tp_adj"][0] and not s["tp_adj"][1] and t[0] != t[1]:
        rng = 2 * abs(t[0] - t[1])
        opp = t[1]
    else:
        return None
    sw = opp - d * rng
    return max(opp, sw), min(opp, sw)


def rng_err(c, yr) -> float:
    if not yr or yr[0] <= yr[1]:
        return 9.0
    return (abs(c.rH - yr[0]) + abs(c.rL - yr[1])) / (yr[0] - yr[1])


def no_crt_why(eng, s: dict, yr) -> str:
    """我們沒有這個 CRT：看我們自己的 HTF K 線，C2 到底有沒有掃到 C1。"""
    H = eng.H
    k = bisect.bisect_left(H.ts, s["c2"])
    if not eng.bars or eng.bars[0].t > s["c2"]:
        return "資料不夠舊"
    if k >= len(H.ts) or H.ts[k] != s["c2"] or k == 0:
        return "我們的 K 線沒有這根 C2（時間對不上 / 休市）"
    c1, c2 = H.bars[k - 1], H.bars[k]
    rng = max(c1.h - c1.l, 1e-12)
    off = (f"；我們的 C1 跟 Yuki 的區間差 {(abs(c1.h - yr[0]) + abs(c1.l - yr[1])) / rng:.0%}" if yr else "")
    if s["dir"] < 0 and c2.h <= c1.h:
        return f"我們的 C2 沒掃到 C1 高點（差 {(c1.h - c2.h) / rng:.0%} 區間{off}）"
    if s["dir"] > 0 and c2.l >= c1.l:
        return f"我們的 C2 沒掃到 C1 低點（差 {(c2.l - c1.l) / rng:.0%} 區間{off}）"
    old = [c for c in eng.history if c.dir == s["dir"] and c.t2 < s["c2"] and (c.xT is None or c.xT >= s["c2"])]
    if not old:
        return "有掃到，但前一個同方向 CRT 還在（同一段掃蕩，我們不重開）"
    o = max(old, key=lambda c: c.t2)
    if o.entered and o.eT >= s["c2"]:
        return (f"有掃到，但前一個同方向 CRT 還在（同一段掃蕩，我們不重開）· 舊的那張後來進場了 · "
                f"{'也到 TP2 以上' if our_max(o) >= 2 else '只到 TP1' if our_max(o) == 1 else '停損' if o.done else '持倉中'}")
    return "有掃到，但前一個同方向 CRT 還在（同一段掃蕩，我們不重開）· 舊的那張" + ("早就進場了" if o.entered else "沒進場")


def compare_all(signals: list[dict], cfg: dict, start: int, over: dict, log=print) -> dict:
    okx = OkxFeed()
    try:
        mt5 = Mt5Feed(cfg)
        mt5.m.search("EURUSD", 1)
    except Exception as e:
        log(f"MT5 連不上，外匯 / 金屬 / 指數不對照：{e}")
        mt5 = None
    groups: dict[tuple, list] = defaultdict(list)
    feeds = {}
    for s in signals:
        feed, inst, sc = route(s["symbol"], okx, mt5)
        s["src"], s["inst"], s["scale"] = (feed.name if feed else None), inst, sc
        if not feed:
            s["status"] = "商品對不到"
            continue
        feeds[inst] = feed
        groups[(inst, s["tf"], s["mode"] == "激進")].append(s)
    since = start - WARM
    ours: list[dict] = []          # 我們期間內所有進場的單（算勝率、找 Yuki 沒發的）
    t0 = time.time()
    for gi, ((inst, tf, aggr), sigs) in enumerate(sorted(groups.items())):
        feed = feeds[inst]
        try:
            eng = run_group(feed, inst, tf, aggr, cfg, since, over)
        except Exception as e:
            for s in sigs:
                s["status"], s["why"] = "我們跑不出來", f"{type(e).__name__}: {e}"
            log(f"{inst} {tf} {'激進' if aggr else '標準'}：{e}")
            continue
        tfms = TF_MS[tf]
        used = set()
        for s in sigs:
            sc = s["scale"]
            yr = y_range(s, sc)
            s["y_rH"], s["y_rL"] = yr if yr else (None, None)
            cands = [c for c in eng.history if c.dir == s["dir"] and abs(c.t2 - s["c2"]) <= tfms]
            if not cands:
                s["status"], s["why"] = "我們沒有這個 CRT", no_crt_why(eng, s, yr)
                continue
            c = min(cands, key=lambda c: (abs(c.t2 - s["c2"]), rng_err(c, yr)))
            used.add(id(c))
            bear = c.dir < 0
            ye = s["entry"] * sc if s["entry"] is not None else None
            ys = s["sl"] * sc if s["sl"] is not None else None
            risk = abs(ye - ys) if ye is not None and ys is not None and ye != ys else None
            rng = max(c.rH - c.rL, 1e-12)
            opp = c.rL if bear else c.rH
            s.update({
                "ours_tag": eng.tagId(c), "ours_model": c.model, "ours_kl": c.klName, "ours_range": f"{eng.px(c.rL)}–{eng.px(c.rH)}",
                "range_err": round(rng_err(c, yr), 3) if yr else None, "dt_c2_min": round((c.t2 - s["c2"]) / 60000),
                "ours_confirmed": c.confirmed, "ours_sl": c.sl,
                "d_sl_R": round((c.sl - ys) * (-c.dir) / risk, 2) if risk and c.sl is not None else None,
                "ours_tp3x": round((opp - c.tp3) * (-c.dir) / rng, 2) if c.tp3 is not None else None,
                "ours_tp4x": round((opp - c.tp4) * (-c.dir) / rng, 2) if c.tp4 is not None else None,
            })
            if c.entered:
                s.update({"status": "同一張", "ours_kind": c.eKind, "ours_entry": c.ePx, "ours_eT": c.eT,
                          "ours_hit_max": our_max(c), "ours_end": report.outcome(c), "ours_r": round(c.rz, 2),
                          "ours_stop": bool(c.stopped and our_max(c) == 0),
                          "d_entry_R": round((c.ePx - ye) * (-c.dir) / risk, 2) if risk else None,
                          "dt_entry_min": round((c.eT - s["sig_t"]) / 60000) if s.get("sig_t") and c.eT else None})
            else:
                why, det = why_not(c)
                s.update({"status": "我們有 CRT 沒進場", "why": why, "why_detail": det,
                          "ours_levels": " · ".join(f"{K_NAME[k][:1]} {eng.px(v)}" for k, v in c.lv.items() if v is not None)})
        for c in eng.trades:
            if c.eT and c.eT >= start:
                ours.append({"inst": inst, "src": feed.name, "tf": tf, "mode": "激進" if aggr else "標準", "tag": eng.tagId(c),
                             "kind": c.eKind, "entry": eng.px(c.ePx), "eT": c.eT, "max": our_max(c), "done": c.done,
                             "stop": bool(c.stopped and our_max(c) == 0), "end": report.outcome(c), "r": round(c.rz, 2),
                             "yuki": id(c) in used})
        log(f"[{gi + 1}/{len(groups)}] {inst} {tf} {'激進' if aggr else '標準'}：Yuki {len(sigs)} 張、我們進場 "
            f"{sum(1 for c in eng.trades if c.eT and c.eT >= start)} 筆（{time.time() - t0:.0f}s）")
    return {"ours": ours}


# ───────────── 輸出 ─────────────
def _px(v) -> str:
    if v is None:
        return "—"
    return f"{0.0 if abs(v) < 1e-9 else v:g}"


def _q(xs: list, q: float):
    xs = sorted(x for x in xs if x is not None)
    return xs[min(len(xs) - 1, int(q * len(xs)))] if xs else None


def _qtxt(xs: list, unit: str = "") -> str:
    xs = [x for x in xs if x is not None]
    if not xs:
        return "—"
    return f"中位數 {_q(xs, 0.5):g}{unit}（一半落在 {_q(xs, 0.25):g} ~ {_q(xs, 0.75):g}{unit}，{len(xs)} 筆）"


def _pct(a: int, b: int) -> str:
    return f"{a}（{a / b:.0%}）" if b else str(a)


def _ysig(s: dict) -> str:
    tps = " / ".join(_px(v) for v in s["tp"])
    return (f"{s['px_kind'] or '價'} {_px(s['entry'])} · SL {_px(s['sl'])} · TP {tps} · 最高 TP{s['hit_max']}"
            + (f" · 觸發 {s['trigger']}" if s.get("trigger") else "") + f" · KL {s.get('kl') or '—'} · TBS {s.get('tbs') or '—'}"
            + f" · CISD {s.get('cisd') or '—'} · {s.get('zone') or ''}")


def write_csv(signals: list[dict], name: str) -> None:
    cols = ["id", "posted", "tag", "symbol", "src", "inst", "tf", "mode", "dir", "px_kind", "entry", "sl", "tp1", "tp2", "tp3", "tp4",
            "hit_max", "trigger", "grade", "body", "zone", "tbs", "cisd", "fibo", "kl", "trend", "early", "status", "why", "why_detail",
            "y_rH", "y_rL", "ours_tag", "ours_model", "ours_kl", "ours_range", "range_err", "dt_c2_min", "ours_kind", "ours_entry", "d_entry_R", "dt_entry_min",
            "ours_sl", "d_sl_R", "ours_hit_max", "ours_end", "ours_r", "ours_tp3x", "ours_tp4x", "ours_levels"]
    with open(OUT / name, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(cols)
        for s in signals:
            row = dict(s, posted=yp.fmt_t(s["posted"]), tp1=s["tp"][0], tp2=s["tp"][1], tp3=s["tp"][2], tp4=s["tp"][3],
                       dir="空" if s["dir"] < 0 else "多", ours_kind=K_NAME.get(s.get("ours_kind"), ""))
            w.writerow(["" if row.get(c) is None else row.get(c) for c in cols])


def write_unparsed(parsed: dict) -> None:
    L = ["# 認不出來的訊息（新 → 舊）", "", "不是卡片、也不是每日結算的訊息（公告、廣告、聊天…）。如果其實是訊號，就是 yuki_parse.py 要補。", ""]
    for m in sorted(parsed["other"], key=lambda x: -x["id"])[:300]:
        if not (m["text"] or "").strip():
            continue
        L += [f"## #{m['id']} · {yp.fmt_t(m['posted'])}", "", "> " + m["text"].replace("\n", "\n> "), ""]
    (OUT / "unparsed.md").write_text("\n".join(L), encoding="utf-8")


def daily_table(daily: list[dict], start: int, ours: list[dict] | None) -> list[str]:
    """Yuki 每日結算加總（看得到停損）vs 我們同期間、同商品的單。"""
    d0 = datetime.fromtimestamp(start / 1000, yp.TPE).strftime("%Y-%m-%d")
    agg: dict[tuple, Counter] = defaultdict(Counter)
    days = defaultdict(set)
    r25: dict[str, list] = defaultdict(list)
    for d in daily:
        if d["date"] < d0:
            continue
        for tf, c in d["tf"].items():
            agg[(d["market"], tf)].update({k: v for k, v in c.items() if k != "win"})
            days[(d["market"], tf)].add(d["date"])
        if d.get("r25") is not None:
            r25[d["market"]].append(d["r25"])
    L = ["| 誰 | 市場 | TF | 天數 | 單數 | 最高 TP1 | TP2 | TP3 | TP4 | 停損 | 勝率（到 TP1 就算） | 到 TP2 以上 | 合計 R | 每筆 R |",
         "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for (mk, tf), c in sorted(agg.items()):
        n = sum(c[k] for k in ("tp1", "tp2", "tp3", "tp4", "sl"))
        win = n - c["sl"]
        L.append(f"| Yuki | {mk} | {tf} | {len(days[(mk, tf)])} | {n} | {c['tp1']} | {c['tp2']} | {c['tp3']} | {c['tp4']} | {c['sl']} | "
                 f"{win / n:.0%} | {(c['tp2'] + c['tp3'] + c['tp4']) / n:.0%} | | |" if n else f"| Yuki | {mk} | {tf} | 0 | 0 |||||||||")
    if ours:
        g: dict[tuple, Counter] = defaultdict(Counter)
        gr: dict[tuple, float] = defaultdict(float)
        for o in ours:
            if not o["done"] and o["max"] == 0:
                continue                                   # 還在持倉、還沒到 TP1：不算
            k = (o["mode"], MARKET.get(o["src"].lower(), o["src"]), o["tf"])
            g[k][("sl" if o["max"] == 0 else f"tp{o['max']}")] += 1
            gr[k] += o["r"]
        for k, c in sorted(g.items()):
            mode, mk, tf = k
            n = sum(c.values())
            L.append(f"| 我們（{mode}） | {mk} | {tf} | | {n} | {c['tp1']} | {c['tp2']} | {c['tp3']} | {c['tp4']} | {c['sl']} | "
                     f"{(n - c['sl']) / n:.0%} | {(c['tp2'] + c['tp3'] + c['tp4']) / n:.0%} | {gr[k]:+.1f} | {gr[k] / n:+.2f} |")
    if r25:
        L += ["", "Yuki 每日結算的「盈利參考 25/25/25/25」加總（1H + 4H，Yuki 沒寫到 TP1 後停損怎麼移）：" +
              "、".join(f"{mk} {sum(v):+.1f}R（{len(v)} 天，每天 {sum(v) / len(v):+.1f}R）" for mk, v in sorted(r25.items())),
              "我們的 R：分批 25/25/25/25、到 TP1 移保本（config 的 w_tp / be_tp1），持倉中的只算已實現。"]
    return L


def write_report(parsed: dict, sig: list[dict], comp: dict | None, cfg: dict, over: dict, start: int, days: int,
                 err: str | None, name: str) -> Path:
    p = dict(cfg.get("params", {}), **over)
    allsig = parsed["signals"]
    L = ["# Yuki × 我們 訊號對照", "",
         f"產生 {datetime.now(yp.TPE):%m/%d %H:%M} · 對照最近 {days} 天（C2 在 {yp.fmt_t(start)} 之後）· Yuki 全部歷史："
         f"{len(allsig)} 張單（{yp.fmt_t(allsig[0]['posted']) if allsig else '—'} 起）、每日結算 {len(parsed['daily'])} 則、其他 {len(parsed['other'])} 則",
         f"我們的設定：config.mt5.json params{('，這次改了 ' + json.dumps(over, ensure_ascii=False)) if over else ''}"
         f"（KL {'只做有 KL' if p.get('kl_only', True) else '不擋'} · ts_gap {p.get('ts_gap', True)} · min_rr {p.get('min_rr', 0)} · "
         f"進場 {''.join(K_NAME[k][0] for k in range(1, 9) if p.get(f'e{k}', k not in (7, 8)))}）",
         "", "> Yuki 的頻道**到 TP2 才發卡片**（之後 TP3、TP4 再發一次），所以卡片全部是贏的單；停損的只出現在每日結算的數字裡（第 2 節）。", ""]
    if err:
        L += [f"> ⚠ 沒有對照：{err}", ""]
    if not sig:
        L += ["這段期間沒有 Yuki 的訊號。"]
        return _save(L, name)
    st = Counter(s.get("status") for s in sig)
    modes = [("標準", "1H"), ("激進", "1H"), ("標準", "4H")]
    modes += sorted({(s["mode"], s["tf"]) for s in sig} - set(modes))

    def col(pred):
        return " | ".join(str(sum(1 for s in sig if (s["mode"], s["tf"]) == m and pred(s))) for m in modes) + f" | {sum(1 for s in sig if pred(s))}"
    same = lambda s: s.get("status") == "同一張"
    L += ["## 1. 對照總覽（Yuki 的贏單，我們抓到幾張）", "",
          "| | " + " | ".join(f"{a} {b}" for a, b in modes) + " | 合計 |", "|---" * (len(modes) + 2) + "|",
          "| Yuki 訊號（都到過 TP2） | " + col(lambda s: True) + " |",
          "| 我們也有這個 CRT（同 C2、同方向） | " + col(lambda s: s.get("status") in ("同一張", "我們有 CRT 沒進場")) + " |",
          "| 　└ 區間（C1 高低）跟 Yuki 差 ≤ 10% | " + col(lambda s: s.get("range_err") is not None and s["range_err"] <= 0.1) + " |",
          "| 我們也有進場 | " + col(same) + " |",
          "| 　├ 我們也到 TP2 以上 | " + col(lambda s: same(s) and s.get("ours_hit_max", 0) >= 2) + " |",
          "| 　├ 我們只到 TP1（之後保本） | " + col(lambda s: same(s) and s.get("ours_hit_max") == 1) + " |",
          "| 　└ 我們直接停損 | " + col(lambda s: same(s) and s.get("ours_stop")) + " |",
          "| 我們有 CRT 但沒進場 | " + col(lambda s: s.get("status") == "我們有 CRT 沒進場") + " |",
          "| 我們沒有這個 CRT | " + col(lambda s: s.get("status") == "我們沒有這個 CRT") + " |",
          "| 對不到（商品 / 資料） | " + col(lambda s: s.get("status") in ("商品對不到", "我們跑不出來", None)) + " |", ""]
    L += ["## 2. 真正的勝率：Yuki 每日結算 vs 我們（同期間、同一批商品）", "",
          "Yuki 的數字是每日結算加總（含停損）；我們的是同一批商品跑同期間（已結束的單 + 已經到 TP 的持倉）。", ""]
    L += daily_table(parsed["daily"], start, comp["ours"] if comp else None)
    nkl = sum(1 for s in sig if s.get("kl") and ("無" in s["kl"] or "NONE" in s["kl"].upper()))
    nkl_any = sum(1 for s in sig if s.get("kl"))
    rs = Counter(s.get("why", "") for s in sig if s.get("status") == "我們有 CRT 沒進場")
    hd = " | ".join(f"{a} {b}" for a, b in modes)
    L += ["", "## 3. 我們有這個 CRT、但沒進場的原因", "", f"| 原因 | {hd} | 合計 | 可能要調 |", "|---" * (len(modes) + 3) + "|"]
    L += [f"| {r or '（沒有原因）'} | " + col(lambda s, r=r: s.get("status") == "我們有 CRT 沒進場" and s.get("why", "") == r)
          + f" | {next((v for k, v in HINT.items() if k in r), '')} |" for r, n in rs.most_common()] or ["| — | 0 | |"]
    det = Counter(s.get("why_detail", "") for s in sig if s.get("status") == "我們有 CRT 沒進場" and s.get("why_detail"))
    if det:
        L += ["", "細節（最後擋下來的進場訊號）：" + "；".join(f"{k} ×{v}" for k, v in det.most_common(8))]
    if nkl_any:
        L += ["", f"Yuki 卡片有寫 KL 欄的 {nkl_any} 張裡，KL 寫「無」的有 {_pct(nkl, nkl_any)}——Yuki 沒有 KL 也照發。"]
    rn = Counter(re.sub(r"（[^）]*）", "", s.get("why", "")) for s in sig if s.get("status") == "我們沒有這個 CRT")
    L += ["", "## 4. 我們沒有這個 CRT 的原因", "", f"| 原因 | {hd} | 合計 |", "|---" * (len(modes) + 2) + "|"]
    L += [f"| {r} | " + col(lambda s, r=r: s.get("status") == "我們沒有這個 CRT" and re.sub(r"（[^）]*）", "", s.get("why", "")) == r) + " |"
          for r, n in rn.most_common()] or ["| — | 0 |"]
    errs = [s["range_err"] for s in sig if s.get("range_err") is not None]
    L += ["", f"同一個 CRT 時，我們的區間跟 Yuki 的差（佔區間）：{_qtxt([round(e * 100, 1) for e in errs], '%')}"]
    L += ["", "## 5. 同一張單：進場、停損、目標差在哪", ""]
    for m in modes:
        g = [s for s in sig if same(s) and (s["mode"], s["tf"]) == m]
        if not g:
            continue
        kinds = Counter(K_NAME.get(s["ours_kind"], "?") for s in g)
        L += [f"**{m[0]} {m[1]}（{len(g)} 張）**", "",
              f"- 我們用的進場訊號：" + "、".join(f"{k} {v}" for k, v in kinds.most_common()),
              f"- 進場價比 Yuki 好多少 R（正 = 我們價格比較好）：{_qtxt([s.get('d_entry_R') for s in g], 'R')}",
              f"- 進場時間比 Yuki 晚幾分鐘：{_qtxt([s.get('dt_entry_min') for s in g], ' 分')}",
              f"- 停損比 Yuki 寬多少 R（正 = 我們比較寬）：{_qtxt([s.get('d_sl_R') for s in g], 'R')}"]
        if m[0] == "激進":
            trig = Counter((s.get("trigger") or "?").split(" + ")[0] for s in g)
            L.append("- Yuki 的觸發來源（第一個）：" + "、".join(f"{k} {v}" for k, v in trig.most_common(6)))
        L.append("")
    x3 = [s.get("ours_tp3x") for s in sig if s.get("ours_tp3x") is not None]
    x4 = [s.get("ours_tp4x") for s in sig if s.get("ours_tp4x") is not None]
    L += [f"TP3 / TP4：Yuki 固定在對側再過 0.5 / 1.5 個區間；我們是對側外面的流動性 → TP3 {_qtxt(x3, ' 個區間')}；TP4 {_qtxt(x4, ' 個區間')}", ""]
    L += ["## 6. 依商品", "", "| 商品 | 我們用 | Yuki | 同一張 | 我們也到 TP2+ | 沒進場 | 沒有 CRT | 對不到 |", "|---|---|---|---|---|---|---|---|"]
    by = defaultdict(list)
    for s in sig:
        by[(s["symbol"], s.get("inst"))].append(s)
    for (sym, inst), g in sorted(by.items(), key=lambda x: -len(x[1])):
        c = Counter(s.get("status") for s in g)
        L.append(f"| {sym} | {inst or '—'} | {len(g)} | {c['同一張']} | {sum(1 for s in g if same(s) and s.get('ours_hit_max', 0) >= 2)} | "
                 f"{c['我們有 CRT 沒進場']} | {c['我們沒有這個 CRT']} | {c['商品對不到'] + c['我們跑不出來']} |")
    if comp:
        miss = [o for o in comp["ours"] if not o["yuki"] and o["max"] >= 2]
        allo = comp["ours"]
        L += ["", f"## 7. 我們有到 TP2 以上、Yuki 沒發（{len(miss)} 筆；我們期間內共進場 {len(allo)} 筆，"
                  f"其中 {sum(1 for o in allo if o['yuki'])} 筆是 Yuki 也有的）", "",
              "Yuki 到 TP2 才發，所以拿我們「也到 TP2」的來比才公平。這些是 Yuki 沒有、我們有的贏單（可能是 Yuki 的過濾比較嚴，或行情差）。", "",
              "| 我們 | 模式 | 進場 | 價格 | 結果 | R |", "|---|---|---|---|---|---|"]
        L += [f"| {o['tag']} | {o['mode']} | {K_NAME.get(o['kind'], '?')} | {o['entry']} | {o['end']} | {o['r']:+.2f} |"
              for o in sorted(miss, key=lambda x: -x["eT"])[:40]]
    bad = [s for s in sig if s.get("status") != "同一張"]
    L += ["", f"## 8. 逐筆：Yuki 有、我們沒進場（最近 60 筆，共 {len(bad)}）", ""]
    for s in sorted(bad, key=lambda x: -x["c2"])[:60]:
        L += [f"- **{s['tag']}** {s['symbol']} {s['tf']} {'空' if s['dir'] < 0 else '多'} {s['mode']} · C2 {yp.fmt_t(s['c2'])}"
              f" · {s.get('status', '—')}：{s.get('why', '')}" + (f"（{s['why_detail']}）" if s.get("why_detail") else ""),
              f"  - Yuki：{_ysig(s)}"]
        if s.get("ours_tag"):
            L.append(f"  - 我們：{s['ours_tag']} · 區間 {s['ours_range']}（差 {s['range_err']:.0%}）· KL {s.get('ours_kl') or '—'} · M{s.get('ours_model')}"
                     + (f" · 當時掛單 {s['ours_levels']}" if s.get("ours_levels") else ""))
    L += ["", "---", "要調的地方寫在 CRT_trader 的 CLAUDE.md「接下來要做」；改引擎時 crt_engine.py 和 Pine 一起改，改完重跑 "
          "`python yuki_report.py` 看第 1、2 節有沒有變好（試參數用 --set … --out 名稱，不會蓋掉主檔）。"]
    return _save(L, name)


def _save(L: list[str], name: str) -> Path:
    path = OUT / name
    path.write_text("\n".join(L), encoding="utf-8")
    return path


def build(compare_ours: bool = True, compare: bool | None = None, days: int | None = None, over: dict | None = None,
          out: str = "", log=print) -> Path:
    if compare is not None:
        compare_ours = compare
    days = int(days or os.environ.get("YUKI_DAYS") or 30)
    over = over or {}
    OUT.mkdir(parents=True, exist_ok=True)
    raw = tg_collect.load_raw()
    parsed = yp.parse_all(raw)
    import scan
    cfg = scan.load_cfg(ROOT / "config.mt5.json")
    start = int(time.time() * 1000) - days * DAY
    sig = [dict(s) for s in parsed["signals"] if s["c2"] >= start]
    comp, err = None, None
    if compare_ours and sig:
        try:
            comp = compare_all(sig, cfg, start, over, log)
        except Exception as e:
            err = f"{type(e).__name__}: {e}"
            log(f"對照失敗：{err}")
    sfx = f"_{out}" if out else ""
    write_csv(sig, f"signals{sfx}.csv")
    write_unparsed(parsed)
    path = write_report(parsed, sig, comp, cfg, over, start, days, err, f"yuki_vs_ours{sfx}.md")
    if not out:
        (OUT / "summary.json").write_text(json.dumps({
            "t": int(time.time() * 1000), "messages": len(raw), "signals_all": len(parsed["signals"]), "days": days,
            "signals": len(sig), "same": sum(1 for s in sig if s.get("status") == "同一張"),
            "same_crt": sum(1 for s in sig if s.get("status") in ("同一張", "我們有 CRT 沒進場")),
            "daily": len(parsed["daily"]), "other": len(parsed["other"]), "error": err}, ensure_ascii=False), encoding="utf-8")
    return path


def _val(v: str):
    lv = v.strip().lower()
    if lv in ("true", "false"):
        return lv == "true"
    try:
        return json.loads(v)
    except json.JSONDecodeError:
        return v


if __name__ == "__main__":
    for st in (sys.stdout, sys.stderr):
        try:
            st.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-compare", action="store_true")
    ap.add_argument("--days", type=int)
    ap.add_argument("--set", action="append", default=[], metavar="參數=值", help="蓋過 config.mt5.json 的 params，例如 kl_only=false")
    ap.add_argument("--out", default="", help="輸出檔名加這個後綴（試參數用，不蓋主檔）")
    a = ap.parse_args()
    ov = {k.strip(): _val(v) for k, v in (x.split("=", 1) for x in a.set)}
    print(build(compare_ours=not a.no_compare, days=a.days, over=ov, out=a.out))
