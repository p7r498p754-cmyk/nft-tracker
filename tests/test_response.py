import unittest
from unittest.mock import Mock

from nft_tracker.bot import _reply, handle_update
from nft_tracker.telegram import TelegramClient, TelegramError
from tests.helpers import FakeTelegram, NOW, make_settings, message


class ResponseTests(unittest.TestCase):
    def test_unchanged_screen_needs_no_second_request(self):
        tg = FakeTelegram(fail={"edit": TelegramError("Bad Request: message is not modified",400)})
        _reply(tg,1,"menu",None,2)
        self.assertFalse(tg.sent)

    def test_rate_limit_is_preserved(self):
        tg = FakeTelegram(fail={"edit": TelegramError("slow down",429,3)})
        with self.assertRaises(TelegramError):
            _reply(tg,1,"menu",None,2)
        self.assertFalse(tg.sent)

    def test_ack_timeout(self):
        tg = TelegramClient("test")
        tg.call = Mock()
        tg.answer_callback("q")
        tg.call.assert_called_once_with("answerCallbackQuery", {"callback_query_id":"q"}, timeout=2)

    def test_ping_measures_queue_age(self):
        update = message("/ping")
        update["message"]["date"] = int(NOW.timestamp()) - 17
        tg = FakeTelegram()
        handle_update(make_settings(),None,tg,update,NOW)
        self.assertIn("17 с", tg.sent[0][1])
