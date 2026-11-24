import json
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import Mock

from nft_tracker.bot import handle_update
from nft_tracker.catalog import catalog_page
from nft_tracker.service import admin, fits, prefs
from nft_tracker.storage import Storage
from nft_tracker.subscriptions import (
    apply_test_message, MONTH, PLANS, entitlement, invoice_spec, reserve_alert, settle_test_event,
    test_order as make_order, validate_test_checkout,
)
from nft_tracker.telegram import TelegramError
from tests.helpers import NOW, OWNER, FakeTelegram, callback, ev, make_settings, message


def grant(store, uid, tier, start=None, charge="offline-fixture"):
    start = int(NOW.timestamp()) if start is None else start
    token = make_order(store.db, uid, tier)
    spec = invoice_spec(store.db,token)
    event = dict(invoice_payload=token, currency=spec["currency"], total_amount=spec["prices"][0]["amount"],
                 telegram_payment_charge_id=charge, subscription_expiration_date=start+MONTH)
    query = dict(event)
    query["from"] = {"id":uid}
    validate_test_checkout(store.db,query,start)
    apply_test_message(store.db, {"chat":{"id":uid, "type":"private"}, "successful_payment":event})
    return event


class SubscriptionTests(TestCase):
    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "test.sqlite"
        self.store = Storage(self.path)
        self.addCleanup(self.store.close)
        self.t = int(NOW.timestamp())
        self.cfg = make_settings(public_users=True)
        self.tg = FakeTelegram()

    def test_purchase_renew_expire_switch_and_restart(self):
        event = grant(self.store, OWNER, "PLUS")
        renew = dict(event, telegram_payment_charge_id="renew",subscription_expiration_date=self.t+2*MONTH)
        settle_test_event(self.store.db,OWNER,renew)
        self.assertEqual(entitlement(self.store.db,OWNER,self.t+MONTH), ("PLUS",self.t+2*MONTH))
        self.assertEqual(entitlement(self.store.db,OWNER,self.t+2*MONTH), ("FREE",None))
        grant(self.store,OWNER,"ULTRA",self.t+2*MONTH,"new-plan")
        reopened = Storage(self.path)
        self.addCleanup(reopened.close)
        self.assertEqual(entitlement(reopened.db,OWNER,self.t+2*MONTH)[0],"ULTRA")

    def test_conflicting_terms_and_forged_payments(self):
        event = grant(self.store,OWNER,"PLUS")
        token = make_order(self.store.db,OWNER,"PRO")
        for update in [dict(currency="USD"), dict(total_amount=True), dict(total_amount=199), dict(invoice_payload="missing"),
                       dict(subscription_expiration_date=None), dict(invoice_payload=token,total_amount=199,telegram_payment_charge_id="second")]:
            with self.assertRaises(ValueError):
                settle_test_event(self.store.db,OWNER,dict(event,**update))
        with self.assertRaises(ValueError):
            settle_test_event(self.store.db,OWNER+1,event)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM star_terms").fetchone()[0],1)

    def test_refund_replay_and_out_of_order_delivery(self):
        event = grant(self.store,OWNER,"PLUS")
        for _ in range(2):
            settle_test_event(self.store.db,OWNER,event,refund=True)
            settle_test_event(self.store.db,OWNER,event)
        self.assertEqual(entitlement(self.store.db,OWNER,self.t)[0],"FREE")
        event.update(telegram_payment_charge_id="earlier-refund")
        settle_test_event(self.store.db,OWNER,event,refund=True)
        settle_test_event(self.store.db,OWNER,event)
        self.assertEqual(entitlement(self.store.db,OWNER,self.t)[0],"FREE")
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM star_refunds").fetchone()[0],2)

    def test_concurrent_replay_and_quota(self):
        event = grant(self.store,OWNER,"PLUS")
        def apply(i):
            s = Storage(self.path)
            try:
                settle_test_event(s.db,OWNER,event)
                return reserve_alert(s.db,"owner",str(i),"2026-11-24",3)
            finally:
                s.close()
        with ThreadPoolExecutor(4) as pool:
            self.assertEqual(sum(pool.map(apply,range(9))),3)
        self.assertTrue(reserve_alert(self.store.db,"owner","next","2026-11-25",3))
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM star_terms").fetchone()[0],1)

    def send(self, text, user=17):
        handle_update(self.cfg,self.store,self.tg,message(text,chat=user),NOW)
        return self.tg.sent[-1][1]

    def test_preview_search_sort_and_callbacks(self):
        rows = [dict(name=f"Gift{i}",prices={"portals":str(20-i)}) for i in range(15)]
        self.store.set("price_catalog",json.dumps(dict(at=NOW.isoformat(),rows=rows)))
        for cmd in ["/prices 2", "/prices 1 portals Gift12","/gift Gift12"]:
            self.assertNotIn("Gift12",self.send(cmd))
        handle_update(self.cfg,self.store,self.tg,callback("prices:2",chat=17),NOW)
        self.assertNotIn("Gift12",self.tg.edits[-1][2])
        grant(self.store,17,"PLUS")
        self.assertIn("Gift12",self.send("/prices Gift12"))
        self.assertIn("Gift14",self.send("/prices portals"))
        self.assertIn("Gift12",self.send("/gift Gift12"))

    def test_access_and_favorites(self):
        self.send("/start")
        self.assertIn("владельцу",self.send("/admin income"))
        self.send("/favorite +A")
        self.assertIn("лимит",self.send("/favorite +B"))
        self.assertIn("ULTRA",self.send("/export"))
        grant(self.store,17,"PRO")
        self.send("/favorite +B")
        self.send("/filter model Cool")
        self.assertTrue(fits(ev(name="B"),prefs(self.store,"17"),PLANS["PRO"]))
        self.assertFalse(fits(ev(name="C"),prefs(self.store,"17"),PLANS["PRO"]))
        self.assertIn("доступна с ULTRA",self.send("/report"))
        handle_update(self.cfg,self.store,self.tg,callback("admin:config",chat=OWNER,sender=17),NOW)
        self.assertEqual(self.tg.edits,[])

    def test_balance_failure_and_idempotent_import(self):
        tg = Mock()
        tg.call.side_effect=TelegramError("temporary",500)
        self.assertIn("недоступны",admin(self.cfg,self.store,tg,OWNER,"income",NOW)[0])
        tx=dict(id="test",date=self.t,amount=99,source={"type":"user","transaction_type":"invoice_payment"})
        refund=dict(id="test",date=self.t+30,amount=-99,receiver=tx["source"])
        for _ in range(2):
            tg.call.side_effect=[{"amount":0},{"transactions":[tx,refund]}]
            self.assertIn("Возвраты: 99",admin(self.cfg,self.store,tg,OWNER,"income",NOW)[0])
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM star_operations").fetchone()[0],2)

    def test_ordinary_message_cannot_activate_subscription(self):
        data = message("",chat=17)
        data["message"]["successful_payment"] = {"currency":"XTR","total_amount":399}
        handle_update(self.cfg,self.store,self.tg,data,NOW)
        self.assertEqual(entitlement(self.store.db,17,self.t)[0],"FREE")

    def test_checkout_does_not_grant_access_and_invalid_queries_fail(self):
        payload = make_order(self.store.db,17,"ULTRA")
        query = {"from":{"id":17}, "currency":"XTR","total_amount":399,"invoice_payload":payload}
        validate_test_checkout(self.store.db,query,self.t)
        self.assertEqual(entitlement(self.store.db,17,self.t)[0],"FREE")
        for change in ({"from":{"id":18}}, {"currency":"USD"}, {"total_amount":399.0}):
            with self.assertRaises(ValueError):
                validate_test_checkout(self.store.db,dict(query,**change),self.t)
        with self.assertRaises(ValueError):
            apply_test_message(self.store.db,{"chat":{"id":17,"type":"private"},"text":"payment accepted"})

    def test_admin_features_export_and_plan_expiry(self):
        self.send("/start")
        grant(self.store,17,"ULTRA")
        self.tg.send_csv = Mock()
        row = {"name":"=unsafe", "prices":{"telegram":"10"}}
        self.store.db.execute("INSERT INTO price_history VALUES (?,?)",(NOW.isoformat(),json.dumps({"rows":[row]})))
        self.store.db.commit()
        self.assertIn("CSV",self.send("/export"))
        self.assertIn("'=unsafe",self.tg.send_csv.call_args.args[1])
        self.send("/admin reports off",OWNER)
        self.assertIn("отключил",self.send("/report"))
        self.assertIn("владельцу",self.send("/admin reports on"))
        handle_update(self.cfg,self.store,self.tg,message("/export",chat=17),NOW+timedelta(days=30))
        self.tg.send_csv.assert_called_once()
        self.assertIn("доступна с ULTRA",self.tg.sent[-1][1])

    def test_free_callback_cannot_enable_paid_threshold(self):
        self.send("/start")
        handle_update(self.cfg,self.store,self.tg,callback("set:discount:50",chat=17),NOW)
        self.assertEqual(self.store.user("17").min_discount,30)
        self.assertIn("PLUS",self.tg.edits[-1][2])

    def test_owner_can_preview_paid_access_without_payment_data(self):
        self.send("/start", OWNER)
        self.assertIn("владельцу", self.send("/admin preview ULTRA"))
        self.assertIn("Тестовый доступ", self.send("/admin preview ULTRA", OWNER))
        self.assertIn("ULTRA", self.send("/subscription", OWNER))
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM star_terms").fetchone()[0], 0)
        handle_update(self.cfg, self.store, self.tg, message("/subscription", chat=OWNER), NOW+timedelta(days=1))
        self.assertIn("Ваш тариф: <b>FREE</b>", self.tg.sent[-1][1])
        self.send("/admin preview PRO", OWNER)
        self.assertIn("Ваш тариф: <b>FREE</b>", self.send("/admin preview FREE", OWNER))

    def test_lifetime_ultra_belongs_to_owner_and_returns_after_preview(self):
        from dataclasses import replace
        self.cfg = replace(self.cfg, owner_lifetime_ultra=True)
        self.assertIn("Ваш тариф: <b>ULTRA</b> · бессрочно", self.send("/subscription", OWNER))
        self.assertIn("Ваш тариф: <b>FREE</b>", self.send("/subscription"))
        self.send("/admin preview PLUS", OWNER)
        self.assertIn("Тестовый доступ владельца", self.send("/subscription", OWNER))
        self.assertIn("Ваш тариф: <b>ULTRA</b>", self.send("/admin preview FREE", OWNER))
        handle_update(self.cfg, self.store, self.tg, message("/subscription", chat=OWNER), NOW+timedelta(days=3650))
        self.assertIn("ULTRA</b> · бессрочно", self.tg.sent[-1][1])
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM star_terms").fetchone()[0], 0)
