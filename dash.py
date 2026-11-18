"""CRT 訊號儀表板（本機，MT5 行情；看訊號、手動下單用）

  python dash.py                                   → http://127.0.0.1:8011
  python dash.py --config config.mt5.json --port 8011

每根 LTF K 收盤後 8 秒跑一次 CRT（跟 `scan.py --loop` 同一套：新訊號推 Discord、進場卡片打勾、sent.json 去重），
網頁顯示每個商品目前的 CRT（掃蕩中 / 已確認 / 掛單 / 持倉）、K 線圖 + 價位線、最近訊號、回放統計、MT5 帳戶與手動持倉。
**只讀 MT5，不下單**（使用者看到通知自己手動下單）。

設定分兩層：config 檔（預設值、策略參數，改完下一次掃描套用）＋ 網頁上改的（商品、要通知哪些事件）存在
$CRT_DATA_DIR/settings.json，蓋過 config 檔。Discord Webhook 在 .env 的 DISCORD_WEBHOOK_URL（網頁也能改）。
資料（sent.json、settings.json、log）在 $CRT_DATA_DIR（預設 ~/crt-dash-data）。
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import os
import sys
import threading
import time
import traceback
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from logging.handlers import RotatingFileHandler
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent


def load_env(path: Path) -> None:
    """.env：KEY=VALUE；已經設定的環境變數優先。"""
    if not path.exists():
        return
    for ln in path.read_text(encoding="utf-8").splitlines():
        ln = ln.strip()
        if ln and not ln.startswith("#") and "=" in ln:
            k, v = ln.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


load_env(ROOT / ".env")
DATA = Path(os.environ.setdefault("CRT_DATA_DIR", str(Path.home() / "crt-dash-data")))

import mt5src  # noqa: E402
import notify  # noqa: E402
import scan  # noqa: E402
from crt_engine import K_NAME, TF_MS  # noqa: E402

log = logging.getLogger("crt")
SETTINGS = DATA / "settings.json"
KINDS = ("limit", "entry", "exit", "cancel", "confirm")
SNAP: dict = {"started": int(time.time() * 1000), "symbols": {}, "account": None}
CHART: dict[str, dict] = {}
WAKE = threading.Event()
LOCK = threading.Lock()         # 掃描執行緒寫、網頁讀，同一時間只有一邊碰 SNAP


class _Tee:
    """scan.py 用 print 輸出：同時寫到視窗和 log 檔。"""

    def __init__(self, stream, level):
        self.stream, self.level, self.buf = stream, level, ""

    def write(self, s):
        try:
            self.stream.write(s)
        except Exception:
            pass
        self.buf += s
        while "\n" in self.buf:
            ln, self.buf = self.buf.split("\n", 1)
            if ln.strip():
                log.log(self.level, ln)

    def flush(self):
        try:
            self.stream.flush()
        except Exception:
            pass


def setup_log() -> None:
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")    # Windows 主控台是 cp950，emoji 會讓 print 失敗
        except Exception:
            pass
    (DATA / "logs").mkdir(parents=True, exist_ok=True)
    h = RotatingFileHandler(DATA / "logs" / "crt.log", maxBytes=5_000_000, backupCount=5, encoding="utf-8")
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.addHandler(h)
    log.setLevel(logging.INFO)
    sys.stdout = _Tee(sys.stdout, logging.INFO)
    sys.stderr = _Tee(sys.stderr, logging.WARNING)


# ───────────── 設定（網頁改的蓋過 config 檔）─────────────
CFG_PATH = ROOT / "config.mt5.json"


def load_settings() -> dict:
    try:
        return json.loads(SETTINGS.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_settings(upd: dict) -> dict:
    s = dict(load_settings(), **upd)
    SETTINGS.parent.mkdir(parents=True, exist_ok=True)
    tmp = SETTINGS.with_suffix(".tmp")
    tmp.write_text(json.dumps(s, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, SETTINGS)
    return s


def load_cfg() -> dict:
    cfg = scan.load_cfg(CFG_PATH)
    s = load_settings()
    for k in ("symbols", "notify"):
        if k in s:
            cfg[k] = s[k]
    cfg["alerts"] = s.get("alerts", True)
    if os.environ.get("SYMBOLS"):
        cfg["symbols"] = os.environ["SYMBOLS"].split(",")
    return cfg


def set_webhook(url: str) -> None:
    """寫進 .env（gitignore）並馬上生效。"""
    env = ROOT / ".env"
    lines = env.read_text(encoding="utf-8").splitlines() if env.exists() else []
    lines = [ln for ln in lines if not ln.strip().startswith("DISCORD_WEBHOOK_URL=")] + [f"DISCORD_WEBHOOK_URL={url}"]
    env.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.environ["DISCORD_WEBHOOK_URL"] = url


def hook_set() -> bool:
    return bool(os.environ.get("DISCORD_WEBHOOK_URL", "").strip())


_last_alert: dict[str, float] = {}


def alert(key: str, title: str, body: str, cfg: dict) -> None:
    """系統警示（MT5 斷線、掃描失敗）推 Discord，同一種 30 分鐘最多一次。"""
    if not cfg.get("alerts", True) or not hook_set() or time.time() - _last_alert.get(key, 0) < 1800:
        return
    _last_alert[key] = time.time()
    try:
        notify.send("exit", 0, f"⚠ {title}\n{body}", datetime.now(ZoneInfo(cfg.get("display_tz", "Asia/Taipei"))).strftime("%m/%d %H:%M"))
    except Exception as e:
        log.warning("警示送不出去：%s", e)


# ───────────── 快照（給網頁）─────────────
def _num(x):
    return None if x is None or (isinstance(x, float) and not math.isfinite(x)) else x


def _dec(tick: float) -> int:
    return max(0, -int(math.floor(math.log10(tick) + 1e-9))) if tick and tick > 0 else 5


def crt_view(eng, c) -> dict:
    bear = c.dir < 0
    lv = [{"k": k, "name": eng.kName(k), "price": _num(v)} for k, v in c.lv.items() if v is not None]
    if c.entered:
        status = "持倉中"
    elif lv:
        status = "掛單中"
    elif c.confirmed:
        status = "已確認，等 LTF 進場訊號"
    elif c.state == 2:
        status = "收在區間外，等收回（M3）"
    else:
        status = "C2 掃蕩中（還沒收盤）"
    return {
        "key": c.key, "dir": c.dir, "model": c.model, "status": status, "confirmed": c.confirmed,
        "entered": c.entered, "rH": c.rH, "rL": c.rL, "mid": c.mid, "sl": _num(c.sl), "slNow": _num(c.slNow),
        "slTag": eng.slTag(c), "slWhy": eng.slWhy(c) if c.sl is not None else "",
        "cisd": _num(c.cisdLvl), "ext": _num(c.ext), "kl": c.klName, "sess": c.sess, "tbs": c.tbs, "poor": c.poor,
        "note": c.note, "t1": c.t1, "t2": c.t2, "levels": lv,
        "entry": {"kind": c.eKind, "name": eng.kName(c.eKind), "price": c.ePx, "t": c.eT} if c.entered else None,
        "tps": [_num(c.t1Px), c.rL if bear else c.rH, _num(c.tp3), _num(c.tp4)],
        "tpNames": ["區間 50%", "對側", c.tp3Name, c.tp4Name],
        "hits": [bool(c.tp1 and not c.t1Skip), c.tp2, c.tp3Hit, c.tp4Hit], "t1Skip": c.t1Skip,
        "rr": [_num(c.r1), _num(c.r2), _num(c.r3), _num(c.r4)], "rz": c.rz, "wRem": c.wRem,
    }


def stats_view(eng) -> dict:
    s, n = eng.stats, eng.stats[0]
    pct = (lambda v: v / n) if n else (lambda v: None)
    return {"settled": int(n), "tp1": pct(s[1]), "tp2": pct(s[2]), "tp3": pct(s[3]), "tp4": pct(s[13]),
            "stopFirst": pct(s[4]), "be": pct(s[16]), "rPer": (s[15] / n) if n else None, "rSum": s[15],
            "setups": int(s[10]), "noTrigger": int(s[11]), "filtered": int(s[12]),
            "kinds": {K_NAME[k]: eng.kCnt[k] for k in range(1, 9) if eng.kCnt[k]}}


def tz_shift(t_ms: int, tz: ZoneInfo) -> int:
    """圖表時間：lightweight-charts 只認 UTC 秒，先加上顯示時區的位移，軸上才會是台灣時間。"""
    return int(t_ms / 1000 + tz.utcoffset(datetime.fromtimestamp(t_ms / 1000, tz=timezone.utc)).total_seconds())


def build_symbol(sym: str, eng, cfg: dict) -> dict:
    ltf_ms = TF_MS[cfg.get("ltf", "5m")]
    bars = eng.ltf_bars
    tz = ZoneInfo(cfg.get("display_tz", "Asia/Taipei"))
    events = [{"t": e.t + ltf_ms, "bar": e.t, "kind": e.kind, "key": e.key, "title": e.text.split("\n")[0],
               "text": e.text, "dir": e.data.get("dir"), "sig": e.data.get("sig"),
               "price": _num(e.data.get("price")), "sl": _num(e.data.get("sl"))} for e in eng.events]
    active = [crt_view(eng, c) for c in eng.crts if not c.done]
    order = {"持倉中": 0, "掛單中": 1}
    active.sort(key=lambda v: (order.get(v["status"], 2), -v["t2"]))
    n = int(cfg.get("chart_bars", 400))
    shift = lambda t: tz_shift(t, tz)
    CHART[sym] = {
        "bars": [{"time": shift(b.t), "open": b.o, "high": b.h, "low": b.l, "close": b.c} for b in bars[-n:]],
        "events": [dict(ev, time=shift(ev["bar"])) for ev in events if bars and ev["bar"] >= bars[-n:][0].t],
    }
    return {"error": None, "tick": eng.p.tick, "dec": _dec(eng.p.tick), "crts": active, "stats": stats_view(eng),
            "events": events[-150:], "from": bars[0].t if bars else None, "lastBar": (bars[-1].t + ltf_ms) if bars else None,
            "quote": None}


def do_scan(cfg: dict) -> None:
    t0 = time.time()
    results: dict = {}
    err = None
    sent = 0
    try:
        sent = scan.scan_once(cfg, results=results)
    except Exception as e:
        err = f"{type(e).__name__}: {e}"
        log.error("掃描失敗：%s\n%s", e, traceback.format_exc())
    syms = {}
    for sym, r in results.items():
        if isinstance(r, Exception):
            old = SNAP["symbols"].get(sym) or {}
            syms[sym] = dict(old, error=str(r))
        else:
            try:
                syms[sym] = build_symbol(sym, r, cfg)
            except Exception as e:
                log.error("整理 %s 失敗：%s\n%s", sym, e, traceback.format_exc())
                syms[sym] = {"error": f"整理資料失敗：{e}"}
    p = cfg.get("params", {})
    with LOCK:
        _publish(syms, err, t0, sent, cfg, p)


def _publish(syms, err, t0, sent, cfg, p) -> None:
    SNAP.update({
        # 掃描整個失敗（例如 MT5 斷線）時保留上一次的畫面；成功就照現在的商品清單（刪掉的商品會消失）
        "symbols": syms if err is None else (syms or SNAP["symbols"]), "scanError": err, "lastScan": int(time.time() * 1000),
        "scanSec": round(time.time() - t0, 2), "lastSent": sent, "sentTotal": SNAP.get("sentTotal", 0) + sent,
        "config": {"ltf": cfg.get("ltf", "5m"), "crt_tf": p.get("crt_tf", "1H"), "kl_tf": p.get("kl_tf", "4H"),
                   "tz": cfg.get("display_tz", "Asia/Taipei"), "symbols": cfg.get("symbols", []), "params": p,
                   "notify": cfg.get("notify", []), "alerts": cfg.get("alerts", True), "discord": hook_set(),
                   "ltf_bars": cfg.get("ltf_bars")},
    })


LIVE: dict = {"t": 0, "symbols": {}}


def refresh_live(cfg: dict) -> None:
    """每秒：帳戶 / 持倉、各商品報價、還沒收盤的那根 K（只給畫面，訊號只看收盤）。"""
    acct, err, live = None, None, {}
    try:
        acct = mt5src.account()
    except Exception as e:
        err = str(e)
    tz = ZoneInfo(cfg.get("display_tz", "Asia/Taipei"))
    ltf = cfg.get("ltf", "5m")
    for sym in list(SNAP["symbols"]):
        try:
            q = mt5src.quote(sym) or {}
            b = mt5src.candles(sym, ltf, 1, closed_only=False)[-1]
            q["bar"] = {"time": tz_shift(b.t, tz), "open": b.o, "high": b.h, "low": b.l, "close": b.c}
            live[sym] = q
        except Exception:
            pass
    with LOCK:
        SNAP["account"] = acct if acct is not None else SNAP.get("account")
        SNAP["mt5Error"] = err
        for sym, q in live.items():
            if sym in SNAP["symbols"]:
                SNAP["symbols"][sym]["quote"] = {"bid": q.get("bid"), "ask": q.get("ask")}
        LIVE.update({"t": int(time.time() * 1000), "symbols": live, "account": SNAP["account"], "mt5Error": err})


def loop() -> None:
    cfg, nxt = None, 0.0
    while True:
        try:
            cfg = load_cfg()
            mt5src.configure(cfg.get("mt5_path", ""))
            SNAP["cfgError"] = None
        except Exception as e:
            SNAP["cfgError"] = f"設定檔讀不到：{e}"
            if cfg is None:
                time.sleep(5)
                continue
        ltf_ms = TF_MS[cfg.get("ltf", "5m")]
        now = time.time() * 1000
        if now >= nxt or WAKE.is_set():
            WAKE.clear()
            do_scan(cfg)
            if SNAP.get("scanError"):
                alert("scan", "CRT 儀表板掃描失敗", SNAP["scanError"] + "\n（這段時間不會有訊號通知）", cfg)
            now = time.time() * 1000
            nxt = (now // ltf_ms + 1) * ltf_ms + 8_000        # 下一根 K 收盤後 8 秒
            SNAP["nextScan"] = int(nxt)
        refresh_live(cfg)
        if SNAP.get("mt5Error"):
            alert("mt5", "CRT 儀表板連不到 MT5", SNAP["mt5Error"] + "\n請確認 MT5（原逆勢攤平那個）有開、有登入", cfg)
        WAKE.wait(max(0.3, min(1.0, (nxt - time.time() * 1000) / 1000)))     # 報價每秒更新


# ───────────── HTTP ─────────────
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code: int = 200) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8"), "application/json; charset=utf-8")

    def do_GET(self):
        u = urlparse(self.path)
        if u.path in ("/", "/index.html"):
            self._send(200, (ROOT / "dash.html").read_bytes(), "text/html; charset=utf-8")
        elif u.path == "/favicon.ico":
            self._send(204, b"", "image/x-icon")
        elif u.path == "/api/state":
            with LOCK:
                body = json.dumps(SNAP, ensure_ascii=False, default=str)
            self._send(200, body.encode("utf-8"), "application/json; charset=utf-8")
        elif u.path == "/api/live":
            with LOCK:
                body = json.dumps(LIVE, ensure_ascii=False, default=str)
            self._send(200, body.encode("utf-8"), "application/json; charset=utf-8")
        elif u.path == "/api/chart":
            sym = parse_qs(u.query).get("sym", [""])[0]
            with LOCK:
                body = json.dumps(CHART.get(sym) or {"bars": [], "events": []}, ensure_ascii=False, default=str)
            self._send(200, body.encode("utf-8"), "application/json; charset=utf-8")
        elif u.path == "/api/search":
            q = parse_qs(u.query).get("q", [""])[0]
            try:
                self._json({"items": mt5src.search(q)})
            except Exception as e:
                self._json({"items": [], "error": str(e)})
        elif u.path == "/api/settings":
            cfg = load_cfg()
            self._json({"symbols": cfg.get("symbols", []), "notify": cfg.get("notify", []), "alerts": cfg.get("alerts", True),
                        "discord": hook_set()})
        else:
            self._send(404, b"not found", "text/plain")

    def do_POST(self):
        path = urlparse(self.path).path
        # 只收 JSON：別的網站沒辦法用表單偷偷改這台的設定（跨站送 JSON 會被瀏覽器擋）
        if not self.headers.get("Content-Type", "").startswith("application/json"):
            return self._json({"ok": False, "error": "要用 JSON"}, 415)
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        except json.JSONDecodeError:
            return self._json({"ok": False, "error": "JSON 格式錯誤"}, 400)
        if path == "/api/scan":
            WAKE.set()
            self._json({"ok": True})
        elif path == "/api/symbols":
            names = [str(x).strip() for x in body.get("symbols", []) if str(x).strip()]
            names = list(dict.fromkeys(names))                       # 去重、保留順序
            bad = []
            for n in names:
                try:
                    mt5src.tick_size(n)
                except Exception:
                    bad.append(n)
            if bad:
                return self._json({"ok": False, "error": f"MT5 找不到：{', '.join(bad)}"}, 400)
            save_settings({"symbols": names})
            WAKE.set()
            self._json({"ok": True, "symbols": names})
        elif path == "/api/settings":
            upd = {}
            if "notify" in body:
                upd["notify"] = [k for k in KINDS if k in body["notify"]]
            if "alerts" in body:
                upd["alerts"] = bool(body["alerts"])
            hook = str(body.get("webhook", "")).strip()
            if hook:
                if not hook.startswith(("https://discord.com/api/webhooks/", "https://discordapp.com/api/webhooks/",
                                        "https://ptb.discord.com/api/webhooks/", "https://canary.discord.com/api/webhooks/")):
                    return self._json({"ok": False, "error": "Webhook 網址要是 https://discord.com/api/webhooks/… 開頭"}, 400)
                set_webhook(hook)
            if upd:
                save_settings(upd)
            self._json({"ok": True, "discord": hook_set()})
        elif path == "/api/test/discord":
            if not hook_set():
                return self._json({"ok": False, "error": "還沒設定 Webhook"})
            try:
                cfg = load_cfg()
                notify.send("confirm", 0, "✅ CRT 儀表板測試訊息\n通知會發到這個頻道（只通知，不會自動下單）\n商品："
                            + "、".join(cfg.get("symbols", [])),
                            datetime.now(ZoneInfo(cfg.get("display_tz", "Asia/Taipei"))).strftime("%m/%d %H:%M"))
                self._json({"ok": True})
            except Exception as e:
                self._json({"ok": False, "error": str(e)})
        elif path == "/api/test/mt5":
            try:
                a = mt5src.account()
                self._json({"ok": True, "msg": f"{a['server']} #{a['login']}（{a['mode']}）餘額 {a['balance']:.2f} {a['currency']}"})
            except Exception as e:
                self._json({"ok": False, "error": str(e)})
        else:
            self._send(404, b"not found", "text/plain")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(ROOT / "config.mt5.json"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("CRT_PORT", "8011")))
    ap.add_argument("--host", default=os.environ.get("CRT_HOST", "127.0.0.1"))
    a = ap.parse_args()
    global CFG_PATH
    CFG_PATH = Path(a.config)
    setup_log()
    try:
        srv = ThreadingHTTPServer((a.host, a.port), Handler)
    except OSError as e:
        print(f"{a.port} 埠被占用（{e}），先關掉占用它的程式（例如舊的逆勢攤平儀表板）再開")
        sys.exit(1)
    threading.Thread(target=loop, daemon=True, name="scanner").start()
    print(f"CRT 訊號儀表板 http://{a.host}:{a.port}  設定 {a.config} + {SETTINGS}"
          f"  Discord {'已設定' if hook_set() else '未設定（只顯示，不推播）'}  只讀 MT5，不會下單")
    srv.serve_forever()


if __name__ == "__main__":
    os.chdir(ROOT)
    main()
