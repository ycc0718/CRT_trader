"""CRT 日報：一個交易日裡每個商品進了幾筆、到 TP1–TP4 各幾筆、停損 / 保本、累積多少 R。

交易依「進場時間」歸到交易日（預設台灣 06:00 換日 = 紐約 17:00，外匯一天結束）。
R 以原始停損為 1R，照設定分批出場（預設 TP1–TP4 各 25%）+ 保本；還在持倉的算已實現 + 另外列浮動。
前一天進場、這段時間才結束的另外列，不會漏掉。
"""
from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

WEEK = "一二三四五六日"


def day_start(now_ms: int, tz: ZoneInfo, hhmm: str = "06:00", back: int = 0) -> int:
    """包含 now 的那個交易日的開始（ms），back = 往前幾天。"""
    h, m = (int(x) for x in hhmm.split(":"))
    now = datetime.fromtimestamp(now_ms / 1000, tz)
    s = now.replace(hour=h, minute=m, second=0, microsecond=0)
    if now < s:
        s -= timedelta(days=1)
    s -= timedelta(days=back)
    return int(s.timestamp() * 1000)


def _hits(c) -> list[bool]:
    return [bool(c.tp1 and not c.t1Skip), bool(c.tp2), bool(c.tp3Hit), bool(c.tp4Hit)]


def _float_r(eng, c) -> float:
    """還沒出場的部位，用最後一根收盤價算的浮動 R。"""
    if c.done or c.wRem <= 0:
        return 0.0
    bear = c.dir < 0
    risk = c.sl - c.ePx if bear else c.ePx - c.sl
    last = eng.b()
    if risk <= 0 or last is None:
        return 0.0
    return c.wRem * ((c.ePx - last.c) if bear else (last.c - c.ePx)) / risk


def outcome(c) -> str:
    """這筆交易走到哪：例如「TP1 ✓ TP2 ✓ → 保本」「✗ 停損」「持倉中」。"""
    hit = [f"TP{i + 1}" for i, h in enumerate(_hits(c)) if h]
    path = " ".join(f"{x} ✓" for x in hit)
    if c.t1Skip:
        path = ("TP1 略過 " + path).strip()
    if not c.done:
        end = "持倉中"
    elif c.stopped:
        sn = c.sl if c.slNow is None else c.slNow
        end = "保本出場" if sn == c.ePx else "停損（已移到 TP1）" if sn == c.t1Px and sn != c.sl else "✗ 停損"
    elif c.tp4Hit:
        end = "全部出場"
    else:
        end = "逾時平倉"
    return f"{path} → {end}" if path else end


def short(o: str) -> str:
    """Discord 用的短版結果：「TP1 ✓ TP2 ✓ → 保本出場」→「TP2→保本」。"""
    top = None
    for i in (4, 3, 2, 1):
        if f"TP{i} ✓" in o:
            top = f"TP{i}"
            break
    end = o.split("→")[-1].strip()
    end = {"保本出場": "保本", "全部出場": "全出", "停損（已移到 TP1）": "停TP1", "逾時平倉": "逾時"}.get(end, end)
    skip = "TP1略過·" if "TP1 略過" in o else ""
    return f"{skip}{top}→{end}" if top else skip + end


def summarize(engines: dict, start: int, end: int, ltf_ms: int) -> dict:
    """engines：商品 → Engine（已經跑完）。start / end：交易日範圍（ms，LTF K 收盤時間）。"""
    rows, trades, carried = [], [], []
    tot = {"n": 0, "tp": [0, 0, 0, 0], "stop": 0, "be": 0, "open": 0, "r": 0.0, "float": 0.0}
    for sym, eng in engines.items():
        r = {"sym": sym, "n": 0, "tp": [0, 0, 0, 0], "stop": 0, "be": 0, "open": 0, "r": 0.0, "float": 0.0}
        for c in eng.trades:
            et = c.eT + ltf_ms
            xt = None if c.xT is None else c.xT + ltf_ms
            if start <= et < end:
                r["n"] += 1
                for i, h in enumerate(_hits(c)):
                    r["tp"][i] += h
                sn = c.sl if c.slNow is None else c.slNow
                if c.done and c.stopped and sn == c.sl:
                    r["stop"] += 1
                elif c.done and c.stopped and sn == c.ePx:
                    r["be"] += 1
                if not c.done:
                    r["open"] += 1
                fl = _float_r(eng, c)
                r["r"] += c.rz
                r["float"] += fl
                trades.append({"sym": sym, "dir": c.dir, "kind": eng.kName(c.eKind), "price": eng.px(c.ePx), "t": et,
                               "outcome": outcome(c), "r": c.rz, "float": fl, "done": c.done})
            elif et < start and c.done and xt is not None and start <= xt < end:
                carried.append({"sym": sym, "dir": c.dir, "kind": eng.kName(c.eKind), "price": eng.px(c.ePx), "t": et,
                                "outcome": outcome(c), "r": c.rz})
        rows.append(r)
        for k in ("n", "stop", "be", "open", "r", "float"):
            tot[k] += r[k]
        tot["tp"] = [a + b for a, b in zip(tot["tp"], r["tp"])]
    trades.sort(key=lambda x: x["t"])
    carried.sort(key=lambda x: x["t"])
    return {"start": start, "end": end, "rows": rows, "total": tot, "trades": trades, "carried": carried}


def _sg(x: float) -> str:
    return f"{x:+.2f}"


def label(rep: dict, tz: ZoneInfo) -> tuple[str, str]:
    s = datetime.fromtimestamp(rep["start"] / 1000, tz)
    e = datetime.fromtimestamp(rep["end"] / 1000, tz)
    day = f"{s:%m/%d}（{WEEK[s.weekday()]}）"
    if rep["end"] - rep["start"] > 26 * 3_600_000:                  # 跨週末等好幾天合成一份
        last = e - timedelta(seconds=1)
        day += f" – {last:%m/%d}（{WEEK[last.weekday()]}）"
    return day, f"{s:%m/%d %H:%M} – {e:%m/%d %H:%M}"


def embed(rep: dict, tz: ZoneInfo, partial: bool = False) -> dict:
    """Discord embed（跟 8010 日報同一種格式：摘要 + 每個商品一行 + 每筆明細）。"""
    t = rep["total"]
    day, window = label(rep, tz)
    tp = " · ".join(f"TP{i + 1} **{n}**" for i, n in enumerate(t["tp"]))
    desc = [f"進場 **{t['n']}** 筆 · {tp}",
            f"停損 {t['stop']} · 保本 {t['be']} · 持倉中 {t['open']}",
            f"累積 **{_sg(t['r'])}R**（已實現）" + (f" · 持倉浮動 {_sg(t['float'])}R" if t["open"] else "")]
    per = [f"`{r['sym']:<8}` 進 {r['n']} · TP1 {r['tp'][0]} · TP2 {r['tp'][1]} · TP3 {r['tp'][2]} · TP4 {r['tp'][3]}"
           f" · 停損 {r['stop']} · 保本 {r['be']} · **{_sg(r['r'])}R**" + (f"（浮動 {_sg(r['float'])}）" if r["open"] else "")
           for r in rep["rows"] if r["n"]]
    fields = [{"name": "各商品", "value": ("\n".join(per) or "這段時間沒有進場")[:1020], "inline": False}]

    def line(x):
        when = datetime.fromtimestamp(x["t"] / 1000, tz).strftime("%m/%d %H:%M")
        fl = f"（浮動 {_sg(x['float'])}）" if not x.get("done", True) else ""
        return (f"{when} {x['sym']} {'空' if x['dir'] < 0 else '多'}{x['kind'][:1]} @{x['price']} "
                f"{short(x['outcome'])} **{_sg(x['r'])}R**{fl}")

    budget = 5400 - len("".join(desc)) - sum(len(f["value"]) for f in fields)    # Discord 一則 embed 最多 6000 字
    for name, items in (("每筆", rep["trades"]), ("之前進場、這段時間結束", rep["carried"])):
        chunks, cur = [], ""
        for i, x in enumerate(items):
            ln = line(x)
            if len(ln) + 40 > budget:                                         # 放不下了：剩下的請看儀表板
                cur += f"\n…還有 {len(items) - i} 筆，看儀表板「每日統計」"
                break
            if len(cur) + len(ln) + 1 > 1000:
                chunks.append(cur)
                cur = ""
            cur += ("\n" if cur else "") + ln
            budget -= len(ln) + 1
        if cur:
            chunks.append(cur)
        for k, ch in enumerate(chunks):
            fields.append({"name": name + (f"（{k + 1}/{len(chunks)}）" if len(chunks) > 1 else ""), "value": ch, "inline": False})
    return {"title": f"📊 CRT 日報 · {day}" + ("（到目前為止）" if partial else ""),
            "color": 0x557571 if t["r"] >= 0 else 0xD49A89, "description": "\n".join(desc), "fields": fields,
            "footer": {"text": f"{window} · 依進場時間歸日 · 1R = 原始停損 · 分批 + 保本 · 模擬訊號（不是實際成交）"}}
