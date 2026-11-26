"""Telegram commands and inline menu."""
from __future__ import annotations

import html
import logging
from concurrent.futures import ThreadPoolExecutor
from time import monotonic
from datetime import datetime, timedelta
from typing import Any

from . import __version__
from .service import effective_access, prefs, threshold, view, SUB
from .subscriptions import PLANS
from .payments import answer_checkout, create_invoice, settle_receipt, pending_message, pending_plan
from .channels import shop
from .config import Settings
from .floors import floor_status
from .storage import Storage
from .telegram import TelegramClient, TelegramError

LOG = logging.getLogger(__name__)
OWNER = "owner"
DISCOUNT_CHOICES = (30, 40, 50)
COMMAND_HINTS = {
    "start": "Включить уведомления и открыть меню",
    "buy": "Выбрать тариф за Stars",
    "subscription": "Посмотреть тариф и условия подписок",
    "prices": "Открыть каталог цен",
    "gift": "Посмотреть карточку коллекции",
    "markets": "Посмотреть цены Telegram и Fragment",
    "status": "Проверить мониторинг",
    "settings": "Настроить порог скидки",
    "favorite": "Управлять избранными коллекциями",
    "filter": "Настроить фильтры",
    "summary": "Посмотреть сводку сигналов",
    "analytics": "Открыть аналитику коллекции",
    "report": "Посмотреть отчёт по источникам",
    "export": "Выгрузить данные в CSV",
    "ping": "Проверить время ожидания команды",
    "stop": "Выключить уведомления",
    "help": "Открыть справку",
    "terms": "Открыть условия оплаты",
    "paysupport": "Получить помощь по платежам",
    "id": "Узнать свой Telegram ID",
    "delete": "Удалить данные с подтверждением",
    "admin": "Открыть панель владельца",
}
COMMANDS = {"/" + name for name in COMMAND_HINTS}
SERVICE = {"/gift", "/subscription", "/admin", "/favorite", "/filter", "/summary", "/analytics", "/report", "/export", "/paysupport"}


def register_commands(cfg: Settings, tg: TelegramClient) -> None:
    general = [{"command": name, "description": description}
               for name, description in COMMAND_HINTS.items() if name != "admin"]
    scopes = [({"type": "all_private_chats"}, general)]
    if not cfg.setup_mode:
        admin = {"command": "admin", "description": COMMAND_HINTS["admin"]}
        scopes.append(({"type": "chat", "chat_id": cfg.owner_chat_id}, [*general, admin]))
    for scope, commands in scopes:
        if tg.call("setMyCommands", {"scope": scope, "commands": commands}, timeout=5) is not True:
            raise TelegramError("Telegram не подтвердил сохранение команд")
        if tg.call("getMyCommands", {"scope": scope}, timeout=5) != commands:
            raise TelegramError("Telegram вернул другой список команд")


MENU = [
    *SUB,
    [{"text": "Все цены подарков", "callback_data": "prices:1"}],
    [{"text": "Цены Telegram и Fragment", "callback_data": "menu:markets"}],
    [{"text": "📊 Статус", "callback_data": "menu:status"}, {"text": "⚙️ Настройки", "callback_data": "menu:settings"}],
    [{"text": "🔕 Выключить", "callback_data": "menu:stop"}, {"text": "🔔 Включить", "callback_data": "menu:start"}],
    [{"text": "❓ Справка", "callback_data": "menu:help"}, {"text": "🗑 Удалить данные", "callback_data": "menu:delete"}],
]
BACK = [[{"text": "⬅️ Меню", "callback_data": "menu:main"}]]

HELP = ("<b>NFT Tracker</b> ищет на Portals подарки дешевле рыночной оценки (по недавним продажам) "
        "и присылает уведомление.\n\n"
        "/ping — проверить задержку\n/start — включить уведомления\n/status — состояние\n/prices — каталог цен (обновление раз в 5 дней)\n/markets — цены Telegram и Fragment\n/settings — порог скидки\n"
        "/gift Название — карточка коллекции · /subscription — тарифы · /favorite — избранное · /filter — фильтры\n/summary — сводка · /analytics — аналитика · /report — отчёт · /export — CSV\n/stop — выключить\n/id — ваш Telegram ID\n/delete — удалить мои данные\n\n"
        "Бот ничего не покупает и не просит ключи или пароли. Оценка не гарантирует прибыль.")


def _delay_note(cfg: Settings) -> str:
    return ("\n\n⏳ Бот проверяет рынок по расписанию (целевой интервал 10 минут, возможны задержки): ответы на команды "
            "могут приходить с задержкой.") if cfg.on_actions else ""


def status_text(cfg: Settings, store: Storage, now: datetime, key=OWNER) -> str:
    user = store.user(key)
    meta = store.get_json("cycle")
    if meta.get("ok_at"):
        mins = max(0, int((now - datetime.fromisoformat(meta["ok_at"])).total_seconds() // 60))
        last = f"{mins} мин назад ({meta.get('listings', 0)} объявл., {meta.get('alerts', 0)} уведомл.)"
    else:
        last = "ещё не было успешной проверки"
    error = meta.get("error")
    link = ("шаблон задан владельцем (требует проверки)" if cfg.listing_url_template
            else "не настроена в конфигурации; может поступить из API")
    current_plan = effective_access(cfg, store, cfg.owner_chat_id if key == OWNER else int(key), int(now.timestamp()))[0]
    active_discount = threshold(store, user, PLANS[current_plan])
    favorites = prefs(store,key).get("favorites", [])
    lines = [
        f"<b>NFT Tracker v{__version__}</b>",
        f"Уведомления: {'включены' if user and user.active else 'выключены'}",
        f"Порог скидки: от {active_discount}%",
        f"Избранных коллекций: {len(favorites)} · Источник объявлений: Portals",
        f"Последняя проверка: {last}",
        f"Продаж в истории оценки: {store.sales_count()}",
        f"Прямая ссылка на лот: {link}",
    ]
    if "recipients" in meta:
        if meta["recipients"] == 0:
            lines.append("В последней успешной проверке не было получателей. Для подписки отправьте /start.")
        else:
            lines.append(f"Кандидатов в последней успешной проверке: {meta['candidates']}. "
                         f"Не подтверждены при проверке цены и наличия: {meta['unverified']}. "
                         f"Пропущены без ссылки: {meta['missing_link']}.")
    if cfg.on_actions:
        stamp = meta.get("scheduled_ok_at")
        if stamp:
            minutes = max(0, int((now - datetime.fromisoformat(stamp)).total_seconds() // 60))
            lines.append(f"Последняя успешная проверка по расписанию: {minutes} мин назад.")
        else:
            lines.append("В сохранённой истории пока нет подтверждённой проверки по расписанию.")
    if not cfg.listing_url_template and not cfg.allow_no_link_alerts:
        lines.append("⚠️ Без прямой ссылки из API сигналы не отправляются (см. README).")
    if store.get("sales_backfill_done") == "1":
        if store.get("sales_backfill_capped") == "1":
            lines.append("История продаж: достигнут предел загрузки Portals (неполное покрытие).")
        else:
            lines.append("История продаж: первичная загрузка завершена (скользящая лента).")
    else:
        lines.append("История продаж: первичная загрузка ещё не завершена.")
    if error:
        lines.append(f"⚠️ Последняя ошибка: {html.escape(error)}")
    if meta.get("warning"):
        lines.append(f"⚠️ Неполная проверка: {html.escape(meta['warning'])}")
    lines.append("Telegram Market и Fragment: прямые ленты не подключены. Ориентиры StarPrice доступны в /markets.")
    return "\n".join(lines)


def settings_view(cfg: Settings, store: Storage, now, key=OWNER) -> tuple[str, list]:
    user = store.user(key)
    tier = effective_access(cfg, store, cfg.owner_chat_id if key == OWNER else int(key), int(now.timestamp()))[0]
    cur = threshold(store, user, PLANS[tier])
    text = (f"<b>Настройки</b>\nМинимальная скидка: <b>{cur}%</b>\nИзбранное: /favorite\nОбъявления: Portals\n\n"
            "Выберите порог скидки:")
    row = [{"text": ("✅ " if c == cur else "") + f"{c}%", "callback_data": f"set:discount:{c}"} for c in DISCOUNT_CHOICES if str(c) in store.get("discount_choices","30,40,50").split(",")]
    return text, [row] + BACK


def _reply(tg: TelegramClient, chat: int, text: str, keyboard: list | None, edit_id: int | None) -> None:
    if edit_id is not None:
        try:
            tg.edit(chat, edit_id, text, keyboard)
            return
        except TelegramError as error:
            if error.code != 400:
                raise
            if "message is not modified" in str(error).lower():
                return
            # The original message may have been deleted or cannot be edited.
    tg.send(chat, text, keyboard)


def _screen(cfg: Settings, store: Storage, action: str, now: datetime, key=OWNER, uid=None) -> tuple[str, list | None]:
    """Text + keyboard for a menu action. Applies the action's side effects."""
    if action == "markets":
        return floor_status(store, now), BACK
    if action == "status":
        return status_text(cfg, store, now, key), BACK
    if action == "settings":
        return settings_view(cfg, store, now, key)
    if action == "help":
        return HELP, BACK
    if action == "stop":
        if store.user(key):
            store.set_active(key, False)
        return "🔕 Уведомления выключены. Нажмите «Включить» или отправьте /start.", MENU
    if action == "start":
        store.ensure_user(key, None if key == OWNER else uid, cfg.default_discount, now, activate=True)
        return ("💎 <b>NFT Tracker</b> включён. Объявления Portals · /settings для настройки." + _delay_note(cfg)), MENU
    if action == "delete":
        return ("Удалить ваши настройки и историю уведомлений из базы бота? "
                "Это нельзя отменить."), [[{"text": "🗑 Да, удалить", "callback_data": "del:yes"},
                                           {"text": "Отмена", "callback_data": "del:no"}]]
    return "Главное меню:", MENU


def handle_update(cfg: Settings, store: Storage, tg: TelegramClient, update: dict, now: datetime) -> None:
    query = update.get("pre_checkout_query")
    if isinstance(query, dict):
        answer_checkout(cfg, store, tg, query, int(now.timestamp()))
        return
    msg: dict[str, Any] | None = update.get("message")
    cb: dict[str, Any] | None = update.get("callback_query")
    if cb:
        chat = ((cb.get("message") or {}).get("chat") or {}).get("id")
        edit_id = (cb.get("message") or {}).get("message_id")
        data = str(cb.get("data") or "")
        sender = (cb.get("from") or {}).get("id")
        def acknowledge():
            try:
                tg.answer_callback(str(cb.get("id", "")))
            except TelegramError:
                pass

        if not isinstance(chat, int) or chat <= 0 or sender != chat or (chat != cfg.owner_chat_id and not cfg.public_users):
            acknowledge()
            return
        # Clear the spinner while rendering the menu on the SQLite thread.
        with ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(acknowledge)
            _on_callback(cfg, store, tg, chat, edit_id, data, now)
        return
    if not msg:
        return
    chat_obj = msg.get("chat") or {}
    chat = chat_obj.get("id")
    if chat_obj.get("type") != "private" or not isinstance(chat, int):
        return
    if "successful_payment" in msg or "refunded_payment" in msg:
        refund = "refunded_payment" in msg
        receipt = msg.get("refunded_payment" if refund else "successful_payment")
        if isinstance(receipt, dict) and (refund or (msg.get("from") or {}).get("id") == chat):
            try:
                plan = settle_receipt(store.db, chat, receipt, msg.get("date"), refund=refund)
            except ValueError:
                LOG.error("Telegram receipt has invalid term fields")
            else:
                if plan:
                    tg.send(chat, f"Оплата подтверждена. Доступ {plan} на 30 дней. /subscription · /paysupport")
        return
    if msg.get("from") and msg["from"].get("id") != chat:
        return
    origin = msg.get("forward_origin") or {}
    if chat == cfg.owner_chat_id and origin.get("type") == "channel":
        channel = origin.get("chat") or {}
        tg.send(chat, f"ID канала «{html.escape(str(channel.get('title', '')))}»: <code>{channel.get('id')}</code>\n"
                      "Укажите его в GitHub Variables как PAID_CHANNEL_ID или FREE_CHANNEL_ID.")
        return
    command = str(msg.get("text") or "").split(" ")[0].split("@")[0].lower()
    if command == "/admin" and (msg.get("from") or {}).get("id") != chat:
        return
    if command not in COMMANDS:
        if (chat == cfg.owner_chat_id or cfg.public_users) and msg.get("text"):
            tg.send(chat, "Выберите действие в меню или отправьте /help.", MENU)
        return
    if command == "/id":
        tg.send(chat, f"Ваш Telegram ID: <code>{chat}</code>\nИспользуйте его только как секрет OWNER_CHAT_ID в настройках хостинга.")
        return
    if command == "/terms":
        terms = [[{"text": "Открыть условия покупки", "url": cfg.stars_terms}]] if cfg.stars_terms else None
        tg.send(chat, "Условия разовой оплаты подписки." if terms else "Условия оплаты пока не опубликованы.", terms)
        return
    if command == "/paysupport":
        text = (f"Поддержка по платежам: @{html.escape(cfg.stars_support)}. Укажите дату и тариф из квитанции. Обращения по покупкам принимает поддержка NFT Tracker."
                if cfg.stars_support else "По вопросам платежей обратитесь к владельцу сервиса.")
        tg.send(chat, text)
        return
    storefront = shop(cfg)
    if storefront and (command in ("/buy", "/subscription") or
                       (chat != cfg.owner_chat_id and not cfg.public_users and command in ("/start", "/help"))):
        tg.send(chat, *storefront)
        return
    if chat != cfg.owner_chat_id and not cfg.public_users:
        tg.send(chat, "🔒 NFT Tracker сейчас работает в закрытом режиме.")
        return
    key = OWNER if chat == cfg.owner_chat_id else str(chat)
    if command == "/ping":
        sent = msg.get("date")
        if type(sent) is int and 0 < sent <= now.timestamp():
            text = f"На связи. От отправки команды до начала обработки прошло около {int(now.timestamp()) - sent} с."
        else:
            text = "На связи. Время отправки команды недоступно."
        tg.send(chat, text)
        return
    if command == "/buy":
        command = "/subscription"
    if command in SERVICE or command == "/prices":
        args = str(msg.get("text", "")).partition(" ")[2].strip()[:150]
        if command == "/subscription" and args.upper() in PLANS:
            args = args.upper()
        text, kb = view(cfg, store, tg, chat, key, command, args, now)
        tg.send(chat, text, kb)
        return
    if command == "/delete":
        text, kb = _screen(cfg, store, "delete", now, key, chat)
        tg.send(chat, text, kb)
        return
    action = {"/markets": "markets", "/start": "start", "/stop": "stop", "/status": "status", "/settings": "settings", "/help": "help"}[command]
    text, kb = _screen(cfg, store, action, now, key, chat)
    tg.send(chat, text, kb)


def _on_callback(cfg: Settings, store: Storage, tg: TelegramClient, chat: int, edit_id: int | None,
                 data: str, now: datetime) -> None:
    key = OWNER if chat == cfg.owner_chat_id else str(chat)
    uid = chat
    if data.startswith("buy:"):
        plan = data.removeprefix("buy:")
        if not cfg.stars_payments or not cfg.public_users or cfg.on_actions or plan not in PLANS or not PLANS[plan].price:
            tg.send(chat, "Счёт на этот тариф недоступен.")
        elif (chat == cfg.owner_chat_id and cfg.owner_lifetime_ultra
              or effective_access(cfg, store, chat, int(now.timestamp()))[0] != "FREE"):
            tg.send(chat, "У вас уже есть доступ. Тариф можно выбрать после окончания текущего периода.")
        elif waiting := pending_plan(store.db, chat):
            tg.send(chat, pending_message(waiting))
        else:
            create_invoice(store.db, tg, chat, plan, int(now.timestamp()))
        return
    if data.startswith(("sub:", "admin:")):
        prefix, arg = data.split(":", 1)
        text, kb = view(cfg, store, tg, chat, key, "/subscription" if prefix == "sub" else "/admin", arg, now)
        _reply(tg, chat, text, kb, edit_id)
        return
    if data.startswith("prices:"):
        number = data[7:]
        if number.isascii() and number.isdecimal() and len(number) <= 8:
            text, kb = view(cfg, store, tg, chat, key, "/prices", number, now)
            _reply(tg, chat, text, kb, edit_id)
        return
    if data == "del:yes":
        store.delete_user(key)
        _reply(tg, chat, "✅ Ваши настройки и история уведомлений удалены. /start — начать заново.", None, edit_id)
        return
    if data == "del:no":
        _reply(tg, chat, "Удаление отменено.", MENU, edit_id)
        return
    if data.startswith("set:discount:"):
        value = data.rsplit(":", 1)[1]
        if value in ("40", "50") and effective_access(cfg, store, chat, int(now.timestamp()))[0] == "FREE":
            _reply(tg, chat, "Пороги 40% и 50% доступны с PLUS. На FREE действует 30%.", SUB, edit_id)
            return
        if value in ("30", "40", "50") and value in store.get("discount_choices","30,40,50").split(","):
            store.ensure_user(key, None if key == OWNER else uid, cfg.default_discount, now)
            store.set_min_discount(key, int(value))
        text, kb = settings_view(cfg, store, now, key)
        _reply(tg, chat, text, kb, edit_id)
        return
    if data.startswith("menu:"):
        action = data.split(":", 1)[1]
        if action in {"markets", "status", "settings", "help", "stop", "start", "delete", "main"}:
            text, kb = _screen(cfg, store, action, now, key, chat)
            _reply(tg, chat, text, kb, edit_id)


def process_updates(cfg: Settings, store: Storage, tg: TelegramClient, now: datetime, timeout: int = 0) -> int:
    started = monotonic()
    offset = int(store.get("offset", "0") or 0)
    updates = tg.updates(offset, timeout)
    for u in updates:
        # Do not acknowledge an update until its handler has finished.
        # Otherwise a temporary Telegram send error permanently loses /start,
        # /stop or /id, because getUpdates will never return it again.
        # A retry can duplicate a reply if Telegram accepted it but the
        # network failed before confirming; that tradeoff is documented.
        try:
            handle_update(cfg, store, tg, u, now + timedelta(seconds=monotonic() - started))
        except TelegramError as e:
            if e.code not in (400, 403):
                raise  # server failure / rate limit: retry the command next run
            # The recipient cannot accept this message; repeated attempts
            # would permanently block every newer command in getUpdates.
            print(f"WARN: update отклонён Telegram (HTTP {e.code}); повтор не поможет")
        offset = max(offset, int(u.get("update_id", 0)) + 1)
        store.set("offset", str(offset))
    return len(updates)
