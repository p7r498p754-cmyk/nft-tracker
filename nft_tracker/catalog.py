"""Periodic snapshot of collection-level prices for the owner's /prices menu."""
import html
import json
import urllib.request
from datetime import timedelta
from decimal import Decimal

from .portals import dec, parse_date

PERIOD = timedelta(days=5)
PAGE_SIZE = 8
CURRENCIES = {"telegram": "STARS", "fragment": "GRAM", "portals": "GRAM"}


def fetch_catalog():
    with urllib.request.urlopen("https://starprice.net/api/v1/gifts", timeout=15) as response:
        return json.load(response)


def update_catalog(store, data, now):
    saved = store.get("price_catalog")
    if saved and now < parse_date(json.loads(saved)["at"]) + PERIOD:
        return
    entries = {}
    gifts = data.get("gifts") if isinstance(data, dict) else None
    if not isinstance(gifts, list) or not gifts or data.get("count", len(gifts)) != len(gifts):
        raise ValueError("StarPrice: неполный каталог")
    for gift in gifts:
        if not isinstance(gift, dict) or not isinstance(gift.get("offers"), list):
            raise ValueError("Некорректная строка каталога")
        name, slug = gift.get("name"), gift.get("slug")
        if not isinstance(name,str) or not name or not isinstance(slug,str) or not slug or slug in entries:
            raise ValueError("StarPrice: некорректная коллекция")
        prices = {}
        for offer in gift.get("offers") or []:
            if not isinstance(offer,dict):
                raise ValueError("Некорректное предложение")
            market = offer.get("market")
            price, at = dec(offer.get("price")), parse_date(offer.get("updated_at"))
            if (market in CURRENCIES and offer.get("currency") == CURRENCIES[market]
                    and price and at and timedelta(0) <= now - at <= timedelta(hours=3)):
                prices[market] = str(price)
        entries[slug] = {"name": name[:100], "prices": prices}
    if not any(row["prices"] for row in entries.values()):
        raise ValueError("StarPrice: отсутствуют свежие цены каталога")
    rows = sorted(entries.values(), key=lambda row: row["name"].casefold())
    snapshot = json.dumps({"at": now.isoformat(), "rows": rows}, ensure_ascii=False)
    with store.db:
        store.db.execute("BEGIN IMMEDIATE")
        old = store.get_json("price_catalog")
        if old:
            if now < parse_date(old["at"]) + PERIOD:
                return
            store.db.execute("INSERT OR IGNORE INTO price_history VALUES (?,?)",
                             (old["at"], json.dumps(old, ensure_ascii=False)))
        store.db.execute("INSERT OR IGNORE INTO price_history VALUES (?,?)", (now.isoformat(), snapshot))
        store.set("price_catalog", snapshot)


def cache_preview(store, now):
    previous = store.get_json("catalog_preview")
    if previous and now < parse_date(previous["at"]) + PERIOD:
        return
    url = "https://nft-tracker-market-beta.nffv2bg9mw.workers.dev/preview"
    request = urllib.request.Request(url, headers={"User-Agent": "NFT-Tracker/0.7.0", "Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=10) as response:
        preview = json.load(response)
    if not isinstance(preview, dict) or not parse_date(preview.get("at")):
        raise ValueError("Некорректная дата предпросмотра")
    rows = preview.get("rows")
    if not isinstance(rows, list) or not 1 <= len(rows) <= 5:
        raise ValueError("Некорректный размер предпросмотра")
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("name"), str) or not isinstance(row.get("prices"), dict):
            raise ValueError("Некорректная строка предпросмотра")
        if any(k not in CURRENCIES or not isinstance(v,str) or dec(v) is None for k,v in row["prices"].items()):
            raise ValueError("Некорректная цена предпросмотра")
    store.set("catalog_preview", json.dumps(preview))


def refresh_catalog_if_due(store, now):
    snapshot = store.get_json("price_catalog")
    if not snapshot or now >= parse_date(snapshot["at"]) + PERIOD:
        try:
            update_catalog(store, fetch_catalog(), now)
        except (OSError, ValueError):
            cache_preview(store, now)
            raise


def visible_catalog(store, full):
    data = store.get_json("price_catalog")
    if full and data:
        return data, False
    preview = store.get_json("catalog_preview")
    if preview and (not data or parse_date(preview["at"]) > parse_date(data["at"])):
        return {**preview, "rows": preview["rows"][:5]}, True
    if data:
        data = {**data, "rows": data["rows"][:5]}
    return data, False


def catalog_page(store, now, page=1, query="", *, full=False, market=""):
    data, fallback = visible_catalog(store, full)
    back = [{"text": "Меню", "callback_data": "menu:main"}]
    if not data:
        return ("Снимок цен пока недоступен. Откройте каталог позже командой /prices.",
                [[{"text": "Все цены", "callback_data": "prices:1"}], back])
    at = parse_date(data["at"])
    rows = [r for r in data["rows"] if query.casefold() in r["name"].casefold()]
    if market in CURRENCIES:
        rows.sort(key=lambda r: Decimal(r["prices"].get(market, "Infinity")))
    pages = max(1, (len(rows) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(1, min(page, pages))
    lines = [f"Минимальные цены коллекций · {at:%d.%m.%Y %H:%M} UTC",
             'Источник: <a href="https://starprice.net/gifts">StarPrice</a>. Ориентиры коллекций, без подтверждения отдельного лота.\nTelegram ⭐ · Fragment GRAM · Portals GRAM (в этом порядке). Прочерк — нет свежей цены.']
    if full and fallback:
        lines.append("Полный снимок временно недоступен. Показан резервный предпросмотр.")
    if now >= at + PERIOD:
        lines.append("Снимок старше 5 дней. Ожидается успешное обновление.")
    for row in rows[(page-1)*PAGE_SIZE:page*PAGE_SIZE]:
        values = " · ".join(row["prices"].get(k, "—") for k in CURRENCIES)
        lines.append(f"<b>{html.escape(row['name'])}</b>  {values}")
    if not rows:
        lines.append("Совпадений нет.")
    lines.append(f"Страница {page}/{pages}. Коллекций: {len(rows)}. Поиск: /prices Pepe. По цене: /prices portals. Карточка: /gift Название")
    buttons = []
    for i, label in ((page-1, "Назад"), (page+1, "Дальше")):
        if 1 <= i <= pages:
            if query or market:
                lines.append(f"{label}: /prices {i} {(market + ' ') if market else ''}{html.escape(query[:100])}")
            else:
                buttons.append({"text": label, "callback_data": f"prices:{i}"})
    if not full:
        lines.append("FREE · до 5 коллекций. Полный каталог доступен с PLUS.")
        buttons.append({"text": "⭐ Подписка", "callback_data": "sub:menu"})
    return "\n".join(lines), ([buttons] if buttons else []) + [back]
