"""Fakes for offline tests. Nothing here touches the network."""
from __future__ import annotations

import io
import json
import urllib.error
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from nft_tracker.config import Settings
from nft_tracker.telegram import TelegramClient, TelegramError
from nft_tracker.valuation import MarketEvent

NOW = datetime(2026, 11, 23, 12, 0, tzinfo=timezone.utc)
OWNER = 777
LINK_TEMPLATE = "https://t.me/test_bot/app?startapp=item_{id}"  # fictional, tests only


def make_settings(**kw) -> Settings:
    base = dict(telegram_token="123456:ABCDEFGHIJKLMNOPQRSTUVWXYZ012345", portals_key="portals-secret-key",
                owner_chat_id=OWNER, db_path=Path(":memory:"), listing_url_template=LINK_TEMPLATE)
    base.update(kw)
    return Settings(**base)


def ev(kind="listing", price="70", gift_id="g1", name="Plush Pepe", model="Cool", backdrop="Blue",
       symbol="Star", when=NOW, link=None, photo=None, number="42") -> MarketEvent:
    return MarketEvent("portals", gift_id, name, model, backdrop, symbol, Decimal(price), when, kind,
                       number, link, photo)


def sales(prices, **kw):
    return [ev("purchase", p, gift_id=f"s{i}", when=NOW - timedelta(hours=i + 1), **kw)
            for i, p in enumerate(prices)]


class FakeTelegram(TelegramClient):
    """Records calls; can simulate failures per method."""
    def __init__(self, updates=None, fail=None):
        self.sent, self.photos, self.edits, self.callbacks = [], [], [], []
        self.pending = list(updates or [])
        self.fail = fail or {}

    def _maybe_fail(self, name):
        err = self.fail.get(name)
        if err:
            raise err

    def send(self, chat_id, text, keyboard=None):
        self._maybe_fail("send")
        self.sent.append((chat_id, text, keyboard))

    def send_photo(self, chat_id, photo, caption, keyboard=None):
        self._maybe_fail("photo")
        self.photos.append((chat_id, photo, caption, keyboard))

    def edit(self, chat_id, message_id, text, keyboard=None):
        self._maybe_fail("edit")
        self.edits.append((chat_id, message_id, text, keyboard))

    def answer_callback(self, callback_id):
        self.callbacks.append(callback_id)

    def updates(self, offset, timeout=0):
        out = [u for u in self.pending if u["update_id"] >= offset]
        return out


class FakePortals:
    def __init__(self, purchases=None, listings=None, listed=True, error=None):
        self._p, self._l, self.listed, self.error = purchases or [], listings or [], listed, error
        self.verify_calls = 0

    def purchases(self, **kw):
        if self.error:
            raise self.error
        return list(self._p)

    def listings(self, **kw):
        if self.error:
            raise self.error
        return list(self._l)

    def still_listed_at(self, event):
        self.verify_calls += 1
        return self.listed(event) if callable(self.listed) else self.listed


class FakeResponse(io.BytesIO):
    def __enter__(self): return self
    def __exit__(self, *a): return False


def json_response(obj):
    return FakeResponse(json.dumps(obj).encode())


def http_error(code, body=b"", headers=None):
    return urllib.error.HTTPError("https://x", code, "err", headers or {}, io.BytesIO(body))


def message(text, chat=OWNER, update_id=1, kind="private"):
    return {"update_id": update_id, "message": {"text": text, "chat": {"id": chat, "type": kind}}}


def callback(data, chat=OWNER, update_id=1, sender=None):
    return {"update_id": update_id, "callback_query": {"id": "cb1", "data": data, "from": {"id": sender or chat},
            "message": {"message_id": 5, "chat": {"id": chat, "type": "private"}}}}
