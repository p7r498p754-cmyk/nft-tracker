"""Telegram commands and inline menu."""
from __future__ import annotations

import html
import json
from datetime import datetime
from typing import Any

from . import __version__
from .config import Settings
from .storage import Storage
from .telegram import TelegramClient, TelegramError

OWNER = "owner"
DISCOUNT_CHOICES = (30, 40, 50)
COMMANDS = {"/start", "/status", "/settings", "/stop", "/help", "/id", "/delete"}

MENU = [
    [{"text": "📊 Статус", "callback_data": "menu:status"}, {"text": "⚙️ Настройки", "callback_data": "menu:settings"}],
    [{"text": "🔕 Выключить", "callback_data": "menu:stop"}, {"text": "🔔 Включить", "callback_data": "menu:start"}],
    [{"text": "❓ Справка", "callback_data": "menu:help"}, {"text": "🗑 Удалить данные", "callback_data": "menu:delete"}],
]
BACK = [[{"text": "⬅️ Меню", "callback_data": "menu:main"}]]

HELP = ("<b>NFT Tracker</b> ищет на Portals подарки дешевле рыночной оценки (по недавним продажам) "
        "и присылает уведомление.\n\n"
        "/start — включить уведомления\n/status — состояние\n/settings — порог скидки\n"
        "/stop — выключить\n/id — ваш Telegram ID\n/delete — удалить мои данные\n\n"
        "Бот ничего не покупает и не просит ключи или пароли. Оценка не гарантирует прибыль.")


def _delay_note(cfg: Settings) -> str:
    return ("\n\n⏳ Бот проверяет рынок по расписанию (примерно раз в 10–15 мин): ответы на команды "
            "могут приходить с задержкой.") if cfg.on_actions else ""


def status_text(cfg: Settings, store: Storage, now: datetime) -> str:
    user = store.user(OWNER)
    meta = store.get_json("cycle")
    if meta.get("ok_at"):
        mins = max(0, int((now - datetime.fromisoformat(meta["ok_at"])).total_seconds() // 60))
        last = f"{mins} мин назад ({meta.get('listings', 0)} объявл., {meta.get('alerts', 0)} уведомл.)"
    else:
        last = "ещё не было успешной проверки"
    error = meta.get("error")
    link = ("шаблон задан владельцем (требует проверки)" if cfg.listing_url_template
            else "не настроена в конфигурации; может поступить из API")
    lines = [
        f"<b>NFT Tracker v{__version__}</b>",
        f"Уведомления: {'включены' if user and user.active else 'выключены'}",
        f"Порог скидки: от {user.min_discount if user else cfg.default_discount}%",
        f"Коллекции: все · Источник: Portals",
        f"Последняя проверка: {last}",
        f"Продаж в истории оценки: {store.sales_count()}",
        f"Прямая ссылка на лот: {link}",
    ]
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
    lines.append("Telegram Market и Fragment: не подключены (нет разрешённого способа доступа).")
    return "\n".join(lines)


def settings_view(cfg: Settings, store: Storage) -> tuple[str, list]:
    user = store.user(OWNER)
    cur = user.min_discount if user else cfg.default_discount
    text = (f"<b>Настройки</b>\nМинимальная скидка: <b>{cur}%</b>\nКоллекции: все\nИсточники: Portals\n\n"
            "Выберите порог скидки:")
    row = [{"text": ("✅ " if c == cur else "") + f"{c}%", "callback_data": f"set:discount:{c}"} for c in DISCOUNT_CHOICES]
    return text, [row] + BACK


def _reply(tg: TelegramClient, chat: int, text: str, keyboard: list | None, edit_id: int | None) -> None:
    if edit_id is not None:
        try:
            tg.edit(chat, edit_id, text, keyboard)
            return
        except TelegramError:
            pass  # e.g. "message is not modified" -> send a fresh message instead
    tg.send(chat, text, keyboard)


def _screen(cfg: Settings, store: Storage, action: str, now: datetime) -> tuple[str, list | None]:
    """Text + keyboard for a menu action. Applies the action's side effects."""
    if action == "status":
        return status_text(cfg, store, now), BACK
    if action == "settings":
        return settings_view(cfg, store)
    if action == "help":
        return HELP, BACK
    if action == "stop":
        if store.user(OWNER):
            store.set_active(OWNER, False)
        return "🔕 Уведомления выключены. Нажмите «Включить» или отправьте /start.", MENU
    if action == "start":
        store.ensure_user(OWNER, None, cfg.default_discount, now, activate=True)
        return ("💎 <b>NFT Tracker</b> включён. Порог скидки: от "
                f"{store.user(OWNER).min_discount}%. Источник: Portals." + _delay_note(cfg)), MENU  # type: ignore[union-attr]
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
        if not isinstance(chat, int) or sender != cfg.owner_chat_id or chat != cfg.owner_chat_id:
            return
        _on_callback(cfg, store, tg, chat, edit_id, data, now)
        return
    if not msg:
        return
    chat_obj = msg.get("chat") or {}
    chat = chat_obj.get("id")
    if chat_obj.get("type") != "private" or not isinstance(chat, int):
        return
    command = str(msg.get("text") or "").split(" ")[0].split("@")[0].lower()
    if command not in COMMANDS:
        return
    if command == "/id":
        tg.send(chat, f"Ваш Telegram ID: <code>{chat}</code>\nИспользуйте его только как секрет OWNER_CHAT_ID в настройках хостинга.")
        return
    if chat != cfg.owner_chat_id:
        tg.send(chat, "🔒 NFT Tracker сейчас работает в закрытом режиме.")
        return
    if command == "/delete":
        text, kb = _screen(cfg, store, "delete", now)
        tg.send(chat, text, kb)
        return
    action = {"/start": "start", "/stop": "stop", "/status": "status", "/settings": "settings", "/help": "help"}[command]
    text, kb = _screen(cfg, store, action, now)
    tg.send(chat, text, kb)


def _on_callback(cfg: Settings, store: Storage, tg: TelegramClient, chat: int, edit_id: int | None,
                 data: str, now: datetime) -> None:
    if data == "del:yes":
        store.delete_user(OWNER)
        _reply(tg, chat, "✅ Ваши настройки и история уведомлений удалены. /start — начать заново.", None, edit_id)
        return
    if data == "del:no":
        _reply(tg, chat, "Удаление отменено.", MENU, edit_id)
        return
    if data.startswith("set:discount:"):
        value = data.rsplit(":", 1)[1]
        if value.isdigit() and int(value) in DISCOUNT_CHOICES:
            store.ensure_user(OWNER, None, cfg.default_discount, now)
            store.set_min_discount(OWNER, int(value))
        text, kb = settings_view(cfg, store)
        _reply(tg, chat, text, kb, edit_id)
        return
    if data.startswith("menu:"):
        action = data.split(":", 1)[1]
        if action in {"status", "settings", "help", "stop", "start", "delete", "main"}:
            text, kb = _screen(cfg, store, action, now)
            _reply(tg, chat, text, kb, edit_id)


def process_updates(cfg: Settings, store: Storage, tg: TelegramClient, now: datetime, timeout: int = 0) -> int:
    offset = int(store.get("offset", "0") or 0)
    updates = tg.updates(offset, timeout)
    for u in updates:
        # Do not acknowledge an update until its handler has finished.
        # Otherwise a temporary Telegram send error permanently loses /start,
        # /stop or /id, because getUpdates will never return it again.
        # A retry can duplicate a reply if Telegram accepted it but the
        # network failed before confirming; that tradeoff is documented.
        try:
            handle_update(cfg, store, tg, u, now)
        except TelegramError as e:
            if e.code not in (400, 403):
                raise  # server failure / rate limit: retry the command next run
            # The recipient cannot accept this message; repeated attempts
            # would permanently block every newer command in getUpdates.
            print(f"WARN: update отклонён Telegram (HTTP {e.code}); повтор не поможет")
        offset = max(offset, int(u.get("update_id", 0)) + 1)
        store.set("offset", str(offset))
    return len(updates)
