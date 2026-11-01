"""Minimal Telegram Bot API client using long polling."""
from __future__ import annotations

import json
import urllib.error
import urllib.request

MESSAGE_LIMIT = 4000  # Telegram allows 4096 characters per message.


class TelegramError(Exception):
    """Raised when the Bot API rejects a request or cannot be reached."""


def chunk_text(text: str, limit: int = MESSAGE_LIMIT):
    """Split text into messages of at most `limit` characters, preferring line breaks."""
    text = text or ""
    parts = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        cut = cut if cut > limit // 2 else limit
        parts.append(text[:cut])
        text = text[cut:].lstrip("\n")
    parts.append(text)
    return [p for p in parts if p.strip()] or [""]


class Telegram:
    def __init__(self, token: str, base_url: str = "https://api.telegram.org"):
        self._url = f"{base_url}/bot{token}/"

    def _call(self, method: str, payload: dict, timeout: float = 30):
        request = urllib.request.Request(
            self._url + method, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                body = json.load(response)
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = json.load(e).get("description", "")
            except Exception:
                pass
            raise TelegramError(f"{method}: HTTP {e.code} {detail}".strip()) from None
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
            raise TelegramError(f"{method}: {e.__class__.__name__}") from None
        if not body.get("ok"):
            raise TelegramError(f"{method}: {body.get('description', 'request failed')}")
        return body["result"]

    def get_updates(self, offset: int, poll_seconds: int = 30):
        return self._call("getUpdates", {"offset": offset, "timeout": poll_seconds,
                                         "allowed_updates": ["message"]}, timeout=poll_seconds + 10)

    def send_message(self, chat_id: int, text: str, reply_markup=None) -> int:
        """Send one message (at most MESSAGE_LIMIT characters) and return its id."""
        payload = {"chat_id": chat_id, "text": text, "disable_web_page_preview": True}
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        return self._call("sendMessage", payload)["message_id"]

    def send_long(self, chat_id: int, text: str, reply_markup=None):
        parts = chunk_text(text)
        for index, part in enumerate(parts):
            self.send_message(chat_id, part, reply_markup if index == len(parts) - 1 else None)

    def edit_message(self, chat_id: int, message_id: int, text: str):
        self._call("editMessageText", {"chat_id": chat_id, "message_id": message_id,
                                       "text": text[:MESSAGE_LIMIT], "disable_web_page_preview": True})
