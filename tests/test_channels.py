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

    def test_config_accepts_public_links_and_bare_names(self):
        for raw in ("https://t.me/nftt_traker", "t.me/nftt_traker/", "nftt_traker", "@nftt_traker"):
            self.assertEqual(Settings.from_env({**self.ENV, "FREE_CHANNEL_ID": raw}).free_channel, "@nftt_traker")

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
                        "createChatSubscriptionInviteLink": {"invite_link": INVITE},
                        "setMyDescription": True, "setMyShortDescription": True}[method]
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


PLUS_ID, ULTRA_ID = -1003333333333, -1004444444444
PLUS_INV, ULTRA_INV = "https://t.me/+PlusPlus1234", "https://t.me/+UltraUltra12"


def tiered(listings, **kw):
    return setup(listings, plus_channel=PLUS_ID, ultra_channel=ULTRA_ID, plus_channel_invite=PLUS_INV,
                 ultra_channel_invite=ULTRA_INV, free_channel="@nftt_traker", **kw)


class Tiers(unittest.TestCase):
    def test_signal_reaches_all_paid_tiers_once(self):
        cfg, store, tg, pt = tiered([cheap()])
        rep = monitor.run_cycle(cfg, store, tg, pt, NOW)
        self.assertEqual(rep.channel_posts, 3)
        self.assertCountEqual(chats(tg), [PAID, PLUS_ID, ULTRA_ID])
        ultra = [t for c, t, _ in tg.sent if c == ULTRA_ID][0]
        self.assertIn("Сопоставимых продаж: 3", ultra)
        monitor.run_cycle(cfg, store, tg, pt, NOW + timedelta(minutes=10))
        self.assertEqual(len(chats(tg)), 3)

    def test_plus_daily_cap(self):
        cfg, store, tg, pt = tiered([cheap("60", "a"), cheap("55", "b"), cheap("50", "c")], plus_daily=2)
        monitor.run_cycle(cfg, store, tg, pt, NOW)
        self.assertEqual(chats(tg).count(PLUS_ID), 2)
        self.assertEqual(chats(tg).count(PAID), 3)
        self.assertEqual(chats(tg).count(ULTRA_ID), 3)

    def test_plus_failure_does_not_block_pro(self):
        cfg, store, tg, pt = tiered([cheap()])
        real = tg.send
        def send(chat, text, kb=None):
            if chat == PLUS_ID:
                raise TelegramError("Forbidden", 403)
            real(chat, text, kb)
        tg.send = send
        rep = monitor.run_cycle(cfg, store, tg, pt, NOW)
        self.assertIn(PAID, chats(tg))
        self.assertIn("Forbidden", rep.warning)

    def test_ultra_daily_report(self):
        cfg, store, tg, pt = tiered([cheap()])
        monitor.run_cycle(cfg, store, tg, pt, NOW)
        pt._l, pt._p = [], []
        monitor.run_cycle(cfg, store, tg, pt, NOW + timedelta(days=1))
        reports = [t for c, t, _ in tg.sent if c == ULTRA_ID and "Рынок Portals" in t]
        self.assertEqual(len(reports), 1)
        self.assertIn("Сигналов за день: <b>1</b>", reports[0])

    def test_shop_lists_tiers_for_any_user(self):
        from nft_tracker.bot import handle_update
        from tests.helpers import message
        cfg, store, tg, _ = tiered([])
        handle_update(cfg, store, tg, message("/start", chat=12345), NOW)
        text, kb = tg.sent[-1][1], tg.sent[-1][2]
        for part in ("FREE", "PLUS</b> · 99 ⭐", "PRO</b> · 199 ⭐", "ULTRA</b> · 399 ⭐", "продлевается автоматически"):
            self.assertIn(part, text)
        urls = [row[0]["url"] for row in kb]
        self.assertEqual(urls, ["https://t.me/nftt_traker", PLUS_INV, INVITE, ULTRA_INV])
        handle_update(cfg, store, tg, message("/status", chat=12345, update_id=2), NOW)
        self.assertIn("закрытом режиме", tg.sent[-1][1])

    def test_setup_creates_missing_links_and_description(self):
        from nft_tracker.__main__ import channel_setup
        made = []

        class Tg(FakeTelegram):
            def call(self, method, payload=None, timeout=0):
                if method == "createChatSubscriptionInviteLink":
                    made.append((payload["chat_id"], payload["subscription_price"]))
                    return {"invite_link": f"https://t.me/+Link{len(made)}abcdefg"}
                if method == "setMyDescription":
                    self.description = payload["description"]
                    return True
                return {"getMe": {"id": 9}, "getChat": {"type": "channel"}, "setMyShortDescription": True,
                        "getChatMember": {"status": "administrator", "can_post_messages": True,
                                          "can_invite_users": True}}[method]
        tg = Tg()
        cfg = make_settings(paid_channel=PAID, paid_channel_invite=INVITE, plus_channel=PLUS_ID,
                            ultra_channel=ULTRA_ID, free_channel="@nftt_traker")
        self.assertEqual(channel_setup(cfg, tg), 0)
        self.assertEqual(made, [(PLUS_ID, 99), (ULTRA_ID, 399)])
        self.assertIn("PLUS_CHANNEL_INVITE = https://t.me/+Link1abcdefg", tg.sent[-1][1])
        self.assertIn("ULTRA 399⭐/мес: https://t.me/+Link2abcdefg", tg.description)
        self.assertIn(INVITE, tg.description)


if __name__ == "__main__":
    unittest.main()
