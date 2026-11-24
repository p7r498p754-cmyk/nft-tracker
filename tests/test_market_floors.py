import unittest
from unittest.mock import Mock, patch
from datetime import timedelta

from nft_tracker.catalog import PERIOD, catalog_page, refresh_catalog_if_due
from nft_tracker.floors import refresh
from nft_tracker.storage import Storage
from tests.helpers import NOW, make_settings


class FloorChanges(unittest.TestCase):
    def test_baseline_new_price_replay_and_stale_observation(self):
        store = Storage(":memory:")
        self.addCleanup(store.close)
        store.ensure_user("owner", None, 30, NOW)
        tg = Mock()
        cfg = make_settings()
        offer = dict(market="telegram", currency="STARS", price=100, updated_at=NOW.isoformat())
        data = dict(gifts=[dict(name="Gift", slug="gift", offers=[offer])])
        refresh(store, tg, cfg, data, NOW)
        tg.send.assert_not_called()
        later = NOW + timedelta(minutes=30)
        offer.update(price=70, updated_at=later.isoformat())
        refresh(store, tg, cfg, data, later)
        refresh(store, tg, cfg, data, later)
        tg.send.assert_called_once()
        with self.assertRaises(ValueError):
            refresh(store, tg, cfg, data, later + timedelta(days=1))
        self.assertEqual(store.get_json("market_floors")["telegram/gift"]["price"], "70")

    def test_prices_do_not_wait_for_network_and_refresh_only_when_due(self):
        store = Storage(":memory:")
        self.addCleanup(store.close)
        offer = dict(market="telegram", currency="STARS", price=100, updated_at=NOW.isoformat())
        data = dict(gifts=[dict(name="Gift", slug="gift", offers=[offer])])
        with patch("nft_tracker.catalog.fetch_catalog", return_value=data) as fetch:
            self.assertIn("пока недоступен", catalog_page(store, NOW)[0])
            fetch.assert_not_called()
            refresh_catalog_if_due(store, NOW)
            self.assertIn("100", catalog_page(store, NOW)[0])
            refresh_catalog_if_due(store, NOW + PERIOD - timedelta(seconds=1))
            fetch.assert_called_once()
            offer.update(price=90, updated_at=(NOW + PERIOD).isoformat())
            refresh_catalog_if_due(store, NOW + PERIOD)
            self.assertEqual(fetch.call_count, 2)
            self.assertIn("90", catalog_page(store, NOW + PERIOD)[0])
