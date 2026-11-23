import os
import tempfile
import unittest
from datetime import timedelta

from nft_tracker import bot
from nft_tracker.storage import Storage
from nft_tracker.telegram import TelegramError
from tests.helpers import NOW, OWNER, FakeTelegram, callback, ev, make_settings, message, sales


class StorageTests(unittest.TestCase):
    def test_delete_cascades_deliveries(self):
        s = Storage(":memory:")
        s.ensure_user("owner", None, 30, NOW)
        s.mark_sent("owner", "sig1", NOW)
        s.delete_user("owner")
        self.assertEqual(s.db.execute("SELECT COUNT(*) FROM deliveries").fetchone()[0], 0)
        self.assertIsNone(s.user("owner"))

    def test_owner_mode_stores_no_chat_id(self):
        s = Storage(":memory:")
        u = s.ensure_user("owner", None, 30, NOW)
        self.assertIsNone(u.chat_id)
        self.assertEqual(s.db.execute("SELECT chat_id FROM users").fetchone()[0], None)

    def test_delivery_dedup_and_prune(self):
        s = Storage(":memory:")
        s.ensure_user("owner", None, 30, NOW)
        s.mark_sent("owner", "a", NOW)
        s.mark_sent("owner", "a", NOW)
        self.assertTrue(s.was_sent("owner", "a"))
        s.mark_sent("owner", "old", NOW - timedelta(days=20))
        s.prune(NOW)
        self.assertFalse(s.was_sent("owner", "old"))
        self.assertTrue(s.was_sent("owner", "a"))

    def test_sales_roundtrip_dedup_and_prune(self):
        s = Storage(":memory:")
        data = sales(["100", "101"])
        self.assertEqual(s.add_sales(data), 2)
        self.assertEqual(s.add_sales(data), 0)
        back = sorted(x.price for x in s.sales())
        self.assertEqual([str(b) for b in back], ["100", "101"])
        s.add_sales([ev("purchase", "5", gift_id="old", when=NOW - timedelta(days=30))])
        s.prune(NOW)
        self.assertEqual(s.sales_count(), 2)

    def test_state_survives_reopen(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "sub", "t.sqlite3")
            s = Storage(path)
            s.set("offset", "9")
            s.add_sales(sales(["1"]))
            s.close()
            s2 = Storage(path)
            self.assertEqual(s2.get("offset"), "9")
            self.assertEqual(s2.sales_count(), 1)


class BotTests(unittest.TestCase):
    def setUp(self):
        self.cfg, self.store = make_settings(), Storage(":memory:")

    def run_updates(self, *updates):
        tg = FakeTelegram(list(updates))
        bot.process_updates(self.cfg, self.store, tg, NOW)
        return tg

    def test_id_works_for_anyone_and_stores_nothing(self):
        tg = self.run_updates(message("/id", chat=999))
        self.assertIn("999", tg.sent[0][1])
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM users").fetchone()[0], 0)

    def test_stranger_is_locked_out(self):
        tg = self.run_updates(message("/start", chat=999))
        self.assertIn("закрытом", tg.sent[0][1])
        self.assertIsNone(self.store.user("owner"))

    def test_start_creates_owner_with_menu(self):
        tg = self.run_updates(message("/start"))
        u = self.store.user("owner")
        self.assertTrue(u.active)
        self.assertIsNone(u.chat_id)
        self.assertEqual(tg.sent[0][0], OWNER)
        self.assertTrue(tg.sent[0][2])  # inline keyboard

    def test_stop_then_start(self):
        self.run_updates(message("/start"))
        self.run_updates(message("/stop", update_id=2))
        self.assertFalse(self.store.user("owner").active)
        self.run_updates(message("/start", update_id=3))
        self.assertTrue(self.store.user("owner").active)

    def test_status_help_settings_reply(self):
        self.run_updates(message("/start"))
        tg = self.run_updates(message("/status", update_id=2), message("/help", update_id=3), message("/settings", update_id=4))
        texts = [t for _, t, _ in tg.sent]
        self.assertTrue(any("Порог скидки" in t for t in texts))
        self.assertTrue(any("/delete" in t for t in texts))
        self.assertTrue(any("Выберите порог" in t for t in texts))

    def test_status_reports_missing_link_honestly(self):
        cfg = make_settings(listing_url_template="")
        from nft_tracker.bot import status_text
        txt = status_text(cfg, self.store, NOW)
        self.assertIn("не настроена", txt)
        self.assertIn("сигналы не отправляются", txt)
        self.assertIn("не подключены", txt)

    def test_discount_setting_via_callback_and_whitelist(self):
        self.run_updates(message("/start"))
        self.run_updates(callback("set:discount:40", update_id=2))
        self.assertEqual(self.store.user("owner").min_discount, 40)
        self.run_updates(callback("set:discount:5", update_id=3))       # not an allowed choice
        self.run_updates(callback("set:discount:abc", update_id=4))
        self.assertEqual(self.store.user("owner").min_discount, 40)

    def test_delete_requires_confirmation(self):
        self.run_updates(message("/start"))
        tg = self.run_updates(message("/delete", update_id=2))
        self.assertIsNotNone(self.store.user("owner"))
        self.assertIn("del:yes", str(tg.sent[-1][2]))
        self.run_updates(callback("del:no", update_id=3))
        self.assertIsNotNone(self.store.user("owner"))
        self.run_updates(callback("del:yes", update_id=4))
        self.assertIsNone(self.store.user("owner"))

    def test_foreign_callback_ignored(self):
        self.run_updates(message("/start"))
        self.run_updates(callback("del:yes", chat=999, update_id=2))
        self.assertIsNotNone(self.store.user("owner"))

    def test_group_chats_ignored(self):
        tg = self.run_updates(message("/start", kind="group"))
        self.assertEqual(tg.sent, [])

    def test_offset_advances_so_commands_are_not_replayed(self):
        tg = FakeTelegram([message("/id", update_id=10)])
        bot.process_updates(self.cfg, self.store, tg, NOW)
        self.assertEqual(self.store.get("offset"), "11")
        bot.process_updates(self.cfg, self.store, tg, NOW)
        self.assertEqual(len(tg.sent), 1)

    def test_failed_send_is_retried_without_losing_command(self):
        tg = FakeTelegram([message("/id", update_id=3)], fail={"send": TelegramError("x", 500)})
        with self.assertRaises(TelegramError):
            bot.process_updates(self.cfg, self.store, tg, NOW)
        self.assertEqual(self.store.get("offset"), None)
        tg.fail = {}
        bot.process_updates(self.cfg, self.store, tg, NOW)
        self.assertEqual(self.store.get("offset"), "4")
        self.assertEqual(len(tg.sent), 1)

    def test_failure_in_second_message_preserves_first_checkpoint(self):
        class FailSecond(FakeTelegram):
            def send(self, chat, text, keyboard=None):
                if len(self.sent) == 1:
                    raise TelegramError("temporary", 500)
                return super().send(chat, text, keyboard)

        tg = FailSecond([message("/id", update_id=3), message("/status", update_id=4)])
        with self.assertRaises(TelegramError):
            bot.process_updates(self.cfg, self.store, tg, NOW)
        self.assertEqual(self.store.get("offset"), "4")

    def test_delay_note_only_on_actions(self):
        tg = FakeTelegram([message("/start")])
        bot.process_updates(make_settings(on_actions=True), self.store, tg, NOW)
        self.assertIn("по расписанию", tg.sent[0][1])


if __name__ == "__main__":
    unittest.main()


class UpdateRetryTests(unittest.TestCase):
    def test_permanent_rejection_does_not_block_following_messages(self):
        class FailFirst(FakeTelegram):
            def send(self, chat, text, keyboard=None):
                if chat == 999:
                    raise TelegramError("Forbidden", 403)
                return super().send(chat, text, keyboard)

        store = Storage(":memory:")
        tg = FailFirst([message("/id", chat=999, update_id=10), message("/id", chat=OWNER, update_id=11)])
        bot.process_updates(make_settings(), store, tg, NOW)
        self.assertEqual(store.get("offset"), "12")
        self.assertEqual(len(tg.sent), 1)
        self.assertEqual(tg.sent[0][0], OWNER)

    def test_rate_limit_does_not_advance_offset(self):
        store = Storage(":memory:")
        tg = FakeTelegram([message("/id", update_id=10)], fail={"send": TelegramError("rate limit", 429)})
        with self.assertRaises(TelegramError):
            bot.process_updates(make_settings(), store, tg, NOW)
        self.assertIsNone(store.get("offset"))
