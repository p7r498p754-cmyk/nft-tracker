import unittest
from datetime import timedelta

from nft_tracker import channels, monitor
from nft_tracker.config import ConfigError, Settings
from nft_tracker.storage import Storage
from nft_tracker.telegram import TelegramError
from tests.helpers import NOW, OWNER, FakePortals, FakeTelegram, ev, make_settings, sales

LINK = "https://t.me/test_bot/app?startapp=item_g1"
INVITE = "https://t.me/+AbCdEfGh12345"
PAID, FREE = -1001111111111, -1002222222222
GOOD_SALES = sales(["100", "100", "100"])


def cheap(price="60", gift="g1", minutes=5):
    return ev(price=price, gift_id=gift, when=NOW - timedelta(minutes=minutes), link=LINK)


class BatchPortals(FakePortals):
    def verify_many(self, batch):
        self.verify_calls += 1
        return {(e.gift_id, e.price): (self.listed(e) if callable(self.listed) else self.listed) for e in batch}


def setup(listings, owner=False, **cfg_kw):
    base = dict(paid_channel=PAID, free_channel=FREE, paid_channel_invite=INVITE, free_delay_min=60)
    base.update(cfg_kw)
    cfg = make_settings(**base)
    store = Storage(":memory:")
    if owner:
        store.ensure_user("owner", None, 30, NOW)
    return cfg, store, FakeTelegram(), BatchPortals(purchases=GOOD_SALES, listings=listings)


def chats(tg):
    return [c for c, *_ in tg.sent] + [c for c, *_ in tg.photos]


class PaidChannel(unittest.TestCase):
    def test_signal_goes_to_paid_channel_once(self):
        cfg, store, tg, pt = setup([cheap()])
        rep = monitor.run_cycle(cfg, store, tg, pt, NOW)
        self.assertEqual(rep.channel_posts, 1)
        self.assertEqual(chats(tg), [PAID])
        self.assertEqual(tg.sent[0][2][0][0]["url"], LINK)
        rep = monitor.run_cycle(cfg, store, tg, pt, NOW + timedelta(minutes=10))
        self.assertEqual(rep.channel_posts, 0)
        self.assertEqual(chats(tg), [PAID])

    def test_owner_and_channel_both_receive(self):
        cfg, store, tg, pt = setup([cheap()], owner=True)
        monitor.run_cycle(cfg, store, tg, pt, NOW)
        self.assertCountEqual(chats(tg), [PAID, OWNER])
        self.assertEqual(pt.verify_calls, 1)  # one batch for both recipients

    def test_unverified_lot_not_posted(self):
        cfg, store, tg, pt = setup([cheap()])
        pt.listed = False
        rep = monitor.run_cycle(cfg, store, tg, pt, NOW)
        self.assertEqual((rep.channel_posts, rep.skipped_unverified), (0, 1))
        self.assertEqual(chats(tg), [])

    def test_channel_threshold_respected(self):
        cfg, store, tg, pt = setup([cheap(price="80")], channel_discount=30)
        rep = monitor.run_cycle(cfg, store, tg, pt, NOW)
        self.assertEqual(rep.channel_posts, 0)

    def test_disabled_without_paid_channel(self):
        cfg, store, tg, pt = setup([cheap()], paid_channel=0)
        rep = monitor.run_cycle(cfg, store, tg, pt, NOW)
        self.assertEqual((rep.channel_posts, chats(tg)), (0, []))

    def test_definite_rejection_retries_and_notifies_owner(self):
        cfg, store, tg, pt = setup([cheap()])
        tg.fail["send"] = TelegramError("Forbidden", 403)
        rep = monitor.run_cycle(cfg, store, tg, pt, NOW)
        self.assertEqual(rep.channel_posts, 0)
        self.assertTrue(rep.error)
        self.assertFalse(channels.already_posted(store, "portals:g1:60"))
        tg.fail.clear()
        rep = monitor.run_cycle(cfg, store, tg, pt, NOW + timedelta(minutes=1))
        self.assertEqual(rep.channel_posts, 1)

    def test_ambiguous_failure_never_duplicates(self):
        cfg, store, tg, pt = setup([cheap()])
        tg.fail["send"] = TelegramError("network")
        monitor.run_cycle(cfg, store, tg, pt, NOW)
        tg.fail.clear()
        rep = monitor.run_cycle(cfg, store, tg, pt, NOW + timedelta(minutes=1))
        self.assertEqual(rep.channel_posts, 0)
        self.assertEqual(chats(tg), [])


class FreeChannel(unittest.TestCase):
    def test_teaser_after_delay_with_status_and_cta(self):
        cfg, store, tg, pt = setup([cheap()])
        monitor.run_cycle(cfg, store, tg, pt, NOW)
        pt._l = []
        rep = monitor.run_cycle(cfg, store, tg, pt, NOW + timedelta(minutes=30))
        self.assertEqual(rep.teasers, 0)
        pt.listed = False
        rep = monitor.run_cycle(cfg, store, tg, pt, NOW + timedelta(minutes=61))
        self.assertEqual(rep.teasers, 1)
        chat, text, kb = tg.sent[-1]
        self.assertEqual(chat, FREE)
        self.assertIn("задержкой 61 мин", text)
        self.assertIn("уже недоступен", text)
        self.assertEqual(kb, [[{"text": "⭐ Сигналы сразу, без задержки", "url": INVITE}]])
        rep = monitor.run_cycle(cfg, store, tg, pt, NOW + timedelta(minutes=80))
        self.assertEqual(rep.teasers, 0)

    def test_still_listed_teaser_keeps_lot_link(self):
        cfg, store, tg, pt = setup([cheap()], free_delay_min=0)
        monitor.run_cycle(cfg, store, tg, pt, NOW)
        pt._l = []
        monitor.run_cycle(cfg, store, tg, pt, NOW + timedelta(minutes=1))
        chat, text, kb = tg.sent[-1]
        self.assertEqual(chat, FREE)
        self.assertIn("всё ещё продаётся", text)
        self.assertEqual(kb[0][0]["url"], LINK)

    def test_daily_quota_picks_best_discounts(self):
        cfg, store, tg, pt = setup([cheap("60", "a"), cheap("50", "b"), cheap("65", "c")], free_max_per_day=2)
        monitor.run_cycle(cfg, store, tg, pt, NOW)
        pt._l = []
        monitor.run_cycle(cfg, store, tg, pt, NOW + timedelta(minutes=61))
        teasers = [t for c, t, _ in tg.sent if c == FREE]
        self.assertEqual(len(teasers), 2)
        self.assertIn("50 GRAM", teasers[0])
        self.assertIn("60 GRAM", teasers[1])
        monitor.run_cycle(cfg, store, tg, pt, NOW + timedelta(minutes=90))
        self.assertEqual(len([c for c, *_ in tg.sent if c == FREE]), 2)

    def test_stale_signals_are_not_teased(self):
        cfg, store, tg, pt = setup([cheap()])
        monitor.run_cycle(cfg, store, tg, pt, NOW)
        pt._l = []
        rep = monitor.run_cycle(cfg, store, tg, pt, NOW + timedelta(hours=5))
        self.assertEqual(rep.teasers, 0)

    def test_daily_stats_posted_once(self):
        cfg, store, tg, pt = setup([cheap("60", "a"), cheap("50", "b")])
        monitor.run_cycle(cfg, store, tg, pt, NOW)
        pt._l = []
        monitor.run_cycle(cfg, store, tg, pt, NOW + timedelta(days=1))
        stats = [t for c, t, _ in tg.sent if c == FREE and "Итоги" in t]
        self.assertEqual(len(stats), 1)
        self.assertIn("Сигналов в платном канале: <b>2</b>", stats[0])
        self.assertIn("Лучшая скидка: <b>50%</b>", stats[0])
        monitor.run_cycle(cfg, store, tg, pt, NOW + timedelta(days=1, minutes=10))
        self.assertEqual(len([t for c, t, _ in tg.sent if "Итоги" in t]), 1)


class SetupAndConfig(unittest.TestCase):
    ENV = {"TELEGRAM_BOT_TOKEN": "1:x", "OWNER_CHAT_ID": "5", "PORTALS_API_KEY": "k"}

    def test_config_parses_channels(self):
        cfg = Settings.from_env({**self.ENV, "PAID_CHANNEL_ID": str(PAID), "FREE_CHANNEL_ID": "@nft_free_test",
                                 "PAID_CHANNEL_INVITE": INVITE, "CHANNEL_PRICE": "300"})
        self.assertEqual((cfg.paid_channel, cfg.free_channel, cfg.channel_price), (PAID, "@nft_free_test", 300))

    def test_config_rejects_bad_values(self):
        for k, v in (("PAID_CHANNEL_ID", "abc"), ("PAID_CHANNEL_INVITE", "http://evil"), ("CHANNEL_PRICE", "0")):
            with self.assertRaises(ConfigError):
                Settings.from_env({**self.ENV, k: v})

    def test_setup_creates_link_and_sends_only_to_owner(self):
        from nft_tracker.__main__ import channel_setup

        class Tg(FakeTelegram):
            def call(self, method, payload=None, timeout=0):
                self.calls = getattr(self, "calls", []) + [method]
                return {"getMe": {"id": 9},
                        "getChat": {"type": "channel"},
                        "getChatMember": {"status": "administrator", "can_post_messages": True,
                                          "can_invite_users": True},
                        "createChatSubscriptionInviteLink": {"invite_link": INVITE}}[method]
        tg = Tg()
        cfg = make_settings(paid_channel=PAID, free_channel=FREE)
        self.assertEqual(channel_setup(cfg, tg), 0)
        self.assertIn("createChatSubscriptionInviteLink", tg.calls)
        self.assertEqual(tg.sent[-1][0], OWNER)
        self.assertIn(INVITE, tg.sent[-1][1])

    def test_setup_reports_missing_rights(self):
        from nft_tracker.__main__ import channel_setup

        class Tg(FakeTelegram):
            def call(self, method, payload=None, timeout=0):
                return {"getMe": {"id": 9}, "getChat": {"type": "channel"},
                        "getChatMember": {"status": "member"}}[method]
        tg = Tg()
        self.assertEqual(channel_setup(make_settings(paid_channel=PAID), tg), 1)
        self.assertIn("не администратор", tg.sent[-1][1])

    def test_forwarded_channel_post_reveals_id_to_owner(self):
        from nft_tracker.bot import handle_update
        cfg, store = make_settings(), Storage(":memory:")
        tg = FakeTelegram()
        upd = {"update_id": 1, "message": {"from": {"id": OWNER}, "chat": {"id": OWNER, "type": "private"},
               "forward_origin": {"type": "channel", "chat": {"id": PAID, "title": "Сигналы"}}}}
        handle_update(cfg, store, tg, upd, NOW)
        self.assertIn(str(PAID), tg.sent[-1][1])


if __name__ == "__main__":
    unittest.main()
