#!/usr/bin/env python3
"""
OutageWatch -- set up phone alerts
==================================

Walks you through connecting a Telegram bot so OutageWatch messages your phone
when a service breaks or recovers.

    python setup_alerts.py

You need two things, and this script finds the second one for you:
  1. a bot token  -- from @BotFather on Telegram
  2. a chat id    -- which is just "where to send the message"

Alerts only fire on a CHANGE. You will not be messaged every two minutes
telling you GitHub is still fine.
"""

from __future__ import annotations

import json
import os
import sys

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from collector import CONFIG_PATH, LABELS, TIMEOUT  # noqa: E402

API = "https://api.telegram.org/bot{token}/{method}"


def say(*parts):
    print(*parts, flush=True)


def call(token, method, **params):
    try:
        r = requests.get(API.format(token=token, method=method),
                         params=params, timeout=TIMEOUT)
        return r.json()
    except requests.exceptions.RequestException as exc:
        return {"ok": False, "description": f"network error: {exc}"}
    except ValueError:
        return {"ok": False, "description": "Telegram sent back something unreadable"}


def main() -> int:
    say("=" * 64)
    say("OutageWatch -- phone alert setup")
    say("=" * 64)
    say()
    say("STEP 1 of 3 -- create the bot")
    say()
    say("  On your phone, open Telegram and search for:   @BotFather")
    say("  Send it:                                       /newbot")
    say("  It asks for a name       -> anything, e.g. My OutageWatch")
    say("  It asks for a username   -> must end in 'bot', e.g. tejes_outage_bot")
    say()
    say("  It then replies with a long token that looks like")
    say("      8123456789:AAHk3l-xxxxxxxxxxxxxxxxxxxxxxxxxxx")
    say()
    say("  Keep that token private -- anyone who has it can post as your bot.")
    say("  Don't paste it into a chat or a screenshot.")
    say()

    token = input("  Paste the token here and press Enter: ").strip()
    if not token or ":" not in token:
        say("\n  That doesn't look like a token. Run the script again when you have it.")
        return 1

    say("\n  Checking the token...")
    me = call(token, "getMe")
    if not me.get("ok"):
        say(f"  Telegram rejected it: {me.get('description')}")
        say("  Check you copied the whole thing, then run this again.")
        return 1
    bot_name = me["result"].get("username", "your bot")
    say(f"  Works. The bot is @{bot_name}")

    say()
    say("STEP 2 of 3 -- say hello to your bot")
    say()
    say(f"  In Telegram, open a chat with @{bot_name} and send it any message.")
    say("  ('hi' is fine.) This is what tells it where to reach you --")
    say("  a bot is not allowed to message you first.")
    say()
    input("  Press Enter once you've sent that message... ")

    chat_id = None
    for attempt in range(3):
        updates = call(token, "getUpdates")
        if not updates.get("ok"):
            say(f"  Could not read messages: {updates.get('description')}")
            return 1
        for item in reversed(updates.get("result", [])):
            msg = item.get("message") or item.get("my_chat_member") or {}
            chat = msg.get("chat") or {}
            if chat.get("id"):
                chat_id = chat["id"]
                who = chat.get("first_name") or chat.get("title") or "you"
                say(f"  Found it -- messages will go to {who} (chat id {chat_id})")
                break
        if chat_id:
            break
        if attempt < 2:
            input("  No message seen yet. Send one to the bot, then press Enter... ")

    if not chat_id:
        say()
        say("  Still nothing. The usual cause is messaging the wrong bot --")
        say(f"  it must be @{bot_name} exactly. Run this script again to retry.")
        return 1

    say()
    say("STEP 3 of 3 -- send a test alert")
    sample = (
        "OutageWatch\n"
        f"[DOWN] Example Service: {LABELS['operational']} -> {LABELS['major_outage']} "
        "-- this is a test, nothing is actually broken"
    )
    sent = call(token, "sendMessage", chat_id=chat_id, text=sample)
    if not sent.get("ok"):
        say(f"  Test message failed: {sent.get('description')}")
        return 1
    say("  Sent. Check your phone -- it should look exactly like a real alert.")

    config = {}
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, encoding="utf-8") as fh:
                config = json.load(fh)
        except ValueError:
            config = {}
    config["telegram_bot_token"] = token
    config["telegram_chat_id"] = str(chat_id)
    config.setdefault("alert_on_unknown", False)

    with open(CONFIG_PATH, "w", encoding="utf-8") as fh:
        json.dump(config, fh, indent=2)
        fh.write("\n")

    say()
    say("=" * 64)
    say(f"Saved to {os.path.basename(CONFIG_PATH)}")
    say()
    say("  Restart the collector window for it to take effect.")
    say()
    say("  From then on you get a message whenever a service changes state,")
    say("  and nothing at all while everything stays the same.")
    say()
    say("  Two notes:")
    say("   - config.json now holds your token. Don't share that file,")
    say("     and don't put it on GitHub if you ever publish this.")
    say('   - to also be told when a status feed itself stops responding,')
    say('     set "alert_on_unknown" to true in config.json. Noisier.')
    say("=" * 64)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        say("\nCancelled. Nothing was saved.")
        raise SystemExit(1)
