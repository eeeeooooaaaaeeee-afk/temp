#!/usr/bin/env python3
"""Simple Telegram bot: premium emoji <-> ID converter.

Usage:
  1) export BOT_TOKEN="123456:ABC..."
  2) python3 premium_emoji_id_bot.py

Behavior:
  - If user sends a message with custom emoji entities, bot replies with their IDs.
  - If user sends one or more numeric IDs, bot sends those custom emojis back
    (via HTML <tg-emoji emoji-id="...">).
"""

from __future__ import annotations

import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Dict, Iterable, List, Optional

API_ROOT = "https://api.telegram.org/bot{token}/{method}"
ID_RE = re.compile(r"\b\d{5,32}\b")


def bot_api(token: str, method: str, payload: Optional[Dict] = None) -> Dict:
    data = None
    if payload is not None:
        encoded = urllib.parse.urlencode(payload).encode("utf-8")
        data = encoded

    req = urllib.request.Request(
        API_ROOT.format(token=token, method=method),
        data=data,
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        body = resp.read().decode("utf-8")
    parsed = json.loads(body)
    if not parsed.get("ok"):
        raise RuntimeError(f"Telegram API error in {method}: {parsed}")
    return parsed["result"]


def extract_custom_emoji_ids(message: Dict) -> List[str]:
    ids: List[str] = []
    for key in ("entities", "caption_entities"):
        for ent in message.get(key, []) or []:
            if ent.get("type") == "custom_emoji" and ent.get("custom_emoji_id"):
                ids.append(str(ent["custom_emoji_id"]))
    return ids


def extract_numeric_ids(text: str) -> List[str]:
    return ID_RE.findall(text)


def render_ids_reply(ids: Iterable[str]) -> str:
    rows = ["<b>Найдены custom emoji ID:</b>"]
    for idx, emoji_id in enumerate(ids, start=1):
        safe = html.escape(emoji_id)
        rows.append(f"{idx}. <code>{safe}</code>")
        rows.append(f"   Bot API: <code>&lt;tg-emoji emoji-id=\"{safe}\"&gt;🙂&lt;/tg-emoji&gt;</code>")
        rows.append(f"   Hikka: <code>&lt;emoji document_id={safe}&gt;&lt;/emoji&gt;</code>")
    return "\n".join(rows)


def render_preview_by_ids(ids: Iterable[str]) -> str:
    rows = ["<b>Превью по ID:</b>"]
    for emoji_id in ids:
        safe = html.escape(emoji_id)
        rows.append(f"• ID <code>{safe}</code>: <tg-emoji emoji-id=\"{safe}\">🙂</tg-emoji>")
    return "\n".join(rows)


def help_text() -> str:
    return (
        "<b>Конвертер premium emoji ↔ ID</b>\n\n"
        "Отправь мне:\n"
        "1) сообщение с premium/custom emoji — я верну их ID;\n"
        "2) один или несколько numeric ID — я отправлю предпросмотр emoji по этим ID."
    )


def main() -> int:
    token = os.getenv("BOT_TOKEN", "").strip()
    if not token:
        print("ERROR: set BOT_TOKEN environment variable", file=sys.stderr)
        return 1

    offset = 0
    print("Bot started. Press Ctrl+C to stop.")

    while True:
        try:
            updates = bot_api(
                token,
                "getUpdates",
                {"timeout": 50, "offset": offset, "allowed_updates": '["message"]'},
            )
            for upd in updates:
                offset = upd["update_id"] + 1
                msg = upd.get("message") or {}
                chat = msg.get("chat") or {}
                chat_id = chat.get("id")
                if not chat_id:
                    continue

                text = (msg.get("text") or "").strip()
                emoji_ids = extract_custom_emoji_ids(msg)

                if emoji_ids:
                    bot_api(
                        token,
                        "sendMessage",
                        {
                            "chat_id": chat_id,
                            "text": render_ids_reply(emoji_ids),
                            "parse_mode": "HTML",
                            "reply_to_message_id": msg.get("message_id"),
                        },
                    )
                    continue

                numeric_ids = extract_numeric_ids(text)
                if numeric_ids:
                    bot_api(
                        token,
                        "sendMessage",
                        {
                            "chat_id": chat_id,
                            "text": render_preview_by_ids(numeric_ids),
                            "parse_mode": "HTML",
                            "reply_to_message_id": msg.get("message_id"),
                        },
                    )
                    continue

                if text in {"/start", "/help"}:
                    bot_api(
                        token,
                        "sendMessage",
                        {
                            "chat_id": chat_id,
                            "text": help_text(),
                            "parse_mode": "HTML",
                            "reply_to_message_id": msg.get("message_id"),
                        },
                    )

        except KeyboardInterrupt:
            print("Stopped.")
            return 0
        except (urllib.error.URLError, TimeoutError) as exc:
            print(f"Network warning: {exc}", file=sys.stderr)
            time.sleep(2)
        except Exception as exc:
            print(f"Unhandled error: {exc}", file=sys.stderr)
            time.sleep(2)


if __name__ == "__main__":
    raise SystemExit(main())
