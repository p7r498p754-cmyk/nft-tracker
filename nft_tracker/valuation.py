"""Market-value estimation and discount calculation (all Decimal, one currency)."""
from __future__ import annotations

import statistics
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import ROUND_DOWN, Decimal

LISTING_KINDS = frozenset({"listing", "price_update"})
# (tier, minimum matching sales, look-back days)
TIERS = (("exact", 3, 14), ("model", 5, 14))
MAX_DATA_AGE = timedelta(hours=72)   # newest comparable sale must be this fresh
MAX_SPREAD = Decimal("0.6")          # (p75 - p25) / median above this = too noisy
HISTORY_DAYS = 14


@dataclass(frozen=True)
class MarketEvent:
    market: str
    gift_id: str
    name: str
    model: str
    backdrop: str
    symbol: str
    price: Decimal
    event_time: datetime
    event_type: str
    number: str = ""
    link: str | None = None
    photo_url: str | None = None

    def key(self, tier: str) -> tuple[str, ...] | None:
        """Comparison key; None when attributes needed for the tier are missing."""
        n, m, b, s = (x.strip().casefold() for x in (self.name, self.model, self.backdrop, self.symbol))
        if not n or n == "unknown":
            return None
        if tier == "exact":
            return (n, m, b, s) if m and b and s else None
        if tier == "model":
            return (n, m) if m else None
        return None


@dataclass(frozen=True)
class Estimate:
    value: Decimal
    tier: str
    sales: int
    newest: datetime


@dataclass(frozen=True)
class Signal:
    event: MarketEvent
    estimate: Estimate
    discount: Decimal          # exact, unrounded, percent

    @property
    def shown_discount(self) -> Decimal:
        return self.discount.quantize(Decimal("0.1"), rounding=ROUND_DOWN)


def percentile(sorted_values: list[Decimal], q: Decimal) -> Decimal:
    idx = int((Decimal(len(sorted_values) - 1) * q).to_integral_value(rounding=ROUND_DOWN))
    return sorted_values[idx]


def trim_outliers(prices: list[Decimal]) -> list[Decimal]:
    med = statistics.median(prices)
    mad = statistics.median([abs(p - med) for p in prices])
    limit = Decimal("3") * Decimal("1.4826") * mad if mad > 0 else med * Decimal("0.25")
    return [p for p in prices if abs(p - med) <= limit]


def estimate(event: MarketEvent, sales: list[MarketEvent], now: datetime) -> Estimate | None:
    """Median of recent, outlier-trimmed, same-currency completed sales.

    Tiers are tried from most to least specific. Not enough or too noisy or
    stale data -> None (no signal rather than a guessed one)."""
    for tier, min_sales, days in TIERS:
        key = event.key(tier)
        if key is None:
            continue
        window = timedelta(days=days)
        rows = [s for s in sales
                if s.gift_id != event.gift_id and s.key(tier) == key
                and timedelta(0) <= event.event_time - s.event_time <= window]
        if len(rows) < min_sales:
            continue
        # Outlier prices must not count towards the freshness check: a lone
        # recent anomalous sale cannot refresh an otherwise stale market.
        kept_prices = trim_outliers([s.price for s in rows])
        if len(kept_prices) < min_sales:
            continue
        remaining = Counter(kept_prices)
        kept_rows = []
        for sale in rows:
            if remaining[sale.price] > 0:
                kept_rows.append(sale)
                remaining[sale.price] -= 1
        newest = max(s.event_time for s in kept_rows)
        if now - newest > MAX_DATA_AGE:
            continue
        kept = sorted(s.price for s in kept_rows)
        med = statistics.median(kept)
        if med <= 0 or (percentile(kept, Decimal("0.75")) - percentile(kept, Decimal("0.25"))) / med > MAX_SPREAD:
            continue
        return Estimate(med, tier, len(kept), newest)
    return None


def discount_pct(price: Decimal, market: Decimal) -> Decimal:
    """(market - price) / market * 100"""
    return (market - price) / market * 100


def evaluate(event: MarketEvent, sales: list[MarketEvent], threshold: Decimal | int,
             now: datetime, model_margin: Decimal = Decimal("10")) -> Signal | None:
    if event.event_type not in LISTING_KINDS or event.price <= 0:
        return None
    est = estimate(event, sales, now)
    if est is None:
        return None
    pct = discount_pct(event.price, est.value)
    required = Decimal(threshold) + (model_margin if est.tier == "model" else Decimal(0))
    return Signal(event, est, pct) if pct >= required else None
