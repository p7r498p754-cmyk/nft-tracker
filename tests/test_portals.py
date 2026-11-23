import json
import unittest
import urllib.error
from datetime import datetime, timezone
from decimal import Decimal

from nft_tracker import portals as p
from tests.helpers import LINK_TEMPLATE, http_error, json_response

RAW = {"type": "listing", "amount": "12.50", "created_at": "2026-11-23T10:00:00Z",
       "nft": {"id": "gift-1", "name": "Cool Gift", "external_collection_number": 77,
               "photo_url": "https://nft.fragment.com/gift/x.jpg",
               "attributes": [{"type": "model", "value": "Pepe"}, {"type": "backdrop", "value": "Black"},
                              {"type": "symbol", "value": "Heart"}]}}


class Parsing(unittest.TestCase):
    def test_documented_shape(self):
        e = p.parse_event(RAW)
        self.assertEqual((e.price, e.model, e.backdrop, e.symbol, e.number), (Decimal("12.50"), "Pepe", "Black", "Heart", "77"))
        self.assertIsNone(e.link)                         # no link is invented
        self.assertTrue(e.photo_url)

    def test_template_link_only_when_configured(self):
        e = p.parse_event(RAW, LINK_TEMPLATE)
        self.assertEqual(e.link, "https://t.me/test_bot/app?startapp=item_gift-1")

    def test_api_supplied_link_used_if_safe(self):
        raw = json.loads(json.dumps(RAW))
        raw["nft"]["listing_url"] = "https://t.me/portals/market?startapp=abc"
        self.assertEqual(p.parse_event(raw).link, "https://t.me/portals/market?startapp=abc")
        raw["nft"]["listing_url"] = "https://evil.com/x"
        self.assertIsNone(p.parse_event(raw).link)

    def test_buy_is_normalised_to_purchase(self):
        self.assertEqual(p.parse_event({**RAW, "type": "buy"}).event_type, "purchase")

    def test_garbage_rejected(self):
        for bad in [None, [], {}, {"nft": 1}, {**RAW, "amount": "-1"}, {**RAW, "amount": "nan"},
                    {**RAW, "amount": True}, {**RAW, "created_at": "yesterday"}, {**RAW, "type": "offer"},
                    {**RAW, "created_at": "2026-11-23T10:00:00"}, {**RAW, "nft": {**RAW["nft"], "id": ""}}]:
            self.assertIsNone(p.parse_event(bad), bad)


def client(responses, **kw):
    calls, sleeps = [], []
    queue = list(responses)

    def opener(req, timeout=None):
        calls.append(req)
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item
    c = p.PortalsClient("secret-key-123", opener=opener, sleep=sleeps.append, clock=lambda: 1000.0 + len(calls) * 0, **kw)
    return c, calls, sleeps


class ClientTests(unittest.TestCase):
    def test_auth_header_and_user_agent(self):
        c, calls, _ = client([json_response({"actions": []})])
        c.get("/market/actions/", {"limit": 1})
        self.assertEqual(calls[0].get_header("Authorization"), "Partners secret-key-123".replace("Partners", "partners"))
        self.assertIn("NFTTracker", calls[0].get_header("User-agent"))
        self.assertTrue(calls[0].full_url.startswith("https://portal-market.com/partners/market/actions/?"))

    def test_throttle_enforces_gap(self):
        t = [0.0]
        sleeps = []
        def clock(): return t[0]
        def sleep(s): sleeps.append(s); t[0] += s
        c = p.PortalsClient("k", opener=lambda r, timeout=None: json_response({}), sleep=sleep, clock=clock)
        c._last = 0.0
        c.get("/a")
        self.assertTrue(sleeps and sleeps[0] >= 1.1 - 1e-9)

    def test_429_retries_then_succeeds(self):
        c, calls, sleeps = client([http_error(429, headers={"Retry-After": "3"}), json_response({"ok": 1})])
        self.assertEqual(c.get("/x"), {"ok": 1})
        self.assertIn(3, sleeps)

    def test_429_with_huge_retry_after_aborts(self):
        c, _, _ = client([http_error(429, headers={"Retry-After": "3600"})])
        with self.assertRaises(p.PortalsError):
            c.get("/x")

    def test_cloudflare_1010_reported_without_bypass(self):
        c, calls, _ = client([http_error(403, b"error code: 1010")])
        with self.assertRaises(p.PortalsError) as cm:
            c.get("/x")
        self.assertIn("Cloudflare", str(cm.exception))
        self.assertEqual(len(calls), 1)  # no retries / no header tricks

    def test_401_message_has_no_secret(self):
        c, _, _ = client([http_error(401)])
        with self.assertRaises(p.PortalsError) as cm:
            c.get("/x")
        self.assertNotIn("secret-key-123", str(cm.exception))

    def test_network_error_retries_then_fails(self):
        c, calls, _ = client([urllib.error.URLError("boom")] * 3)
        with self.assertRaises(p.PortalsError):
            c.get("/x")
        self.assertEqual(len(calls), 3)

    def test_non_dict_json_rejected(self):
        c, _, _ = client([json_response([1, 2])])
        with self.assertRaises(p.PortalsError):
            c.get("/x")

    def test_events_paginates_and_stops_on_short_page(self):
        page = [{**RAW, "nft": {**RAW["nft"], "id": f"item-{i}"}} for i in range(20)]
        more = [{**RAW, "nft": {**RAW["nft"], "id": f"item-{i}"}} for i in range(20, 23)]
        c, calls, _ = client([json_response({"actions": page}), json_response({"actions": more})])
        self.assertEqual(len(c.events(["listing"], pages=5)), 23)
        self.assertEqual(len(calls), 2)

    def test_events_deduplicate_across_pages(self):
        page = [RAW] * 20
        c, calls, _ = client([json_response({"actions": page}), json_response({"actions": [RAW] * 3})])
        self.assertEqual(len(c.events(["listing"], pages=5)), 1)
        self.assertEqual(len(calls), 2)

    def test_truncated_feed_detected_when_checkpoint_unreached(self):
        from datetime import timedelta
        page = [{**RAW, "nft": {**RAW["nft"], "id": f"id-{i}"}} for i in range(20)]
        c, calls, _ = client([json_response({"actions": page})])
        c.events(["listing"], pages=1, since=datetime(2026, 11, 22, tzinfo=timezone.utc))
        self.assertTrue(c.last_events_truncated)
        self.assertEqual(len(calls), 1)

    def test_malformed_known_market_action_does_not_silently_advance_checkpoint(self):
        good = {**RAW, "created_at": "2026-11-24T10:00:00Z"}
        bad_old = {"type": "listing", "created_at": "2026-11-21T10:00:00Z", "amount": "0", "nft": {}}
        page = [good] * 19 + [bad_old]
        c, calls, _ = client([json_response({"actions": page})])
        with self.assertRaises(p.PortalsError):
            c.events(["listing"], pages=5, since=datetime(2026, 11, 22, tzinfo=timezone.utc))
        self.assertEqual(len(calls), 1)

    def test_events_stops_at_since(self):
        old = {**RAW, "created_at": "2026-11-22T10:00:00Z"}
        c, calls, _ = client([json_response({"actions": [old] * 20})])
        since = datetime(2026, 11, 23, tzinfo=timezone.utc)
        c.events(["listing"], pages=5, since=since)
        self.assertEqual(len(calls), 1)

    def test_missing_actions_list_is_error(self):
        c, _, _ = client([json_response({"nope": []})])
        with self.assertRaises(p.PortalsError):
            c.events(["listing"])

    def test_still_listed_requires_status_and_exact_price(self):
        e = p.parse_event(RAW)
        for body, expected in [({"results": [{"id": "gift-1", "status": "listed", "price": "12.5"}]}, True),
                               ({"results": [{"id": "gift-1", "status": "listed", "price": "13"}]}, False),
                               ({"results": [{"id": "gift-1", "status": "sold", "price": "12.5"}]}, False),
                               ({"results": []}, False), ({"results": [{"id": "other", "status": "listed", "price": "12.5"}]}, False),
                               ]:
            c, _, _ = client([json_response(body)])
            self.assertEqual(c.still_listed_at(e), expected, body)

    def test_invalid_verification_schema_raises_visible_error(self):
        c, _, _ = client([json_response({"oops": 1})])
        with self.assertRaises(p.PortalsError):
            c.still_listed_at(p.parse_event(RAW))

    def test_verify_many_different_gifts_and_prices_single_request(self):
        e1 = p.parse_event(RAW)
        e2 = p.parse_event({**RAW, "amount": "10.0", "nft": {**RAW["nft"], "id": "gift-2"}})
        e3 = p.parse_event({**RAW, "amount": "8.0"})
        c, calls, _ = client([json_response({"results": [
            {"id": "gift-1", "status": "listed", "price": "12.5"},
            {"id": "gift-2", "status": "unlisted", "price": "10"},
        ]})])
        verdict = c.verify_many([e1, e2, e3])
        self.assertEqual(verdict, {("gift-1", Decimal("12.5")): True,
                                   ("gift-2", Decimal("10")): False,
                                   ("gift-1", Decimal("8")): False})
        self.assertEqual(len(calls), 1)
        self.assertIn("ids=gift-1", calls[0].full_url)
        self.assertIn("ids=gift-2", calls[0].full_url)

    def test_only_get_requests(self):
        c, calls, _ = client([json_response({})])
        c.get("/x")
        self.assertEqual(calls[0].get_method(), "GET")


if __name__ == "__main__":
    unittest.main()
