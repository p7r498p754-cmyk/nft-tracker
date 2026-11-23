import unittest

from nft_tracker import links
from nft_tracker.config import ConfigError, Settings
from nft_tracker.redact import RedactFilter

ENV = {"TELEGRAM_BOT_TOKEN": "123456:ABCDEFGHIJKLMNOPQRSTUVWXYZ012345", "PORTALS_API_KEY": "k" * 12,
       "OWNER_CHAT_ID": "777"}


class Links(unittest.TestCase):
    def test_bad_links_rejected(self):
        for bad in ["http://t.me/x/y", "https://evil.com/item/7", "https://t.me.evil.com/x/y", "https://t.me/",
                    "https://u:p@t.me/x", "https://127.0.0.1/x", "javascript:alert(1)", "https://t.me/x y",
                    "https://t.me:8443/x", "https://t.me/x#frag", None, 5, "", "https://t.me/" + "a" * 3000]:
            self.assertIsNone(links.safe_link(bad), bad)

    def test_good_link(self):
        self.assertEqual(links.safe_link("https://t.me/bot/app?startapp=1"), "https://t.me/bot/app?startapp=1")

    def test_template_builds_and_quotes_id(self):
        out = links.build_listing_link("https://t.me/b/a?startapp=x_{id}", "a b/../c")
        self.assertEqual(out, "https://t.me/b/a?startapp=x_a%20b%2F..%2Fc")

    def test_template_validation(self):
        links.validate_template("https://t.me/b/a?startapp=x_{id}")
        for bad in ["https://t.me/b/a", "https://t.me/b/{id}/{id}", "https://evil.com/{id}", "http://t.me/b/{id}",
                    "https://t.me/{id}"[:0] + "https://t.me/"]:
            with self.assertRaises(ConfigError):
                links.validate_template(bad)

    def test_image_hosts(self):
        self.assertIsNotNone(links.safe_image_url("https://nft.fragment.com/gift/x.jpg"))
        self.assertIsNone(links.safe_image_url("https://evil.com/x.jpg"))
        self.assertIsNone(links.safe_image_url("http://nft.fragment.com/x.jpg"))
        self.assertIsNone(links.safe_image_url("https://notfragment.com/x.jpg"))


class Config(unittest.TestCase):
    def test_defaults(self):
        s = Settings.from_env(ENV)
        self.assertEqual((s.owner_chat_id, s.default_discount, s.allow_no_link_alerts), (777, 30, False))

    def test_missing_secrets(self):
        for drop in ("TELEGRAM_BOT_TOKEN", "PORTALS_API_KEY", "OWNER_CHAT_ID"):
            env = {k: v for k, v in ENV.items() if k != drop}
            with self.assertRaises(ConfigError):
                Settings.from_env(env)

    def test_owner_must_be_numeric(self):
        with self.assertRaises(ConfigError):
            Settings.from_env({**ENV, "OWNER_CHAT_ID": "@name"})

    def test_discount_floor_is_30(self):
        with self.assertRaises(ConfigError):
            Settings.from_env({**ENV, "DEFAULT_DISCOUNT": "10"})

    def test_bad_template_rejected(self):
        with self.assertRaises(ConfigError):
            Settings.from_env({**ENV, "PORTALS_LISTING_URL_TEMPLATE": "https://evil.com/{id}"})

    def test_listing_pages_bounds(self):
        self.assertEqual(Settings.from_env(ENV).listing_pages, 12)
        self.assertEqual(Settings.from_env({**ENV, "LISTING_PAGES": "36"}).listing_pages, 36)
        for val in ("0", "37", "spam"):
            with self.assertRaises(ConfigError):
                Settings.from_env({**ENV, "LISTING_PAGES": val})

    def test_actions_detected(self):
        self.assertTrue(Settings.from_env({**ENV, "GITHUB_ACTIONS": "true"}).on_actions)


class Redaction(unittest.TestCase):
    def test_secrets_scrubbed(self):
        f = RedactFilter([ENV["TELEGRAM_BOT_TOKEN"], "portals-secret-key"])
        out = f.scrub("fail https://api.telegram.org/bot123456:ABCDEFGHIJKLMNOPQRSTUVWXYZ012345/getMe "
                      "Authorization: partners portals-secret-key")
        self.assertNotIn("ABCDEFGHIJ", out)
        self.assertNotIn("portals-secret-key", out)


if __name__ == "__main__":
    unittest.main()


class SetupMode(unittest.TestCase):
    def test_owner_zero_needs_no_portals_key_and_is_setup_mode(self):
        env = {"TELEGRAM_BOT_TOKEN": ENV["TELEGRAM_BOT_TOKEN"], "OWNER_CHAT_ID": "0"}
        s = Settings.from_env(env)
        self.assertTrue(s.setup_mode)

    def test_setup_mode_cli_does_not_need_portals_client(self):
        from pathlib import Path
        from tempfile import TemporaryDirectory
        from unittest.mock import patch
        from nft_tracker.__main__ import _make, cycle_once
        from nft_tracker.telegram import TelegramClient
        with TemporaryDirectory() as d:
            cfg = Settings.from_env({"TELEGRAM_BOT_TOKEN": ENV["TELEGRAM_BOT_TOKEN"],
                                     "OWNER_CHAT_ID": "0", "DB_PATH": str(Path(d) / "db.sqlite")})
            store, _tg, pt = _make(cfg)
            self.assertIsNone(pt)
            from tests.helpers import FakeTelegram, NOW
            try:
                cycle_once(cfg, store, FakeTelegram(), pt)
            finally:
                store.close()

    def test_setup_mode_only_answers_id(self):
        from nft_tracker import bot
        from nft_tracker.storage import Storage
        from tests.helpers import NOW, FakeTelegram, make_settings, message
        cfg, store = make_settings(owner_chat_id=0), Storage(":memory:")
        tg = FakeTelegram([message("/id", chat=555, update_id=1), message("/start", chat=555, update_id=2)])
        bot.process_updates(cfg, store, tg, NOW)
        self.assertIn("555", tg.sent[0][1])
        self.assertIn("закрытом", tg.sent[1][1])
        self.assertIsNone(store.user("owner"))
