import os
import tempfile
import unittest
from datetime import timedelta

from nft_tracker import monitor
from nft_tracker.portals import PortalsError
from nft_tracker.storage import Storage
from nft_tracker.telegram import TelegramError, format_alert, fmt_price
from nft_tracker.valuation import evaluate
from tests.helpers import LINK_TEMPLATE, NOW, OWNER, FakePortals, FakeTelegram, ev, make_settings, sales

LINK = "https://t.me/test_bot/app?startapp=item_g1"
GOOD_SALES = sales(["100", "100", "100"])


def setup(cfg=None, **portal_kw):
    cfg = cfg or make_settings()
    store = Storage(":memory:")
    store.ensure_user("owner", None, 30, NOW)
    return cfg, store, FakeTelegram(), FakePortals(**portal_kw)


def cheap(**kw):
    return ev(price="60", when=NOW - timedelta(minutes=5), link=LINK, **kw)


class Cycle(unittest.TestCase):
    def test_alert_sent_once_with_button_and_never_duplicated(self):
        cfg, store, tg, pt = setup(purchases=GOOD_SALES, listings=[cheap()])
        r1 = monitor.run_cycle(cfg, store, tg, pt, NOW)
        self.assertEqual((r1.alerts, len(tg.sent)), (1, 1))
        chat, text, kb = tg.sent[0]
        self.assertEqual(chat, OWNER)
        self.assertIn("НАЙДЕН ДЕШЁВЫЙ NFT", text)
        self.assertEqual(kb[0][0]["url"], LINK)
        self.assertEqual(kb[0][0]["text"], "🔗 Открыть объявление")
        r2 = monitor.run_cycle(cfg, store, tg, pt, NOW + timedelta(minutes=10))
        self.assertEqual((r2.alerts, len(tg.sent)), (0, 1))

    def test_message_has_all_required_fields(self):
        cfg, store, tg, pt = setup(purchases=GOOD_SALES, listings=[cheap()])
        monitor.run_cycle(cfg, store, tg, pt, NOW)
        text = tg.sent[0][1]
        for part in ["Коллекция: Plush Pepe", "Модель: Cool", "Номер: #42", "Цена объявления: 60 GRAM",
                     "Рыночная оценка: 100 GRAM", "Предполагаемая скидка: 40.0%", "Маркетплейс: Portals",
                     "Время обнаружения: 23.11.2026 12:00:00", "не гарантированная"]:
            self.assertIn(part, text)

    def test_same_price_different_decimal_scales_are_deduplicated(self):
        cfg, store, tg, pt = setup(purchases=GOOD_SALES, listings=[cheap()])
        monitor.run_cycle(cfg, store, tg, pt, NOW)
        pt._l = [ev(price="60.000", when=NOW - timedelta(minutes=4), link=LINK)]
        rep = monitor.run_cycle(cfg, store, tg, pt, NOW + timedelta(minutes=5))
        self.assertEqual(rep.alerts, 0)
        self.assertEqual(len(tg.sent), 1)

    def test_coverage_warning_visible_in_status(self):
        from nft_tracker.bot import status_text
        cfg, store, tg, pt = setup(purchases=GOOD_SALES, listings=[])
        pt.last_events_truncated = True
        rep = monitor.run_cycle(cfg, store, tg, pt, NOW)
        self.assertIn("Часть объявлений", rep.warning)
        self.assertIn("Неполная проверка", status_text(cfg, store, NOW))
        pt.last_events_truncated = False
        rep = monitor.run_cycle(cfg, store, tg, pt, NOW + timedelta(minutes=10))
        self.assertIsNone(rep.warning)
        self.assertNotIn("Неполная проверка", status_text(cfg, store, NOW + timedelta(minutes=10)))

    def test_overlapping_events_do_not_send_same_price_twice_in_one_cycle(self):
        cfg, store, tg, pt = setup(purchases=GOOD_SALES, listings=[cheap(), cheap()])
        rep = monitor.run_cycle(cfg, store, tg, pt, NOW)
        self.assertEqual((rep.alerts, rep.candidates, len(tg.sent)), (1, 1, 1))

    def test_batch_validation_reduces_queries_and_blocks_changed_prices(self):
        class BatchPortals(FakePortals):
            def __init__(self, **kw):
                super().__init__(**kw)
                self.batches = []

            def verify_many(self, batch):
                self.batches.append(batch)
                return {(e.gift_id, e.price): e.gift_id == "g2" for e in batch}

        cfg, store, tg, _ = setup()
        pt = BatchPortals(purchases=GOOD_SALES,
                          listings=[cheap(gift_id="g1"), cheap(gift_id="g2")])
        rep = monitor.run_cycle(cfg, store, tg, pt, NOW)
        self.assertEqual((rep.alerts, rep.skipped_unverified), (1, 1))
        self.assertEqual(len(pt.batches), 1)
        self.assertEqual(len(pt.batches[0]), 2)
        self.assertEqual(pt.verify_calls, 0)

    def test_batch_verification_failure_is_not_silenced(self):
        class BrokenBatch(FakePortals):
            def verify_many(self, batch):
                raise PortalsError("Portals: /nfts/search недоступен")

        cfg, store, tg, _ = setup()
        pt = BrokenBatch(purchases=GOOD_SALES, listings=[cheap()])
        rep = monitor.run_cycle(cfg, store, tg, pt, NOW)
        self.assertEqual(rep.alerts, 0)
        self.assertIn("/nfts/search", rep.warning)
        self.assertEqual(tg.sent, [])

    def test_price_change_to_new_price_alerts_again(self):
        cfg, store, tg, pt = setup(purchases=GOOD_SALES, listings=[cheap()])
        monitor.run_cycle(cfg, store, tg, pt, NOW)
        pt._l = [ev("price_update", "55", when=NOW + timedelta(minutes=1), link=LINK)]
        r = monitor.run_cycle(cfg, store, tg, pt, NOW + timedelta(minutes=10))
        self.assertEqual(r.alerts, 1)

    def test_price_updates_of_same_gift_are_verified_separately(self):
        # A stale price update must not block a later, still-active price.
        old = ev("price_update", "60", when=NOW - timedelta(minutes=8), link=LINK)
        current = ev("price_update", "55", when=NOW - timedelta(minutes=4), link=LINK)
        cfg, store, tg, pt = setup(purchases=GOOD_SALES, listings=[old, current],
                                   listed=lambda item: item.price == current.price)
        rep = monitor.run_cycle(cfg, store, tg, pt, NOW)
        self.assertEqual(rep.alerts, 1)
        self.assertEqual(pt.verify_calls, 2)
        self.assertIn("55 GRAM", tg.sent[0][1])
        self.assertNotIn("60 GRAM", tg.sent[0][1])

    def test_stale_listing_ignored(self):
        cfg, store, tg, pt = setup(purchases=GOOD_SALES, listings=[ev(price="60", when=NOW - timedelta(hours=3), link=LINK)])
        self.assertEqual(monitor.run_cycle(cfg, store, tg, pt, NOW).alerts, 0)

    def test_sold_or_changed_listing_is_not_alerted(self):
        cfg, store, tg, pt = setup(purchases=GOOD_SALES, listings=[cheap()], listed=False)
        r = monitor.run_cycle(cfg, store, tg, pt, NOW)
        self.assertEqual((r.alerts, r.skipped_unverified, tg.sent), (0, 1, []))
        self.assertFalse(store.was_sent("owner", "portals:g1:60"))   # can alert later if it is listed again

    def test_no_link_means_no_alert_and_one_notice(self):
        cfg, store, tg, pt = setup(make_settings(listing_url_template=""), purchases=GOOD_SALES,
                                   listings=[ev(price="60", when=NOW - timedelta(minutes=5))])
        r = monitor.run_cycle(cfg, store, tg, pt, NOW)
        self.assertEqual((r.alerts, r.skipped_no_link), (0, 1))
        self.assertEqual(len(tg.sent), 1)
        self.assertIn("не настроена", tg.sent[0][1])
        monitor.run_cycle(cfg, store, tg, pt, NOW + timedelta(minutes=10))
        self.assertEqual(len(tg.sent), 1)            # notice is rate-limited

    def test_optional_no_link_mode_is_explicit_and_labelled(self):
        cfg, store, tg, pt = setup(make_settings(listing_url_template="", allow_no_link_alerts=True),
                                   purchases=GOOD_SALES, listings=[ev(price="60", when=NOW - timedelta(minutes=5))])
        monitor.run_cycle(cfg, store, tg, pt, NOW)
        chat, text, kb = tg.sent[0]
        self.assertIsNone(kb)
        self.assertIn("Прямая ссылка не настроена", text)

    def test_insufficient_data_creates_no_signal(self):
        cfg, store, tg, pt = setup(purchases=sales(["100", "100"]), listings=[cheap()])
        self.assertEqual(monitor.run_cycle(cfg, store, tg, pt, NOW).alerts, 0)

    def test_user_threshold_respected(self):
        cfg, store, tg, pt = setup(purchases=GOOD_SALES, listings=[cheap()])
        store.set_min_discount("owner", 50)           # listing is 40% below
        self.assertEqual(monitor.run_cycle(cfg, store, tg, pt, NOW).alerts, 0)

    def test_stopped_user_gets_nothing(self):
        cfg, store, tg, pt = setup(purchases=GOOD_SALES, listings=[cheap()])
        store.set_active("owner", False)
        self.assertEqual(monitor.run_cycle(cfg, store, tg, pt, NOW).alerts, 0)

    def test_telegram_block_deactivates_user(self):
        cfg, store, tg, pt = setup(purchases=GOOD_SALES, listings=[cheap()])
        tg.fail = {"send": TelegramError("Forbidden", 403)}
        monitor.run_cycle(cfg, store, tg, pt, NOW)
        self.assertFalse(store.user("owner").active)

    def test_temporary_send_failure_retries_next_cycle(self):
        cfg, store, tg, pt = setup(purchases=GOOD_SALES, listings=[cheap()])
        tg.fail = {"send": TelegramError("boom", 500)}
        monitor.run_cycle(cfg, store, tg, pt, NOW)
        self.assertFalse(store.was_sent("owner", "portals:g1:60"))
        tg.fail = {}
        self.assertEqual(monitor.run_cycle(cfg, store, tg, pt, NOW + timedelta(minutes=5)).alerts, 1)

    def test_photo_used_when_available_and_falls_back(self):
        cfg, store, tg, pt = setup(purchases=GOOD_SALES,
                                   listings=[cheap(photo="https://nft.fragment.com/gift/x.jpg")])
        monitor.run_cycle(cfg, store, tg, pt, NOW)
        self.assertEqual((len(tg.photos), len(tg.sent)), (1, 0))
        cfg, store, tg, pt = setup(purchases=GOOD_SALES, listings=[cheap(photo="https://nft.fragment.com/gift/x.jpg")])
        tg.fail = {"photo": TelegramError("wrong file", 400)}
        monitor.run_cycle(cfg, store, tg, pt, NOW)
        self.assertEqual(len(tg.sent), 1)

    def test_portals_failure_recorded_and_notified_after_three(self):
        cfg, store, tg, pt = setup(error=PortalsError("Portals: HTTP 500"))
        for i in range(2):
            r = monitor.run_cycle(cfg, store, tg, pt, NOW + timedelta(minutes=10 * i))
            self.assertEqual(r.error, "Portals: HTTP 500")
        self.assertEqual(tg.sent, [])
        monitor.run_cycle(cfg, store, tg, pt, NOW + timedelta(minutes=30))
        self.assertEqual(len(tg.sent), 1)
        self.assertIn("недоступен", tg.sent[0][1])
        pt.error = None
        monitor.run_cycle(cfg, store, tg, pt, NOW + timedelta(minutes=40))
        self.assertEqual(store.get_json("cycle")["failures"], 0)

    def test_sales_accumulate_across_cycles(self):
        cfg, store, tg, pt = setup(purchases=sales(["100"]), listings=[])
        monitor.run_cycle(cfg, store, tg, pt, NOW)
        pt._p = sales(["100", "100", "100"])           # overlaps with first batch
        monitor.run_cycle(cfg, store, tg, pt, NOW + timedelta(minutes=10))
        self.assertEqual(store.sales_count(), 3)

    def test_restart_keeps_dedup_and_history(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "state.sqlite3")
            cfg = make_settings()
            store = Storage(path)
            store.ensure_user("owner", None, 30, NOW)
            tg1, pt = FakeTelegram(), FakePortals(purchases=GOOD_SALES, listings=[cheap()])
            monitor.run_cycle(cfg, store, tg1, pt, NOW)
            store.close()
            store2 = Storage(path)                       # simulated process restart (new run)
            tg2 = FakeTelegram()
            r = monitor.run_cycle(cfg, store2, tg2, FakePortals(purchases=[], listings=[cheap()]), NOW + timedelta(minutes=10))
            self.assertEqual((len(tg1.sent), r.alerts, len(tg2.sent)), (1, 0, 0))
            self.assertEqual(store2.sales_count(), 3)

    def test_single_verification_call_per_lot(self):
        cfg, store, tg, pt = setup(purchases=GOOD_SALES, listings=[cheap()])
        store.ensure_user("second", 5, 30, NOW)
        monitor.run_cycle(cfg, store, tg, pt, NOW)
        self.assertEqual(pt.verify_calls, 1)

    def test_future_dated_event_rejected(self):
        cfg, store, tg, pt = setup(purchases=GOOD_SALES, listings=[ev(price="60", when=NOW + timedelta(hours=2), link=LINK)])
        self.assertEqual(monitor.run_cycle(cfg, store, tg, pt, NOW).alerts, 0)


class Formatting(unittest.TestCase):
    def test_price_format(self):
        from decimal import Decimal
        self.assertEqual(fmt_price(Decimal("12.5000"), "GRAM"), "12.5 GRAM")
        self.assertEqual(fmt_price(Decimal("100"), "GRAM"), "100 GRAM")
        self.assertEqual(fmt_price(Decimal("1E+2"), "GRAM"), "100 GRAM")

    def test_html_is_escaped(self):
        sig = evaluate(ev(price="60", name="<b>X</b>&"), sales(["100"] * 3, name="<b>X</b>&"), 30, NOW)
        text = format_alert(sig, "GRAM", NOW, "UTC", True)
        self.assertNotIn("<b>X</b>", text)
        self.assertIn("&lt;b&gt;X&lt;/b&gt;&amp;", text)

    def test_unknown_timezone_falls_back(self):
        sig = evaluate(ev(price="60"), GOOD_SALES, 30, NOW)
        self.assertIn("UTC", format_alert(sig, "GRAM", NOW, "Not/AZone", True))


if __name__ == "__main__":
    unittest.main()


class CursorReliability(unittest.TestCase):
    def test_incomplete_feed_does_not_advance_checkpoint(self):
        cfg, store, tg, pt = setup(purchases=[], listings=[cheap()])
        original = "2026-11-23T10:30:00+00:00"
        store.set("last_listing_at", original)
        pt.last_events_truncated = True
        monitor.run_cycle(cfg, store, tg, pt, NOW)
        self.assertEqual(store.get("last_listing_at"), original)

    def test_stale_checkpoint_only_scans_eligible_window(self):
        cfg, store, tg, pt = setup(listings=[])
        store.set("last_listing_at", (NOW - timedelta(days=2)).isoformat())
        from unittest.mock import patch
        with patch.object(pt, "listings", wraps=pt.listings) as listings:
            monitor.run_cycle(cfg, store, tg, pt, NOW)
        self.assertEqual(listings.call_args.kwargs["since"], NOW - timedelta(minutes=cfg.max_event_age_min))

    def test_future_event_does_not_advance_checkpoint(self):
        future = ev(price="60", when=NOW + timedelta(days=12), link=LINK)
        cfg, store, tg, pt = setup(purchases=[], listings=[future])
        monitor.run_cycle(cfg, store, tg, pt, NOW)
        self.assertIsNone(store.get("last_listing_at"))

    def test_valid_event_updates_checkpoint_when_feed_complete(self):
        cfg, store, tg, pt = setup(purchases=[], listings=[cheap()])
        monitor.run_cycle(cfg, store, tg, pt, NOW)
        self.assertEqual(store.get("last_listing_at"), cheap().event_time.isoformat())

    def test_future_event_cannot_override_valid_event_checkpoint(self):
        later = ev(price="60", when=NOW + timedelta(hours=8), link=LINK)
        cfg, store, tg, pt = setup(purchases=[], listings=[cheap(), later])
        monitor.run_cycle(cfg, store, tg, pt, NOW)
        self.assertEqual(store.get("last_listing_at"), cheap().event_time.isoformat())


class CheckpointRetries(unittest.TestCase):
    def test_telegram_send_failure_keeps_checkpoint_for_retry(self):
        cfg, store, tg, pt = setup(purchases=GOOD_SALES, listings=[cheap()])
        tg.fail = {"send": TelegramError("temporary", 500)}
        monitor.run_cycle(cfg, store, tg, pt, NOW)
        self.assertIsNone(store.get("last_listing_at"))
        tg.fail = {}
        self.assertEqual(monitor.run_cycle(cfg, store, tg, pt, NOW + timedelta(minutes=2)).alerts, 1)

    def test_verification_failure_keeps_checkpoint_for_retry(self):
        class Down(FakePortals):
            def still_listed_at(self, event):
                raise PortalsError("server failure")
        cfg, store, tg, _ = setup(purchases=GOOD_SALES, listings=[cheap()])
        rep = monitor.run_cycle(cfg, store, tg, Down(purchases=GOOD_SALES, listings=[cheap()]), NOW)
        self.assertEqual(rep.skipped_unverified, 1)
        self.assertIsNone(store.get("last_listing_at"))

    def test_no_link_candidate_keeps_checkpoint_until_fixed(self):
        cfg = make_settings(listing_url_template="")
        _, store, tg, pt = setup(cfg, purchases=GOOD_SALES,
                                  listings=[ev(price="60", when=NOW - timedelta(minutes=2))])
        monitor.run_cycle(cfg, store, tg, pt, NOW)
        self.assertIsNone(store.get("last_listing_at"))

    def test_sent_delivery_is_deduplicated_during_retry(self):
        cfg, store, tg, pt = setup(purchases=GOOD_SALES, listings=[cheap()])
        monitor.run_cycle(cfg, store, tg, pt, NOW)
        self.assertEqual(len(tg.sent), 1)
        store.set("last_listing_at", "2026-11-23T10:00:00+00:00")
        monitor.run_cycle(cfg, store, tg, pt, NOW + timedelta(minutes=1))
        self.assertEqual(len(tg.sent), 1)
