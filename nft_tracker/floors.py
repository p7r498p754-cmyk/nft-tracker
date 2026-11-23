"""Read collection floors from StarPrice. Only compare the same market and currency."""
import html
import json
import urllib.request
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from .catalog import update_catalog
from .config import Settings
from .portals import dec, parse_date
from .storage import Storage
from .telegram import TelegramClient

URL = "https://starprice.net/api/v1/gifts"
MARKETS = {"telegram": "STARS", "fragment": "GRAM"}
MAX_AGE = timedelta(hours=3)


def observations(data, now):
    gifts = data.get("gifts") if isinstance(data, dict) else None
    if not isinstance(gifts, list) or not gifts:
        raise ValueError("StarPrice: отсутствует каталог")
    result = {}
    for gift in gifts:
        if not isinstance(gift, dict) or not isinstance(gift.get("offers"), list):
            continue
        slug, name = gift.get("slug"), gift.get("name")
        if not isinstance(slug, str) or not slug or not isinstance(name, str) or not name:
            continue
        for offer in gift["offers"]:
            if not isinstance(offer, dict):
                continue
            market = offer.get("market")
            if market not in MARKETS or offer.get("currency") != MARKETS[market]:
                continue
            price, at = dec(offer.get("price")), parse_date(offer.get("updated_at"))
            if price and at and timedelta(0) <= now - at <= MAX_AGE:
                result[f"{market}/{slug}"] = dict(name=name[:100], market=market, price=str(price), at=at.isoformat())
    if not result:
        raise ValueError("StarPrice: нет свежих цен")
    return result


def refresh(store, tg, cfg, data, now):
    rows = observations(data, now)
    update_catalog(store, data, now)
    previous = store.get_json("market_floors")
    user = store.user("owner")
    changes = []
    for key, row in rows.items():
        old = previous.get(key)
        if old and row["at"] <= old["at"]:
            continue
        if user and user.active and old and now - datetime.fromisoformat(old["at"]) <= MAX_AGE:
            before, after = Decimal(old["price"]), Decimal(row["price"])
            if after * 100 <= before * (100 - user.min_discount):
                changes.append(f"{row['market'].capitalize()} · {html.escape(row['name'])}: "
                               f"{before} → {after} {MARKETS[row['market']]}")
        previous[key] = row
    if changes:
        lines = ["Снижение минимальной цены коллекции (StarPrice)", *changes[:15]]
        if len(changes) > 15:
            lines.append(f"Ещё коллекций: {len(changes)-15}")
        tg.send(cfg.owner_chat_id, "\n".join(lines))
    previous = {k: v for k, v in previous.items() if now - datetime.fromisoformat(v["at"]) <= MAX_AGE}
    store.set("market_floors", json.dumps(previous))
    return len(rows)


def floor_status(store, now):
    rows = store.get_json("market_floors").values()
    lines = ["Минимальные цены коллекций · StarPrice"]
    for market, currency in MARKETS.items():
        fresh = [r for r in rows if r["market"] == market and
                 timedelta(0) <= now - datetime.fromisoformat(r["at"]) <= MAX_AGE]
        lines.append(f"{market.capitalize()}: {len(fresh)} коллекций с ценой в {currency} за последние 3 ч.")
        if fresh:
            row = min(fresh, key=lambda r: Decimal(r["price"]))
            lines.append(f"Самая доступная: {html.escape(row['name'])} · {row['price']} {currency}.")
    return "\n".join(lines)


def main():
    cfg = Settings.from_env(need_portals=False)
    if cfg.setup_mode:
        return
    with urllib.request.urlopen(URL, timeout=15) as response:
        data = json.load(response)
    store = Storage(cfg.db_path)
    try:
        count = refresh(store, TelegramClient(cfg.telegram_token), cfg, data, datetime.now(timezone.utc))
        print(f"Свежих цен коллекций по площадкам: {count}")
    finally:
        store.close()


if __name__ == "__main__":
    main()
