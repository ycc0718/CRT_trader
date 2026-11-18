"""Yuki 的 Telegram 訊息：分類 + 抓欄位。

類型：
  signal  有方向、也有價位（進場 / 掛單 / SL / TP）——一張 CRT 單
  result  說打到 TP 幾、停損、保本（通常是回覆原訊號、或帶同一個 #編號）
  other   其他（公告、圖、聊天…）

規則照 CRT 卡片的格式寫（🔍 #編號、【激進 / 標準】、💰 進場價 / 掛單價、🎯 TP1…、🚫 SL、⚡ 觸發來源、🔑 KL…），
看到真正的 Yuki 訊息後再補；認不出來的會整理在 unparsed.md，不會丟掉。
訊號被事後編輯（例如補上 TP2 ✅）時，用最新的文字，舊版本留在 raw.json。
"""
from __future__ import annotations

import re
from datetime import datetime
from zoneinfo import ZoneInfo

TPE = ZoneInfo("Asia/Taipei")
NUM = r"([0-9][0-9,]*(?:\.[0-9]+)?)"
HIT = r"(✅|✔️|✔|☑️|☑|🎯✅|達成|到達|打到|已到|止盈|hit|HIT|Hit)"
STOP_WORDS = {"CRT", "SHORT", "LONG", "TP", "SL", "RR", "TBS", "TWS", "CISD", "KL", "OB", "FVG", "IFVG", "BB", "MSS",
              "QM", "FIB", "OHP", "OLP", "EQH", "EQL", "PDH", "PDL", "CRH", "CRL", "NY", "LD", "AS", "TKO", "BUY", "SELL",
              "H1", "H4", "D1", "M1", "M2", "M3", "M4", "M5", "M15", "LTF", "HTF", "SMT", "AMD", "PO3", "EQ", "ATR", "UTC"}
K_OF = {"①": 1, "②": 2, "③": 3, "④": 4, "⑤": 5, "⑥": 6, "⑦": 7, "⑧": 8}
K_WORD = [(1, r"CISD\s*收盤"), (2, r"回踩\s*CISD"), (3, r"IFVG"), (4, r"LTF\s*OB|MSS"), (5, r"QM|左肩"), (6, r"\bBB\b|回測\s*BB"),
          (7, r"FIB|斐波|0\.6|0\.7"), (8, r"C2\s*收盤")]


def _num(s: str | None) -> float | None:
    try:
        return float(s.replace(",", "")) if s else None
    except ValueError:
        return None


def _first(pat: str, text: str, grp: int = 1, flags=0) -> str | None:
    m = re.search(pat, text, flags)
    return m.group(grp).strip() if m else None


def direction(text: str) -> int:
    line = _first(r"方向[：:]\s*([^\n]+)", text) or ""
    for src in (line, text):
        best = None
        for pat, d in ((r"看空|SHORT|Short|做空|空單|空方|賣出|\bSELL\b|\bSell\b", -1), (r"看多|LONG|Long|做多|多單|多方|買入|\bBUY\b|\bBuy\b", 1)):
            m = re.search(pat, src)
            if m and (best is None or m.start() < best[0]):
                best = (m.start(), d)
        if best:
            return best[1]
    return 0


def parse_tag(tag: str | None, posted: int) -> dict:
    """#XAUUSD1H111821 → 商品、時間框架、C2 那根的時間（台灣時間 mmddHH，年份用發文時間推）。"""
    out = {"tag_sym": None, "tag_tf": None, "c2": None}
    if not tag:
        return out
    m = re.match(r"([A-Za-z][A-Za-z0-9.]*?)(\d{1,2}[MmHh]|[Dd]|[Hh]\d)?(\d{6,8})(?:\D.*)?$", tag)
    if not m:
        out["tag_sym"] = re.sub(r"[^A-Za-z0-9.].*$", "", tag) or None
        return out
    out["tag_sym"], tf, ds = m.group(1), m.group(2), m.group(3)
    if tf:
        out["tag_tf"] = tf.upper().replace("M", "m") if tf[-1] in "mM" else tf.upper()
    try:
        mo, dd, hh = int(ds[0:2]), int(ds[2:4]), int(ds[4:6])
        post = datetime.fromtimestamp(posted / 1000, TPE)
        y = post.year - (1 if mo > post.month + 1 else 0)
        out["c2"] = int(datetime(y, mo, dd, hh, tzinfo=TPE).timestamp() * 1000)
    except ValueError:
        pass
    return out


def symbol(text: str, tag_sym: str | None) -> str | None:
    if tag_sym and tag_sym.upper() not in STOP_WORDS:
        return tag_sym
    head = "\n".join(text.splitlines()[:3])
    for tok in re.findall(r"\b([A-Z][A-Z0-9]{2,11}(?:\.[A-Z]+)?)\b", head):
        if tok not in STOP_WORDS and not re.fullmatch(r"M\d+|H\d+|TP\d|RR\d*", tok):
            return tok
    return None


def timeframe(text: str, tag_tf: str | None) -> str | None:
    m = re.search(r"\b(\d{1,2})\s*([mMhH])\b\s*(?:CRT|級別|時框)?", text)
    if m and ("CRT" in text[m.start(): m.end() + 6] or not tag_tf):
        return f"{m.group(1)}{'m' if m.group(2) in 'mM' else 'H'}"
    if tag_tf:
        return tag_tf
    m = re.search(r"(\d{1,2})\s*(分|小時)", text)
    return f"{m.group(1)}{'m' if m.group(2) == '分' else 'H'}" if m else None


def hits(text: str) -> list[int]:
    """哪些 TP 打到了：同一行有 TPn 和 ✅ / 達成…；或「TP2 ✅」這種結果訊息。"""
    got = set()
    for ln in text.splitlines():
        if re.search(HIT, ln):
            for n in re.findall(r"TP\s*([1-4])", ln, re.I):
                got.add(int(n))
            m = re.search(r"TP\s*1\s*[-~～到]\s*TP?\s*([1-4])", ln, re.I)
            if m:
                got.update(range(1, int(m.group(1)) + 1))
    return sorted(got)


def outcome_words(text: str) -> str:
    """結果字眼（只認像結果的說法，不看「到 TP1 保本」這種規則說明）。"""
    for pat, w in ((r"全部(止盈|出場|平倉)|TP4\s*" + HIT, "全部出場"),
                   (r"保本(出場|離場|平倉)|已保本|保本\s*(✅|✔)", "保本"),
                   (r"(止損|停損)(出場|離場|平倉|了)|打(到)?(止|停)損|已(止|停)損|SL\s*(✅|hit|觸發|被打)|❌", "停損")):
        if re.search(pat, text):
            return w
    return ""


def entry_kind(trigger: str | None) -> int:
    if not trigger:
        return 0
    first = trigger.split("+")[0]
    for ch, k in K_OF.items():
        if ch in first:
            return k
    for k, pat in K_WORD:
        if re.search(pat, first, re.I):
            return k
    return 0


def parse_msg(r: dict) -> dict:
    text = r.get("text") or ""
    tag = _first(r"#([A-Za-z][A-Za-z0-9_.]*\d[^\s]*)", text) or _first(r"#(\S+)", text)
    tg = parse_tag(tag, r["date"])
    d = direction(text)
    em = re.search(r"(進場價|掛單價|入場價|進場|入場|掛單|Entry|ENTRY)[^\d\n]{0,8}" + NUM, text)
    entry = _num(em.group(2)) if em else None
    sl = _num(_first(r"(?:\bSL\b|止損|停損|Stop)[^\d\n%]{0,8}" + NUM + r"(?![0-9.,]*\s*%)", text, 1))
    tps: dict[int, float | None] = {}
    for n, v in re.findall(r"TP\s*([1-4])\s*[:：=]\s*" + NUM + r"(?![0-9.,]*\s*%)", text, re.I):   # 「TP1: 價格」，不抓「TP1 25%」
        tps.setdefault(int(n), _num(v))
    rr = {int(n): _num(v) for n, v in re.findall(r"TP\s*([1-4])[^\n]*?RR[：:\s]*" + NUM, text, re.I)}
    trig = _first(r"(?:觸發來源|觸發|Trigger)[：:]\s*([^\n]+)", text)
    p = {
        "id": r["id"], "posted": r["date"], "edited": r.get("edit"), "reply_to": r.get("reply_to"), "tag": tag, **tg,
        "symbol": symbol(text, tg["tag_sym"]), "tf": timeframe(text, tg["tag_tf"]), "dir": d,
        "mode": "激進" if "激進" in text or re.search(r"aggressive", text, re.I) else "標準" if "標準" in text else "",
        "order": ("掛單" if "掛單" in em.group(1) else "進場") if em else "",
        "entry": entry, "sl": sl, "tp": [tps.get(i) for i in (1, 2, 3, 4)], "rr": [rr.get(i) for i in (1, 2, 3, 4)],
        "trigger": trig, "kind": entry_kind(trig), "kl": _first(r"\bKL[：:]\s*([^\n]+)", text),
        "model": _first(r"Model[：:]?\s*M?(\d)", text) or _first(r"\bM([1-4])\b", text),
        "session": _first(r"時段\s*([A-Za-z.0-9]+)", text), "grade": _first(r"盈虧評級[：:]\s*([^\n]+)", text),
        "zone": _first(r"相對位置[：:]\s*([^\n]+)", text), "tbs": _first(r"TBS[：:]\s*([^\n]+)", text),
        "cisd": _first(r"CISD[：:]\s*([^\n]+)", text), "depth": _num(_first(r"掃蕩深度[：:]\s*(-?" + NUM[1:], text)),
        "hits": hits(text), "end": outcome_words(text), "text": text, "versions": len(r.get("versions", [])),
    }
    has_px = entry is not None or sl is not None or any(v is not None for v in p["tp"])
    if d and has_px:
        p["type"] = "signal"
    elif p["hits"] or p["end"]:
        p["type"] = "result"
    else:
        p["type"] = "other"
    return p


def parse_all(raw: dict) -> dict:
    """全部訊息 → signals（結果已經併進去）、results、other。"""
    msgs = sorted((parse_msg(r) for r in raw.values()), key=lambda x: x["id"])
    sig = [m for m in msgs if m["type"] == "signal"]
    by_id = {m["id"]: m for m in sig}
    by_tag = {}
    for m in sig:
        if m["tag"]:
            by_tag.setdefault(m["tag"], m)
    res = [m for m in msgs if m["type"] == "result"]
    for s in sig:
        s["result_ids"], s["hit_max"] = [], max(s["hits"]) if s["hits"] else 0
    for r in res:
        tgt = by_id.get(r["reply_to"]) or (by_tag.get(r["tag"]) if r["tag"] else None)
        r["linked"] = tgt["id"] if tgt else None
        if tgt:
            tgt["result_ids"].append(r["id"])
            tgt["hit_max"] = max([tgt["hit_max"]] + r["hits"])
            tgt["end"] = tgt["end"] or r["end"]
    return {"signals": sig, "results": res, "other": [m for m in msgs if m["type"] == "other"]}


def fmt_t(ms: int | None) -> str:
    return "—" if not ms else datetime.fromtimestamp(ms / 1000, TPE).strftime("%m/%d %H:%M")

