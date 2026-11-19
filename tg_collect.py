"""Yuki 的 Telegram 訊號：這台主機的收集設定（實際收集程式在 tg_collector/collector.py，兩邊共用）。

兩種用法：
  A. 另一台電腦收集（建議）：把 tg_collector\\ 整個資料夾複製過去，照裡面的「操作說明.txt」做；
     這台在 .env 設 YUKI_INBOX = 那台寫進來的資料夾（雲端同步或共用資料夾），儀表板會自動讀、自動對照。
  B. 這台自己收集：雙擊 tg_login.bat（python tg_collect.py --login），儀表板每 5 分鐘自動抓。

  python tg_collect.py --login | --backfill | --poll

資料在 $CRT_DATA_DIR/yuki/（預設 ~/crt-dash-data/yuki/），只在本機，不上 GitHub（別人的內容）：
  raw.json（訊息）、tg.session（這台自己收集時的 Telegram 登入，等於密碼）、yuki_vs_ours.md（對照）
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "tg_collector"))
from collector import Collector, read_env  # noqa: E402

for _k, _v in read_env(ROOT / ".env").items():
    os.environ.setdefault(_k, _v)
DIR = Path(os.environ.get("CRT_DATA_DIR") or Path.home() / "crt-dash-data") / "yuki"
RAW = DIR / "raw.json"
SESSION = DIR / "tg"
LOCAL = Collector(ROOT / ".env", SESSION, RAW, DIR / "status.json")


def inbox() -> Path | None:
    """另一台主機寫進來的資料夾（.env 的 YUKI_INBOX）；沒設 = 這台自己收集。"""
    p = (read_env(ROOT / ".env").get("YUKI_INBOX") or os.environ.get("YUKI_INBOX") or "").strip()
    return Path(p) if p else None


def configured() -> bool:
    return LOCAL.configured()


def load_raw() -> dict:
    return LOCAL.load_raw()


def save_raw(raw: dict) -> None:
    LOCAL.save_raw(raw)


def poll(full: bool = False) -> dict:
    return LOCAL.poll(full)


def pull_inbox() -> dict | None:
    """把另一台寫的 raw.json 抄過來（檔案有變才抄；同步到一半、壞掉的不抄）。回傳 None = 沒變。"""
    src = inbox()
    if not src:
        raise RuntimeError("沒設定 YUKI_INBOX")
    f = src / "raw.json"
    if not f.exists():
        raise RuntimeError(f"{f} 還沒有檔案（另一台收集了嗎？資料夾有同步過來嗎？）")
    stamp = DIR / "inbox_stamp.json"
    st = f.stat()
    key = {"mtime": st.st_mtime, "size": st.st_size}
    try:
        if json.loads(stamp.read_text(encoding="utf-8")) == key and RAW.exists():
            return None
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    data = json.loads(f.read_text(encoding="utf-8"))     # 解析得了才換掉本機的
    DIR.mkdir(parents=True, exist_ok=True)
    old = load_raw()
    save_raw(data)
    stamp.write_text(json.dumps(key), encoding="utf-8")
    new = sum(1 for k in data if k not in old)
    edited = sum(1 for k, v in data.items() if k in old and old[k].get("text") != v.get("text"))
    return {"new": new, "edited": edited, "total": len(data)}


def inbox_status() -> dict:
    """另一台最後一次抓取的狀態（status.json）。"""
    src = inbox()
    try:
        return json.loads((src / "status.json").read_text(encoding="utf-8")) if src else {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


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
        LOCAL.login(ask_output=False)
    if a.login or a.backfill or a.poll:
        r = poll(full=a.login or a.backfill)
        print(f"新 {r['new']} 則、編輯 {r['edited']} 則，共 {r['total']} 則 → {RAW}")
        import yuki_report
        print(f"整理好了：{yuki_report.build(compare_ours=True)}")
    else:
        ap.print_help()


if __name__ == "__main__":
    os.chdir(ROOT)
    main()
