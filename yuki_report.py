"""Yuki 訊號整理 + 跟我們的引擎對照 → 一份檔案（給使用者和 Claude 看了再調引擎）。

  python yuki_report.py            重新整理（會跑我們的引擎對照，要 MT5 開著）
  python yuki_report.py --no-compare   只整理 Yuki 的訊號

輸出（$CRT_DATA_DIR/yuki/，只在本機）：
  yuki_vs_ours.md  主檔：總覽、沒進場的原因、依商品、Yuki 的結果、逐筆不一致（含原文）、我們多的
  signals.csv      每張 Yuki 訊號一列：抓到的欄位 + 對到的我們那張
  unparsed.md      認不出來的訊息原文（拿來補解析規則）

對照方法：Yuki 的 #編號 有 C2 時間就用「同商品、同方向、C2 差 ≤ 1 根」找我們的 CRT（引擎的 history，含沒進場、被過濾的）；
沒有時間就用發文前 3 天內、SL / 進場價最接近的。標準對標準（aggressive=False）、激進對激進（aggressive=True）。
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import report  # noqa: E402
import tg_collect  # noqa: E402
import yuki_parse as yp  # noqa: E402
from crt_engine import K_NAME, TF_MS  # noqa: E402

LTF_OF = {"15m": "1m", "1H": "5m", "4H": "15m", "1D": "1H"}      # Pine 建議的 CRT → 進場時間框架
OUT = tg_collect.DIR


def norm_tf(tf: str | None) -> str:
    if not tf:
        return "1H"
    t = tf.strip().upper().replace("MIN", "M")
    return {"15M": "15m", "1H": "1H", "60M": "1H", "4H": "4H", "240M": "4H", "D": "1D", "1D": "1D", "D1": "1D", "H1": "1H",
            "H4": "4H"}.get(t, "1H")


_sym_cache: dict[str, str | None] = {}


def map_symbol(sym: str | None) -> str | None:
    """Yuki 的商品名 → MT5 上的（XAUUSD → XAUUSDm、NAS100 → USTECm、BTCUSDT.P → BTCUSDm）。"""
    if not sym:
        return None
    if sym in _sym_cache:
        return _sym_cache[sym]
    import mt5src
    base = sym.upper().replace(".P", "").replace("PERP", "")
    hit = None
    for q in (base, base.replace("USDT", "USD"), base[:6]):
        items = mt5src.search(q, 5)
        if items:
            hit = items[0]["name"]
            break
    _sym_cache[sym] = hit
    return hit


def hit_max(c) -> int:
    if c.tp4Hit:
        return 4
    if c.tp3Hit:
        return 3
    if c.tp2:
        return 2
    return 1 if c.tp1 and not c.t1Skip else 0


def why_not(c) -> str:
    """我們這個 CRT 為什麼沒進場（引擎留下的原因）。"""
    s = c.skipTxt or c.result or c.note or ("還在等（沒結束）" if not c.done else "")
    return s.replace("跳過：", "").replace("未進場：", "")


def run_ours(mt5sym: str, tf: str, aggressive: bool, since: int, cfg: dict):
    import mt5src
    import scan
    ltf = LTF_OF.get(tf, "5m")
    now = time.time() * 1000
    n_ltf = int(min(99_000, (now - since) / TF_MS[ltf] + 600))
    n_htf = int(min(20_000, (now - since) / TF_MS[tf] + 600))
    c2 = dict(cfg, ltf=ltf, ltf_bars=n_ltf, htf_bars=n_htf,
              params=dict(cfg.get("params", {}), crt_tf=tf, aggressive=aggressive, kl_tf=cfg.get("params", {}).get("kl_tf", "4H")))
    return scan.run_symbol(mt5sym, c2, mt5src.tick_size(mt5sym))


def match(sig: dict, eng, tf_ms: int):
    cands = [c for c in eng.history if c.dir == sig["dir"]]
    if sig.get("c2"):
        near = [c for c in cands if abs(c.t2 - sig["c2"]) <= tf_ms]
        if near:
            return min(near, key=lambda c: (abs(c.t2 - sig["c2"]), abs((c.sl or 0) - (sig["sl"] or c.sl or 0)))), "C2 時間"
        return None, "C2 時間"
    if sig["sl"] is None and sig["entry"] is None:
        return None, "資訊不足"
    lo = sig["posted"] - 3 * 86_400_000
    near = [c for c in cands if lo <= c.t2 <= sig["posted"]]

    def score(c):
        rng = max(c.rH - c.rL, 1e-12)
        s = abs((c.sl or 0) - sig["sl"]) / rng if sig["sl"] is not None and c.sl is not None else 1.0
        if sig["entry"] is not None:
            px = [c.ePx] if c.entered else [v for v in c.lv.values() if v is not None] or [c.cisdLvl or c.mid]
            s += min(abs(p - sig["entry"]) for p in px if p is not None) / rng
        return s
    if not near:
        return None, "價位"
    best = min(near, key=score)
    return (best if score(best) <= 0.6 else None), "價位"


def compare_signals(signals: list[dict], cfg: dict) -> dict:
    """每張 Yuki 訊號找我們的 CRT；另外列出我們有進場、Yuki 沒發的。"""
    groups: dict[tuple, list] = {}
    for s in signals:
        s["mt5"] = map_symbol(s["symbol"])
        s["tfn"] = norm_tf(s["tf"])
        if not s["mt5"]:
            s["status"] = "商品對不到"
            continue
        groups.setdefault((s["mt5"], s["tfn"], s["mode"] == "激進"), []).append(s)
    extra = []
    for (sym, tf, aggr), sigs in groups.items():
        since = min((s.get("c2") or s["posted"]) for s in sigs) - 10 * 86_400_000
        try:
            eng = run_ours(sym, tf, aggr, since, cfg)
        except Exception as e:
            for s in sigs:
                s["status"], s["ours_reason"] = "我們跑不出來", str(e)
            continue
        used = set()
        for s in sigs:
            c, how = match(s, eng, TF_MS[tf])
            s["match_by"] = how
            if c is None:
                s["status"] = "資訊不足" if how == "資訊不足" else "我們沒有這個 CRT"
                continue
            used.add(id(c))
            bear = c.dir < 0
            risk = abs((s["entry"] if s["entry"] is not None else (c.ePx or c.cisdLvl or c.mid)) - (s["sl"] or c.sl or 0)) or None
            s.update({
                "ours_tag": eng.tagId(c), "ours_model": c.model, "ours_kl": c.klName, "ours_range": f"{eng.px(c.rL)}–{eng.px(c.rH)}",
                "ours_sl": c.sl, "ours_ext": c.ext, "dt_c2_min": round((c.t2 - s["c2"]) / 60000) if s.get("c2") else None,
                "d_sl_R": round((c.sl - s["sl"]) / risk * (-1 if bear else 1), 2) if risk and s["sl"] is not None and c.sl is not None else None,
            })
            if c.entered:
                s.update({"status": "同一張", "ours_kind": c.eKind, "ours_entry": c.ePx, "ours_hit_max": hit_max(c),
                          "ours_end": report.outcome(c), "ours_r": round(c.rz, 2),
                          "same_kind": bool(s["kind"]) and s["kind"] == c.eKind,
                          "d_entry_R": round((c.ePx - s["entry"]) / risk * (-1 if bear else 1), 2) if risk and s["entry"] is not None else None})
            else:
                s.update({"status": "我們有 CRT 沒進場", "ours_reason": why_not(c),
                          "ours_levels": " · ".join(f"{K_NAME[k][:1]} {eng.px(v)}" for k, v in c.lv.items() if v is not None)})
        t0 = min(s.get("c2") or s["posted"] for s in sigs)
        t1 = max(s["posted"] for s in sigs)
        for c in eng.trades:
            if id(c) not in used and t0 <= c.t2 <= t1:
                extra.append({"sym": sym, "tf": tf, "mode": "激進" if aggr else "標準", "tag": eng.tagId(c),
                              "kind": eng.kName(c.eKind), "entry": eng.px(c.ePx), "end": report.outcome(c), "r": round(c.rz, 2),
                              "t": c.eT})
    return {"extra": sorted(extra, key=lambda x: -x["t"])}


# ───────────── 輸出 ─────────────
def _px(v) -> str:
    if v is None:
        return "—"
    return f"{0.0 if abs(v) < 1e-9 else v:g}"


def _sig_line(s: dict) -> str:
    tps = " / ".join(_px(v) for v in s["tp"])
    res = f"最高 TP{s['hit_max']}" if s["hit_max"] else "沒寫到 TP"
    return (f"{K_NAME.get(s['kind'], s['trigger'] or '進場方式？')} {s['order'] or ''} {_px(s['entry'])} · SL {_px(s['sl'])} · TP {tps}"
            f" · {res}{(' · ' + s['end']) if s['end'] else ''} · KL {s['kl'] or '—'} · M{s['model'] or '?'}")


def write_csv(signals: list[dict]) -> None:
    cols = ["id", "posted", "tag", "symbol", "mt5", "tf", "mode", "dir", "order", "entry", "sl", "tp1", "tp2", "tp3", "tp4",
            "kind", "trigger", "kl", "model", "session", "hit_max", "end", "status", "ours_tag", "ours_kind", "ours_entry",
            "ours_sl", "d_entry_R", "d_sl_R", "dt_c2_min", "ours_hit_max", "ours_end", "ours_r", "ours_reason", "match_by"]
    with open(OUT / "signals.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(cols)
        for s in signals:
            row = dict(s, posted=yp.fmt_t(s["posted"]), tp1=s["tp"][0], tp2=s["tp"][1], tp3=s["tp"][2], tp4=s["tp"][3],
                       dir="空" if s["dir"] < 0 else "多")
            w.writerow([row.get(c, "") if row.get(c) is not None else "" for c in cols])


def write_unparsed(parsed: dict) -> None:
    L = ["# 認不出來的訊息（新 → 舊）", "", "這些沒有被當成訊號或結果。如果其實是訊號 / 結果，就是解析規則要補（yuki_parse.py）。", ""]
    for m in sorted(parsed["other"], key=lambda x: -x["id"])[:300]:
        if not m["text"].strip():
            continue
        L += [f"## #{m['id']} · {yp.fmt_t(m['posted'])}" + (f" · 回覆 #{m['reply_to']}" if m["reply_to"] else ""), "",
              "> " + m["text"].replace("\n", "\n> "), ""]
    unl = [r for r in parsed["results"] if not r.get("linked")]
    if unl:
        L += ["# 是結果、但找不到是哪一張訊號的", ""]
        for m in sorted(unl, key=lambda x: -x["id"])[:100]:
            L += [f"## #{m['id']} · {yp.fmt_t(m['posted'])}", "", "> " + m["text"].replace("\n", "\n> "), ""]
    (OUT / "unparsed.md").write_text("\n".join(L), encoding="utf-8")


def write_report(parsed: dict, comp: dict | None, cfg: dict, err: str | None) -> Path:
    sig = parsed["signals"]
    modes = ["標準", "激進"]
    cnt = lambda pred, mode=None: sum(1 for s in sig if (mode is None or s["mode"] == mode) and pred(s))
    row = lambda name, pred: f"| {name} | " + " | ".join(str(cnt(pred, m)) for m in modes) + f" | {cnt(pred)} |"
    p = cfg.get("params", {})
    span = (f"{yp.fmt_t(min(s['posted'] for s in sig))} – {yp.fmt_t(max(s['posted'] for s in sig))}" if sig else "—")
    L = ["# Yuki × 我們 訊號對照", "",
         f"產生 {datetime.now(yp.TPE):%m/%d %H:%M} · 頻道 {os.environ.get('TG_CHAT_NAME', '—')} · 訊息 "
         f"{len(parsed['signals']) + len(parsed['results']) + len(parsed['other'])} 則（訊號 {len(sig)}、結果 {len(parsed['results'])}、"
         f"其他 {len(parsed['other'])}）· 訊號期間 {span}",
         f"我們的設定：config.mt5.json params（KL {'只做有 KL' if p.get('kl_only', True) else '不擋'} · TS 間隔 {p.get('ts_gap', True)} · "
         f"min_rr {p.get('min_rr', 0)} · 進場 ①～⑧ 開關 {[k for k in range(1, 9) if p.get(f'e{k}', k not in (7, 8))]}）；"
         "標準對 aggressive=False、激進對 aggressive=True；時間框架照 Yuki 的（15M→1M、1H→5M、4H→15M 進場）", ""]
    if err:
        L += [f"> ⚠ 沒有對照：{err}", ""]
    if not sig:
        L += ["還沒有認得的訊號。看 unparsed.md，把 Yuki 的實際格式補進 yuki_parse.py。"]
    else:
        same = lambda s: s.get("status") == "同一張"
        L += ["## 1. 總覽", "", "| | 標準 | 激進 | 合計 |", "|---|---|---|---|",
              row("Yuki 訊號", lambda s: True),
              row("我們同一張也有進場", same),
              row("　├ 進場訊號一樣（①～⑧）", lambda s: same(s) and s.get("same_kind")),
              row("　├ 進場價差 ≤ 0.1R", lambda s: same(s) and s.get("d_entry_R") is not None and abs(s["d_entry_R"]) <= 0.1),
              row("　└ 最高 TP 一樣（Yuki 有寫結果的）", lambda s: same(s) and s["hit_max"] > 0 and s.get("ours_hit_max") == s["hit_max"]),
              row("我們有這個 CRT 但沒進場", lambda s: s.get("status") == "我們有 CRT 沒進場"),
              row("我們沒有這個 CRT", lambda s: s.get("status") == "我們沒有這個 CRT"),
              row("對不到（資訊不足 / 商品對不到 / 跑不出來）", lambda s: s.get("status") in ("資訊不足", "商品對不到", "我們跑不出來")),
              ""]
        if comp:
            ex = Counter(x["mode"] for x in comp["extra"])
            L += [f"我們有進場、Yuki 沒發（同商品、同期間）：標準 {ex['標準']} · 激進 {ex['激進']}（見第 7 節）", ""]
        rs = Counter(s.get("ours_reason", "") for s in sig if s.get("status") == "我們有 CRT 沒進場")
        hint = {"附近沒有 KL": "KL 判斷（klCheck、kl_tol_pct、KL 來源）", "TS 沒間隔 1 根 K": "ts_gap", "逾時": "進場訊號偵測 / c3_bars",
                "已到對側": "進場太慢（CISD / IFVG / BB 偵測）", "被新 CRT 取代": "retire 規則", "區間偏小": "min_rng",
                "區間偏大": "max_rng", "不是 TBS": "tbs_only"}
        L += ["## 2. Yuki 有、我們沒進場的原因", "", "| 原因 | 次數 | 可能要調 |", "|---|---|---|"]
        L += [f"| {r or '（沒有原因）'} | {n} | {next((v for k, v in hint.items() if k in r), '')} |" for r, n in rs.most_common()] or ["| — | 0 | |"]
        L += ["", "## 3. 依商品 / 時間框架", "", "| 商品 | MT5 | TF | Yuki | 同一張 | 沒進場 | 沒有 CRT | 對不到 |", "|---|---|---|---|---|---|---|---|"]
        for key, ss in sorted(Counter((s["symbol"], s.get("mt5"), s.get("tfn")) for s in sig).items(), key=lambda x: -x[1]):
            g = [s for s in sig if (s["symbol"], s.get("mt5"), s.get("tfn")) == key]
            c = Counter(s.get("status") for s in g)
            L.append(f"| {key[0]} | {key[1] or '—'} | {key[2]} | {len(g)} | {c['同一張']} | {c['我們有 CRT 沒進場']} | {c['我們沒有這個 CRT']} | "
                     f"{c['資訊不足'] + c['商品對不到'] + c['我們跑不出來']} |")
        L += ["", "## 4. Yuki 的結果（最高到 TP 幾；後面保本還是停損，訊息大多沒寫）", "", "| 最高到 | 標準 | 激進 |", "|---|---|---|"]
        for h in (4, 3, 2, 1, 0):
            L.append(f"| {'TP' + str(h) if h else '沒寫 TP'} | {cnt(lambda s, h=h: s['hit_max'] == h, '標準')} | {cnt(lambda s, h=h: s['hit_max'] == h, '激進')} |")
        bad = [s for s in sig if s.get("status") != "同一張"]
        L += ["", f"## 5. Yuki 有、我們沒有同一張（{len(bad)} 筆，新 → 舊）", ""]
        for s in sorted(bad, key=lambda x: -x["posted"])[:80]:
            L += [f"### {s['tag'] or '#' + str(s['id'])} · {s['symbol']} {s.get('tfn')} {'空' if s['dir'] < 0 else '多'} · {s['mode'] or '模式？'}"
                  f" · Yuki 發文 {yp.fmt_t(s['posted'])}" + (f" · C2 {yp.fmt_t(s['c2'])}" if s.get("c2") else ""),
                  f"- 狀態：**{s.get('status', '—')}**" + (f" —— {s['ours_reason']}" if s.get("ours_reason") else ""),
                  f"- Yuki：{_sig_line(s)}"]
            if s.get("ours_tag"):
                L.append(f"- 我們：{s['ours_tag']} · 區間 {s['ours_range']} · 掃點 {_px(s.get('ours_ext'))} · SL 差 {_px(s.get('d_sl_R'))}R"
                         f" · C2 差 {s.get('dt_c2_min')} 分 · KL {s.get('ours_kl') or '—'} · M{s.get('ours_model')}"
                         + (f" · 當時的掛單 {s['ours_levels']}" if s.get("ours_levels") else ""))
            L += ["", "> " + s["text"].replace("\n", "\n> "), ""]
        # Yuki 沒寫結果（hit_max 0）時不算「結果不一樣」
        diff = [s for s in sig if s.get("status") == "同一張" and not (s.get("same_kind") and (s["hit_max"] == 0 or s.get("ours_hit_max") == s["hit_max"])
                                                                   and s.get("d_entry_R") is not None and abs(s["d_entry_R"]) <= 0.1)]
        L += [f"## 6. 同一張但不一樣（{len(diff)} 筆：進場訊號 / 價差 / 結果）", "",
              "| Yuki | 模式 | Yuki 進場 | 我們進場 | 價差 R | SL 差 R | Yuki 最高 | 我們 | 我們結果 |", "|---|---|---|---|---|---|---|---|---|"]
        for s in sorted(diff, key=lambda x: -x["posted"])[:80]:
            L.append(f"| {s['tag'] or s['id']} | {s['mode']} | {K_NAME.get(s['kind'], '?')[:1]} {_px(s['entry'])} | "
                     f"{K_NAME.get(s.get('ours_kind'), '?')[:1]} {_px(s.get('ours_entry'))} | {_px(s.get('d_entry_R'))} | {_px(s.get('d_sl_R'))} | "
                     f"{'TP' + str(s['hit_max']) if s['hit_max'] else '沒寫'} | {'TP' + str(s.get('ours_hit_max')) if s.get('ours_hit_max') else '沒到 TP'} | {s.get('ours_end', '')} {s.get('ours_r', '')}R |")
        if comp:
            L += ["", f"## 7. 我們有進場、Yuki 沒發（最近 40 筆，共 {len(comp['extra'])}）", "",
                  "| 我們 | 模式 | 進場 | 價格 | 結果 | R |", "|---|---|---|---|---|---|"]
            L += [f"| {x['tag']} | {x['mode']} | {x['kind']} | {x['entry']} | {x['end']} | {x['r']:+.2f} |" for x in comp["extra"][:40]]
    L += ["", "---", "看完要調的地方：寫在 CRT_trader 的 CLAUDE.md「接下來要做」，改引擎時 crt_engine.py 和 Pine 一起改，"
          "改完重跑 `python yuki_report.py` 看總覽有沒有變好。"]
    path = OUT / "yuki_vs_ours.md"
    path.write_text("\n".join(L), encoding="utf-8")
    return path


def build(compare_ours: bool = True, compare: bool | None = None) -> Path:
    if compare is not None:
        compare_ours = compare
    OUT.mkdir(parents=True, exist_ok=True)
    parsed = yp.parse_all(tg_collect.load_raw())
    import scan
    cfg = scan.load_cfg(ROOT / "config.mt5.json")
    comp, err = None, None
    if compare_ours and parsed["signals"]:
        try:
            import mt5src
            mt5src.configure(cfg.get("mt5_path", ""))
            comp = compare_signals(parsed["signals"], cfg)
        except Exception as e:
            err = f"{type(e).__name__}: {e}"
    write_csv(parsed["signals"])
    write_unparsed(parsed)
    path = write_report(parsed, comp, cfg, err)
    (OUT / "summary.json").write_text(json.dumps({
        "t": int(time.time() * 1000), "messages": len(tg_collect.load_raw()), "signals": len(parsed["signals"]),
        "results": len(parsed["results"]), "other": len(parsed["other"]),
        "same": sum(1 for s in parsed["signals"] if s.get("status") == "同一張"), "error": err}, ensure_ascii=False), encoding="utf-8")
    return path


if __name__ == "__main__":
    for st in (sys.stdout, sys.stderr):
        try:
            st.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-compare", action="store_true")
    a = ap.parse_args()
    print(build(compare_ours=not a.no_compare))
