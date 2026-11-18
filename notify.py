"""手機通知：Discord webhook（預設）或 Telegram bot。"""
from __future__ import annotations

import os
import time

import requests

COLORS = {"entry_short": 0xFF5577, "entry_long": 0x17A88F, "limit": 0x4C8DFF, "exit": 0xE8A21B, "confirm": 0xA77BFF}


def _color(kind: str, d: int) -> int:
    if kind == "entry":
        return COLORS["entry_short" if d < 0 else "entry_long"]
    return COLORS.get(kind, 0x888888)


def edit(msg_id: str, title: str, desc: str, color: int) -> None:
    """改掉之前送出的那則（例如在 TP 後面打勾）。"""
    hook = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    if hook and msg_id:
        requests.patch(f"{hook}/messages/{msg_id}", timeout=10,
                       json={"embeds": [{"title": title[:256], "description": desc[:4000], "color": color}]})


def send_embed(emb: dict, hook_env: str = "DISCORD_WEBHOOK_URL") -> bool:
    """送一整個 embed（日報用）。hook_env 沒設定就退回 DISCORD_WEBHOOK_URL；都沒有就印在畫面上。"""
    hook = (os.environ.get(hook_env, "") or os.environ.get("DISCORD_WEBHOOK_URL", "")).strip()
    if not hook:
        print(f"[未設定通知] {emb.get('title')}\n{emb.get('description')}")
        return False
    for _ in range(3):
        r = requests.post(hook, json={"embeds": [emb]}, timeout=10)
        if r.status_code == 429:
            time.sleep(float(r.headers.get("Retry-After", "1")) + 0.2)
            continue
        r.raise_for_status()
        return True
    return False


def send(kind: str, direction: int, text: str, when: str) -> dict | None:
    """送出；Discord 會回傳這則訊息的 id（之後拿來改）。"""
    hook = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    tg_token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    tg_chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    title, _, body = text.partition("\n")
    desc = (body + f"\n\n⏰ {when}\nCRT Toolkit")[:4000]
    info = None
    if hook:
        payload = {"embeds": [{"title": title[:256], "description": desc, "color": _color(kind, direction)}]}
        for attempt in range(3):
            r = requests.post(hook + ("&" if "?" in hook else "?") + "wait=true", json=payload, timeout=10)
            if r.status_code == 429:
                time.sleep(float(r.headers.get("Retry-After", "1")) + 0.2)
                continue
            r.raise_for_status()
            info = {"id": r.json().get("id"), "title": title, "desc": desc, "color": _color(kind, direction)}
            break
        time.sleep(0.4)
    if tg_token and tg_chat:
        requests.post(f"https://api.telegram.org/bot{tg_token}/sendMessage",
                      json={"chat_id": tg_chat, "text": f"{text}\n\n🕒 {when}"}, timeout=10).raise_for_status()
    if not hook and not (tg_token and tg_chat):
        print(f"[未設定通知] {title}\n{body}\n🕒 {when}\n")
    return info
