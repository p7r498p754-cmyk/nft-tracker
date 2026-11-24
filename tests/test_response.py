import unittest
from threading import Event
from unittest.mock import Mock, patch

from nft_tracker.bot import _reply, handle_update, process_updates
from nft_tracker.storage import Storage
from nft_tracker.telegram import TelegramClient, TelegramError
from tests.helpers import FakeTelegram, callback, NOW, make_settings, message


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

    def test_settings_screen_does_not_wait_for_callback_ack(self):
        edited = Event()
        class SlowAck(FakeTelegram):
            def answer_callback(self, callback_id):
                self.screen_visible_during_ack = edited.wait(2)
            def edit(self, *args):
                super().edit(*args)
                edited.set()
        tg = SlowAck()
        store = Storage(":memory:")
        self.addCleanup(store.close)
        handle_update(make_settings(), store, tg, callback("menu:settings"), NOW)
        self.assertTrue(tg.screen_visible_during_ack)
        self.assertIn("Настройки", tg.edits[0][2])

    def test_ping_arriving_during_long_poll(self):
        update = message("/ping")
        update["message"]["date"] = int(NOW.timestamp()) + 4
        tg = FakeTelegram([update])
        store = Storage(":memory:")
        self.addCleanup(store.close)
        with patch("nft_tracker.bot.monotonic", side_effect=[10,15]):
            process_updates(make_settings(), store, tg, NOW, timeout=20)
        self.assertIn("1 с", tg.sent[0][1])
