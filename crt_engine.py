"""
CRT engine — Python port of CRT_Toolkit.pine (Yuki 的 CRT 教室).

Runs bar by bar over CLOSED lower-timeframe (LTF) candles, with higher-timeframe
(HTF) values taken from separately fetched HTF candles the same way the Pine
script reads them: request.security(..., [x[1]], lookahead_on) → the previous
CLOSED HTF bar (no repaint).

Only the trading logic is ported (drawings / panel are not). Every decision
follows the Pine script in the same order, so signals should match the
indicator on the same data feed (e.g. TradingView OKX:BTCUSDT.P).
"""
from __future__ import annotations

import bisect
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")
MIN = 60_000
HOUR = 3_600_000
DAY = 86_400_000

TF_MS = {"1m": MIN, "3m": 3 * MIN, "5m": 5 * MIN, "15m": 15 * MIN, "1H": HOUR,
         "4H": 4 * HOUR, "1D": DAY}
TF_NAME = {"15m": "15M", "1H": "1H", "4H": "4H", "1D": "D"}

K_NAME = {1: "① CISD 收盤", 2: "② 回踩 CISD", 3: "③ 回踩 IFVG", 4: "④ 回踩 LTF OB",
          5: "⑤ QM 左肩", 6: "⑥ 回測 BB", 7: "⑦ FIB", 8: "⑧ C2 收盤"}
LIMIT_K = (2, 3, 5, 6, 7)       # 限價掛單類的進場訊號


@dataclass
class Bar:
    t: int          # open time, ms UTC
    o: float
    h: float
    l: float
    c: float


@dataclass
class Params:
    # ① CRT
    crt_tf: str = "1H"
    use_m3: bool = True
    m3_max: int = 3
    reopen: bool = True         # 同方向重開 CRT：每根新掃蕩都開新的（False = 前一個同方向的還沒進場就不開）
    c3_bars: int = 2
    sess_only: bool = False
    min_rng: float = 0.5
    max_rng: float = 3.0
    hide_poor: bool = False
    # ② entries
    aggressive: bool = False
    e1: bool = True
    e2: bool = True
    e3: bool = True
    e4: bool = True
    e5: bool = True
    e6: bool = True
    e7: bool = False
    fib_lvl: float = 0.618
    e8: bool = False            # ⑧ C2 收盤價直接進（= Yuki 的「標準」；MT5 外匯 / 指數 30 天是負的，2026-11-20 關掉）
    # ③ filters
    rr_target: str = "TP2"
    min_rr: float = 0.0
    need_pd: bool = False
    tbs_only: bool = False
    ts_gap: bool = True
    sl_buf: int = 0            # ticks
    tick: float = 0.0          # instrument tick size (set from OKX instrument info)
    apl_sl_ob: bool = True     # ④ 的停損：LTF OB 外（False = 掃點外）
    tp1_cap: float = 0.0
    # ③-2 exits
    w_tp: tuple = (25, 25, 25, 25)
    be_tp1: bool = True
    tr_tp2: bool = False
    hold_max: int = 0
    # ④ KL
    kl_only: bool = True
    kl_same: bool = True
    kl_tf: str = "4H"
    kl_day: bool = True
    kl_tol_pct: float = 10.0
    use_eq: bool = True
    use_av: bool = True
    use_sd: bool = True
    max_zone: int = 30
    # ⑤ sessions
    s_as: bool = True
    s_tko: bool = True
    s_ld: bool = True
    s_ny8: bool = True
    s_ny9: bool = True
    use_pre: bool = True
    # ⑥ LTF
    swing_len: int = 3
    need_gap: bool = True
    # ⑨ daily rules
    max_daily: int = 0
    max_loss: int = 0
    news_min: int = 0


@dataclass
class Event:
    t: int          # LTF bar open time (ms) that produced the event
    kind: str       # confirm / limit / entry / exit
    key: str        # stable CRT key: dir:C1 time
    text: str
    data: dict = field(default_factory=dict)


@dataclass
class CRT:
    id: int
    dir: int
    rH: float
    rL: float
    mid: float
    entry: float
    t1: int
    t2: int
    sweepT: int
    c1Idx: int | None
    brIdx: int
    model: int = 1
    state: int = 0
    confirmed: bool = False
    multi: bool = False
    counted: bool = False
    ext: float | None = None
    extT: int | None = None
    tbs: bool = False
    sl: float | None = None
    cisdLvl: float | None = None
    runT: int | None = None
    cbHit: bool = False
    cbN: int = 0
    cbGap: bool = False
    cbOKIdx: int | None = None
    legX: float | None = None
    ifTop: float | None = None
    ifBot: float | None = None
    ifT: int | None = None
    bbTop: float | None = None
    bbBot: float | None = None
    bbAct: bool = False
    bbT: int | None = None
    qm: float | None = None
    mss: bool = False
    mssT: int | None = None
    mssLvl: float | None = None
    mssIdx: int | None = None
    obTop: float | None = None
    obBot: float | None = None
    retest: bool = False
    lv: dict = field(default_factory=lambda: {2: None, 3: None, 5: None, 6: None, 7: None})
    ix: dict = field(default_factory=lambda: {2: None, 3: None, 5: None, 6: None, 7: None})
    note: str = ""
    eKind: int = 0
    t1Px: float | None = None
    tp3: float | None = None
    tp3Name: str = ""
    tp4: float | None = None
    tp4Name: str = ""
    r1: float | None = None
    r2: float | None = None
    r3: float | None = None
    r4: float | None = None
    slNow: float | None = None
    wRem: float = 1.0
    rz: float = 0.0
    touchNB: bool = False
    age: int = 0
    waitCnt: int = 0
    sess: str = ""
    kl: bool = False
    klName: str = ""
    klTop: float | None = None
    klBot: float | None = None
    poor: str = ""
    entered: bool = False
    preConf: bool = False
    checked: bool = False
    c2ok: bool = False
    ePx: float | None = None
    eT: int | None = None
    eBar: int | None = None
    tp1: bool = False
    t1Skip: bool = False
    eLv: float | None = None
    tp2: bool = False
    tp3Hit: bool = False
    tp4Hit: bool = False
    stopped: bool = False
    oppHit: bool = False
    skipTxt: str = ""
    done: bool = False
    result: str = ""
    xT: int | None = None       # 結束那根 LTF K 的開盤時間（日報用）
    actLv: float | None = None  # 已經通知出去、要使用者掛的那一張（最近的價位）
    actK: int = 0

    @property
    def key(self) -> str:
        return f"{self.dir}:{self.t1}"


@dataclass
class Sess:
    name: str
    enabled: bool
    draw: bool
    hi: float | None = None
    lo: float | None = None
    startT: int | None = None
    endT: int | None = None
    forming: bool = False
    ready: bool = False


@dataclass
class Zone:
    dir: int
    top: float
    bot: float
    kind: str
    tf: str
    inv: bool = False


def _rma_atr(bars: list[Bar], n: int = 14) -> list[float | None]:
    out: list[float | None] = []
    trs: list[float] = []
    prev_c = None
    a = None
    for i, b in enumerate(bars):
        tr = b.h - b.l if prev_c is None else max(b.h - b.l, abs(b.h - prev_c), abs(b.l - prev_c))
        prev_c = b.c
        trs.append(tr)
        if i + 1 < n:
            out.append(None)
        elif i + 1 == n:
            a = sum(trs) / n
            out.append(a)
        else:
            a = (a * (n - 1) + tr) / n
            out.append(a)
    return out


def _ob_calc(bars: list[Bar]) -> list[tuple]:
    """f_obCalc: detection at each HTF bar (the Pine function returns it shifted by 1)."""
    out = []
    dnOp = dnLo = dnT = upOp = upHi = upT = None
    bullUsed = bearUsed = True
    pu = pd = False
    for b in bars:
        up = b.c > b.o
        dn = b.c < b.o
        if dn and not pd:
            dnOp, dnLo, dnT, bullUsed = b.o, b.l, b.t, False
        elif dn:
            dnLo = min(dnLo, b.l)
        if up and not pu:
            upOp, upHi, upT, bearUsed = b.o, b.h, b.t, False
        elif up:
            upHi = max(upHi, b.h)
        bT = bB = bTm = sT = sB = sTm = None
        if not bullUsed and up and b.c > dnOp:
            bT, bB, bTm, bullUsed = dnOp, dnLo, dnT, True
        if not bearUsed and dn and b.c < upOp:
            sT, sB, sTm, bearUsed = upHi, upOp, upT, True
        out.append((bT, bB, bTm, sT, sB, sTm))
        pu, pd = up, dn
    return out


class HTF:
    """Higher-timeframe series; prev(t) is the bar request.security(..., x[1], lookahead_on) sees.

    用實際的 HTF K 線清單找（bisect），不用時間除法：外匯 / 黃金 / 指數有週末、每日休市，
    「前一根 HTF K」不一定剛好是 period 之前那根；幣圈 24 小時連續時結果跟除法一樣。
    """

    def __init__(self, bars: list[Bar], period: int):
        self.period = period
        self.bars = bars
        self.ts = [b.t for b in bars]
        self.atr = _rma_atr(bars)
        self.ob = _ob_calc(bars)

    def _find(self, t: int) -> tuple[int, bool]:
        """(k, inside)：k = 開盤時間 ≤ t 的最後一根；inside = t 落在這根 K 的時間範圍內。"""
        k = bisect.bisect_right(self.ts, t) - 1
        return k, k >= 0 and t < self.ts[k] + self.period

    def bucket(self, t: int) -> int:
        """包含 t 的那根 HTF K 的開盤時間（資料裡沒有那根時，從前一根往後推算）。"""
        k, inside = self._find(t)
        if inside:
            return self.ts[k]
        if k >= 0:
            return self.ts[k] + (t - self.ts[k]) // self.period * self.period
        return t - t % self.period

    def prev(self, t: int) -> int | None:
        """Index of the previous CLOSED HTF bar for chart time t."""
        k, inside = self._find(t)
        if inside:
            return k - 1 if k >= 1 else None
        return k if k >= 0 else None


def ny_dt(t: int) -> datetime:
    return datetime.fromtimestamp(t / 1000, tz=timezone.utc).astimezone(NY)


def in_sess(t: int, start_hm: int, end_hm: int) -> bool:
    d = ny_dt(t)
    m = d.hour * 60 + d.minute
    return start_hm <= m < end_hm


def fmt_px(p, tick: float) -> str:
    if p is None:
        return "—"
    if tick and tick > 0:
        dec = max(0, -int(math.floor(math.log10(tick) + 1e-9)))
        return f"{round(p / tick) * tick:.{dec}f}"
    return f"{p:.6g}"


def r_txt(r) -> str:
    return "—" if r is None else ("已過進場價" if r <= 0 else f"{r:.2f}R")


def sgn(x: float) -> str:
    return ("+" if x >= 0 else "") + f"{x:.2f}"


class Engine:
    def __init__(self, p: Params, symbol: str, crt: HTF, kl: HTF | None, daily: HTF):
        self.p = p
        self.sym = symbol
        self.H = crt
        self.B = kl
        self.Dy = daily
        self.crtSec = TF_MS[p.crt_tf]
        self.klSec = TF_MS[p.kl_tf]
        self.crtName = TF_NAME.get(p.crt_tf, p.crt_tf)
        self.klName_tf = TF_NAME.get(p.kl_tf, p.kl_tf)
        self.tfB_ok = self.klSec > self.crtSec and self.klSec != DAY and kl is not None
        self.tfD_ok = DAY > self.crtSec
        ws = max(1, sum(p.w_tp))
        self.w = [x / ws for x in p.w_tp]
        self.events: list[Event] = []
        self.stats = [0.0] * 17
        self.kCnt = [0] * 9
        self.day = [0.0, 0.0, 0.0]
        self.crts: list[CRT] = []
        self.trades: list[CRT] = []     # 有進場的 CRT（結束後 crts 會刪掉，這裡留著給日報）
        self.history: list[CRT] = []    # 每一個出現過的 CRT（含沒進場、被過濾的；跟 Yuki 訊號對照用）
        self.sessions =[Sess("AS.", p.s_as, True), Sess("TKO.", p.s_tko, True), Sess("LD.", p.s_ld, True),
                         Sess("NY.8", p.s_ny8, True), Sess("NY.9", p.s_ny9, True),
                         Sess("亞洲盤", True, False), Sess("倫敦盤", True, False), Sess("開盤前", p.use_pre, False)]
        self.zones: list[Zone] = []
        self.hO, self.hH, self.hL, self.hC, self.hT = [], [], [], [], []
        self.hSwHi, self.hSwLo = [], []
        self.swptHi, self.swptHiT, self.swptLo, self.swptLoT = [], [], [], []
        self.avLv, self.avDr, self.eqLv = [], [], []
        self.bfTop, self.bfBot, self.bfT = [], [], []
        self.sfTop, self.sfBot, self.sfT = [], [], []
        self.lastCRT: CRT | None = None
        self.idSeq = 0
        self.madeBearT = self.madeBullT = None
        # HTF run tracker
        self.runH = self.runL = None
        self.runHT = self.runLT = self.runHI = self.runLI = None
        self.c1HIdx = self.c1LIdx = None
        # LTF state
        self.bars: list[Bar] = []
        self.i = -1
        self.upRunOpen = self.upRunT = self.upRunHigh = None
        self.dnRunOpen = self.dnRunT = self.dnRunLow = None
        self.bullBaseTop = self.bullBaseBot = self.bearBaseTop = self.bearBaseBot = None
        self.bearCisdUsed = self.bullCisdUsed = True
        self.swH = self.swHIdx = None
        self.swHUsed = True
        self.swL = self.swLIdx = None
        self.swLUsed = True
        self.swHPrevL = self.swLPrevH = None
        self.legHi = self.legHiOp = self.legLo = self.legLoOp = None
        self.prevUp = self.prevDn = False
        self.prevCrtB = self.prevKlB = self.prevDayB = None

    # ───────────── helpers ─────────────
    def px(self, v) -> str:
        return fmt_px(v, self.p.tick)

    def pickR(self, a1, a2, a3, a4):
        return {"TP1": a1, "TP2": a2, "TP3": a3}.get(self.p.rr_target, a4)

    def b(self, k: int = 0) -> Bar | None:
        j = self.i - k
        return self.bars[j] if j >= 0 else None

    def sessTag(self, t: int) -> str:
        if self.crtSec != HOUR:
            return ""
        d = ny_dt(t)
        if d.minute != 0:
            return ""
        return {18: "AS.", 21: "TKO.", 2: "LD.", 8: "NY.8", 9: "NY.9"}.get(d.hour, "")

    def kDesc(self, k: int, bear: bool) -> str:
        brk = "跌破" if bear else "漲破"
        return {
            1: f"帶缺口收盤{brk} CISD（掃{'高' if bear else '低'}點那段連續{'陽' if bear else '陰'}線第一根的開盤價），那根收盤進",
            2: f"CISD 被帶缺口{brk}後，價格回踩 CISD 價位成交",
            3: f"推{'上去' if bear else '下來'}那段留下的{'多' if bear else '空'}方 FVG 被收盤{brk}變 IFVG，回踩 IFVG {'下' if bear else '上'}緣成交",
            4: "LTF 帶缺口 MSS 後，回踩 LTF OB、留影線收回，那根收盤進（A+）",
            5: f"掃{'高' if bear else '低'}（頭）之後{brk}前{'低' if bear else '高'}（MSS），回到左肩成交（QM）",
            6: f"推{'上去' if bear else '下來'}之前那段連續{'陰' if bear else '陽'}線（OB）被收盤{brk}變 BB，回測 BB {'下' if bear else '上'}緣成交",
            7: f"CISD 被帶缺口{brk}後，從掃點拉到之後的極值，在 {self.p.fib_lvl} 回撤成交",
            8: "CRT 一確認就用 C2 收盤價進（不等 LTF）",
        }.get(k, "")

    def kName(self, k: int) -> str:
        return K_NAME.get(k, "等 LTF 訊號") + (f" {self.p.fib_lvl}" if k == 7 else "")

    # ───────────── KL ─────────────
    def zoneName(self, z: Zone) -> str:
        if z.kind == "SD":
            return z.tf + " 供需區"
        return z.tf + " " + (("IFVG" if z.inv else "FVG") if z.kind == "FVG" else ("BB" if z.inv else "OB"))

    def newZone(self, d, top, bot, kind, tfn):
        self.zones.append(Zone(d, top, bot, kind, tfn))
        while len(self.zones) > self.p.max_zone:
            self.zones.pop(0)

    def addZones(self, on, chg, tfn, h1, l1, h3, l3, ob):
        if on and chg:
            if h3 is not None and l1 is not None and l1 > h3:
                self.newZone(1, l1, h3, "FVG", tfn)
            if l3 is not None and h1 is not None and h1 < l3:
                self.newZone(-1, l3, h1, "FVG", tfn)
            if ob is not None:
                bT, bB, _, sT, sB, _ = ob
                if bT is not None and bB is not None and bT > bB:
                    self.newZone(1, bT, bB, "OB", tfn)
                if sT is not None and sB is not None and sT > sB:
                    self.newZone(-1, sT, sB, "OB", tfn)

    def klCheck(self, d, rH, rL, ext, c1T, c2T):
        p = self.p
        tol = (rH - rL) * p.kl_tol_pct / 100
        lo = rH - tol if d < 0 else ext - tol
        hi = ext + tol if d < 0 else rL + tol
        eqT = tol if self.hAtr is None else self.hAtr * 0.1
        name, kt, kb = "", None, None
        if d < 0:
            for j, v in enumerate(self.swptHi):
                if name == "" and self.swptHiT[j] >= c2T and lo <= v <= hi:
                    name, kt, kb = "前高（OHP）", v, v
        else:
            for j, v in enumerate(self.swptLo):
                if name == "" and self.swptLoT[j] >= c2T and lo <= v <= hi:
                    name, kt, kb = "前低（OLP）", v, v
        ci = self.hT.index(c1T) if c1T in self.hT else -1
        if name == "" and ci >= 1:
            i0 = max(0, ci - 30)
            for i in range(i0, ci):
                for j in range(i + 1, ci + 1):
                    if name == "":
                        a = self.hH[i] if d < 0 else self.hL[i]
                        bb = self.hH[j] if d < 0 else self.hL[j]
                        if abs(a - bb) <= eqT:
                            lv = max(a, bb) if d < 0 else min(a, bb)
                            intact = lo <= lv <= hi
                            if intact and j < ci:
                                for k in range(j + 1, ci + 1):
                                    if (self.hH[k] > lv + eqT) if d < 0 else (self.hL[k] < lv - eqT):
                                        intact = False
                            if intact:
                                name, kt, kb = ("EQH" if d < 0 else "EQL"), lv, lv
        if name == "" and d < 0 and self.pdh is not None and lo <= self.pdh <= hi:
            name, kt, kb = "PDH", self.pdh, self.pdh
        if name == "" and d > 0 and self.pdl is not None and lo <= self.pdl <= hi:
            name, kt, kb = "PDL", self.pdl, self.pdl
        if name == "" and p.use_eq and self.pdh is not None and self.pdl is not None:
            pdEq = (self.pdh + self.pdl) / 2
            if lo <= pdEq <= hi:
                name, kt, kb = "昨日 EQ 50%", pdEq, pdEq
        if name == "":
            for s in self.sessions:
                if name == "" and s.enabled and s.ready and s.hi is not None and s.startT < c2T and s.startT != c1T:
                    if d < 0 and lo <= s.hi <= hi:
                        name, kt, kb = s.name + " 高點", s.hi, s.hi
                    elif d > 0 and lo <= s.lo <= hi:
                        name, kt, kb = s.name + " 低點", s.lo, s.lo
        if name == "" and p.use_av:
            for j, v in enumerate(self.avLv):
                if name == "" and self.avDr[j] == d and lo <= v <= hi:
                    name, kt, kb = ("A 水平" if d < 0 else "V 水平"), v, v
        if name == "" and p.use_eq:
            for v in self.eqLv:
                if name == "" and lo <= v <= hi:
                    name, kt, kb = "EQ 50%", v, v
        if name == "":
            for z in self.zones:
                if name == "" and z.dir == d:
                    ov = (z.bot <= ext + tol and z.top >= rH - tol) if d < 0 else (z.top >= ext - tol and z.bot <= rL + tol)
                    if ov:
                        name, kt, kb = self.zoneName(z), z.top, z.bot
        return name, kt, kb

    def tp34(self, d, rH, rL):
        rng = rH - rL
        tol = rng * 0.05
        lim = rL - tol if d < 0 else rH + tol
        cv, cn = [], []
        pv = self.pdl if d < 0 else self.pdh
        if pv is not None:
            cv.append(pv)
            cn.append("PDL" if d < 0 else "PDH")
        for s in self.sessions:
            v = s.lo if d < 0 else s.hi
            if s.enabled and s.ready and v is not None:
                cv.append(v)
                cn.append(s.name + ("L" if d < 0 else "H"))
        for v in (self.hSwLo if d < 0 else self.hSwHi):
            cv.append(v)
            cn.append("前低" if d < 0 else "前高")

        def nearest(limit):
            b, n = None, ""
            for j, v in enumerate(cv):
                if (v < limit if d < 0 else v > limit) and (b is None or (v > b if d < 0 else v < b)):
                    b, n = v, cn[j]
            return b, n

        b3, n3 = nearest(lim)
        if b3 is None:
            b3, n3 = (rL - rng if d < 0 else rH + rng), "1:1 延伸"
        b4, n4 = nearest(b3 - tol if d < 0 else b3 + tol)
        if b4 is None:
            b4, n4 = (b3 - rng if d < 0 else b3 + rng), "再延伸 1 倍"
        return b3, n3, b4, n4

    def poorTxt(self, rH, rL):
        if self.hAtr is None or self.hAtr == 0:
            return ""
        r = (rH - rL) / self.hAtr
        return "區間偏小" if r < self.p.min_rng else ("區間偏大" if self.p.max_rng > 0 and r > self.p.max_rng else "")

    # ───────────── R / plan ─────────────
    def calcR(self, c: CRT, px):
        bear = c.dir < 0
        risk = c.sl - px if bear else px - c.sl
        tp2 = c.rL if bear else c.rH
        t1 = c.mid
        if self.p.tp1_cap > 0 and risk > 0:
            rMid = (px - c.mid if bear else c.mid - px) / risk
            if rMid > self.p.tp1_cap:
                t1 = px - self.p.tp1_cap * risk if bear else px + self.p.tp1_cap * risk

        def r(tp):
            if risk <= 0 or tp is None:
                return None
            return (px - tp if bear else tp - px) / risk
        return r(t1), r(tp2), r(c.tp3), r(c.tp4), t1

    def setR(self, c: CRT, px):
        a1, a2, a3, a4, t1 = self.calcR(c, px)
        c.r1, c.r2, c.r3, c.r4, c.t1Px = a1, a2, a3, a4, t1
        return self.pickR(a1, a2, a3, a4)

    def hasLv(self, c: CRT) -> bool:
        return any(v is not None for v in c.lv.values())

    def nearLv(self, c: CRT):
        best, bk = None, 0
        close = self.b().c
        for k in (2, 3, 5, 6, 7):
            v = c.lv[k]
            if v is not None and (best is None or abs(close - v) < abs(close - best)):
                best, bk = v, k
        return best, bk

    def plan(self, c: CRT):
        p = self.p
        px, k = c.cisdLvl, 0
        if c.entered:
            px, k = c.ePx, c.eKind
        else:
            lp, lk = self.nearLv(c)
            if lp is not None:
                px, k = lp, lk
            elif p.e1 or p.e2 or p.e7:
                k = 1 if p.e1 else (2 if p.e2 else 7)
            elif p.e4 and c.mss and c.obTop is not None:
                px, k = (c.obBot if c.dir < 0 else c.obTop), 4
            elif p.e8:
                px, k = c.entry, 8
        return px, k

    def tsGapOK(self, c: CRT) -> bool:
        return c.c1Idx is None or c.brIdx is None or c.brIdx - c.c1Idx >= 2

    def gate(self, c: CRT, px, withDay: bool) -> str:
        a1, a2, a3, a4, _ = self.calcR(c, px)
        rt = self.pickR(a1, a2, a3, a4)
        bear = c.dir < 0
        if rt is None:
            return "進場價已在停損外"
        if self.p.need_pd and (px < c.mid if bear else px > c.mid):
            return "在折價區" if bear else "在溢價區"
        if self.p.min_rr > 0 and rt < self.p.min_rr:
            return f"{self.p.rr_target} 只有 {r_txt(rt)}"
        if withDay and self.dayBlock:
            return self.dayBlock
        return ""

    def preGate(self, c: CRT) -> str:
        p = self.p
        if p.sess_only and self.crtSec == HOUR and c.sess == "":
            return "不是時段 CRT"
        if p.kl_only and not c.kl:
            return "附近沒有 KL"
        if p.hide_poor and c.poor:
            return c.poor
        if p.tbs_only and not c.tbs:
            return "不是 TBS（只有影線掃）"
        if p.ts_gap and not self.tsGapOK(c):
            return "TS 沒間隔 1 根 K"
        return ""

    def info(self, c: CRT):
        c.sess = self.sessTag(c.t1)
        kn, kt, kb = self.klCheck(c.dir, c.rH, c.rL, c.ext, c.t1, c.t2)
        c.kl, c.klName, c.klTop, c.klBot = kn != "", kn, kt, kb
        c.poor = self.poorTxt(c.rH, c.rL)
        c.tp3, c.tp3Name, c.tp4, c.tp4Name = self.tp34(c.dir, c.rH, c.rL)
        self.setR(c, self.plan(c)[0])

    # ───────────── lifecycle ─────────────
    def emit(self, kind: str, c: CRT, text: str, **data):
        bar = self.b()
        self.events.append(Event(bar.t, kind, c.key, text, dict(data, dir=c.dir, model=c.model)))

    def setLv(self, c: CRT, k, lv, ix):
        c.lv[k] = lv
        c.ix[k] = ix

    def enter(self, c: CRT, px, kind: int):
        bar = self.b()
        self.setR(c, px)
        c.entered = True
        c.eKind = kind
        c.preConf = not c.confirmed
        c.ePx, c.eBar, c.eT = px, self.i, bar.t
        c.slNow, c.wRem, c.rz = c.sl, 1.0, 0.0
        if c.r1 is not None and c.r1 <= 0:  # TP1 已過進場價 → 略過，部位併到 TP2，保本改在 TP2
            c.tp1 = c.t1Skip = True
        self.trades.append(c)
        self.day[0] += 1
        self.kCnt[kind] += 1
        self.emit("entry", c, self.entryText(c), sig=kind, price=px, sl=c.sl, tp1=c.t1Px,
                  tp2=c.rL if c.dir < 0 else c.rH, tp3=c.tp3, tp4=c.tp4, r=[c.r1, c.r2, c.r3, c.r4],
                  kl=c.klName, aggressive=c.preConf)

    def slTag(self, c: CRT) -> str:
        """進場後停損移到哪裡了（沒移動 = ""）。"""
        if not c.entered or c.slNow is None or c.slNow == c.sl:
            return ""
        return "保本" if c.slNow == c.ePx else "TP1" if c.slNow == c.t1Px else "移動"

    def slWhy(self, c: CRT) -> str:
        """原始停損是怎麼來的（給儀表板說明用）。"""
        bear = c.dir < 0
        buf = self.p.sl_buf * self.p.tick
        swept = None if c.ext is None else (c.ext + buf if bear else c.ext - buf)
        if c.entered and c.eKind == 4 and swept is not None and c.sl != swept:
            return "LTF OB 外（④ 的停損放在 OB 外，比掃點近）"
        s = f"C2 掃到的最{'高' if bear else '低'}點" + (f"外 {self.p.sl_buf} tick" if self.p.sl_buf else "")
        if c.model == 3:
            s += "（M3：掃了好幾根，停損跟著移到最新的掃點）"
        return s

    def mgmtTxt(self) -> str:
        w = [round(x * 100) for x in self.w]
        s = f"分批 TP1 {w[0]}% / TP2 {w[1]}% / TP3 {w[2]}% / TP4 {w[3]}%"
        if self.p.be_tp1:
            s += " · 到 TP1 保本"
        if self.p.tr_tp2:
            s += " · 到 TP2 停損移到 TP1"
        if self.p.hold_max > 0:
            s += f" · 最多抱 {self.p.hold_max} 根"
        return s

    def head(self, c: CRT) -> str:
        return f"{self.sym} — {self.crtName}"

    def disp(self) -> str:
        return self.sym.replace("-SWAP", ".P").replace("-", "")

    def tagId(self, c: CRT) -> str:
        """這個 CRT 的編號（掛單 / 改單 / 取消 / 進場卡片都帶同一個，才對得起來）。"""
        d = datetime.fromtimestamp(c.t2 / 1000, tz=timezone.utc).astimezone(ZoneInfo("Asia/Taipei"))
        return f"#{self.disp().replace('.', '')}{self.crtName}{d:%m%d%H}{'空' if c.dir < 0 else '多'}"

    def card(self, c: CRT, px, sig: int, mode: str) -> str:
        """Discord 訊息卡片（進場 / 掛單）。第一行是標題。"""
        bear = c.dir < 0
        a1, a2, a3, a4, t1 = self.calcR(c, px)
        tp, rr = [t1, c.rL if bear else c.rH, c.tp3, c.tp4], [a1, a2, a3, a4]
        skip1 = mode == "entry" and c.t1Skip
        r1 = (a2 if skip1 else a1) or 0.0
        grade = "🔴 低 (RR < 0.5)" if r1 < 0.5 else "🟡 中 (RR 0.5~1.0)" if r1 < 1 else "🟢 高 (RR ≥ 1.0)"
        prem = px > c.mid
        good = prem if bear else not prem
        rng = c.rH - c.rL
        depth = (((c.rH - c.ext) if bear else (c.ext - c.rL)) / rng) if rng > 0 else 0.0
        conf = [self.kName(sig)]
        same = {1: "CISD", 2: "CISD", 3: "IFVG", 4: "MSS", 6: "BB"}.get(sig)
        conf += [n for n, ok in (("CISD", c.cbHit), ("IFVG", c.ifTop is not None), ("BB", c.bbAct), ("MSS", c.mss), ("TBS", c.tbs))
                 if ok and n != same]
        aggr = mode == "entry" and c.preConf or mode != "entry" and not c.confirmed
        head = f"{'⚡' if aggr else '🆕'} {self.disp()} | {self.crtName} CRT {'進場' if mode == 'entry' else '掛單'}【{'激進' if aggr else '標準'}】"
        how = ""
        if mode == "entry":
            if sig in (2, 3, 5, 6, 7) and c.eLv is not None and abs(c.ePx - c.eLv) > 1e-12:
                how = f"🧾 成交：訊號價 {self.px(c.eLv)} 出現時價格已經過了，下一根開盤 {self.px(c.ePx)} 直接成交（沒有另外發掛單通知）"
            elif sig in (2, 3, 5, 6, 7):
                how = f"🧾 成交：掛單 {self.px(c.ePx)} 被碰到"
            elif sig == 1:
                how = "🧾 成交：K 棒收盤帶缺口" + ("跌破" if bear else "漲破") + " CISD，收盤價市價進場（之前的掛單作廢）"
            elif sig == 4:
                how = "🧾 成交：回踩 LTF OB 後收回，收盤價市價進場（之前的掛單作廢）"
            elif sig == 8:
                how = "🧾 成交：C2 收盤價進場"
        L = [f"🔍 {self.tagId(c)}", "━━━━━━━━━━━━━━",
             f"💰 {'進場價' if mode == 'entry' else '掛單價'}：{self.px(px)}"] + ([how] if how else []) + [
             f"{'📉' if bear else '📈'} 方向：{'看空 (SHORT)' if bear else '看多 (LONG)'}",
             f"⚡ 觸發來源：{' + '.join(conf)}",
             f"📊 盈虧評級：{grade}",
             f"📍 相對位置：{'🟢' if good else '🔴'} {'溢價區 Premium' if prem else '折價區 Discount'}",
             f"🐢 TBS：{'✅ 收盤穿越' if c.tbs else '⚪ 只有影線（TWS）'}",
             f"🔄 CISD：{'🟢 已破' if c.cbHit else '⚪ 未破'} {self.px(c.cisdLvl)}",
             f"📐 掃蕩深度：{depth:.2f}",
             f"🔑 KL：{c.klName or '無'}",
             f"🧩 Model：M{c.model}" + (f" · 時段 {c.sess}" if c.sess else "")]
        if aggr:
            L.append("⚠️ 尚未收盤確認，提前進場")
        L.append("━━━━━━━━━━━━━━")
        for k in range(4):
            if rr[k] is not None and rr[k] <= 0:
                L.append(f"⏭ TP{k + 1}: {self.px(tp[k])}（已過進場價，略過）")
            else:
                L.append(f"🎯 TP{k + 1}: {self.px(tp[k])} (RR: {'—' if rr[k] is None else f'{rr[k]:.2f}'})")
        L += ["", f"🚫 SL: {self.px(c.sl)}"]
        return head + "\n" + "\n".join(L)

    def entryText(self, c: CRT) -> str:
        s = self.card(c, c.ePx, c.eKind, "entry") + f"\n\n📋 {self.mgmtTxt()}"
        if self.orderVoided(c):
            s += f"\n❗ 原本通知的掛單 {self.px(c.actLv)}（{self.kName(c.actK)}）請刪掉：已經用 {self.kName(c.eKind)} 進場"
        return s

    def orderVoided(self, c: CRT) -> bool:
        """進場了，但不是用通知出去的那張掛單成交（例如 ① / ④ 市價進場）→ 那張要刪。"""
        return c.entered and c.actLv is not None and not (c.eKind in LIMIT_K and c.eLv is not None and abs(c.eLv - c.actLv) < 1e-12)

    def pendingLv(self, c: CRT) -> list[tuple[float, int]]:
        """還在等價格過來的掛單價位，近 → 遠（價格已經過了的，下一根開盤會直接成交，不算）。"""
        bear, close = c.dir < 0, self.b().c
        out = [(v, k) for k, v in c.lv.items()
               if v is not None and c.ix[k] is not None and c.ix[k] >= 0 and ((close < v) if bear else (close > v))]
        out.sort(key=lambda x: x[0] if bear else -x[0])
        return out

    def cancelConds(self, c: CRT) -> list[str]:
        """沒成交前，什麼情況這張掛單會取消（給卡片和儀表板）。"""
        bear, p = c.dir < 0, self.p
        out = [f"價格先到 {'CRL' if bear else 'CRH'} {self.px(c.rL if bear else c.rH)}（區間另一邊）→ CRT 結束",
               f"價格{'漲' if bear else '跌'}過掃點 {self.px(c.ext)}（= SL）→ 停損移到新掃點，等新的訊號"]
        if p.use_m3:
            out.append(f"{self.crtName} 收盤在 {'CRH' if bear else 'CRL'} {self.px(c.rH if bear else c.rL)} 外面 → 先取消，"
                       f"{p.m3_max} 根內收回區間再重新判斷（M3）")
        else:
            out.append(f"{self.crtName} 收盤在 {'CRH' if bear else 'CRL'} {self.px(c.rH if bear else c.rL)} 外面 → CRT 結束")
        if c.confirmed and c.state == 1 and getattr(self, "curT", None) is not None:
            dl = self.curT + self.H.period * max(1, p.c3_bars - c.age)
            d = datetime.fromtimestamp(dl / 1000, tz=timezone.utc).astimezone(ZoneInfo("Asia/Taipei"))
            out.append(f"{d:%m/%d %H:%M} 前沒成交（逾時）→ CRT 結束")
        out.append("出現新的 CRT 訊號 → 被取代")
        return out

    def cancelBrief(self, c: CRT) -> str:
        """取消條件的一行短版（儀表板卡片用）。"""
        bear, p = c.dir < 0, self.p
        s = [f"到 {self.px(c.rL if bear else c.rH)}", f"{'漲' if bear else '跌'}過 {self.px(c.ext)}",
             f"{self.crtName} 收在 {self.px(c.rH if bear else c.rL)} 外"]
        if c.confirmed and c.state == 1 and getattr(self, "curT", None) is not None:
            dl = self.curT + self.H.period * max(1, p.c3_bars - c.age)
            s.append(f"{datetime.fromtimestamp(dl / 1000, tz=timezone.utc).astimezone(ZoneInfo('Asia/Taipei')):%H:%M} 逾時")
        return " · ".join(s)

    def limitText(self, c: CRT, k: int, lv, others=(), prev=None) -> str:
        """掛單通知（簡短版，2026-11-20 使用者要求）：掛哪裡、SL、TP、什麼情況取消，各一行。
        完整評級（TBS / CISD / KL…）留在進場卡片；比較遠的價位不列（近的一定先成交）。"""
        bear = c.dir < 0
        a1, a2, a3, a4, t1 = self.calcR(c, lv)
        tp, rr = [t1, c.rL if bear else c.rH, c.tp3, c.tp4], [a1, a2, a3, a4]
        tps = " · ".join(f"TP{i + 1} {self.px(tp[i])}" + ("（略過）" if rr[i] is not None and rr[i] <= 0 else
                                                          f"（{rr[i]:.1f}R）" if rr[i] is not None else "")
                         for i in range(4) if tp[i] is not None)
        head = (f"{'🔁' if prev is not None else '📌'} {self.disp()} | {self.crtName} {'改掛單' if prev is not None else '掛單'}"
                f" {'空' if bear else '多'}【{'標準' if c.confirmed else '激進'}】")
        L = [f"🔍 {self.tagId(c)}",
             f"💰 {self.px(lv)}（{self.kName(k)}）· 🚫 SL {self.px(c.sl)}" + (f" · 原本掛 {self.px(prev)}" if prev is not None else ""),
             f"🎯 {tps}",
             f"⛔ 取消：{self.cancelBrief(c)} · 新 CRT 取代"]
        if k == 7:
            L.append("（FIB 掛單價會跟著新的極值移動）")
        return head + "\n" + "\n".join(L)

    def syncOrder(self, c: CRT):
        """只通知最近的那一張（價格一定先碰到近的）；出現更近的新價位（或原本那張不見了）就通知改單。"""
        if c.done or c.entered:
            return
        pend = self.pendingLv(c)
        if not pend:
            return
        lv, k = pend[0]
        bear = c.dir < 0
        if c.actLv is not None:
            still = any(abs(v - c.actLv) < 1e-12 for v, _ in pend)
            nearer = (lv < c.actLv) if bear else (lv > c.actLv)
            if still and not nearer:
                return
        prev = c.actLv
        c.actLv, c.actK = lv, k
        a1, a2, a3, a4, t1 = self.calcR(c, lv)
        self.emit("limit", c, self.limitText(c, k, lv, pend[1:], prev), sig=k, price=lv, sl=c.sl, kl=c.klName, prev=prev,
                  tp=[t1, c.rL if bear else c.rH, c.tp3, c.tp4], r=[a1, a2, a3, a4])

    def limitGroupText(self, evs: list) -> str:
        lines = evs[0].text.split("\n")
        items = " / ".join(f"{self.px(e.data['price'])}（{self.kName(e.data['sig'])}）" for e in evs)
        return "\n".join(f"💰 {items}（先碰到的成交）· 🚫 SL {self.px(evs[0].data.get('sl'))}" if ln.startswith("💰") else ln
                         for ln in lines)

    def resText(self, c: CRT) -> str:
        if not c.entered:
            s = c.skipTxt or ("未進場：已到對側" if c.oppHit else "未進場：逾時")
            if not c.skipTxt and c.note:
                s += f"（{c.note}）"
            return s
        sn = c.sl if c.slNow is None else c.slNow
        s = "✓TP4" if c.tp4Hit else "✓TP3" if c.tp3Hit else "✓TP2" if c.tp2 else "✓TP1" if c.tp1 and not c.t1Skip else ""
        if c.stopped:
            s = "✗ 停損" if s == "" else s + (" → 保本出場" if sn == c.ePx else " → 停損（已移到 TP1）出場" if sn != c.sl else " → 回到停損")
        elif not c.tp4Hit and c.wRem > 0.0001:
            s = "逾時（按現價結算）" if s == "" else s + "（逾時，剩下按現價）"
        if c.tp2 and c.age >= 1:
            s += " · 多根派發 M4"
        return s + f" · 合計 {sgn(c.rz)}R · {self.kName(c.eKind)}"

    def account(self, c: CRT):
        bear = c.dir < 0
        close = self.b().c
        risk = c.sl - c.ePx if bear else c.ePx - c.sl
        mtm = ((c.ePx - close) if bear else (close - c.ePx)) / risk if risk > 0 else 0.0
        sn = c.sl if c.slNow is None else c.slNow
        rS = ((c.ePx - sn) if bear else (sn - c.ePx)) / risk if risk > 0 else -1.0
        if not c.stopped and c.wRem > 0.0001:
            c.rz += c.wRem * mtm
        h1 = c.tp1 and not c.t1Skip
        o = [((c.r1 or 0.0) if h1 else rS if c.stopped else mtm),
             ((c.r2 or 0.0) if c.tp2 else rS if c.stopped else mtm),
             ((c.r3 or 0.0) if c.tp3Hit else rS if c.stopped else mtm),
             ((c.r4 or 0.0) if c.tp4Hit else rS if c.stopped else mtm)]
        st = self.stats
        st[0] += 1
        st[1] += h1
        st[2] += c.tp2
        st[3] += c.tp3Hit
        st[13] += c.tp4Hit
        st[4] += c.stopped and not h1
        st[5] += (not c.stopped) and not h1
        st[16] += c.stopped and h1
        st[6] += o[0]
        st[7] += o[1]
        st[8] += o[2]
        st[14] += o[3]
        st[15] += c.rz
        if c.r1 is not None and c.r1 <= 0:
            st[9] += 1
        self.day[2] += c.rz
        self.day[1] = self.day[1] + 1 if c.rz < 0 else 0.0

    def finish(self, c: CRT, reason: str | None = None):
        c.done = True
        c.xT = self.b().t
        if c.entered:
            self.account(c)
        elif c.counted and c.skipTxt == "":
            self.stats[12 if c.note else 11] += 1
        c.result = reason or self.resText(c)
        if not c.entered:
            self.cancelPending(c, c.result)

    def cancelPending(self, c: CRT, why: str):
        """通知出去的那張掛單沒成交就作廢時，通知去 MT5 刪單，並說明這個 CRT 之後還會不會有新的單。"""
        if c.actLv is None or c.entered:
            return
        bear = c.dir < 0
        nxt = ("這個 CRT 結束了，不會再用到" if c.done else
               "同一個 CRT 還在：之後有新的進場訊號會再通知掛單 / 進場（編號一樣）")
        self.emit("cancel", c, f"🚫 {self.disp()} | {self.crtName} CRT 取消掛單 {self.px(c.actLv)}（{'空' if bear else '多'}）\n"
                               f"🔍 {self.tagId(c)}\n原本：{self.kName(c.actK)} {self.px(c.actLv)} · SL {self.px(c.sl)}\n"
                               f"原因：{why}\n接下來：{nxt}", levels=[c.actLv])
        c.actLv, c.actK = None, 0

    def skipWhy(self, c: CRT, why: str):
        c.skipTxt = "跳過：" + why
        self.stats[12] += 1
        self.finish(c)

    def tryLv(self, c: CRT, k: int, lv) -> bool:
        why = self.gate(c, lv, False)
        ok = why == ""
        self.setLv(c, k, lv if ok else None, self.i if ok else -1)
        if not ok:
            c.note = f"{self.kName(k)} {why}"
        return ok          # 通知在這根 K 最後由 syncOrder 統一發（只發最近的那張）

    def resetTrig(self, c: CRT):
        c.cbHit, c.cbN, c.cbGap, c.cbOKIdx = False, 0, False, None
        c.legX = c.ifTop = c.ifBot = c.ifT = None
        c.bbAct, c.bbT, c.qm = False, None, None
        c.mss, c.obTop, c.obBot, c.retest = False, None, None, False
        c.touchNB, c.note = False, ""
        for k in (2, 3, 5, 6, 7):
            self.setLv(c, k, None, None)

    def newExt(self, c: CRT):
        bar = self.b()
        bear = c.dir < 0
        c.ext = bar.h if bear else bar.l
        c.extT = bar.t
        buf = self.p.sl_buf * self.p.tick
        c.sl = c.ext + buf if bear else c.ext - buf
        lv = self.upRunOpen if bear else self.dnRunOpen
        if lv is None or ((lv >= c.sl or lv <= c.rL) if bear else (lv <= c.sl or lv >= c.rH)):
            lv = c.rH if bear else c.rL
        c.cisdLvl = lv
        c.runT = self.upRunT if bear else self.dnRunT
        bt = self.bullBaseTop if bear else self.bearBaseTop
        bb = self.bullBaseBot if bear else self.bearBaseBot
        bok = bt is not None and bb is not None and bt > bb and (bt < c.ext if bear else bb > c.ext)
        c.bbTop = bt if bok else None
        c.bbBot = bb if bok else None
        self.resetTrig(c)

    def aggOK(self, c: CRT) -> bool:
        ok = True
        if not c.confirmed:
            self.info(c)
            if not c.counted:
                c.counted = True
                self.stats[10] += 1
            pre = self.preGate(c)
            if pre:
                ok = False
                self.skipWhy(c, pre)
        return ok

    def retire(self, c: CRT):
        for o in self.crts:
            if o.id != c.id and not o.done and not o.entered and (o.confirmed or self.hasLv(o)):
                self.finish(o, "未進場：被新 CRT 取代")

    def stopOut(self, c: CRT):
        bear = c.dir < 0
        risk = c.sl - c.ePx if bear else c.ePx - c.sl
        sn = c.sl if c.slNow is None else c.slNow
        rS = ((c.ePx - sn) if bear else (sn - c.ePx)) / risk if risk > 0 else -1.0
        c.rz += c.wRem * rS
        c.wRem = 0.0
        c.stopped = True
        self.finish(c)

    def manage(self, c: CRT) -> str:
        bar = self.b()
        bear = c.dir < 0
        sn = c.sl if c.slNow is None else c.slNow
        hitS = ((bar.h > sn) if bear else (bar.l < sn)) if sn == c.sl else ((bar.h >= sn) if bear else (bar.l <= sn))
        ev = ""
        w1, w2, w3, _ = self.w
        if hitS:
            ev = "✗ 停損" if sn == c.sl else "保本出場" if sn == c.ePx else "停損（已移到 TP1）出場"
            self.stopOut(c)
            return ev
        if not c.tp1 and ((bar.l <= c.t1Px) if bear else (bar.h >= c.t1Px)):
            c.tp1 = True
            c.rz += w1 * (c.r1 or 0.0)
            c.wRem -= w1
            ev = "✓ TP1" + (f"（停損移到保本 {self.px(c.ePx)}）" if self.p.be_tp1 else "")
            if self.p.be_tp1:
                c.slNow = c.ePx
        if c.tp1 and not c.tp2 and ((bar.l <= c.rL) if bear else (bar.h >= c.rH)):
            wq = w2 + (w1 if c.t1Skip else 0.0)
            c.tp2 = True
            c.rz += wq * (c.r2 or 0.0)
            c.wRem -= wq
            if c.t1Skip:
                ev += (" → " if ev else "") + "✓ TP2" + (f"（停損移到保本 {self.px(c.ePx)}）" if self.p.be_tp1 else "")
                if self.p.be_tp1:
                    c.slNow = c.ePx
            else:
                ev += (" → " if ev else "") + "✓ TP2" + (f"（停損移到 TP1 {self.px(c.t1Px)}）" if self.p.tr_tp2 else "")
                if self.p.tr_tp2:
                    c.slNow = c.t1Px
        if c.tp2 and not c.tp3Hit and c.tp3 is not None and ((bar.l <= c.tp3) if bear else (bar.h >= c.tp3)):
            c.tp3Hit = True
            c.rz += w3 * (c.r3 or 0.0)
            c.wRem -= w3
            ev += (" → " if ev else "") + "✓ TP3"
        if c.tp3Hit and not c.tp4Hit and c.tp4 is not None and ((bar.l <= c.tp4) if bear else (bar.h >= c.tp4)):
            c.tp4Hit = True
            c.rz += c.wRem * (c.r4 or 0.0)
            c.wRem = 0.0
            ev += (" → " if ev else "") + "✓ TP4（全部出場）"
        if c.wRem <= 0.0001:
            c.wRem = 0.0
            self.finish(c)
        return ev

    # ───────────── main loop ─────────────
    def run(self, bars: list[Bar]):
        for bar in bars:
            self.step(bar)
        return self

    def step(self, bar: Bar):
        p = self.p
        self.bars.append(bar)
        self.i += 1
        i = self.i
        t, o, h, l, c = bar.t, bar.o, bar.h, bar.l, bar.c
        prv = self.b(1)

        # ── HTF values (previous closed HTF bar) ──
        H = self.H
        crtB = H.bucket(t)
        newHTF = self.prevCrtB is not None and crtB != self.prevCrtB
        self.prevCrtB = crtB
        j = H.prev(t)
        self.curT = crtB
        if j is not None:
            hb = H.bars[j]
            kO, kH, kL, kC, kT = hb.o, hb.h, hb.l, hb.c, hb.t
            self.hAtr = H.atr[j]
            aH1, aL1 = hb.h, hb.l
            aH3 = H.bars[j - 2].h if j >= 2 else None
            aL3 = H.bars[j - 2].l if j >= 2 else None
            aOB = H.ob[j]
        else:
            kO = kH = kL = kC = kT = None
            self.hAtr = None
            aH1 = aL1 = aH3 = aL3 = aOB = None
        newB = False
        bH1 = bL1 = bH3 = bL3 = bOB = None
        if self.B is not None:
            kb = self.B.bucket(t)
            newB = self.prevKlB is not None and kb != self.prevKlB
            self.prevKlB = kb
            jb = self.B.prev(t)
            if jb is not None:
                bH1, bL1 = self.B.bars[jb].h, self.B.bars[jb].l
                bH3 = self.B.bars[jb - 2].h if jb >= 2 else None
                bL3 = self.B.bars[jb - 2].l if jb >= 2 else None
                bOB = self.B.ob[jb]
        db = self.Dy.bucket(t)
        dayChg = self.prevDayB is not None and db != self.prevDayB
        self.prevDayB = db
        jd = self.Dy.prev(t)
        if jd is not None:
            self.pdh, self.pdl = self.Dy.bars[jd].h, self.Dy.bars[jd].l
            dH3 = self.Dy.bars[jd - 2].h if jd >= 2 else None
            dL3 = self.Dy.bars[jd - 2].l if jd >= 2 else None
            dOB = self.Dy.ob[jd]
        else:
            self.pdh = self.pdl = dH3 = dL3 = dOB = None

        if dayChg:
            self.day = [0.0, 0.0, 0.0]
        nyd = ny_dt(t)
        nyMin = nyd.hour * 60 + nyd.minute
        inNews = p.news_min > 0 and abs(nyMin - 510) <= p.news_min
        self.dayBlock = (f"今日已進場 {p.max_daily} 筆" if p.max_daily > 0 and self.day[0] >= p.max_daily else
                         f"今日連續停損 {p.max_loss} 次，先休息" if p.max_loss > 0 and self.day[1] >= p.max_loss else
                         "8:30 數據前後" if inNews else "")

        # ── current HTF bar high / low: time & bar index ──
        cHT = cLT = None
        if newHTF:
            cHT, cLT = self.runHT, self.runLT
            self.c1HIdx, self.c1LIdx = self.runHI, self.runLI
            self.runH, self.runL, self.runHT, self.runLT, self.runHI, self.runLI = h, l, t, t, i, i
        else:
            if self.runH is None or h > self.runH:
                self.runH, self.runHT, self.runHI = h, t, i
            if self.runL is None or l < self.runL:
                self.runL, self.runLT, self.runLI = l, t, i

        # ── LTF runs ──
        isUp, isDn = c > o, c < o
        if isUp and not self.prevUp:
            self.bullBaseTop, self.bullBaseBot = self.dnRunOpen, self.dnRunLow
            self.upRunOpen, self.upRunT, self.upRunHigh = o, t, h
            self.bearCisdUsed = False
        elif isUp:
            self.upRunHigh = max(self.upRunHigh, h)
        if isDn and not self.prevDn:
            self.bearBaseTop, self.bearBaseBot = self.upRunHigh, self.upRunOpen
            self.dnRunOpen, self.dnRunT, self.dnRunLow = o, t, l
            self.bullCisdUsed = False
        elif isDn:
            self.dnRunLow = min(self.dnRunLow, l)
        self.prevUp, self.prevDn = isUp, isDn

        # ── pivots / MSS / QM ──
        L = p.swing_len
        ph = pl = None
        if i >= 2 * L:
            ctr = self.bars[i - L]
            left = self.bars[i - 2 * L:i - L]
            right = self.bars[i - L + 1:i + 1]
            if all(ctr.h > x.h for x in right) and all(ctr.h >= x.h for x in left):
                ph = ctr.h
            if all(ctr.l < x.l for x in right) and all(ctr.l <= x.l for x in left):
                pl = ctr.l
        win = self.bars[max(0, i - L):i + 1]
        hiN = max(x.h for x in win)
        loN = min(x.l for x in win)
        if ph is not None:
            pIdx = i - L
            self.swHPrevL = self.swL if self.swLIdx is not None and self.swLIdx < pIdx else None
            self.swH, self.swHIdx, self.swHUsed = ph, pIdx, False
            self.legLo, self.legLoOp = loN, self.dnRunOpen
        elif self.legLo is not None and l < self.legLo:
            self.legLo, self.legLoOp = l, self.dnRunOpen
        if pl is not None:
            pIdx = i - L
            self.swLPrevH = self.swH if self.swHIdx is not None and self.swHIdx < pIdx else None
            self.swL, self.swLIdx, self.swLUsed = pl, pIdx, False
            self.legHi, self.legHiOp = hiN, self.upRunOpen
        elif self.legHi is not None and h > self.legHi:
            self.legHi, self.legHiOp = h, self.upRunOpen
        bullMSS = bearMSS = False
        bullLvl = bullIdx = bearLvl = bearIdx = bullQM = bearQM = None
        if self.swH is not None and not self.swHUsed and c > self.swH:
            bullMSS, self.swHUsed = True, True
            bullLvl, bullIdx, bullQM = self.swH, self.swHIdx, self.swHPrevL
        if self.swL is not None and not self.swLUsed and c < self.swL:
            bearMSS, self.swLUsed = True, True
            bearLvl, bearIdx, bearQM = self.swL, self.swLIdx, self.swLPrevH

        # ── gaps within the last 3 bars ──
        def B(k):
            return self.b(k)
        bullGapTop = bullGapBot = bearGapTop = bearGapBot = None
        if i >= 2 and l > B(2).h:
            bullGapTop, bullGapBot = l, B(2).h
        elif i >= 3 and B(1).l > B(3).h:
            bullGapTop, bullGapBot = B(1).l, B(3).h
        elif i >= 4 and B(2).l > B(4).h:
            bullGapTop, bullGapBot = B(2).l, B(4).h
        if i >= 2 and h < B(2).l:
            bearGapTop, bearGapBot = B(2).l, h
        elif i >= 3 and B(1).h < B(3).l:
            bearGapTop, bearGapBot = B(3).l, B(1).h
        elif i >= 4 and B(2).h < B(4).l:
            bearGapTop, bearGapBot = B(4).l, B(2).h
        bullGap, bearGap = bullGapTop is not None, bearGapTop is not None

        if i >= 2 and l > B(2).h:
            self.bfTop.append(l); self.bfBot.append(B(2).h); self.bfT.append(B(1).t)
            if len(self.bfTop) > 30:
                self.bfTop.pop(0); self.bfBot.pop(0); self.bfT.pop(0)
        if i >= 2 and h < B(2).l:
            self.sfTop.append(B(2).l); self.sfBot.append(h); self.sfT.append(B(1).t)
            if len(self.sfTop) > 30:
                self.sfTop.pop(0); self.sfBot.pop(0); self.sfT.pop(0)

        bullMSSok = bullMSS and (not p.need_gap or bullGap)
        bearMSSok = bearMSS and (not p.need_gap or bearGap)
        bullOBTop = bullOBBot = bearOBTop = bearOBBot = None
        if bullMSS:
            if self.legLo is not None and self.legLoOp is not None and self.legLo < self.legLoOp < c:
                bullOBTop, bullOBBot = self.legLoOp, self.legLo
            elif bullGap:
                bullOBTop, bullOBBot = bullGapTop, bullGapBot
        if bearMSS:
            if self.legHi is not None and self.legHiOp is not None and c < self.legHiOp < self.legHi:
                bearOBTop, bearOBBot = self.legHi, self.legHiOp
            elif bearGap:
                bearOBTop, bearOBBot = bearGapTop, bearGapBot
        if not self.bearCisdUsed and self.upRunOpen is not None and isDn and c < self.upRunOpen:
            self.bearCisdUsed = True
        if not self.bullCisdUsed and self.dnRunOpen is not None and isUp and c > self.dnRunOpen:
            self.bullCisdUsed = True

        # ── sessions ──
        inArr = [in_sess(t, 18 * 60, 19 * 60), in_sess(t, 21 * 60, 22 * 60), in_sess(t, 2 * 60, 3 * 60),
                 in_sess(t, 8 * 60, 9 * 60), in_sess(t, 9 * 60, 10 * 60), in_sess(t, 20 * 60, 23 * 60 + 59),
                 in_sess(t, 2 * 60, 5 * 60), in_sess(t, 0, 9 * 60 + 30)]
        for s, inS in zip(self.sessions, inArr):
            if not s.enabled:
                continue
            if inS and not s.forming:
                s.forming, s.ready, s.hi, s.lo, s.startT = True, False, h, l, t
            elif inS and s.forming:
                s.hi, s.lo = max(s.hi, h), min(s.lo, l)
            if not inS and s.forming:
                s.forming, s.ready = False, True
                sd = ny_dt(s.startT)
                e = int(sd.replace(hour=17, minute=0, second=0, microsecond=0).timestamp() * 1000)
                s.endT = e + DAY if e <= s.startT else e
            if s.ready and not s.forming and not (t < s.endT):
                s.ready = False

        # ── KL zones ──
        self.addZones(self.p.kl_same, newHTF, self.crtName, aH1, aL1, aH3, aL3, aOB)
        self.addZones(self.tfB_ok, newB, self.klName_tf, bH1, bL1, bH3, bL3, bOB)
        self.addZones(self.p.kl_day and self.tfD_ok, dayChg, "D", self.pdh, self.pdl, dH3, dL3, dOB)
        for zi in range(len(self.zones) - 1, -1, -1):
            z = self.zones[zi]
            thru = c < z.bot if z.dir > 0 else c > z.top
            if thru:
                if z.inv or z.kind == "SD":
                    self.zones.pop(zi)
                else:
                    z.inv, z.dir = True, -z.dir

        # ── swept HTF swing points ──
        for si in range(len(self.hSwHi) - 1, -1, -1):
            v = self.hSwHi[si]
            if h > v:
                self.swptHi.append(v); self.swptHiT.append(t); self.hSwHi.pop(si)
        for si in range(len(self.hSwLo) - 1, -1, -1):
            v = self.hSwLo[si]
            if l < v:
                self.swptLo.append(v); self.swptLoT.append(t); self.hSwLo.pop(si)
        while len(self.swptHi) > 20:
            self.swptHi.pop(0); self.swptHiT.pop(0)
        while len(self.swptLo) > 20:
            self.swptLo.pop(0); self.swptLoT.pop(0)

        # ── CRT-TF history: A / V, EQ 50%, supply / demand, swings ──
        if newHTF and kH is not None:
            for ai in range(len(self.avLv) - 1, -1, -1):
                v, d = self.avLv[ai], self.avDr[ai]
                if (d < 0 and kC > v) or (d > 0 and kC < v):
                    self.avLv.pop(ai); self.avDr.pop(ai)
            self.hO.append(kO); self.hH.append(kH); self.hL.append(kL); self.hC.append(kC); self.hT.append(kT)
            while len(self.hH) > 40:
                for arr in (self.hO, self.hH, self.hL, self.hC, self.hT):
                    arr.pop(0)
            n = len(self.hH)
            if p.use_av and n >= 3:
                o1, c1 = self.hO[n - 2], self.hC[n - 2]
                o2, c2 = self.hO[n - 3], self.hC[n - 3]
                if c1 > o1 and kC < kO and c1 > max(o2, c2):
                    self.avLv.append(max(c1, kO)); self.avDr.append(-1)
                if c1 < o1 and kC > kO and c1 < min(o2, c2):
                    self.avLv.append(min(c1, kO)); self.avDr.append(1)
                while len(self.avLv) > 20:
                    self.avLv.pop(0); self.avDr.pop(0)
            if p.use_eq and self.hAtr is not None and kH - kL >= 2 * self.hAtr:
                self.eqLv.append((kH + kL) / 2)
                while len(self.eqLv) > 10:
                    self.eqLv.pop(0)
            if p.use_sd and n >= 4 and self.hAtr is not None:
                hh, ll = self.hH, self.hL
                bH = max(hh[n - 2], hh[n - 3], hh[n - 4])
                bL = min(ll[n - 2], ll[n - 3], ll[n - 4])
                mxL = max(ll[n - 2], ll[n - 3], ll[n - 4])
                mnH = min(hh[n - 2], hh[n - 3], hh[n - 4])
                small = all(hh[n - k] - ll[n - k] <= 0.8 * self.hAtr for k in (2, 3, 4))
                disp = abs(kC - kO) >= 1.2 * self.hAtr
                if small and mxL < mnH and disp:
                    if kC < bL:
                        self.newZone(-1, bH, bL, "SD", self.crtName)
                    elif kC > bH:
                        self.newZone(1, bH, bL, "SD", self.crtName)
            if n >= 5:
                midH, midL = self.hH[n - 3], self.hL[n - 3]
                if midH > self.hH[n - 5] and midH > self.hH[n - 4] and midH > self.hH[n - 2] and midH > self.hH[n - 1]:
                    self.hSwHi.append(midH)
                if midL < self.hL[n - 5] and midL < self.hL[n - 4] and midL < self.hL[n - 2] and midL < self.hL[n - 1]:
                    self.hSwLo.append(midL)
            while len(self.hSwHi) > 30:
                self.hSwHi.pop(0)
            while len(self.hSwLo) > 30:
                self.hSwLo.pop(0)

        # ── HTF close: state of every CRT ──
        if newHTF and self.crts and kC is not None:
            for ci in range(len(self.crts) - 1, -1, -1):
                cr = self.crts[ci]
                if cr.done:
                    self.crts.pop(ci)
                    continue
                bear = cr.dir < 0
                inside = cr.rL < kC < cr.rH
                outS = kC >= cr.rH if bear else kC <= cr.rL
                conf = False
                if cr.entered:
                    if cr.state == 0 and cr.sweepT == kT and not cr.checked:
                        cr.checked, cr.c2ok = True, inside
                    if inside:
                        cr.state, cr.confirmed = 1, True
                    cr.age += 1
                    if p.hold_max > 0 and cr.age >= p.hold_max + (1 if cr.preConf else 0):
                        self.finish(cr)
                        self.emit("exit", cr, f"{self.head(cr)}{' 空單 ' if bear else ' 多單 '}持倉到上限 {p.hold_max} 根，剩下按現價平倉"
                                  f" · 累計 {sgn(cr.rz)}R · {self.kName(cr.eKind)}", ev="逾時平倉", rz=cr.rz, done=True)
                elif cr.state == 0 and cr.sweepT == kT:
                    other = kL <= cr.rL if bear else kH >= cr.rH
                    if not cr.confirmed and not cr.multi and other:
                        first = (cHT < cLT) if bear else (cLT < cHT)
                        cr.done = True
                        if first:
                            cr.model = 2
                            cr.result = "Model 2：C2 自己掃完並打到另一端（不列入統計）"
                    elif inside:
                        conf = True
                    elif outS and p.use_m3 and cr.waitCnt < p.m3_max:
                        cr.state, cr.waitCnt, cr.multi, cr.tbs = 2, cr.waitCnt + 1, True, True
                    elif cr.confirmed:
                        self.finish(cr, "收在區間外，CRT 失效")
                    else:
                        cr.done = True
                elif cr.state == 2:
                    if inside:
                        conf = True
                    elif outS and cr.waitCnt < p.m3_max:
                        cr.waitCnt += 1
                    elif cr.confirmed:
                        self.finish(cr, "沒有收回，CRT 失效")
                    else:
                        cr.done = True
                elif cr.state == 1:
                    if outS and p.use_m3:
                        cr.state, cr.waitCnt, cr.multi, cr.tbs = 2, 1, True, True
                        self.cancelPending(cr, "C 棒收在區間外（Model 3），先取消，等收回區間再重新判斷")
                        for k in (2, 3, 5, 6, 7):
                            self.setLv(cr, k, None, None)
                    elif outS:
                        self.finish(cr, "收在區間外，CRT 失效")
                    else:
                        cr.age += 1
                        if cr.age >= p.c3_bars:
                            self.finish(cr)
                if conf:
                    first = not cr.confirmed
                    cr.state, cr.age, cr.waitCnt = 1, 0, 0
                    cr.model = 3 if cr.multi else 1
                    cr.entry = kC
                    cr.confirmed = True
                    self.info(cr)
                    if first and not cr.counted:
                        cr.counted = True
                        self.stats[10] += 1
                        pre = self.preGate(cr)
                        if pre:
                            self.skipWhy(cr, pre)
                        else:
                            if p.e8:
                                why = self.gate(cr, kC, True)
                                if why == "":
                                    self.enter(cr, kC, 8)
                                else:
                                    cr.note = f"{self.kName(8)} {why}"
                            self.retire(cr)
                            self.lastCRT = cr
                            self.emit("confirm", cr, self.confirmText(cr))
                if cr.done:
                    self.crts.pop(ci)

        # ── every bar: fills, re-sweep, LTF structure, entry signals, stop / targets ──
        if self.crts:
            for ci in range(len(self.crts) - 1, -1, -1):
                cr = self.crts[ci]
                if cr.done:
                    continue
                bear = cr.dir < 0
                newAct = False
                if cr.entered:
                    if i > cr.eBar:
                        ev = self.manage(cr)
                        if ev:
                            txt = (f"{self.head(cr)}{' 空單 ' if bear else ' 多單 '}{ev} · 累計 {sgn(cr.rz)}R"
                                   + ("" if cr.done else f" · 還剩 {round(cr.wRem * 100)}%") + f" · {self.kName(cr.eKind)}")
                            self.emit("exit", cr, txt, ev=ev, rz=cr.rz, done=cr.done,
                                      hits=[cr.tp1 and not cr.t1Skip, cr.tp2, cr.tp3Hit, cr.tp4Hit], stopped=cr.stopped,
                                      be=cr.stopped and cr.slNow == cr.ePx, sl_now=cr.slNow, sl_tag=self.slTag(cr),
                                      sl_now_txt=self.px(cr.slNow))
                else:
                    canEnter = cr.state == 1 or (p.aggressive and (c < cr.rH if bear else c > cr.rL))
                    filled = False
                    if canEnter and self.hasLv(cr):
                        best, bk = None, 0
                        for k in (2, 3, 5, 6, 7):
                            lv, ix = cr.lv[k], cr.ix[k]
                            if lv is not None and ix is not None and ix >= 0 and i > ix and (h >= lv if bear else l <= lv):
                                if best is None or (lv < best if bear else lv > best):
                                    best, bk = lv, k
                        openOK = o < cr.sl if bear else o > cr.sl
                        if best is not None and openOK:
                            why = self.gate(cr, best, True) if bk == 7 else self.dayBlock
                            if why:
                                cr.note = f"{self.kName(bk)} {why}"
                                if bk == 7:
                                    if cr.actK == 7:
                                        self.cancelPending(cr, f"{self.kName(7)} {why}")
                                    self.setLv(cr, 7, None, -1)
                            else:
                                fpx = max(o, best) if bear else min(o, best)
                                cr.eLv = best
                                self.enter(cr, fpx, bk)
                                filled = newAct = True
                                if (h > cr.sl) if bear else (l < cr.sl):
                                    self.stopOut(cr)
                                    self.emit("exit", cr, f"{self.head(cr)}{' 空單 ' if bear else ' 多單 '}掛單成交的同一根 K 就碰到停損 · {sgn(cr.rz)}R",
                                              ev="✗ 停損", rz=cr.rz, done=True, hits=[False] * 4, stopped=True, be=False)
                    if not cr.done and not filled:
                        if (h > cr.ext) if bear else (l < cr.ext):
                            self.cancelPending(cr, f"價格再掃{'更高' if bear else '更低'}，掃點 / 停損更新，等新的進場訊號")
                            self.newExt(cr)
                            if cr.state != 0:
                                cr.state, cr.sweepT = 0, self.curT
                            if self.curT != cr.t2:
                                cr.multi = True
                        elif cr.confirmed and ((l <= cr.rL) if bear else (h >= cr.rH)):
                            cr.oppHit = True
                            self.finish(cr)
                    if not cr.done and not cr.entered:
                        cr.legX = (l if cr.legX is None else min(cr.legX, l)) if bear else (h if cr.legX is None else max(cr.legX, h))
                        cisdNew = rtJust = False
                        if cr.state == 0 and ((c > cr.rH) if bear else (c < cr.rL)):
                            cr.tbs = True
                        gapNow = bearGap if bear else bullGap
                        # 破了之後又收盤回到 CISD 另一側，而且沒有 ② 掛單可以接 → ① 重新生效
                        canNow = cr.state == 1 or (p.aggressive and (c < cr.rH if bear else c > cr.rL))
                        if cr.cbHit and cr.lv[2] is None and (not canNow or not p.e2 or cr.ix[2] == -1) \
                                and ((c > cr.cisdLvl) if bear else (c < cr.cisdLvl)):
                            cr.cbHit, cr.cbN, cr.cbGap, cr.cbOKIdx, cr.touchNB = False, 0, False, None, False
                            if cr.ix[2] == -1:
                                cr.ix[2] = None
                        if cr.cbHit and not cr.cbGap and cr.cbN >= 2:
                            cr.cbHit = False
                        if not cr.cbHit and ((c < cr.cisdLvl) if bear else (c > cr.cisdLvl)):
                            cr.cbHit, cr.cbN, cr.cbGap = True, 0, gapNow
                        elif cr.cbHit and not cr.cbGap and cr.cbN < 2:
                            cr.cbN += 1
                            cr.cbGap = gapNow
                        if not cr.cbHit and ((l <= cr.cisdLvl < c) if bear else (c < cr.cisdLvl <= h)):
                            cr.touchNB = True
                        cisdOK = cr.cbHit and (cr.cbGap or not p.need_gap)
                        if cisdOK and cr.cbOKIdx is None:
                            cr.cbOKIdx, cisdNew = i, True
                        if cr.ifTop is None and cr.runT is not None:
                            tops, bots, ts = (self.bfTop, self.bfBot, self.bfT) if bear else (self.sfTop, self.sfBot, self.sfT)
                            for k in range(len(tops) - 1, -1, -1):
                                if cr.ifTop is None:
                                    ft, tp, bt = ts[k], tops[k], bots[k]
                                    if cr.runT <= ft <= cr.extT and ((c < bt) if bear else (c > tp)):
                                        cr.ifTop, cr.ifBot, cr.ifT = tp, bt, t
                        if not cr.bbAct and cr.bbTop is not None and ((c < cr.bbBot) if bear else (c > cr.bbTop)):
                            cr.bbAct, cr.bbT = True, t
                        if cr.mss and cr.obTop is not None and not cr.retest and ((h > cr.obTop) if bear else (l < cr.obBot)):
                            cr.mss, cr.obTop, cr.obBot = False, None, None
                        if (bearMSSok if bear else bullMSSok) and not cr.mss:
                            cr.mss, cr.mssT = True, t
                            cr.mssLvl = bearLvl if bear else bullLvl
                            cr.mssIdx = bearIdx if bear else bullIdx
                            cr.obTop = bearOBTop if bear else bullOBTop
                            cr.obBot = bearOBBot if bear else bullOBBot
                            q = bearQM if bear else bullQM
                            if cr.qm is None and q is not None and ((c < q < cr.ext) if bear else (cr.ext < q < c)):
                                cr.qm = q
                        if cr.mss and not cr.retest and cr.obTop is not None and t > cr.mssT:
                            rt = (cr.obBot <= h <= cr.obTop and c < cr.obBot) if bear else (cr.obBot <= l <= cr.obTop and c > cr.obTop)
                            if rt:
                                cr.retest = rtJust = True

                        canEnter = cr.state == 1 or (p.aggressive and (c < cr.rH if bear else c > cr.rL))
                        if canEnter and not cr.done:
                            beyond = c < cr.cisdLvl if bear else c > cr.cisdLvl
                            t1 = p.e1 and cisdNew and beyond
                            t4 = p.e4 and rtJust
                            a2 = p.e2 and cisdOK and cr.ix[2] is None
                            a3 = p.e3 and cr.ifTop is not None and cr.ix[3] is None
                            a5 = p.e5 and cr.qm is not None and cr.ix[5] is None
                            a6 = p.e6 and cr.bbAct and cr.ix[6] is None
                            a7 = p.e7 and cisdOK and cr.legX is not None and (cr.ix[7] is None or cr.ix[7] >= 0)
                            ok = True
                            if (t1 or t4 or a2 or a3 or a5 or a6 or (a7 and cr.ix[7] is None)) and not cr.confirmed:
                                ok = self.aggOK(cr)
                            if ok:
                                if t1:
                                    why = self.gate(cr, c, True)
                                    if why == "":
                                        self.enter(cr, c, 1)
                                        newAct = True
                                    else:
                                        cr.note = f"{self.kName(1)} {why}"
                                if t4 and not cr.entered:
                                    oldSL = cr.sl
                                    if p.apl_sl_ob:
                                        buf = p.sl_buf * p.tick
                                        obSL = cr.obTop + buf if bear else cr.obBot - buf
                                        if (cr.sl > obSL > c) if bear else (cr.sl < obSL < c):
                                            cr.sl = obSL
                                    why = self.gate(cr, c, True)
                                    if why == "":
                                        self.enter(cr, c, 4)
                                        newAct = True
                                    else:
                                        cr.sl = oldSL
                                        cr.note = f"{self.kName(4)} {why}"
                                if not cr.entered:
                                    if a2 and self.tryLv(cr, 2, cr.cisdLvl):
                                        newAct = True
                                    if a3 and self.tryLv(cr, 3, cr.ifBot if bear else cr.ifTop):
                                        newAct = True
                                    if a5 and self.tryLv(cr, 5, cr.qm):
                                        newAct = True
                                    if a6 and self.tryLv(cr, 6, cr.bbBot if bear else cr.bbTop):
                                        newAct = True
                                    if a7:
                                        fp = cr.legX + p.fib_lvl * (cr.ext - cr.legX) if bear else cr.legX - p.fib_lvl * (cr.legX - cr.ext)
                                        first7 = cr.ix[7] is None
                                        self.setLv(cr, 7, fp, i if first7 else cr.ix[7])
                                        if first7:
                                            newAct = True
                if not cr.done and not cr.entered:
                    self.syncOrder(cr)
                if newAct:
                    if self.lastCRT is None or self.lastCRT.id != cr.id:
                        self.retire(cr)
                        self.lastCRT = cr
            self.crts = [x for x in self.crts if not x.done]

        # ── new sweep: C2 takes the C1 high / low ──
        if kH is not None:
            if h > kH and self.madeBearT != self.curT:
                self.madeBearT = self.curT
                dup = not p.reopen and any(not x.done and not x.entered and x.dir < 0 and x.rL < kH <= x.ext + (x.rH - x.rL) * 0.05
                                           for x in self.crts)
                if not dup:
                    self.idSeq += 1
                    cr = CRT(self.idSeq, -1, kH, kL, (kH + kL) / 2, c, kT, self.curT, self.curT, self.c1HIdx, i)
                    self.newExt(cr)
                    if c > kH:
                        cr.tbs = True
                    self.crts.append(cr)
                    self.history.append(cr)
            if l < kL and self.madeBullT != self.curT:
                self.madeBullT = self.curT
                dup = not p.reopen and any(not x.done and not x.entered and x.dir > 0 and x.ext - (x.rH - x.rL) * 0.05 <= kL < x.rH
                                           for x in self.crts)
                if not dup:
                    self.idSeq += 1
                    cr = CRT(self.idSeq, 1, kH, kL, (kH + kL) / 2, c, kT, self.curT, self.curT, self.c1LIdx, i)
                    self.newExt(cr)
                    if c < kL:
                        cr.tbs = True
                    self.crts.append(cr)
                    self.history.append(cr)

    def confirmText(self, c: CRT) -> str:
        bear = c.dir < 0
        return (f"{self.head(c)}{' 空方' if bear else ' 多方'} CRT 確認（M{c.model}"
                f"{' · ' + c.sess if c.sess else ''} · KL：{c.klName or '無'}）\n"
                f"CRH {self.px(c.rH)} / CRL {self.px(c.rL)} · 掃點 {self.px(c.ext)}\n"
                f"CISD 觸發線 {self.px(c.cisdLvl)}：LTF 收盤{'跌破' if bear else '漲破'}{' + 缺口' if self.p.need_gap else ''}才進場")
