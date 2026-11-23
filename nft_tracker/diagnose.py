"""Safe connectivity/structure check. Prints field NAMES and types only, never
secrets, prices, ids or chat data, so the output can be shared as-is."""
from __future__ import annotations

from typing import Any

from .config import Settings
from .portals import PortalsClient, PortalsError
from .telegram import TelegramClient, TelegramError


def shape(value: Any, depth: int = 0) -> Any:
    if isinstance(value, dict):
        return {k: shape(v, depth + 1) for k, v in list(value.items())[:40]} if depth < 4 else "{…}"
    if isinstance(value, list):
        return [shape(value[0], depth + 1)] if value else []
    return type(value).__name__


def url_fields(value: Any, path: str = "") -> list[str]:
    found: list[str] = []
    if isinstance(value, dict):
        for k, v in value.items():
            p = f"{path}.{k}" if path else str(k)
            if isinstance(v, str) and (v.startswith("http") or "url" in str(k).lower() or "link" in str(k).lower()):
                found.append(p)
            found += url_fields(v, p)
    elif isinstance(value, list) and value:
        found += url_fields(value[0], path + "[]")
    return found


def run_diagnose(cfg: Settings) -> int:
    ok = True
    try:
        me = TelegramClient(cfg.telegram_token).call("getMe")
        if not isinstance(me, dict) or str(me.get("username", "")).lower() != "nfttrecker_bot":
            raise TelegramError("Telegram: токен принадлежит другому боту или ответ некорректен")
        print(f"✅ Telegram: токен принят, бот @{me.get('username', '?')}")
    except TelegramError as e:
        print(f"❌ {e}")
        ok = False
    portals = PortalsClient(cfg.portals_key, link_template=cfg.listing_url_template, purchase_action=cfg.purchase_action)
    try:
        data = portals.get("/market/actions/", {"limit": 20, "sort_by": "created_at desc"})
        print("✅ Portals /market/actions/: ответ получен")
        print("   ключи ответа:", sorted(data.keys()))
        if not isinstance(data.get("actions"), list):
            raise PortalsError("Portals: отсутствует список actions")
        actions = data["actions"]
        print("   типы действий в ответе:", sorted({str(a.get("type")) for a in actions if isinstance(a, dict)}))
        if actions:
            print("   структура действия:", shape(actions[0]))
            print("   поля, похожие на ссылки:", url_fields(actions[0]) or "нет")
    except PortalsError as e:
        print(f"❌ {e}")
        return 1
    try:
        data = portals.get("/nfts/search", {"status": "listed", "limit": 1})
        if not isinstance(data.get("results"), list):
            raise PortalsError("Portals: отсутствует список results")
        results = data["results"]
        print("✅ Portals /nfts/search: ответ получен, ключи:", sorted(data.keys()))
        if results:
            print("   структура лота:", shape(results[0]))
            print("   поля, похожие на ссылки:", url_fields(results[0]) or "нет")
    except PortalsError as e:
        print(f"❌ {e}")
        ok = False
    try:
        purchases = portals.purchases(pages=1)
        print("Распознано продаж на первой странице:", len(purchases))
        events = portals.listings(pages=1)
        if events:
            ev = events[0]
            print("   проверка актуальности свежего лота (ожидается True; False = параметры поиска "
                  "по id не подходят или цена уже изменилась):", portals.still_listed_at(ev))
            print("   прямая ссылка из API/шаблона:", "есть" if ev.link else "нет (не настроена)")
    except PortalsError as e:
        ok = False
        print(f"❌ {e}")
    print("Готово. Секреты, цены и идентификаторы в этот вывод не попадают.")
    return 0 if ok else 1
