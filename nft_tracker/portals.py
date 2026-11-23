"""Read-only Portals Partners API client (GET requests only)."""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Callable

from . import __version__
from .links import build_listing_link, safe_image_url, safe_link
from .valuation import MarketEvent

BASE_URL = "https://portal-market.com/partners"
TIMEOUT = 20
PAGE = 20
MAX_OFFSET = 700  # documented market actions upper bound
KINDS = {"listing", "price_update", "purchase", "buy"}


class PortalsError(RuntimeError):
    """Portals request failed. Message never contains secrets."""


def parse_date(raw: Any) -> datetime | None:
    if not isinstance(raw, str):
        return None
    try:
        ts = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except (ValueError, OverflowError):
        return None
    return ts.astimezone(timezone.utc) if ts.tzinfo else None


def dec(raw: Any) -> Decimal | None:
    if raw is None or isinstance(raw, bool):
        return None
    try:
        num = Decimal(str(raw))
    except (InvalidOperation, ValueError):
        return None
    return num if num.is_finite() and num > 0 else None


def attr(nft: dict, target: str) -> str:
    attrs = nft.get("attributes")
    if isinstance(attrs, list):
        for a in attrs:
            if isinstance(a, dict) and str(a.get("type", "")).casefold() == target:
                return str(a.get("value") or "").strip()[:80]
    flat = nft.get(target)  # some responses expose flat fields
    return str(flat).strip()[:80] if isinstance(flat, (str, int)) and not isinstance(flat, bool) else ""


def parse_event(action: Any, link_template: str = "") -> MarketEvent | None:
    if not isinstance(action, dict) or not isinstance(action.get("nft"), dict):
        return None
    nft = action["nft"]
    gift_id = nft.get("id")
    price = dec(action.get("amount"))
    at = parse_date(action.get("created_at"))
    kind = str(action.get("type") or "")
    name = nft.get("name")
    if (not isinstance(gift_id, str) or not gift_id or price is None or at is None or
            kind not in KINDS or not isinstance(name, str) or not name.strip()):
        return None
    kind = "purchase" if kind == "buy" else kind
    link = (safe_link(nft.get("listing_url")) or safe_link(action.get("listing_url"))
            or build_listing_link(link_template, gift_id))
    number = nft.get("external_collection_number") or nft.get("tg_id") or ""
    return MarketEvent(
        market="portals", gift_id=gift_id, name=name.strip()[:120],
        model=attr(nft, "model"), backdrop=attr(nft, "backdrop"), symbol=attr(nft, "symbol"),
        price=price, event_time=at, event_type=kind, number=str(number)[:20],
        link=link, photo_url=safe_image_url(nft.get("photo_url")))


class PortalsClient:
    def __init__(self, api_key: str, *, link_template: str = "", purchase_action: str = "buy",
                 min_gap: float = 1.15, opener: Callable[..., Any] | None = None,
                 sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic):
        if not api_key:
            raise ValueError("PORTALS_API_KEY is required")
        self._key = api_key
        self.link_template = link_template
        self.purchase_action = purchase_action
        self._gap = max(1.1, min_gap)       # free plan: 1 request per second
        self._open = opener or urllib.request.urlopen
        self._sleep, self._clock = sleep, clock
        self._last = -1e9
        self.requests_made = 0
        # True means newest pages did not reach the previous checkpoint. The
        # scan is incomplete; never silently claim that all events were seen.
        self.last_events_truncated = False

    def _throttle(self) -> None:
        wait = self._gap - (self._clock() - self._last)
        if wait > 0:
            self._sleep(wait)

    def get(self, endpoint: str, query: dict | None = None) -> dict:
        qs = urllib.parse.urlencode(query or {}, doseq=True)
        url = BASE_URL + endpoint + ("?" + qs if qs else "")
        last_error = "неизвестная ошибка"
        for attempt in range(3):
            self._throttle()
            req = urllib.request.Request(url, headers={
                "Accept": "application/json",
                "User-Agent": f"NFTTracker/{__version__} (read-only monitor)"})
            req.add_unredirected_header("Authorization", "partners " + self._key)
            self._last = self._clock()
            self.requests_made += 1
            try:
                with self._open(req, timeout=TIMEOUT) as r:
                    data = json.load(r)
            except urllib.error.HTTPError as e:
                body = ""
                try:
                    body = e.read(300).decode("utf-8", "replace").lower()
                except Exception:  # noqa: BLE001 - body is best-effort only
                    pass
                if e.code == 429 or e.code >= 500:
                    retry = e.headers.get("Retry-After", "") if e.headers else ""
                    delay = int(retry) if retry.isdigit() else 2 ** (attempt + 1)
                    last_error = f"HTTP {e.code}"
                    if delay > 60 or attempt == 2:
                        raise PortalsError(f"Portals: {last_error} (лимит или сбой сервера)") from None
                    self._sleep(delay)
                    continue
                if e.code == 403 and ("1010" in body or "cloudflare" in body):
                    raise PortalsError("Portals: Cloudflare блокирует этот сервер (403/1010). "
                                       "Обход не используется; нужно разрешение от Portals") from None
                if e.code in (401, 403):
                    raise PortalsError(f"Portals: HTTP {e.code}, ключ отклонён или нет доступа") from None
                raise PortalsError(f"Portals: HTTP {e.code}") from None
            except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError):
                last_error = "сеть/некорректный ответ"
                if attempt == 2:
                    raise PortalsError(f"Portals: {last_error}") from None
                self._sleep(2 ** attempt)
                continue
            if not isinstance(data, dict):
                raise PortalsError("Portals: неожиданная форма ответа")
            return data
        raise PortalsError(f"Portals: {last_error}")

    def events(self, actions: list[str], *, pages: int = 3, since: datetime | None = None,
               start_offset: int = 0) -> list[MarketEvent]:
        """Newest-first events from a documented offset; stop at `since` or end of feed."""
        if start_offset < 0 or start_offset > MAX_OFFSET or start_offset % PAGE:
            raise ValueError("start_offset must be a page-aligned offset in 0..700")
        out: list[MarketEvent] = []
        self.last_events_truncated = False
        page_count = max(1, min(pages, (MAX_OFFSET - start_offset) // PAGE + 1))
        reached_checkpoint = False
        expected_types = set(actions)
        if expected_types.intersection({"buy", "purchase"}):
            expected_types.update({"buy", "purchase"})
        for i in range(page_count):
            data = self.get("/market/actions/", {"action_types": actions, "limit": PAGE,
                                                  "offset": start_offset + i * PAGE, "sort_by": "created_at desc"})
            batch = data.get("actions")
            if not isinstance(batch, list):
                raise PortalsError("Portals: в ответе нет списка actions")
            parsed: list[MarketEvent] = []
            for raw in batch:
                item = parse_event(raw, self.link_template)
                if item is None:
                    # An incomplete or changed API schema must not silently
                    # advance the checkpoint and hide potentially cheap lots.
                    if isinstance(raw, dict) and raw.get("type") in expected_types:
                        raise PortalsError("Portals: запись рыночной ленты не распознана; "
                                           "нужна проверка формата API")
                    continue
                parsed.append(item)
            out.extend(parsed)
            # Use timestamps from raw records too: incomplete attributes on an
            # older record must not force another page or hide checkpoint reach.
            times = [t for raw in batch if isinstance(raw, dict)
                     if (t := parse_date(raw.get("created_at"))) is not None]
            if len(batch) < PAGE or (since is not None and times and min(times) <= since):
                reached_checkpoint = True
                break
        if since is not None and not reached_checkpoint:
            self.last_events_truncated = True
        # Offset pagination can overlap while the live market feed changes;
        # never double-process the same event within one cycle.
        seen: set[tuple[str, str, datetime, Decimal]] = set()
        unique: list[MarketEvent] = []
        for e in out:
            key = (e.gift_id, e.event_type, e.event_time, e.price)
            if key not in seen:
                unique.append(e)
                seen.add(key)
        return unique

    def historical_purchases(self, *, start_offset: int, pages: int,
                             oldest: datetime) -> tuple[list[MarketEvent], int, bool, bool]:
        """Bounded incremental backfill, overlapping one page between runs.

        Returns (sales, next_offset, complete, capped). The feed can shift
        between scheduled runs; this is best-effort, not a snapshot of all
        historical sales. 'capped' means the API's 700 offset limit was hit.
        """
        records = self.events([self.purchase_action], pages=pages, since=oldest,
                              start_offset=start_offset)
        complete = not self.last_events_truncated
        last_offset = start_offset + min(pages, (MAX_OFFSET - start_offset) // PAGE + 1) * PAGE
        capped = not complete and last_offset > MAX_OFFSET
        # At a full page boundary resume with a one-page overlap. Duplicates
        # are harmless: SQLite has a stable sale key.
        next_offset = max(PAGE * 3, min(MAX_OFFSET, last_offset - PAGE))
        return [e for e in records if e.event_type == "purchase"], next_offset, complete, capped

    def purchases(self, *, pages: int = 2, since: datetime | None = None) -> list[MarketEvent]:
        return [e for e in self.events([self.purchase_action], pages=pages, since=since)
                if e.event_type == "purchase"]

    def listings(self, *, pages: int = 4, since: datetime | None = None) -> list[MarketEvent]:
        return [e for e in self.events(["listing", "price_update"], pages=pages, since=since)
                if e.event_type in ("listing", "price_update")]

    def verify_many(self, events: list[MarketEvent]) -> dict[tuple[str, Decimal], bool]:
        """Verify up to 20 gift/price pairs in one documented GET /nfts/search.

        Missing gifts, changed prices and non-listed statuses all fail closed.
        Using one GET per batch respects the free API tier and is much cheaper
        than one GET per candidate on busy feeds.
        """
        if not events:
            return {}
        ids = list(dict.fromkeys(event.gift_id for event in events))
        if len(ids) > PAGE:
            raise ValueError(f"verify_many allows at most {PAGE} distinct gifts")
        data = self.get("/nfts/search", {"ids": ids, "status": "listed", "limit": len(ids)})
        results = data.get("results")
        if not isinstance(results, list):
            raise PortalsError("Portals: неверный формат ответа /nfts/search")
        current: dict[str, Decimal | None] = {}
        for nft in results:
            if isinstance(nft, dict) and nft.get("id") in ids and nft.get("status") == "listed":
                current[nft["id"]] = dec(nft.get("price"))
        return {(e.gift_id, e.price): current.get(e.gift_id) == e.price for e in events}

    def still_listed_at(self, event: MarketEvent) -> bool:
        """True only if the item is currently listed at exactly the alerted price."""
        return self.verify_many([event])[(event.gift_id, event.price)]
