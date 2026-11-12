#!/usr/bin/env python3
"""
OutageWatch -- send a test alert and say exactly where it went
==============================================================

    python test_alert.py

Reads the token already saved in config.json, confirms which bot it belongs
to, sends a message, and tells you the exact chat to look in.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from collector import CONFIG_PATH, TIMEOUT  # noqa: E402


def main() -> int:
    if not os.path.exists(CONFIG_PATH):
        print("No config.json found. Run:  python setup_alerts.py")
        return 1

    with open(CONFIG_PATH, encoding="utf-8") as fh:
        config = json.load(fh)

    token = config.get("telegram_bot_token")
    chat_id = config.get("telegram_chat_id")
    if not token or not chat_id:
        print("config.json is missing the token or chat id. Run setup_alerts.py again.")
        return 1

    base = f"https://api.telegram.org/bot{token}"

    print("Checking the bot...")
    try:
        me = requests.get(f"{base}/getMe", timeout=TIMEOUT).json()
    except requests.exceptions.RequestException as exc:
        print(f"  Could not reach Telegram: {exc}")
        return 1

    if not me.get("ok"):
        print(f"  Telegram rejected the token: {me.get('description')}")
        print("  The token may have been revoked. Run setup_alerts.py again.")
        return 1

    info = me["result"]
    username = info.get("username", "?")
    print(f"  Bot name    : {info.get('first_name')}")
    print(f"  Bot username: @{username}")
    print(f"  Sending to  : chat id {chat_id}")

    stamp = datetime.now().strftime("%H:%M:%S")
    text = (
        "OutageWatch test\n"
        f"[DOWN] Example Service: Operational -> Service Outage -- test sent at {stamp}"
    )
    try:
        sent = requests.post(f"{base}/sendMessage",
                             json={"chat_id": chat_id, "text": text},
                             timeout=TIMEOUT).json()
    except requests.exceptions.RequestException as exc:
        print(f"  Send failed: {exc}")
        return 1

    if not sent.get("ok"):
        print(f"  Telegram refused to send: {sent.get('description')}")
        if "blocked" in str(sent.get("description", "")).lower():
            print("  You have blocked the bot. Unblock it in Telegram and try again.")
        return 1

    result = sent.get("result", {})
    chat = result.get("chat", {})
    print()
    print("  SENT successfully.")
    print(f"  Delivered to : {chat.get('first_name') or chat.get('title') or '?'}")
    print(f"  Message id   : {result.get('message_id')}")
    print()
    print("  " + "-" * 56)
    print(f"  LOOK IN TELEGRAM FOR THE CHAT WITH:   @{username}")
    print("  " + "-" * 56)
    print()
    print("  Not the chat with @BotFather -- that is where you CREATED the")
    print("  bot. Alerts arrive in the chat with the bot itself.")
    print()
    print(f"  If you cannot find it, search Telegram for: {username}")
    print("  Make sure you are on the same Telegram account you used earlier.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
