"""Read collection floors from StarPrice. Only compare the same market and currency."""
import html
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from .catalog import fetch_catalog, update_catalog
from .config import Settings
from .service import prefs, limits, threshold
from .subscriptions import PLANS, entitlement, reserve_alert
from .portals import dec, parse_date
from .storage import Storage
from .telegram import TelegramClient

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
            if not isinstance(offer,dict):
                raise ValueError("Некорректное предложение")
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
    drops = []
    for key, row in rows.items():
        old = previous.get(key)
        if old and row["at"] <= old["at"]:
            continue
        if old and now - datetime.fromisoformat(old["at"]) <= MAX_AGE:
            drops.append((key, row, Decimal(old["price"])))
        previous[key] = row
    for user in store.active_users():
        uid = user.chat_id or cfg.owner_chat_id
        name = entitlement(store.db, uid, int(now.timestamp()))[0]
        plan = PLANS[name]
        settings = prefs(store, user.user_key)
        selected = settings.get("favorites", [])[:plan.favorites]
        discount = threshold(store, user, plan)
        changes = []
        for key, row, before in drops:
            collection = row["name"].casefold()
            after = Decimal(row["price"])
            if selected and collection not in selected:
                continue
            if plan.level and (settings.get("market", row["market"]) != row["market"]
                               or not Decimal(settings.get("min","0")) <= after <= Decimal(settings.get("max","Infinity"))):
                continue
            if plan.level >= 2 and any(settings.get(k) for k in ("model","backdrop","symbol")):
                continue  # A collection floor carries no individual gift attributes.
            if plan.level >= 3 and collection in settings.get("exclude", []):
                continue
            if after * 100 <= before * (100 - discount):
                changes.append((key, row, before))
        if changes:
            first, row, _ = changes[0]
            sid = "floor:" + first + ":" + row["at"]
            if reserve_alert(store.db, user.user_key, sid, now.date().isoformat(), limits(store,name)[0]):
                lines = ["Снижение ориентира коллекции · StarPrice"]
                for _, row, before in changes[:15]:
                    lines.append(f"{row['market']} · {html.escape(row['name'])}: {before} → {row['price']} {MARKETS[row['market']]}")
                tg.send(uid, "\n".join(lines))
                store.mark_sent(user.user_key, sid, now)
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
    data = fetch_catalog()
    store = Storage(cfg.db_path)
    try:
        count = refresh(store, TelegramClient(cfg.telegram_token), cfg, data, datetime.now(timezone.utc))
        print(f"Свежих цен коллекций по площадкам: {count}")
    finally:
        store.close()


if __name__ == "__main__":
    main()
