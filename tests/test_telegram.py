import unittest
from unittest.mock import patch

from support import make_config  # noqa: F401
from bot.telegram_api import MESSAGE_LIMIT, Telegram, chunk_text


class ChunkTests(unittest.TestCase):
    def test_short_text_is_one_message(self):
        self.assertEqual(chunk_text("hi"), ["hi"])

    def test_long_text_splits_on_lines_within_the_limit(self):
        text = "\n".join("line %d" % i for i in range(2000))
        parts = chunk_text(text)
        self.assertGreater(len(parts), 1)
        self.assertTrue(all(len(p) <= MESSAGE_LIMIT for p in parts))
        self.assertEqual("\n".join(parts).split("\n"), text.split("\n"))

    def test_unbroken_text_is_cut_at_the_limit(self):
        parts = chunk_text("x" * (MESSAGE_LIMIT * 2 + 5))
        self.assertEqual([len(p) for p in parts], [MESSAGE_LIMIT, MESSAGE_LIMIT, 5])

    def test_send_long_attaches_a_keyboard_only_to_the_last_chunk(self):
        telegram = Telegram("token")
        keyboard = {"keyboard": [[{"text": "Menu"}]]}
        with patch.object(telegram, "_call", return_value={"message_id": 1}) as call:
            telegram.send_long(7, "x" * (MESSAGE_LIMIT + 1), reply_markup=keyboard)
        payloads = [item.args[1] for item in call.call_args_list]
        self.assertNotIn("reply_markup", payloads[0])
        self.assertEqual(payloads[1]["reply_markup"], keyboard)


class DownloadTests(unittest.TestCase):
    def test_download_saves_the_file_and_errors_hide_the_token(self):
        import tempfile
        import urllib.error
        from pathlib import Path
        from unittest import mock
        from bot.telegram_api import TelegramError
        telegram = Telegram("secret-token")
        response = mock.MagicMock()
        response.__enter__.return_value.read.side_effect = [b"abc", b""]
        with tempfile.TemporaryDirectory() as work:
            dest = Path(work) / "shot.png"
            with patch.object(telegram, "_call", return_value={"file_path": "photos/f.png", "file_size": 3}), \
                    patch("bot.telegram_api.urllib.request.urlopen", return_value=response) as urlopen:
                self.assertEqual(telegram.download_file("id", dest), dest)
            self.assertEqual(dest.read_bytes(), b"abc")
            self.assertTrue(urlopen.call_args.args[0].full_url.endswith("/file/botsecret-token/photos/f.png"))
            error = urllib.error.HTTPError("https://api.telegram.org/file/botsecret-token/x", 404, "nf", {}, None)
            with patch.object(telegram, "_call", return_value={"file_path": "x"}), \
                    patch("bot.telegram_api.urllib.request.urlopen", side_effect=error), \
                    self.assertRaises(TelegramError) as caught:
                telegram.download_file("id", dest)
            self.assertNotIn("secret-token", str(caught.exception))
            with patch.object(telegram, "_call", return_value={"file_path": "x", "file_size": 10}), \
                    self.assertRaisesRegex(TelegramError, "download limit"):
                telegram.download_file("id", dest, limit=5)


class FormattedMessageTests(unittest.TestCase):
    def test_html_messages_set_parse_mode_and_edits_are_not_cut(self):
        telegram = Telegram("token")
        text = "<blockquote expandable>" + "x" * MESSAGE_LIMIT + "</blockquote>"
        with patch.object(telegram, "_call", return_value={"message_id": 1}) as call:
            telegram.send_message(7, "<b>hi</b>", parse_mode="HTML")
            telegram.edit_message(7, 1, text, parse_mode="HTML")
            telegram.edit_message(7, 1, "y" * (MESSAGE_LIMIT + 5))
        sent, edited, plain = (item.args[1] for item in call.call_args_list)
        self.assertEqual(sent["parse_mode"], "HTML")
        self.assertEqual((edited["parse_mode"], edited["text"]), ("HTML", text))
        self.assertNotIn("parse_mode", plain)
        self.assertEqual(len(plain["text"]), MESSAGE_LIMIT)


if __name__ == "__main__":
    unittest.main()
