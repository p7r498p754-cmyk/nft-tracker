from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import Mock

from nft_tracker.bot import handle_update
from nft_tracker.config import ConfigError, Settings
from nft_tracker.storage import Storage
from nft_tracker.subscriptions import MONTH, entitlement
from tests.helpers import FakeTelegram, NOW, callback, make_settings, message


class StarsPayments(TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = Storage(Path(directory.name) / "private.sqlite3")
        self.addCleanup(self.store.close)
        self.cfg = replace(make_settings(), stars_payments=True, public_users=True,
                           stars_terms="https://example.test/terms", stars_support="test_support")
        self.uid = 17
        self.telegram = FakeTelegram()
        self.telegram.call = Mock(return_value=True)
        self.stamp = int(NOW.timestamp())

    def update(self, data, at=NOW):
        handle_update(self.cfg, self.store, self.telegram, data, at)

    def invoice(self, plan="PRO", at=NOW):
        self.update(callback("buy:" + plan, chat=self.uid), at)
        method, payload = self.telegram.call.call_args.args
        self.assertEqual(method, "sendInvoice")
        self.assertEqual(payload["currency"], "XTR")
        self.assertNotIn("subscription_period", payload)
        return {"invoice_payload": payload["payload"], "currency": "XTR",
                "total_amount": payload["prices"][0]["amount"]}

    def check(self, event, query="checkout", uid=None, at=NOW):
        self.update({"pre_checkout_query": dict(event, id=query, **{"from": {"id": uid or self.uid}})},at)
        return self.telegram.call.call_args.args[1]["ok"]

    def receipt(self, event, field="successful_payment", charge="fixture-charge"):
        update = message("", chat=self.uid)
        update["message"]["date"] = self.stamp
        update["message"][field] = dict(event, telegram_payment_charge_id=charge)
        self.update(update)

    def test_payment_unlocks_access_once_and_refund_revokes_it(self):
        event = self.invoice()
        self.assertTrue(self.check(event))
        self.assertEqual(entitlement(self.store.db,self.uid,self.stamp)[0],"FREE")
        self.receipt(event)
        self.receipt(event)
        with_open_file = Storage(self.store.db.execute("PRAGMA database_list").fetchone()[2])
        self.addCleanup(with_open_file.close)
        self.assertEqual(entitlement(with_open_file.db,self.uid,self.stamp), ("PRO",self.stamp+MONTH))
        self.assertEqual(entitlement(with_open_file.db,self.uid,self.stamp+MONTH), ("FREE",None))
        self.assertEqual(len(self.telegram.sent),1)
        self.receipt(event,"refunded_payment")
        self.receipt(event,"refunded_payment")
        self.assertEqual(entitlement(self.store.db,self.uid,self.stamp),("FREE",None))

    def test_rejects_expired_tampered_unapproved_and_parallel_orders(self):
        event = self.invoice()
        self.receipt(event)
        self.assertEqual(entitlement(self.store.db,self.uid,self.stamp)[0],"FREE")
        other = self.invoice("PLUS")
        self.assertFalse(self.check(event,at=NOW+timedelta(minutes=31)))
        self.assertFalse(self.check(dict(event,total_amount=99)))
        self.assertFalse(self.check(event, uid=18))
        self.assertTrue(self.check(event))
        self.assertTrue(self.check(event))
        self.assertFalse(self.check(event,"different-query"))
        self.assertFalse(self.check(other,"another"))

    def test_disabled_sales_decline_checkout_and_issue_no_invoice(self):
        event = self.invoice()
        self.cfg = replace(self.cfg, stars_payments=False)
        self.assertFalse(self.check(event))
        self.telegram.call.reset_mock()
        self.update(callback("buy:PRO", chat=self.uid))
        self.telegram.call.assert_not_called()

    def test_public_actions_cannot_enable_sales(self):
        with self.assertRaises(ConfigError):
            Settings.from_env({"TELEGRAM_BOT_TOKEN": "test", "OWNER_CHAT_ID": "777",
                               "PORTALS_API_KEY": "test", "PUBLIC_USERS": "1",
                               "STARS_PAYMENTS": "1", "GITHUB_ACTIONS": "true"})

    def test_closed_original_bot_publishes_terms_and_support(self):
        self.cfg = Settings.from_env({
            "GITHUB_REPOSITORY": "p7r498p754-cmyk/nft-tracker",
            "GITHUB_ACTIONS": "true", "OWNER_CHAT_ID": "777", "TELEGRAM_BOT_TOKEN": "test",
        }, need_portals=False)
        self.update(message("/terms", chat=self.uid))
        self.assertEqual(self.telegram.sent[-1][2][0][0]["url"], self.cfg.stars_terms)
        self.assertTrue(self.cfg.stars_terms.endswith("/main/docs/stars-purchase-terms.md"))
        self.update(message("/paysupport", chat=self.uid))
        self.assertIn("@nishjta", self.telegram.sent[-1][1])
        self.update(message("/buy", chat=self.uid))
        self.assertIn("закрытом режиме", self.telegram.sent[-1][1])
        self.telegram.call.assert_not_called()

    def test_terms_are_shown_before_requesting_invoice(self):
        self.update(message("/subscription PRO", chat=self.uid))
        keyboard = self.telegram.sent[-1][2]
        self.assertEqual(keyboard[0][0]["url"], self.cfg.stars_terms)
        self.assertIn("Принимаю условия", keyboard[1][0]["text"])
        self.telegram.call.assert_not_called()
        self.update(message("/terms", chat=self.uid))
        self.assertEqual(self.telegram.sent[-1][2][0][0]["url"], self.cfg.stars_terms)

    def test_refund_before_receipt_preserves_both_events_without_grant(self):
        event = self.invoice()
        self.assertTrue(self.check(event))
        self.receipt(event,"refunded_payment")
        self.receipt(event)
        self.receipt(event)
        self.assertEqual(entitlement(self.store.db,self.uid,self.stamp),("FREE",None))
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM star_terms").fetchone()[0],1)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM star_refunds").fetchone()[0],1)
        self.assertFalse(self.telegram.sent)

    def test_checkout_replay_after_expiry_declined(self):
        event = self.invoice()
        self.assertTrue(self.check(event))
        self.assertFalse(self.check(event,at=NOW+timedelta(minutes=31)))

    def test_all_paid_prices_and_switch_after_expiry(self):
        at = NOW
        for plan,price in (("PLUS",99),("PRO",199),("ULTRA",399)):
            event = self.invoice(plan, at)
            self.assertEqual(event["total_amount"],price)
            self.assertTrue(self.check(event,query=plan,at=at))
            self.receipt(event,charge=plan)
            self.assertEqual(entitlement(self.store.db,self.uid,self.stamp),(plan,self.stamp+MONTH))
            self.stamp += MONTH
            self.assertEqual(entitlement(self.store.db,self.uid,self.stamp),("FREE",None))
            at += timedelta(seconds=MONTH)

    def test_waiting_checkout_visible_only_to_buyer_and_blocks_extra_invoice(self):
        event = self.invoice()
        self.assertTrue(self.check(event))
        self.telegram.call.reset_mock()
        self.update(callback("buy:PLUS",chat=self.uid))
        self.telegram.call.assert_not_called()
        self.update(message("/subscription",chat=self.uid))
        self.assertIn("Ожидается подтверждение оплаты тарифа PRO", self.telegram.sent[-1][1])
        self.update(message("/subscription",chat=self.uid+1))
        self.assertNotIn("Ожидается подтверждение", self.telegram.sent[-1][1])
        self.receipt(event)
        self.update(message("/subscription",chat=self.uid))
        self.assertNotIn("Ожидается подтверждение", self.telegram.sent[-1][1])

    def test_buy_command_opens_terms_without_charge(self):
        self.update(message("/buy plus", chat=self.uid))
        text, keyboard = self.telegram.sent[-1][1:]
        self.assertIn("99", text)
        self.assertEqual(keyboard[1][0]["callback_data"], "buy:PLUS")
        self.telegram.call.assert_not_called()

    def test_public_rate_limit_never_drops_paid_receipt(self):
        event = self.invoice()
        self.assertTrue(self.check(event))
        for _ in range(25):
            self.update(message("/ping", chat=self.uid))
        count = len(self.telegram.sent)
        self.update(message("/ping", chat=self.uid))
        self.assertEqual(len(self.telegram.sent), count)
        self.receipt(event)
        later = NOW + timedelta(minutes=1)
        self.update(message("/payments", chat=self.uid), later)
        self.assertIn("199 ⭐ · оплачено", self.telegram.sent[-1][1])
        self.update(message("/payments", chat=self.uid+1), later)
        self.assertIn("пока нет", self.telegram.sent[-1][1])
        self.receipt(event, "refunded_payment")
        self.update(message("/payments", chat=self.uid), later)
        self.assertIn("199 ⭐ · возврат", self.telegram.sent[-1][1])

    def test_paid_runtime_ignores_stored_owner_preview(self):
        self.store.set("owner_plan_preview", '{"plan":"PRO","end":2147483647}')
        self.update(message("/subscription", chat=self.cfg.owner_chat_id))
        self.assertIn("Ваш тариф: <b>FREE</b>", self.telegram.sent[-1][1])
        self.update(message("/admin preview ULTRA", chat=self.cfg.owner_chat_id))
        self.assertIn("отключена", self.telegram.sent[-1][1])

    def test_restore_paid_customer_from_private_backup(self):
        from nft_tracker.backup import snapshot
        self.update(message("/start", chat=self.uid))
        self.assertIn("FREE", self.telegram.sent[-1][1] + str(entitlement(self.store.db,self.uid,self.stamp)))
        event = self.invoice("PLUS")
        self.assertTrue(self.check(event))
        self.receipt(event)
        path = Path(self.store.db.execute("PRAGMA database_list").fetchone()[2])
        backup = path.with_name("backup.db")
        restored = path.with_name("restored.db")
        snapshot(path, backup)
        snapshot(backup, restored)
        with_store = Storage(restored)
        self.addCleanup(with_store.close)
        for command, expected in [("/subscription", "PLUS"), ("/payments","99 ⭐ · оплачено"), ("/summary","С начала суток")]:
            handle_update(self.cfg, with_store, self.telegram, message(command,chat=self.uid), NOW)
            self.assertIn(expected,self.telegram.sent[-1][1])
        self.assertTrue(with_store.user(str(self.uid)).active)
