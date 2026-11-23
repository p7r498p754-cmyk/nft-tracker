"""One monitoring cycle: refresh sales history, scan fresh listings, notify."""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from .bot import OWNER
from .config import Settings
from .portals import PortalsClient, PortalsError
from .storage import Storage, User, iso
from .telegram import TelegramClient, TelegramError, alert_keyboard, format_alert
from .valuation import HISTORY_DAYS, MarketEvent, Signal, evaluate

BACKFILL_PAGES = 8      # first run: build sales history (8 requests ~ 10 s)
REFRESH_PAGES = 3       # first 60 new purchases each run
BACKFILL_BATCH_PAGES = 5  # additional older purchases while building history
BACKFILL_START = REFRESH_PAGES * 20
NOTICE_COOLDOWN = timedelta(hours=12)
FAILURES_BEFORE_NOTICE = 3


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class CycleReport:
    listings: int = 0
    candidates: int = 0
    alerts: int = 0
    skipped_no_link: int = 0
    skipped_unverified: int = 0
    new_sales: int = 0
    error: str | None = None      # fatal: cycle could not run
    warning: str | None = None    # non-fatal (e.g. one lot could not be re-checked)


def signal_id(sig: Signal) -> str:
    # Same gift at the same price is one signal, however often it is re-listed.
    # Decimal(60), Decimal(60.0) and Decimal(6E+1) are the same price.
    price = format(sig.event.price.normalize(), "f")
    return f"portals:{sig.event.gift_id}:{price}"


def _notice(store: Storage, tg: TelegramClient, cfg: Settings, key: str, text: str, now: datetime) -> None:
    last = store.get("notice:" + key)
    if last and now - datetime.fromisoformat(last) < NOTICE_COOLDOWN:
        return
    try:
        tg.send(cfg.owner_chat_id, text)
        store.set("notice:" + key, iso(now))
    except TelegramError:
        pass


def _deliver(cfg: Settings, tg: TelegramClient, chat: int, sig: Signal, link: str | None, now: datetime) -> None:
    text = format_alert(sig, cfg.price_unit, now, cfg.timezone, bool(link))
    kb = alert_keyboard(link)
    if sig.event.photo_url and len(text) <= 1024:
        try:
            tg.send_photo(chat, sig.event.photo_url, text, kb)
            return
        except TelegramError as e:
            if e.blocked:
                raise
    tg.send(chat, text, kb)


def run_cycle(cfg: Settings, store: Storage, tg: TelegramClient, portals: PortalsClient,
              now: datetime | None = None) -> CycleReport:
    now = now or utcnow()
    rep = CycleReport()
    try:
        # Refresh newest purchases every cycle. The old implementation stopped
        # backfilling after ANY purchase was stored, so rare gifts could have
        # almost no comparable history for days. Resume a bounded historical
        # backfill across scheduled runs without extra requests forever.
        backfill = getattr(portals, "historical_purchases", None)
        sold = portals.purchases(pages=REFRESH_PAGES if callable(backfill) else
                                 (BACKFILL_PAGES if store.sales_count() == 0 else REFRESH_PAGES))
        extra_sales: list[MarketEvent] = []
        next_backfill_offset: int | None = None
        done = store.get("sales_backfill_done", "0") == "1"
        completed = capped = False
        if callable(backfill) and not done:
            offset = int(store.get("sales_backfill_offset", str(BACKFILL_START)) or BACKFILL_START)
            extra_sales, next_backfill_offset, completed, capped = backfill(
                start_offset=offset, pages=BACKFILL_BATCH_PAGES,
                oldest=now - timedelta(days=HISTORY_DAYS))
        # Persist sales BEFORE their cursor; a crash must not advance past
        # unwritten historical rows. SQLite deduplicates any replayed page.
        rep.new_sales = store.add_sales(sold + extra_sales)
        if next_backfill_offset is not None:
            store.set("sales_backfill_offset", str(next_backfill_offset))
        if completed or capped:
            store.set("sales_backfill_capped", "1" if capped else "0")
            store.set("sales_backfill_done", "1")
        if callable(backfill) and store.get("sales_backfill_capped") == "1":
            rep.warning = ("Глубина истории продаж ограничена Portals (offset ≤ 700). "
                           "Для редких NFT может быть недостаточно сравнений.")
        elif callable(backfill) and not done and store.get("sales_backfill_done") != "1":
            rep.warning = ("История продаж ещё пополняется; оценки редких NFT пока ограничены.")
        since = datetime.fromisoformat(store.get("last_listing_at", iso(now - timedelta(minutes=cfg.max_event_age_min))))
        listings = portals.listings(pages=cfg.listing_pages, since=since)
        if getattr(portals, "last_events_truncated", False):
            msg = ("Лента Portals оказалась длиннее проверенных страниц. "
                   "Часть объявлений могла быть пропущена; увеличьте LISTING_PAGES (макс. 36).")
            rep.warning = (rep.warning + " | " if rep.warning else "") + msg
    except PortalsError as e:
        rep.error = str(e)
        _record(store, tg, cfg, rep, now)
        return rep

    rep.listings = len(listings)
    # A truncated feed has NOT reached the previous checkpoint. Retain it so
    # we can recover missed events if the feed slows down or page depth grows.
    # Also reject future-dated events as checkpoint material: one bad timestamp
    # must not prevent the bot from seeing newly listed items for hours/days.
    complete = not getattr(portals, "last_events_truncated", False)
    checkpoint_events = [e for e in listings if e.event_time <= now]
    # Commit checkpoint only AFTER verification and delivery have succeeded.
    # Losing it on a transient failure would permanently skip some new events
    # if the feed becomes busy before the next scheduled GitHub Actions run.
    checkpoint = max((e.event_time for e in checkpoint_events), default=None)
    retry_needed = False
    sales = store.sales()
    users = store.active_users()
    max_age = timedelta(minutes=cfg.max_event_age_min)
    verified: dict[tuple[str, Decimal], bool] = {}
    no_link_hits = 0
    pending: list[tuple[Signal, User, str | None]] = []
    pending_keys: set[tuple[str, str]] = set()
    check_events: dict[tuple[str, Decimal], MarketEvent] = {}

    # First evaluate all candidates. Verify distinct gifts in batches to save
    # requests (the free Portals tier is limited to 1 request/second).
    for ev in sorted(listings, key=lambda e: e.event_time):
        if now - ev.event_time > max_age or ev.event_time > now + timedelta(minutes=5):
            continue
        for user in users:
            sig = evaluate(ev, sales, user.min_discount, now, cfg.model_extra_margin)
            if sig is None:
                continue
            sid = signal_id(sig)
            if store.was_sent(user.user_key, sid) or (user.user_key, sid) in pending_keys:
                continue
            pending_keys.add((user.user_key, sid))
            rep.candidates += 1
            link = ev.link
            if link is None and not cfg.allow_no_link_alerts:
                rep.skipped_no_link += 1
                no_link_hits += 1
                retry_needed = True
                continue
            pending.append((sig, user, link))
            check_events[(ev.gift_id, ev.price)] = ev

    checks = list(check_events.values())
    verify_many = getattr(portals, "verify_many", None)
    if callable(verify_many):
        # The official search API supports ids[] and returning up to 20 gifts
        # in one request; batches also work when a gift changed price twice.
        for i in range(0, len(checks), 20):
            batch = checks[i:i + 20]
            try:
                verified.update(verify_many(batch))
            except PortalsError as e:
                rep.error = str(e)
                rep.warning = (rep.warning + " | " if rep.warning else "") + str(e)
                retry_needed = True
                # No result -> fail closed. Do not send guesses.
    else:
        # Compatibility with small offline test fakes and older connectors.
        for ev in checks:
            k = (ev.gift_id, ev.price)
            try:
                verified[k] = portals.still_listed_at(ev)
            except PortalsError as e:
                rep.error = str(e)
                rep.warning = (rep.warning + " | " if rep.warning else "") + str(e)
                retry_needed = True

    for sig, user, link in pending:
        ev = sig.event
        if not verified.get((ev.gift_id, ev.price), False):
            rep.skipped_unverified += 1
            continue
        sid = signal_id(sig)
        chat = user.chat_id if user.chat_id is not None else cfg.owner_chat_id
        try:
            _deliver(cfg, tg, chat, sig, link, now)
        except TelegramError as e:
            if e.blocked:
                store.set_active(user.user_key, False)
            else:
                rep.error = str(e)
                retry_needed = True
            continue
        store.mark_sent(user.user_key, sid, now)
        rep.alerts += 1

    if complete and not retry_needed and checkpoint is not None:
        store.set("last_listing_at", iso(checkpoint))

    if no_link_hits:
        _notice(store, tg, cfg, "no_link",
                f"ℹ️ Найдено сигналов: {no_link_hits}, но прямая ссылка на лот не настроена, поэтому они не отправлены. "
                "Как это исправить — в README (раздел «Прямая ссылка»).", now)
    store.prune(now)
    _record(store, tg, cfg, rep, now)
    return rep


def _record(store: Storage, tg: TelegramClient, cfg: Settings, rep: CycleReport, now: datetime) -> None:
    meta = store.get_json("cycle")
    if rep.error:
        failures = int(meta.get("failures", 0)) + 1
        meta.update(error=rep.error, failures=failures)
        if failures >= FAILURES_BEFORE_NOTICE:
            _notice(store, tg, cfg, "portals_error",
                    f"⚠️ Сбой мониторинга уже {failures} проверок подряд: {rep.error}", now)
    else:
        meta.update(error=None, failures=0, ok_at=iso(now), listings=rep.listings, alerts=rep.alerts)
    meta["warning"] = rep.warning
    store.set("cycle", json.dumps(meta))
