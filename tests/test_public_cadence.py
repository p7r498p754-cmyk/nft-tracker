from datetime import timedelta
from unittest import TestCase
from unittest.mock import Mock

from nft_tracker.monitor import run_cycle
from nft_tracker.payments import create_invoice, reserve_checkout, settle_receipt
from tests.helpers import NOW, ev, make_settings
from tests.test_monitor import GOOD_SALES, setup


class PublicCadence(TestCase):
    def test_one_shared_feed_processes_four_plans_at_distinct_intervals(self):
        cfg, store, tg, portals = setup(make_settings(public_users=True, stars_payments=True), purchases=GOOD_SALES)
        self.addCleanup(store.close)
        store.set_active("owner", False)
        plans = [(101,"FREE",30), (102,"PLUS",15), (103,"PRO",5), (104,"ULTRA",1)]
        stamp = int(NOW.timestamp())
        for uid, plan, interval in plans:
            store.ensure_user(str(uid),uid,30,NOW)
            if plan == "FREE":
                continue
            api = Mock()
            create_invoice(store.db,api,uid,plan,stamp)
            order = api.call.call_args.args[1]
            query = dict(invoice_payload=order["payload"],currency="XTR",total_amount=order["prices"][0]["amount"],id=str(uid))
            self.assertTrue(reserve_checkout(store.db,uid,query,stamp))
            settle_receipt(store.db,uid,dict(query,telegram_payment_charge_id="fixture-"+str(uid)),stamp)
        observed = {str(uid): [] for uid, _, _ in plans}
        portals.listings = Mock(wraps=portals.listings)
        for minute in range(31):
            now = NOW + timedelta(minutes=minute)
            portals._l = [ev(price="60", gift_id=str(minute), when=now,link=("https://t.me/portals/app?startapp=item_" + str(minute)) if minute % 2 else None)] if minute else []
            report = run_cycle(cfg,store,tg,portals,now)
            self.assertFalse(report.error)
            for key, in store.db.execute("SELECT user_key FROM beta_ticks WHERE at=?", (int(now.timestamp()),)):
                observed[key].append(minute)
        self.assertEqual(portals.listings.call_count, 31)
        for uid, _, interval in plans:
            self.assertEqual(observed[str(uid)], list(range(0,31,interval)))
        for uid, _, _ in plans:
            self.assertGreater(store.db.execute("SELECT count(*) FROM deliveries WHERE user_key=?", (str(uid),)).fetchone()[0],0)
