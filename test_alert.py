#!/usr/bin/env python3
"""
OutageWatch -- send a test alert to every destination
=====================================================

    python test_alert.py

Reads the token from config.json and the channel from channel.json, then sends
one test message to each destination and reports exactly what Telegram said.

Use it after changing where alerts go -- especially to confirm the bot is
actually an administrator of the channel. Without that, Telegram silently
refuses every post and you would only find out when a real outage didn't
reach anybody.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from collector import TIMEOUT, alert_targets, load_config  # noqa: E402


def main() -> int:
    config = load_config()
    token = config.get("telegram_bot_token")

    if not token:
        print("No bot token found.")
        print("On this PC it comes from config.json -- run: python setup_alerts.py")
        return 1

    base = f"https://api.telegram.org/bot{token}"

    print("Checking the bot...")
    try:
        me = requests.get(f"{base}/getMe", timeout=TIMEOUT).json()
    except (requests.exceptions.RequestException, ValueError) as exc:
        print(f"  Could not reach Telegram: {exc}")
        return 1
    if not me.get("ok"):
        print(f"  Telegram rejected the token: {me.get('description')}")
        return 1
    print(f"  Bot: @{me['result'].get('username')}")

    targets = alert_targets(config)
    if not targets:
        print("\nNo destinations configured.")
        print("Expected a chat id in config.json and/or a channel in channel.json.")
        return 1

    print(f"\nSending to {len(targets)} destination(s):")
    stamp = datetime.now().strftime("%H:%M:%S")
    text = ("OutageWatch test\n"
            f"[DOWN] Example Service: Operational -> Service Outage "
            f"-- test sent at {stamp}, nothing is actually broken")

    failures = 0
    for target in targets:
        kind = "channel" if str(target).startswith("@") else "direct chat"
        print(f"\n  {target}  ({kind})")
        try:
            body = requests.post(f"{base}/sendMessage",
                                 json={"chat_id": target, "text": text},
                                 timeout=TIMEOUT).json()
        except (requests.exceptions.RequestException, ValueError) as exc:
            print(f"    FAILED: {exc}")
            failures += 1
            continue

        if body.get("ok"):
            chat = body.get("result", {}).get("chat", {})
            name = chat.get("title") or chat.get("first_name") or "?"
            print(f"    SENT -- delivered to \"{name}\"")
        else:
            failures += 1
            reason = str(body.get("description", "unknown"))
            print(f"    REFUSED: {reason}")
            low = reason.lower()
            if "chat not found" in low:
                print("    -> The channel name is wrong, or the bot was never added to it.")
            elif "not enough rights" in low or "administrator" in low:
                print("    -> The bot is in the channel but is not an admin with")
                print("       'Post Messages' enabled. Fix that in the channel settings.")
            elif "blocked" in low:
                print("    -> You blocked the bot. Unblock it in Telegram.")

    print()
    if failures:
        print(f"{failures} of {len(targets)} destination(s) failed -- see above.")
        return 1
    print("All destinations working.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
