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

    def invoice(self, plan="PRO"):
        self.update(callback("buy:" + plan, chat=self.uid))
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
        self.assertFalse(self.check(event,at=NOW+timedelta(minutes=31)))
        self.assertFalse(self.check(dict(event,total_amount=99)))
        self.assertFalse(self.check(event, uid=18))
        self.assertTrue(self.check(event))
        self.assertTrue(self.check(event))
        self.assertFalse(self.check(event,"different-query"))
        self.assertFalse(self.check(self.invoice("PLUS"),"another"))

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

    def test_terms_are_shown_before_requesting_invoice(self):
        self.update(message("/subscription PRO", chat=self.uid))
        keyboard = self.telegram.sent[-1][2]
        self.assertEqual(keyboard[0][0]["url"], self.cfg.stars_terms)
        self.assertIn("Принимаю условия", keyboard[1][0]["text"])
        self.telegram.call.assert_not_called()
        self.update(message("/terms", chat=self.uid))
        self.assertEqual(self.telegram.sent[-1][2][0][0]["url"], self.cfg.stars_terms)
