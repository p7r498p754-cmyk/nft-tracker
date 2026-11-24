"""Tariff-aware commands shared by private deployment and the owner preview."""
import csv
import html
import hashlib
import io
import json
from contextlib import closing, nullcontext
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from .catalog import CURRENCIES, catalog_page
from .storage import Storage
from .subscriptions import PLANS, entitlement
from .telegram import TelegramError

SUB = [[{"text": "⭐ Подписка", "callback_data": "sub:menu"}]]
DETAILS = {
    "FREE": "Пять коллекций в каталоге и базовые сигналы от 30%.",
    "PLUS": "Полный каталог, диапазон цен, площадки, выбор скидки и сводка.",
    "PRO": "Анализ продаж, история цен и фильтры по характеристикам.",
    "ULTRA": "Экспорт истории, отчёт по источникам и исключения коллекций.",
}


def effective_access(cfg, store, uid, now):
    if uid == cfg.owner_chat_id and uid > 0:
        trial = store.get_json("owner_plan_preview")
        name, end = trial.get("plan"), trial.get("end")
        if name in PLANS and type(end) is int and end > now:
            return name, end, True
        if cfg.owner_lifetime_ultra:
            return "ULTRA", None, False
    name, end = entitlement(store.db, uid, now)
    return name, end, False


def prefs(store, key):
    row = store.db.execute("SELECT settings FROM preferences WHERE user_key=?", (key,)).fetchone()
    return json.loads(row[0]) if row else {}


def limits(store, name):
    p = PLANS[name]
    return (int(store.get("limit:" + name, str(p.alerts))), p.favorites)


def threshold(store, user, plan):
    allowed = (store.get("discount_choices", "30,40,50") or "").split(",")
    return user.min_discount if user and plan.level and str(user.min_discount) in allowed else 30


def fits(event, settings, plan):
    names = settings.get("favorites", [])[:plan.favorites]
    if names and event.name.casefold() not in names:
        return False
    if plan.level >= 1:
        if settings.get("market") not in (None, event.market):
            return False
        if not Decimal(settings.get("min", "0")) <= event.price <= Decimal(settings.get("max", "Infinity")):
            return False
    if plan.level >= 2:
        for field in ("model", "backdrop", "symbol"):
            if field in settings and getattr(event, field).casefold() != settings[field]:
                return False
    return not (plan.level >= 3 and event.name.casefold() in settings.get("exclude", []))


def menu(store, name, end, preview=False, *, sales_enabled=False):
    until = datetime.fromtimestamp(end, timezone.utc).strftime("%d.%m.%Y %H:%M UTC") if end else "бессрочно"
    lines = ["⭐ <b>Подписка NFT Tracker</b>", f"Ваш тариф: <b>{name}</b> · {until}",
             ("Разовая оплата Stars. Автопродление выключено." if sales_enabled else
              "Автопродление выключено. Платные планы открыты для ознакомления."), ""]
    if preview:
        lines.insert(2, "Тестовый доступ владельца · без оплаты.")
    elif name == "ULTRA" and end is None:
        lines.insert(2, "Доступ владельца · без оплаты.")
    buttons = []
    for tier, plan in PLANS.items():
        cost = f"{plan.price} ⭐ / 30 дней" if plan.price else "бесплатно"
        alerts, favorites = limits(store, tier)
        lines.extend([f"<b>{tier}</b> · {cost}", f"{alerts} сигналов в сутки · {favorites} избранных. {DETAILS[tier]}"])
        buttons.append({"text": tier, "callback_data": "sub:" + tier})
    return "\n".join(lines), [buttons[:2], buttons[2:], [{"text": "Меню", "callback_data": "menu:main"}]]


def analytics(store, collection, now):
    rows = [s for s in store.sales() if s.name.casefold() == collection.casefold()]
    history = []
    for at, raw in store.db.execute("SELECT at,snapshot FROM price_history ORDER BY at DESC LIMIT 10"):
        for row in json.loads(raw)["rows"]:
            if row["name"].casefold() == collection.casefold():
                history.append(at[:10] + " · " + ", ".join(f"{m}: {p} {CURRENCIES[m]}" for m,p in row["prices"].items()))
    text = [f"<b>{html.escape(collection)}</b>", "Ориентиры StarPrice по площадкам (валюты указаны отдельно):",
            *history] if history else [f"<b>{html.escape(collection)}</b>", "Истории снимков пока нет."]
    text.append(f"Продаж Portals в сохранённой выборке: {len(rows)}.")
    for attr, label in (("model","Модель"), ("backdrop","Фон"), ("symbol","Символ")):
        counts = Counter(getattr(s, attr) for s in rows if getattr(s, attr))
        if counts:
            rare, n = min(counts.items(), key=lambda p: (p[1], p[0]))
            text.append(f"{label}: {html.escape(rare[:60])} · {n}/{len(rows)} наблюдений.")
    text.append("Частота относится к выборке продаж. Тираж и редкость всего выпуска неизвестны. "
                "Оценка сигнала — медиана сопоставимых продаж после исключения выбросов.")
    return "\n".join(text)


def admin(cfg, store, tg, uid, action, now):
    if uid != cfg.owner_chat_id or not uid:
        return "Раздел доступен владельцу.", None
    keyboard = [[{"text": label, "callback_data": "admin:" + cmd}] for cmd,label in (
        ("users","Пользователи"), ("income","Доход NFT Tracker"), ("monitor","Мониторинг"), ("config","Настройки"), ("preview","Проверить тариф"))]
    action = action.strip() or "menu"
    label = action.split()[0]
    if label not in ("menu","users","income","monitor","config","limit","reports","threshold","preview"):
        label = "unknown"
    with store.db:
        store.db.execute("INSERT INTO audit(at,action) VALUES (?,?)", (int(now.timestamp()), "admin:"+label))
    if action == "preview":
        tier_buttons = [{"text": name, "callback_data": "admin:preview " + name} for name in PLANS]
        return ("Выберите тариф для бесплатной проверки на аккаунте владельца. Доступ действует 24 часа. "
                "FREE завершает проверку.", [tier_buttons[:2], tier_buttons[2:]] + keyboard)
    if action.startswith("preview "):
        name = action.removeprefix("preview ").strip().upper()
        if name in PLANS:
            trial = {} if name == "FREE" else {"plan": name, "end": int(now.timestamp()) + 86400}
            store.set("owner_plan_preview", json.dumps(trial))
            return menu(store, *effective_access(cfg, store, uid, int(now.timestamp())))
    if action == "users":
        names = Counter(entitlement(store.db, u or uid, int(now.timestamp()))[0] for u, in store.db.execute("SELECT chat_id FROM users"))
        return f"Пользователей: {sum(names.values())} · с активным платным доступом: {sum(names.values())-names['FREE']}\n" + " · ".join(f"{n}: {names[n]}" for n in PLANS), keyboard
    if action == "income":
        lines = ["<b>Доход NFT Tracker</b>", ("Разовая оплата Stars включена." if cfg.stars_payments else "Приём новых платежей выключен."),
                 "Отправка подарков выключена. Использование для вознаграждения владельца не подтверждено."]
        try:
            b = tg.call("getMyStarBalance", timeout=4)
            lines.append(f"Баланс бота: {star_amount(b)} ⭐")
            # Never write financial records to the cache of a public Actions repository.
            with (closing(Storage(":memory:")) if cfg.on_actions else nullcontext(store)) as ledger:
                offset = int(ledger.get("star_offset","0"))
                page = tg.call("getStarTransactions", {"offset":offset,"limit":100}, timeout=4)["transactions"]
                if not isinstance(page,list):
                    raise ValueError("Unexpected transactions")
                with ledger.db:
                    for tx in page:
                        incoming = "source" in tx
                        direction = "source" if incoming else "receiver"
                        partner = tx[direction]
                        category = "Поступления" if incoming else ("Возвраты" if partner.get("transaction_type") == "invoice_payment" else "Расходы")
                        # The refund can share an identifier with the incoming payment.
                        identifier = hashlib.sha256(tx["id"].encode()).hexdigest()
                        ledger.db.execute("INSERT OR IGNORE INTO star_operations VALUES (?,?,?,?,?)",
                                         (identifier,direction,int(tx["date"]),category,str(abs(star_amount(tx)))))
                    ledger.set("star_offset", str(offset + len(page)) if len(page)==100 else "0")
                totals = Counter()
                for category,amount in ledger.db.execute("SELECT category,amount FROM star_operations"):
                    totals[category] += Decimal(amount)
                lines.append("Суммы по синхронизированным операциям:")
                lines.extend(f"{label}: {totals[label]} ⭐" for label in ("Поступления","Расходы","Возвраты"))
                if len(page)==100:
                    lines.append("Показаны только первые 100 операций." if cfg.on_actions else "Есть следующая страница. Откройте раздел ещё раз.")
                for stamp, category, amount in ledger.db.execute(
                        "SELECT at,category,amount FROM star_operations ORDER BY at DESC LIMIT 5"):
                    lines.append(datetime.fromtimestamp(stamp, timezone.utc).strftime("%d.%m %H:%M UTC") +
                                 f" · {category} · {amount} ⭐")
                revenue = {plan: (count, total) for plan, count, total in store.db.execute("""
                    SELECT o.plan, count(*), sum(o.amount) FROM star_terms t
                    JOIN star_checkouts c ON c.completed_charge=t.charge
                    JOIN star_orders o ON o.payload=t.payload
                    LEFT JOIN star_refunds r ON r.charge=t.charge WHERE r.charge IS NULL GROUP BY o.plan
                    """)}
                lines.append("Подтверждённые продажи тарифов после возвратов:")
                for tier in ("PLUS", "PRO", "ULTRA"):
                    count, total = revenue.get(tier, (0, 0))
                    lines.append(f"{tier} · {count} оплат · {total} ⭐")
        except (TelegramError, KeyError, ValueError, TypeError, OverflowError):
            lines.append("Полные данные Telegram о балансе и операциях сейчас недоступны.")
        return "\n".join(lines), keyboard
    if action == "monitor":
        m = store.get_json("cycle")
        return (f"Portals · последняя проверка: {html.escape(str(m.get('ok_at') or 'нет данных'))}\n"
                f"Объявлений: {m.get('listings', 0)} · последовательных ошибок: {m.get('failures',0)}\n"
                "Telegram и Fragment: ориентиры StarPrice, прямые ленты не подключены."), keyboard
    if action.startswith("limit "):
        fields = action.split()
        if len(fields)==3 and fields[1].upper() in PLANS and fields[2].isascii() and fields[2].isdigit():
            name = fields[1].upper()
            value = int(fields[2])
            if 1 <= value <= PLANS[name].alerts:
                store.set("limit:"+name,str(value))
                return "Дневной лимит сохранён.", keyboard
    if action.startswith("threshold "):
        choices = action.split()[1:]
        if choices and "30" in choices and all(x in ("30","40","50") for x in choices):
            store.set("discount_choices", ",".join(dict.fromkeys(choices)))
            return "Доступные пороги обновлены.", keyboard
    if action in ("reports on", "reports off"):
        store.set("reports_enabled", "1" if action.endswith(" on") else "0")
        return "Доступность аналитических отчётов изменена.", keyboard
    if action == "config":
        return ("Лимиты сигналов: " + ", ".join(f"{n} {limits(store,n)[0]}" for n in PLANS) +
                "\n/admin limit PLUS 15 — изменить в пределах тарифа. /admin threshold 30 40 50 — доступные пороги. /admin reports on или off — доступность экспериментального отчёта ULTRA. Финансовые операции отключены."), keyboard
    return "Панель владельца · NFT Tracker", keyboard


def star_amount(row):
    return Decimal(row["amount"]) + Decimal(row.get("nanostar_amount",0)) / 10**9


def view(cfg, store, tg, uid, key, command, args, now):
    name, end, preview = effective_access(cfg, store, uid, int(now.timestamp()))
    plan = PLANS[name]
    required = {"/filter":1, "/summary":1, "/analytics":2, "/export":3, "/report":3}
    if command in required and plan.level < required[command]:
        tier = tuple(PLANS)[required[command]]
        return f"Эта функция доступна с {tier}. Ваш тариф — {name}.", SUB
    if command == "/admin":
        return admin(cfg, store, tg, uid, args or "menu", now)
    if command == "/subscription":
        if args in PLANS:
            price = PLANS[args].price
            text = f"<b>{args}</b> · {price} ⭐\n{DETAILS[args]}"
            if (cfg.stars_payments and cfg.public_users and not cfg.on_actions and price and name == "FREE"
                    and not (uid == cfg.owner_chat_id and cfg.owner_lifetime_ultra)):
                button = {"text": f"Получить счёт на {price} ⭐", "callback_data": "buy:" + args}
                terms = {"text": "Условия оплаты", "url": cfg.stars_terms}
                return text + "\nРазовый платёж за 30 дней доступа. Поддержка: /paysupport.", [[button], [terms], *SUB]
            return text + "\nУправление доступом — /subscription.", SUB
        return menu(store, name, end, preview, sales_enabled=cfg.stars_payments)
    if command == "/prices":
        fields = args.split()
        page = int(fields.pop(0)) if fields and fields[0].isascii() and fields[0].isdecimal() else 1
        market = fields.pop(0) if fields and fields[0] in CURRENCIES else ""
        return catalog_page(store, now, page, " ".join(fields), full=plan.level>=1, market=market)
    if command == "/summary":
        count = store.db.execute("SELECT count(*) FROM deliveries WHERE user_key=? AND substr(sent_at,1,10)=?", (key,now.date().isoformat())).fetchone()[0]
        return f"С начала суток UTC доставлено предложений: {count}.", SUB
    if command == "/analytics":
        return analytics(store, args, now) if args else "Укажите коллекцию: /analytics Plush Pepe", SUB
    if command == "/report" and store.get("reports_enabled", "1") != "1":
        return "Владелец временно отключил экспериментальный отчёт.", SUB
    if command == "/gift":
        snap = store.get_json("price_catalog")
        rows = snap.get("rows", [])
        visible = rows if plan.level else rows[:5]
        row = next((r for r in visible if r["name"].casefold()==args.casefold()), None)
        if not row:
            return "Укажите доступную коллекцию из /prices. Полный каталог доступен с PLUS.", SUB
        lines = [html.escape(row["name"]), "Снимок StarPrice: " + snap["at"][:10],
                 "Ориентиры: " + ", ".join(f"{m} {p} {CURRENCIES[m]}" for m,p in row["prices"].items())]
        if (now - datetime.fromisoformat(snap["at"])).days >= 5:
            lines.append("Снимок устарел.")
        sale = store.db.execute("SELECT model,backdrop,symbol,price,event_time FROM sales WHERE name=? ORDER BY event_time DESC LIMIT 1",
                                (row["name"],)).fetchone()
        if sale:
            lines.extend(["Последняя полученная завершённая продажа Portals: " + html.escape(sale[3]) + " " + html.escape(cfg.price_unit) + " · " + sale[4][:10],
                          "Модель / фон / символ: " + " / ".join(html.escape(v or "—") for v in sale[:3])])
        else:
            lines.append("Подтверждённых продаж и характеристик в истории нет.")
        return "\n".join(lines), SUB
    if command in ("/export", "/report"):
        rows = store.db.execute("SELECT at,snapshot FROM price_history ORDER BY at DESC LIMIT 1").fetchone()
        if not rows:
            return "История цен пока пуста.", SUB
        items = json.loads(rows[1])["rows"]
        if command == "/report":
            return f"Снимок {rows[0][:10]} · коллекций: {len(items)}\n" + "\n".join(
                f"{m}: {sum(m in r['prices'] for r in items)} коллекций" for m in CURRENCIES), SUB
        out = io.StringIO()
        writer = csv.writer(out)
        writer.writerow(["at","collection","market","price","currency"])
        for item in items:
            label = item["name"]
            if label.startswith(("\t","\r","\n")) or label.lstrip().startswith(("=", "+", "-", "@")):
                label = "'" + label
            for m, p in item["prices"].items():
                writer.writerow([rows[0],label,m,p,CURRENCIES[m]])
        tg.send_csv(uid, out.getvalue())
        return "Снимок отправлен в CSV.", SUB
    if command not in ("/favorite","/filter"):
        return None
    if not store.user(key):
        return "Для настройки мониторинга отправьте /start.", SUB
    settings = prefs(store,key)
    if command == "/favorite":
        saved = settings.get("favorites", [])
        if not args:
            return "Избранные: " + (html.escape(", ".join(saved)) or "нет") + "\n/favorite +Plush Pepe или /favorite -Plush Pepe", SUB
        value = args[1:].strip().casefold()
        if args.startswith("+") and value and value not in saved:
            if len(saved) >= plan.favorites:
                return "Достигнут лимит избранных коллекций для " + name, SUB
            saved.append(value)
        elif args.startswith("-") and value in saved:
            saved.remove(value)
        else:
            return "Укажите + или − перед названием (например /favorite +Plush Pepe).", SUB
        settings["favorites"] = saved
    else:
        field, _, value = args.partition(" ")
        value = value.strip().casefold()
        needed = {"market":1,"min":1,"max":1,"model":2,"backdrop":2,"symbol":2,"exclude":3}
        if field not in needed:
            return "Фильтры: market, min, max, model, backdrop, symbol, exclude. Для сброса укажите поле без значения.", SUB
        if plan.level < needed[field]:
            return "Этот фильтр доступен с " + tuple(PLANS)[needed[field]], SUB
        if not value:
            settings.pop(field,None)
        elif field == "market" and value not in CURRENCIES:
            return "Площадки: portals, telegram, fragment. Прямые объявления подключены только для Portals.", SUB
        elif field == "exclude":
            settings[field] = [v.strip() for v in value.split(",") if v.strip()]
        else:
            settings[field] = value
        try:
            low, high = Decimal(settings.get("min","0")), Decimal(settings.get("max","Infinity"))
            if not low.is_finite() or ("max" in settings and not high.is_finite()) or not 0 <= low <= high:
                raise ValueError
        except (InvalidOperation, ValueError):
            return "Проверьте числовые границы диапазона.", SUB
    with store.db:
        store.db.execute("INSERT OR REPLACE INTO preferences VALUES (?,?)", (key,json.dumps(settings)))
    return "Настройки сохранены.", SUB
