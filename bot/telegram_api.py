"""Minimal Telegram Bot API client using long polling."""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path

MESSAGE_LIMIT = 4000  # Telegram allows 4096 characters per message.
DOWNLOAD_LIMIT = 20 * 1024 * 1024  # Largest file the Bot API lets a bot download.


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
        self._file_url = f"{base_url}/file/bot{token}/"

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

    def send_message(self, chat_id: int, text: str, reply_markup=None, parse_mode=None) -> int:
        """Send one message (at most MESSAGE_LIMIT characters) and return its id."""
        payload = {"chat_id": chat_id, "text": text, "disable_web_page_preview": True}
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        if parse_mode:
            payload["parse_mode"] = parse_mode
        return self._call("sendMessage", payload)["message_id"]

    def send_long(self, chat_id: int, text: str, reply_markup=None):
        parts = chunk_text(text)
        for index, part in enumerate(parts):
            self.send_message(chat_id, part, reply_markup if index == len(parts) - 1 else None)

    def edit_message(self, chat_id: int, message_id: int, text: str, parse_mode=None):
        """Replace a message's text; formatted text is sent whole because cutting it could break its markup."""
        payload = {"chat_id": chat_id, "message_id": message_id,
                   "text": text if parse_mode else text[:MESSAGE_LIMIT], "disable_web_page_preview": True}
        if parse_mode:
            payload["parse_mode"] = parse_mode
        self._call("editMessageText", payload)

    def download_file(self, file_id: str, dest: Path, limit: int = DOWNLOAD_LIMIT) -> Path:
        """Save a file sent to the bot; errors never include the download URL, which holds the token."""
        info = self._call("getFile", {"file_id": file_id})
        path = info.get("file_path") if isinstance(info, dict) else None
        if not path:
            raise TelegramError("getFile: no file path returned")
        if int(info.get("file_size") or 0) > limit:
            raise TelegramError("getFile: file is larger than the download limit")
        request = urllib.request.Request(self._file_url + path)
        try:
            with urllib.request.urlopen(request, timeout=120) as response, open(dest, "wb") as output:
                copied = 0
                while True:
                    chunk = response.read(1 << 16)
                    if not chunk:
                        break
                    copied += len(chunk)
                    if copied > limit:
                        raise TelegramError("download: file is larger than the download limit")
                    output.write(chunk)
        except urllib.error.HTTPError as e:
            raise TelegramError(f"download: HTTP {e.code}") from None
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
            raise TelegramError(f"download: {e.__class__.__name__}") from None
        return Path(dest)
