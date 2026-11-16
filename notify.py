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


def send(kind: str, direction: int, text: str, when: str) -> None:
    hook = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    tg_token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    tg_chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    title, _, body = text.partition("\n")
    if hook:
        payload = {"embeds": [{"title": title[:256], "description": (body + f"\n\n🕒 {when}")[:4000],
                               "color": _color(kind, direction)}]}
        for attempt in range(3):
            r = requests.post(hook, json=payload, timeout=10)
            if r.status_code == 429:
                time.sleep(float(r.headers.get("Retry-After", "1")) + 0.2)
                continue
            r.raise_for_status()
            break
        time.sleep(0.4)
    if tg_token and tg_chat:
        requests.post(f"https://api.telegram.org/bot{tg_token}/sendMessage",
                      json={"chat_id": tg_chat, "text": f"{text}\n\n🕒 {when}"}, timeout=10).raise_for_status()
    if not hook and not (tg_token and tg_chat):
        print(f"[未設定通知] {title}\n{body}\n🕒 {when}\n")
