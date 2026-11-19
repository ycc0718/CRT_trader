"""Telegram 頻道收集器（Yuki 的 CRT 訊號）——可以整個資料夾搬到別台電腦單獨跑，不需要 MT5。

用你自己的 Telegram 帳號「唯讀」讀取頻道（Telethon），不會發任何訊息。抓到的訊息寫成 raw.json 放在 OUTPUT_DIR；
CRT 儀表板那台主機讀同一個資料夾（雲端同步資料夾或 Windows 共用資料夾），自動跟自己的訊號對照。

  python collector.py --login   第一次：api_id / api_hash、手機驗證碼、選頻道、設定輸出資料夾，然後抓全部歷史
  python collector.py --once    抓一次
  python collector.py --loop    一直跑：每 INTERVAL_MIN 分鐘抓一次（預設 5）

這個資料夾的 .env：TG_API_ID、TG_API_HASH、TG_CHAT、TG_CHAT_NAME、OUTPUT_DIR、INTERVAL_MIN
data\tg.session = 你 Telegram 帳號的登入狀態 ⚠ 等於密碼，不要給別人、不要放進同步資料夾
CRT 主專案的 tg_collect.py 也用這支（同一台收集時），所以兩邊行為一樣。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import socket
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
RECENT = 300            # 每次重抓最近幾則：新訊息 + 事後編輯（例如補上「TP2 ✅」）


def read_env(path: Path) -> dict:
    out = {}
    if path.exists():
        for ln in path.read_text(encoding="utf-8").splitlines():
            ln = ln.strip()
            if ln and not ln.startswith("#") and "=" in ln:
                k, v = ln.split("=", 1)
                out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def write_env(path: Path, key: str, val: str) -> None:
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    lines = [ln for ln in lines if not ln.strip().startswith(f"{key}=")] + [f"{key}={val}"]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


class Collector:
    """env_file：設定檔；session：Telegram 登入檔（不含 .session）；raw：輸出的 raw.json；status：輸出的 status.json。"""

    def __init__(self, env_file: Path, session: Path, raw: Path, status: Path | None = None):
        self.env_file, self.session, self.raw, self.status = env_file, session, raw, status

    # ───── 設定 ─────
    def env(self) -> dict:
        e = {k: v for k, v in os.environ.items() if k.startswith("TG_")}
        e.update(read_env(self.env_file))
        return e

    def set(self, key: str, val: str) -> None:
        write_env(self.env_file, key, val)
        os.environ[key] = val

    def configured(self) -> bool:
        e = self.env()
        return bool(e.get("TG_API_ID") and e.get("TG_API_HASH") and e.get("TG_CHAT")) and self.session.with_suffix(".session").exists()

    # ───── 資料 ─────
    def load_raw(self) -> dict:
        try:
            return json.loads(self.raw.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def save_raw(self, raw: dict) -> None:
        self.raw.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.raw.with_suffix(".tmp")
        tmp.write_text(json.dumps(raw, ensure_ascii=False, indent=0), encoding="utf-8")
        os.replace(tmp, self.raw)

    def write_status(self, **kw) -> None:
        if not self.status:
            return
        self.status.parent.mkdir(parents=True, exist_ok=True)
        st = {"t": int(time.time() * 1000), "host": socket.gethostname(), "chat": self.env().get("TG_CHAT_NAME", ""), **kw}
        tmp = self.status.with_suffix(".tmp")
        tmp.write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.status)

    # ───── Telegram ─────
    def _client(self):
        from telethon import TelegramClient
        e = self.env()
        self.session.parent.mkdir(parents=True, exist_ok=True)
        return TelegramClient(str(self.session), int(e["TG_API_ID"]), e["TG_API_HASH"],
                              device_model="CRT collector", app_version="1.0")

    @staticmethod
    def _rec(m) -> dict:
        return {"id": m.id, "date": int(m.date.timestamp() * 1000),
                "edit": int(m.edit_date.timestamp() * 1000) if m.edit_date else None,
                "text": m.message or "", "reply_to": getattr(m.reply_to, "reply_to_msg_id", None) if m.reply_to else None,
                "media": type(m.media).__name__ if m.media else None, "grouped": m.grouped_id}

    def _merge(self, raw: dict, m) -> str:
        r = self._rec(m)
        old = raw.get(str(m.id))
        if old is None:
            raw[str(m.id)] = dict(r, versions=[])
            return "new"
        if old.get("text") != r["text"] or old.get("edit") != r["edit"]:
            vers = old.get("versions", [])
            if old.get("text") != r["text"]:          # 被編輯：舊文字留下來
                vers.append({"text": old.get("text", ""), "until": r["edit"] or int(time.time() * 1000)})
            raw[str(m.id)] = dict(r, versions=vers)
            return "edited"
        return "same"

    async def _entity(self, client):
        e = self.env()
        chat = e["TG_CHAT"]
        try:
            return await client.get_entity(int(chat) if chat.lstrip("-").isdigit() else chat)
        except Exception:
            async for d in client.iter_dialogs():      # 新的 session 要先把頻道載入
                if str(d.id) == chat or d.name == e.get("TG_CHAT_NAME"):
                    return d.entity
        raise RuntimeError(f"找不到頻道 {chat}（帳號還在這個頻道 / 群組裡嗎？）")

    async def _fetch(self, full: bool) -> dict:
        client = self._client()
        await client.connect()
        try:
            if not await client.is_user_authorized():
                raise RuntimeError("Telegram 還沒登入（執行登入那一步）")
            ent = await self._entity(client)
            raw = self.load_raw()
            cnt = {"new": 0, "edited": 0, "same": 0}
            async for m in client.iter_messages(ent, limit=None if full or not raw else RECENT):
                cnt[self._merge(raw, m)] += 1
            self.save_raw(raw)
            return dict(cnt, total=len(raw))
        finally:
            await client.disconnect()

    def poll(self, full: bool = False) -> dict:
        try:
            r = asyncio.run(self._fetch(full))
        except Exception as e:
            self.write_status(ok=False, error=f"{type(e).__name__}: {e}")
            raise
        self.write_status(ok=True, error=None, **r)
        return r

    async def _login(self, ask_output: bool) -> None:
        e = self.env()
        if not e.get("TG_API_ID") or not e.get("TG_API_HASH"):
            print("需要 api_id / api_hash：瀏覽器開 https://my.telegram.org → 用手機號碼登入 → API development tools → 建一個 App（名稱隨便）")
            self.set("TG_API_ID", input("api_id：").strip())
            self.set("TG_API_HASH", input("api_hash：").strip())
        client = self._client()
        await client.start(phone=lambda: input("手機號碼（國碼開頭，例如 +8869xxxxxxxx）："),
                           code_callback=lambda: input("Telegram 傳給你的驗證碼（在 Telegram App 裡）："),
                           password=lambda: input("兩步驟驗證密碼（沒設就直接 Enter）："))
        me = await client.get_me()
        print(f"\n已登入：{me.first_name or ''}（之後不用再登入）\n")
        ds = [d async for d in client.iter_dialogs() if d.is_channel or d.is_group]
        for i, d in enumerate(ds):
            print(f"{i:3d}. {d.name}")
        while True:
            s = input("\n要收集哪一個？輸入編號（也可以打幾個字篩選）：").strip()
            if s.isdigit() and int(s) < len(ds):
                d = ds[int(s)]
                break
            for i, x in enumerate(ds):
                if s and s.lower() in x.name.lower():
                    print(f"{i:3d}. {x.name}")
        self.set("TG_CHAT", str(d.id))
        self.set("TG_CHAT_NAME", d.name)
        await client.disconnect()
        if ask_output:
            cur = self.env().get("OUTPUT_DIR") or str(HERE / "output")
            print("\n抓到的訊息要放哪個資料夾？要放「CRT 主機也看得到」的地方，例如：")
            print("  雲端同步：C:\\Users\\你的帳號\\OneDrive\\CRT-Yuki")
            print("  區網共用：\\\\主機名稱\\CRT-Yuki-Inbox")
            out = input(f"資料夾（直接 Enter = {cur}）：").strip().strip('"') or cur
            Path(out).mkdir(parents=True, exist_ok=True)
            self.set("OUTPUT_DIR", out)
            self.raw, self.status = Path(out) / "raw.json", Path(out) / "status.json"
        print(f"\n收集：{d.name}，開始抓全部歷史訊息…")

    def login(self, ask_output: bool = False) -> None:
        asyncio.run(self._login(ask_output))

    def loop(self, minutes: float) -> None:
        print(f"每 {minutes:g} 分鐘抓一次，輸出到 {self.raw.parent}（這個視窗關掉就停止）")
        while True:
            try:
                r = self.poll()
                print(f"{time.strftime('%m/%d %H:%M:%S')}  新 {r['new']}、編輯 {r['edited']}，共 {r['total']} 則")
            except Exception as e:
                print(f"{time.strftime('%m/%d %H:%M:%S')}  失敗：{e}（下次再試）")
            time.sleep(max(1.0, minutes) * 60)


def autostart(on: bool) -> Path:
    """開機（登入 Windows）自動開始收集：在「啟動」資料夾放一個 YukiCollector.cmd（縮小視窗跑 --loop）。"""
    d = Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
    f = d / "YukiCollector.cmd"
    if on:
        py = HERE / "venv" / "Scripts" / "python.exe"
        f.write_text(f'@echo off\r\nchcp 65001 >nul\r\ncd /d "{HERE}"\r\n'
                     f'start "Yuki 訊號收集（關掉就停止）" /min "{py}" collector.py --loop\r\n', encoding="utf-8")
    elif f.exists():
        f.unlink()
    return f


def bundle() -> Collector:
    """搬到別台時的路徑：設定在這個資料夾的 .env，登入檔在 data\\，輸出在 OUTPUT_DIR（預設 output\\）。"""
    env = read_env(HERE / ".env")
    out = Path(env.get("OUTPUT_DIR") or HERE / "output")
    return Collector(HERE / ".env", HERE / "data" / "tg", out / "raw.json", out / "status.json")


def main() -> None:
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--login", action="store_true")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--loop", action="store_true")
    ap.add_argument("--autostart", choices=["on", "off"])
    a = ap.parse_args()
    if a.autostart:
        f = autostart(a.autostart == "on")
        print(f"開機自動收集：{'已開啟' if a.autostart == 'on' else '已取消'}（{f}）")
        return
    c = bundle()
    if a.login:
        c.login(ask_output=True)
        r = c.poll(full=True)
        print(f"\n抓好了：{r['total']} 則 → {c.raw}")
        return
    if not c.configured():
        print("還沒登入：先執行「2_登入Telegram.bat」")
        sys.exit(1)
    if a.once:
        r = c.poll()
        print(f"新 {r['new']}、編輯 {r['edited']}，共 {r['total']} 則 → {c.raw}")
    elif a.loop:
        c.loop(float(c.env().get("INTERVAL_MIN") or 5))
    else:
        ap.print_help()


if __name__ == "__main__":
    os.chdir(HERE)
    main()
