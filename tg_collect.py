"""收集 Telegram 頻道 / 群組的訊息（Yuki 的 CRT 訊號）——用你自己的 Telegram 帳號「唯讀」讀取，不會發任何訊息。

  python tg_collect.py --login      第一次：輸入 api_id / api_hash（my.telegram.org 申請）、手機、驗證碼，選要收集哪個頻道，
                                    然後抓全部歷史訊息、整理成檔案
  python tg_collect.py --backfill   重抓全部歷史（整理規則改了想重跑也可以用 yuki_report.py）
  python tg_collect.py --poll       抓一次新訊息（dash.py 每 2 分鐘會自動做）

資料放 $CRT_DATA_DIR/yuki/（預設 ~/crt-dash-data/yuki/），只在本機，不上 GitHub（別人的內容）：
  raw.json     每則原始訊息：id、發文時間、文字、最後編輯時間、回覆哪一則、歷次版本（被編輯過會留舊文字）
  tg.session   你 Telegram 帳號的登入狀態 ⚠ 等於密碼，不要給別人、不要上傳
設定在 .env：TG_API_ID、TG_API_HASH、TG_CHAT（頻道 id）、TG_CHAT_NAME
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def _load_env() -> None:
    env = ROOT / ".env"
    if env.exists():
        for ln in env.read_text(encoding="utf-8").splitlines():
            ln = ln.strip()
            if ln and not ln.startswith("#") and "=" in ln:
                k, v = ln.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_env()
DIR = Path(os.environ.get("CRT_DATA_DIR") or Path.home() / "crt-dash-data") / "yuki"
RAW = DIR / "raw.json"
SESSION = DIR / "tg"           # Telethon 會加 .session
RECENT = 300                    # 每次輪詢重抓最近幾則（抓編輯，例如事後補上「TP2 ✅」）


def set_env(key: str, val: str) -> None:
    env = ROOT / ".env"
    lines = env.read_text(encoding="utf-8").splitlines() if env.exists() else []
    lines = [ln for ln in lines if not ln.strip().startswith(f"{key}=")] + [f"{key}={val}"]
    env.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.environ[key] = val


def configured() -> bool:
    return bool(os.environ.get("TG_API_ID") and os.environ.get("TG_API_HASH") and os.environ.get("TG_CHAT"))


def load_raw() -> dict[str, dict]:
    try:
        return json.loads(RAW.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_raw(raw: dict) -> None:
    DIR.mkdir(parents=True, exist_ok=True)
    tmp = RAW.with_suffix(".tmp")
    tmp.write_text(json.dumps(raw, ensure_ascii=False, indent=0), encoding="utf-8")
    os.replace(tmp, RAW)


def _client():
    from telethon import TelegramClient
    DIR.mkdir(parents=True, exist_ok=True)
    return TelegramClient(str(SESSION), int(os.environ["TG_API_ID"]), os.environ["TG_API_HASH"],
                          device_model="CRT collector", app_version="1.0")


def _rec(m) -> dict:
    return {"id": m.id, "date": int(m.date.timestamp() * 1000),
            "edit": int(m.edit_date.timestamp() * 1000) if m.edit_date else None,
            "text": m.message or "", "reply_to": getattr(m.reply_to, "reply_to_msg_id", None) if m.reply_to else None,
            "media": type(m.media).__name__ if m.media else None, "grouped": m.grouped_id}


def merge(raw: dict, m) -> str:
    """存進 raw；回傳 new / edited / same。被編輯過的舊文字放進 versions。"""
    r = _rec(m)
    old = raw.get(str(m.id))
    if old is None:
        raw[str(m.id)] = dict(r, versions=[])
        return "new"
    if old.get("text") != r["text"] or old.get("edit") != r["edit"]:
        vers = old.get("versions", [])
        if old.get("text") != r["text"]:
            vers.append({"text": old.get("text", ""), "until": r["edit"] or int(time.time() * 1000)})
        raw[str(m.id)] = dict(r, versions=vers)
        return "edited"
    return "same"


async def _entity(client):
    chat = os.environ["TG_CHAT"]
    try:
        return await client.get_entity(int(chat) if chat.lstrip("-").isdigit() else chat)
    except Exception:
        async for d in client.iter_dialogs():          # 第一次用新的 session：先把頻道載進來
            if str(d.id) == chat or d.name == os.environ.get("TG_CHAT_NAME"):
                return d.entity
        raise RuntimeError(f"找不到頻道 {chat}（帳號還在這個頻道 / 群組裡嗎？）")


async def fetch(full: bool = False) -> dict:
    """抓訊息：full = 全部歷史；否則只抓最近 RECENT 則（新訊息 + 最近的編輯）。"""
    client = _client()
    await client.connect()
    try:
        if not await client.is_user_authorized():
            raise RuntimeError("Telegram 還沒登入：執行 python tg_collect.py --login")
        ent = await _entity(client)
        raw = load_raw()
        cnt = {"new": 0, "edited": 0, "same": 0}
        async for m in client.iter_messages(ent, limit=None if full or not raw else RECENT):
            cnt[merge(raw, m)] += 1
        save_raw(raw)
        return dict(cnt, total=len(raw))
    finally:
        await client.disconnect()


def poll(full: bool = False) -> dict:
    return asyncio.run(fetch(full))


async def _login() -> None:
    if not os.environ.get("TG_API_ID") or not os.environ.get("TG_API_HASH"):
        print("先到 https://my.telegram.org → 登入 → API development tools → 建一個 App（名稱隨便），把 api_id / api_hash 貼過來")
        set_env("TG_API_ID", input("api_id：").strip())
        set_env("TG_API_HASH", input("api_hash：").strip())
    client = _client()
    await client.start(phone=lambda: input("手機號碼（國碼開頭，例如 +8869xxxxxxxx）："),
                       code_callback=lambda: input("Telegram 傳給你的驗證碼："),
                       password=lambda: input("兩步驟驗證密碼（沒設就直接 Enter）："))
    me = await client.get_me()
    print(f"\n已登入：{me.first_name or ''}（之後不用再登入）\n")
    ds = [d async for d in client.iter_dialogs() if d.is_channel or d.is_group]
    for i, d in enumerate(ds):
        print(f"{i:3d}. {d.name}")
    while True:
        s = input("\n要收集哪一個？輸入編號（可以先打幾個字篩選）：").strip()
        if s.isdigit() and int(s) < len(ds):
            d = ds[int(s)]
            break
        hit = [(i, d) for i, d in enumerate(ds) if s and s.lower() in d.name.lower()]
        for i, d in hit:
            print(f"{i:3d}. {d.name}")
    set_env("TG_CHAT", str(d.id))
    set_env("TG_CHAT_NAME", d.name)
    await client.disconnect()
    print(f"\n收集：{d.name}，開始抓全部歷史訊息…")


def main() -> None:
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--login", action="store_true")
    ap.add_argument("--backfill", action="store_true")
    ap.add_argument("--poll", action="store_true")
    a = ap.parse_args()
    if a.login:
        asyncio.run(_login())
    if a.login or a.backfill or a.poll:
        r = poll(full=a.login or a.backfill)
        print(f"新 {r['new']} 則、編輯 {r['edited']} 則，共 {r['total']} 則 → {RAW}")
        import yuki_report
        out = yuki_report.build(compare=True)
        print(f"整理好了：{out}")
    else:
        ap.print_help()


if __name__ == "__main__":
    os.chdir(ROOT)
    main()
