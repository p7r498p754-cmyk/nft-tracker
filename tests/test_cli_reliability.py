import unittest
from unittest.mock import patch

from nft_tracker.__main__ import cycle_once, main
from nft_tracker.portals import PortalsError
from nft_tracker.storage import Storage
from nft_tracker.telegram import TelegramError
from tests.helpers import NOW, FakePortals, FakeTelegram, make_settings


class ExitStatuses(unittest.TestCase):
    def test_portals_failure_reports_failed_cycle(self):
        cfg = make_settings()
        store = Storage(":memory:")
        fake = FakePortals(error=PortalsError("server unavailable"))
        self.assertFalse(cycle_once(cfg, store, FakeTelegram(), fake))
        self.assertEqual(store.get_json("cycle")["failures"], 1)

    def test_telegram_failure_reports_failed_cycle(self):
        cfg = make_settings()
        store = Storage(":memory:")
        tg = FakeTelegram(fail={"send": TelegramError("temporary", 500)})
        tg.pending = [{"update_id": 1, "message": {"text": "/id", "chat": {"id": 777, "type": "private"}}}]
        self.assertFalse(cycle_once(cfg, store, tg, FakePortals()))
        self.assertIsNone(store.get("offset"))

    def test_clean_cycle_reports_success(self):
        self.assertTrue(cycle_once(make_settings(), Storage(":memory:"), FakeTelegram(), FakePortals()))

    def test_setup_mode_requires_successful_telegram(self):
        cfg = make_settings(owner_chat_id=0)
        self.assertTrue(cycle_once(cfg, Storage(":memory:"), FakeTelegram(), None))
        tg = FakeTelegram(fail={"send": TelegramError("temporary", 500)})
        tg.pending = [{"update_id": 1, "message": {"text": "/id", "chat": {"id": 777, "type": "private"}}}]
        self.assertFalse(cycle_once(cfg, Storage(":memory:"), tg, None))
