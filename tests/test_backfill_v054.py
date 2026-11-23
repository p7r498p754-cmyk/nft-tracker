"""v0.5.4: history backfill and strict market schema regression tests."""
from __future__ import annotations

import json
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from decimal import Decimal

from nft_tracker.bot import status_text
from nft_tracker.monitor import run_cycle
from nft_tracker.portals import PortalsError
from nft_tracker.storage import Storage
from nft_tracker.telegram import format_alert
from nft_tracker.valuation import Estimate, Signal
from tests.helpers import FakePortals, FakeTelegram, NOW, LINK_TEMPLATE, ev, make_settings, json_response
from tests.test_portals import RAW, client


def page(count: int):
    return [{**RAW, "type": "purchase", "nft": {**RAW["nft"], "id": f"unique-{i}"}} for i in range(count)]


class HistoricalAPI(unittest.TestCase):
    def test_backfill_starts_at_requested_page_and_overlaps_next_run(self):
        c, calls, _ = client([json_response({"actions": page(20)}),
                              json_response({"actions": page(20)})])
        records, offset, complete, capped = c.historical_purchases(
            start_offset=60, pages=2, oldest=NOW - timedelta(days=14))
        self.assertEqual(len(records), 20)  # repeated gift IDs get deduplicated within this cycle
        self.assertEqual(["offset=60", "offset=80"],
                         [next(x for x in ["offset=60", "offset=80"] if x in r.full_url) for r in calls])
        self.assertEqual((offset, complete, capped), (80, False, False))

    def test_backfill_ends_when_a_short_page_is_seen(self):
        c, calls, _ = client([json_response({"actions": page(3)})])
        records, offset, complete, capped = c.historical_purchases(
            start_offset=160, pages=5, oldest=NOW - timedelta(days=14))
        self.assertEqual((len(records), complete, capped), (3, True, False))
        self.assertEqual(len(calls), 1)

    def test_backfill_warns_at_documented_offset_limit(self):
        c, calls, _ = client([json_response({"actions": page(20)})])
        records, offset, complete, capped = c.historical_purchases(
            start_offset=700, pages=5, oldest=NOW - timedelta(days=14))
        self.assertEqual((len(records), offset, complete, capped), (20, 700, False, True))
        self.assertEqual(len(calls), 1)
        self.assertIn("offset=700", calls[0].full_url)

    def test_default_sales_filter_and_malformed_purchase(self):
        broken = {**RAW, "type": "purchase", "amount": None}
        c, calls, _ = client([json_response({"actions": [broken]})])
        with self.assertRaises(PortalsError):
            c.purchases(pages=1)
        self.assertIn("action_types=buy", calls[0].full_url)

    def test_start_offset_validation(self):
        c, calls, _ = client([])
        for offset in (-20, 701, 1, 19):
            with self.assertRaises(ValueError):
                c.events(["purchase"], start_offset=offset)
        self.assertEqual(calls, [])

    def test_api_missing_gift_name_raises_without_silent_checkpoint(self):
        broken = {**RAW, "nft": {"id": "x"}}
        c, _, _ = client([json_response({"actions": [broken]})])
        with self.assertRaises(PortalsError):
            c.events(["listing"])


class ScheduledBackfill(unittest.TestCase):
    def test_backfill_progress_survives_new_storage_instance(self):
        class Progressive(FakePortals):
            def __init__(self):
                super().__init__(listings=[])
                self.offsets = []

            def purchases(self, **kw):
                return []

            def historical_purchases(self, **kw):
                self.offsets.append(kw["start_offset"])
                if len(self.offsets) == 1:
                    return [ev("purchase", "100", gift_id="sale-a")], 140, False, False
                return [ev("purchase", "100", gift_id="sale-b")], 220, True, False

        cfg = make_settings()
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "state.sqlite3"
            p = Progressive()
            with_store1 = Storage(path)
            with_store1.ensure_user("owner", None, 30, NOW)
            report1 = run_cycle(cfg, with_store1, FakeTelegram(), p, NOW)
            self.assertEqual(report1.new_sales, 1)
            self.assertIn("пополняется", report1.warning)
            self.assertEqual(with_store1.get("sales_backfill_offset"), "140")
            with_store1.close()
            with_store2 = Storage(path)
            report2 = run_cycle(cfg, with_store2, FakeTelegram(), p, NOW + timedelta(minutes=10))
            self.assertEqual((report2.new_sales, with_store2.sales_count()), (1, 2))
            self.assertEqual(p.offsets, [60, 140])
            self.assertEqual(with_store2.get("sales_backfill_done"), "1")
            self.assertIn("завершена", status_text(cfg, with_store2, NOW))
            run_cycle(cfg, with_store2, FakeTelegram(), p, NOW + timedelta(minutes=20))
            self.assertEqual(len(p.offsets), 2)  # never backfill after completed
            with_store2.close()

    def test_backfill_cap_is_explicit_in_status(self):
        class Capped(FakePortals):
            def historical_purchases(self, **kw):
                return [], 700, False, True
        cfg = make_settings()
        store = Storage(":memory:")
        store.ensure_user("owner", None, 30, NOW)
        p = Capped()
        report = run_cycle(cfg, store, FakeTelegram(), p, NOW)
        self.assertIn("ограничена", report.warning)
        self.assertIn("неполное покрытие", status_text(cfg, store, NOW))
        store.close()

    def test_html_escaped_in_listing_number_and_currency(self):
        event = ev(number="<b>BAD</b>")
        sig = Signal(event, Estimate(Decimal("100"), "exact", 3, NOW), Decimal("30"))
        text = format_alert(sig, "<GRAM>", NOW, "UTC", True)
        self.assertIn("&lt;b&gt;BAD&lt;/b&gt;", text)
        self.assertIn("&lt;GRAM&gt;", text)
        self.assertNotIn("<b>BAD</b>", text)


if __name__ == "__main__":
    unittest.main()
