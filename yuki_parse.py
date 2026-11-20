"""Yuki 的 Telegram 訊息（CRT Sniper 頻道）：分類 + 抓欄位。

頻道的實際樣子（2026-11 看過全部歷史後照實際格式寫）：
  卡片   第一行「🔍#<商品><TF><mmddHH>[A]」，下一行「🆕 / ⚡ <商品> | <TF> CRT SNIPE【標準 / 激進】」，
         💰 收盤價 / 現價 / 進場價、方向、觸發來源（激進）、評級、TBS、CISD、KL…、🎯 TP1–4（RR，打到加 ✅）、🚫 SL、⏰ 訊號時間。
         **到 TP2 才第一次發**，之後到 TP3、TP4 各再發一次整張（不是編輯），所以停損的單不會出現在卡片裡。
         同一個 #編號 可能是兩張不同的單（同一小時先空後多），用「編號 + 方向 + SL」分開。
  日報   「📊 【… 每日結算 …】」：每個時間框架當天最高到 TP1–4、SL 各幾單、勝率 → 這才看得到 Yuki 的停損。
  其他   公告、圖、盈利分享的說明文字…（盈利分享裡夾的卡片照樣當卡片收）

編號的時間 = C2 那根 K 的開盤時間（台灣時間 mmddHH）；⏰ = 訊號發出的時間（台灣時間）。
認不出來的訊息會整理在 unparsed.md，不會丟掉。
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

TPE = ZoneInfo("Asia/Taipei")
NUM = r"([0-9][0-9,]*(?:\.[0-9]+)?)"
TF_MS = {"15m": 900_000, "1H": 3_600_000, "4H": 14_400_000, "1D": 86_400_000}
FIELDS = {                       # 卡片上「名稱：值」的欄位 → 存成的 key
    "盈虧評級": "grade", "區間評級": "body", "相對位置": "zone", "TBS": "tbs", "CISD": "cisd", "FIBO KL": "fibo_kl",
    "FIBO": "fibo", "KL": "kl", "VP": "vp", "觸發來源": "trigger", "趨勢評估": "trend_eval",
}


def _num(s: str | None) -> float | None:
    try:
        return float(s.replace(",", "")) if s else None
    except ValueError:
        return None


def norm_tf(tf: str | None) -> str | None:
    if not tf:
        return None
    t = tf.strip().upper()
    return {"15M": "15m", "1H": "1H", "H1": "1H", "60M": "1H", "4H": "4H", "H4": "4H", "240M": "4H", "D": "1D", "1D": "1D",
            "D1": "1D"}.get(t)


def parse_tag(tag: str | None, posted: int) -> dict:
    """NAS100USD1H101616 / 1000PEPEUSDTP1H091105A / ETH1H031306 / 4H031318 → 商品、TF、C2 開盤時間、是不是激進（A）。"""
    out = {"tag_sym": None, "tag_tf": None, "c2": None, "tag_aggr": False}
    m = re.match(r"^(.*?)(\d{1,2}[HhMm]|[Dd])(\d{6})(A?)$", tag or "")
    if not m:
        return out
    out["tag_sym"] = m.group(1) or None
    out["tag_tf"] = norm_tf(m.group(2))
    out["tag_aggr"] = m.group(4) == "A"
    ds = m.group(3)
    try:
        mo, dd, hh = int(ds[0:2]), int(ds[2:4]), int(ds[4:6])
        post = datetime.fromtimestamp(posted / 1000, TPE)
        y = post.year - (1 if mo > post.month + 1 else 0)          # 跨年：12 月的單 1 月才發
        out["c2"] = int(datetime(y, mo, dd, hh, tzinfo=TPE).timestamp() * 1000)
    except ValueError:
        pass
    return out


def _field(text: str, name: str) -> str | None:
    # 「🔑KL: …」「🐢TBS : …」「📊 盈虧評級： …」：名稱前面可以有表情符號，冒號全形半形都可以
    m = re.search(r"(?m)^[^\w\n]*" + re.escape(name) + r"\s*[：:]\s*(.+?)\s*$", text)
    return m.group(1) if m else None


def parse_card(text: str, posted: int) -> dict | None:
    """一則訊息裡的 CRT 卡片；沒有卡片回傳 None。"""
    m = re.search(r"🔍\s*#(\S+)", text)
    if not m:
        return None
    body = text[m.start():]
    tag = re.sub(r"[^A-Za-z0-9]+$", "", m.group(1))
    tg = parse_tag(tag, posted)
    hd = re.search(r"(?m)^\s*([^\w\s|]+)\s*([^\s|]+)\s*\|\s*(\S+)\s*CRT\s*SNIPE(?:\s*【(激進|標準)】)?", body)
    px = re.search(r"💰\s*(收盤價|現價|進場價)\s*[：:]\s*" + NUM, body)
    dl = _field(body, "方向") or ""
    d = -1 if re.search(r"看空|SHORT", dl) else 1 if re.search(r"看多|LONG", dl) else 0
    if not d and hd:                                  # 早期的簡短卡片沒有「方向」：看標題的 📉 / 📈
        d = -1 if "📉" in hd.group(1) else 1 if "📈" in hd.group(1) else 0
    tr = re.search(r"/\s*\S*\s*(順勢|逆勢)", dl)
    aggr = bool((hd and ("⚡" in hd.group(1) or hd.group(4) == "激進")) or tg["tag_aggr"])
    tps, rr, adj, hit = [None] * 4, [None] * 4, [None] * 4, [False] * 4
    for n, v, r, rest in re.findall(r"(?m)^\s*🎯\s*TP\s*([1-4])\s*[:：]\s*" + NUM + r"\s*(?:\(\s*RR\s*[:：]\s*" + NUM + r"\s*\))?(.*)$", body):
        k = int(n) - 1
        tps[k], rr[k] = _num(v), _num(r)
        hit[k] = "✅" in rest
        a = re.search(r"⚙️?\s*" + NUM + r"\s*→\s*1\s*:\s*" + NUM, rest)     # 「⚙️1.72 → 1:1」= 原本 RR 1.72，改成 1R
        if a:
            adj[k] = (_num(a.group(1)), _num(a.group(2)))
    sl = _num((re.search(r"🚫\s*SL\s*[:：]\s*" + NUM, body) or [None, None])[1])
    clk = re.search(r"⏰\s*(\d{4}-\d\d-\d\d \d\d:\d\d(?::\d\d)?)", body)
    sig_t = None
    if clk:
        s = clk.group(1) + (":00" if clk.group(1).count(":") == 1 else "")
        sig_t = int(datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=TPE).timestamp() * 1000)
    tf = norm_tf(hd.group(3)) if hd else None
    tf = tf or tg["tag_tf"]
    if sig_t is None and not aggr and tg["c2"] and tf:
        sig_t = tg["c2"] + TF_MS[tf]                 # 標準：C2 收盤那一刻
    out = {
        "tag": tag, **tg, "symbol": (hd.group(2) if hd else None) or tg["tag_sym"], "tf": tf, "dir": d,
        "mode": "激進" if aggr else "標準", "trend": tr.group(1) if tr else "",
        "px_kind": px.group(1) if px else "", "entry": _num(px.group(2)) if px else None, "sl": sl,
        "tp": tps, "rr": rr, "tp_adj": adj, "hit": hit, "hit_max": max([i + 1 for i in range(4) if hit[i]] or [0]),
        "early": "尚未收盤確認" in body, "sig_t": sig_t, "resent": "補發" in body,
    }
    for name, key in FIELDS.items():
        v = _field(body, name)
        if name == "FIBO" and v is None:
            continue
        out[key] = v
    if out.get("fibo") is None and out.get("fibo_kl"):
        out["fibo"] = out["fibo_kl"]
    if not out["dir"] and tps[0] is not None and tps[1] is not None and tps[0] != tps[1]:
        out["dir"] = -1 if tps[1] < tps[0] else 1     # TP1 = 區間 50%、TP2 = 對側，順序就是方向
    return out if out["dir"] and tps[1] is not None else None


def parse_daily(text: str, posted: int) -> dict | None:
    """每日結算：{date, market, tf: {1H: {tp1..tp4, sl, win}}}。半夜 00:0x 發的是前一天。"""
    if "每日結算" not in text:
        return None
    mk = re.search(r"【([^】]*?)\s*-\s*每日結算", text)
    market = mk.group(1).strip() if mk else ""
    market = "加密" if "加密" in market or "Crypto" in market else "外匯黃金" if ("外匯" in market or "CFD" in market) else "全部"
    day = datetime.fromtimestamp(posted / 1000, TPE) - timedelta(hours=6)
    out = {"date": day.strftime("%Y-%m-%d"), "market": market, "posted": posted, "tf": {}}
    for sec in re.split(r"🎯\s*", text)[1:]:
        tfm = re.match(r"(\S+)\s*CRT", sec)
        if not tfm or not norm_tf(tfm.group(1)):
            continue
        cnt = {}
        for k, pat in (("tp1", r"TP1"), ("tp2", r"TP2"), ("tp3", r"TP3"), ("tp4", r"TP4"), ("sl", r"SL")):
            v = re.search(pat + r"\s*[：:]\s*(\d+)", sec)
            cnt[k] = int(v.group(1)) if v else 0
        w = re.search(r"勝率\s*[：:]\s*" + NUM, sec)
        cnt["win"] = _num(w.group(1)) if w else None
        out["tf"][norm_tf(tfm.group(1))] = cnt
    # 「盈利參考 - 分批平倉」的 25/25/25/25（舊格式「【25% 25% 25% 25%】」）：當天所有單合計幾 R
    r = re.search(r"[\[【]\s*25%?[\s/]+25%?[\s/]+25%?[\s/]+25%?\s*[\]】]\s*[：:]\s*([+-]?\s*[0-9.]+)\s*R", text)
    out["r25"] = float(r.group(1).replace(" ", "")) if r else None
    return out if out["tf"] else None


def parse_all(raw: dict) -> dict:
    """全部訊息 → signals（同一張的多則合併：第一次出現、最高 TP）、daily（每日結算）、other（其他）。"""
    sigs: dict[tuple, dict] = {}
    daily, other = [], []
    for r in sorted(raw.values(), key=lambda x: x["id"]):
        text = r.get("text") or ""
        cd = parse_card(text, r["date"])
        if cd:
            key = (cd["tag"], cd["dir"], cd["sl"] if cd["sl"] is not None else cd["entry"])
            s = sigs.get(key)
            if s is None:
                sigs[key] = s = dict(cd, id=r["id"], posted=r["date"], last=r["date"], posts=0, text=text)
            s["posts"] += 1
            s["last"] = r["date"]
            if cd["hit_max"] >= s["hit_max"]:
                s.update(hit=cd["hit"], hit_max=cd["hit_max"], text=text)
            continue
        dy = parse_daily(text, r["date"])
        if dy:
            daily.append(dict(dy, id=r["id"]))
            continue
        other.append({"id": r["id"], "posted": r["date"], "reply_to": r.get("reply_to"), "text": text, "media": r.get("media")})
    return {"signals": sorted(sigs.values(), key=lambda s: s["posted"]), "daily": daily, "other": other}


def fmt_t(ms: int | None) -> str:
    return "—" if not ms else datetime.fromtimestamp(ms / 1000, TPE).strftime("%m/%d %H:%M")
