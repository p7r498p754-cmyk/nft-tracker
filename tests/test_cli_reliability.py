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

    def test_menu_listener_processes_pending_start(self):
        from nft_tracker.__main__ import listen_for_commands
        from tests.helpers import OWNER
        cfg = make_settings()
        store = Storage(":memory:")
        self.addCleanup(store.close)
        tg = FakeTelegram()
        tg.pending = [{"update_id": 7, "message": {"text": "/start", "chat": {"id": OWNER, "type": "private"}}}]
        with patch("nft_tracker.__main__.time.monotonic", side_effect=[0, 1, 91]):
            self.assertTrue(listen_for_commands(cfg, store, tg, 90))
        self.assertEqual(store.get("offset"), "8")
        self.assertTrue(store.user("owner").active)

    def test_loop_refreshes_floors_and_subtracts_scan_time(self):
        from nft_tracker.__main__ import run_loop
        cfg = make_settings(scan_seconds=300)
        with patch("nft_tracker.__main__.time.monotonic", side_effect=[10, 45, 50]), \
             patch("nft_tracker.__main__.cycle_once"), \
             patch("nft_tracker.__main__.fetch_catalog", return_value={"gifts": []}), \
             patch("nft_tracker.__main__.refresh") as refresh, \
             patch("nft_tracker.__main__.listen_for_commands", return_value=False) as listen:
            self.assertEqual(run_loop(cfg, None, None, None), 1)
        refresh.assert_called_once()
        self.assertEqual(listen.call_args.args[-1], 260)
