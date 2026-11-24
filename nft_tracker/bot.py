"""Telegram commands and inline menu."""
from __future__ import annotations

import html
from time import monotonic
from datetime import datetime, timedelta
from typing import Any

from . import __version__
from .service import prefs, threshold, view, SUB
from .subscriptions import entitlement, PLANS
from .config import Settings
from .floors import floor_status
from .storage import Storage
from .telegram import TelegramClient, TelegramError

OWNER = "owner"
DISCOUNT_CHOICES = (30, 40, 50)
COMMANDS = {"/start", "/status", "/settings", "/stop", "/help", "/id", "/delete", "/markets", "/prices"}

SERVICE = {"/gift", "/subscription", "/admin", "/favorite", "/filter", "/summary", "/analytics", "/report", "/export", "/paysupport"}
COMMANDS |= SERVICE

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
        "/start — включить уведомления\n/status — состояние\n/prices — каталог цен (обновление раз в 5 дней)\n/markets — цены Telegram и Fragment\n/settings — порог скидки\n"
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
    current_plan = entitlement(store.db, cfg.owner_chat_id if key == OWNER else int(key), int(now.timestamp()))[0]
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
    tier = entitlement(store.db, cfg.owner_chat_id if key == OWNER else int(key), int(now.timestamp()))[0]
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
        except TelegramError:
            pass  # e.g. "message is not modified" -> send a fresh message instead
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
    msg: dict[str, Any] | None = update.get("message")
    cb: dict[str, Any] | None = update.get("callback_query")
    if cb:
        chat = ((cb.get("message") or {}).get("chat") or {}).get("id")
        edit_id = (cb.get("message") or {}).get("message_id")
        data = str(cb.get("data") or "")
        sender = (cb.get("from") or {}).get("id")
        try:
            tg.answer_callback(str(cb.get("id", "")))
        except TelegramError:
            pass
        if not isinstance(chat, int) or chat <= 0 or sender != chat or (chat != cfg.owner_chat_id and not cfg.public_users):
            return
        _on_callback(cfg, store, tg, chat, edit_id, data, now)
        return
    if not msg:
        return
    chat_obj = msg.get("chat") or {}
    chat = chat_obj.get("id")
    if chat_obj.get("type") != "private" or not isinstance(chat, int):
        return
    if msg.get("from") and msg["from"].get("id") != chat:
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
    if chat != cfg.owner_chat_id and not cfg.public_users:
        tg.send(chat, "🔒 NFT Tracker сейчас работает в закрытом режиме.")
        return
    key = OWNER if chat == cfg.owner_chat_id else str(chat)
    if command == "/paysupport":
        tg.send(chat, "Оплата в NFT Tracker отключена. По вопросам платежей обратитесь к владельцу сервиса.")
        return
    if command in SERVICE or command == "/prices":
        args = str(msg.get("text", "")).partition(" ")[2].strip()[:150]
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
        if value in ("40", "50") and entitlement(store.db, chat, int(now.timestamp()))[0] == "FREE":
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
