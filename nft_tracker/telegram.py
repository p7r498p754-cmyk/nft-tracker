"""Telegram Bot API client and message formatting (Russian UI)."""
from __future__ import annotations

import html
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from decimal import Decimal
from typing import Any, Callable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .valuation import Signal

API = "https://api.telegram.org/bot"
TIMEOUT = 25


class TelegramError(RuntimeError):
    def __init__(self, message: str, code: int | None = None, retry_after: int | None = None):
        super().__init__(message)
        self.code = code
        self.retry_after = retry_after

    @property
    def blocked(self) -> bool:
        return self.code == 403


class TelegramClient:
    def __init__(self, token: str, opener: Callable[..., Any] | None = None):
        self._token = token
        self._open = opener or urllib.request.urlopen

    def call(self, method: str, payload: dict | None = None, timeout: int = TIMEOUT) -> Any:
        url = API + urllib.parse.quote(self._token, safe=":") + "/" + method
        req = urllib.request.Request(url, data=json.dumps(payload or {}, ensure_ascii=False).encode(),
                                     headers={"Content-Type": "application/json"})
        try:
            with self._open(req, timeout=timeout) as r:
                result = json.load(r)
        except urllib.error.HTTPError as e:
            desc, retry = f"HTTP {e.code}", None
            try:
                body = json.load(e)
                desc = str(body.get("description", desc))[:200]
                retry = (body.get("parameters") or {}).get("retry_after")
            except Exception:  # noqa: BLE001
                pass
            raise TelegramError(f"Telegram {method}: {desc}", e.code, retry) from None
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError):
            raise TelegramError(f"Telegram {method}: сеть/некорректный ответ") from None
        if not isinstance(result, dict) or result.get("ok") is not True:
            raise TelegramError(f"Telegram {method}: запрос не подтверждён")
        return result.get("result")

    def send(self, chat_id: int, text: str, keyboard: list | None = None) -> Any:
        payload: dict = {"chat_id": chat_id, "text": text, "parse_mode": "HTML",
                         "link_preview_options": {"is_disabled": True}}
        if keyboard:
            payload["reply_markup"] = {"inline_keyboard": keyboard}
        return self.call("sendMessage", payload)

    def send_photo(self, chat_id: int, photo: str, caption: str, keyboard: list | None = None) -> Any:
        payload: dict = {"chat_id": chat_id, "photo": photo, "caption": caption, "parse_mode": "HTML"}
        if keyboard:
            payload["reply_markup"] = {"inline_keyboard": keyboard}
        return self.call("sendPhoto", payload)

    def edit(self, chat_id: int, message_id: int, text: str, keyboard: list | None = None) -> Any:
        payload: dict = {"chat_id": chat_id, "message_id": message_id, "text": text, "parse_mode": "HTML",
                         "link_preview_options": {"is_disabled": True}}
        if keyboard:
            payload["reply_markup"] = {"inline_keyboard": keyboard}
        return self.call("editMessageText", payload)

    def answer_callback(self, callback_id: str) -> Any:
        return self.call("answerCallbackQuery", {"callback_query_id": callback_id}, timeout=2)

    def updates(self, offset: int, timeout: int = 0) -> list[dict]:
        payload = {"offset": offset, "timeout": timeout, "allowed_updates": ["message", "callback_query"]}
        try:
            result = self.call("getUpdates", payload, timeout=TIMEOUT + timeout)
        except TelegramError as error:
            if error.code is not None and error.code < 500:
                raise
            time.sleep(1)
            result = self.call("getUpdates", payload, timeout=TIMEOUT + timeout)
        if not isinstance(result, list):
            raise TelegramError("Telegram getUpdates: неожиданный ответ")
        return result


def fmt_price(value: Decimal, unit: str) -> str:
    text = f"{value:f}"
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return f"{text} {unit}"


def local_time(at: datetime, tz_name: str) -> str:
    try:
        zone = ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError):
        zone = ZoneInfo("UTC")
    return at.astimezone(zone).strftime("%d.%m.%Y %H:%M:%S") + f" ({zone.key})"


TIER_TEXT = {"exact": "та же коллекция, модель, фон и символ", "model": "та же коллекция и модель"}


def format_alert(sig: Signal, unit: str, found_at: datetime, tz_name: str, has_link: bool) -> str:
    ev, est = sig.event, sig.estimate
    unit = html.escape(unit)
    lines = [
        "🚨 <b>НАЙДЕН ДЕШЁВЫЙ NFT!</b>", "",
        f"💎 Коллекция: {html.escape(ev.name)}",
        f"🎨 Модель: {html.escape(ev.model or '—')}",
        f"🔢 Номер: {html.escape('#' + ev.number if ev.number else '—')}",
        f"💰 Цена объявления: {fmt_price(ev.price, unit)}",
        f"📊 Рыночная оценка: {fmt_price(est.value, unit)}",
        f"📉 Предполагаемая скидка: {sig.shown_discount}%",
        "🏪 Маркетплейс: Portals",
        f"⏱ Время обнаружения: {local_time(found_at, tz_name)}", "",
        f"ℹ️ Оценка = медиана {est.sales} недавних продаж ({TIER_TEXT[est.tier]}).",
        "⚠️ Это оценка, а не гарантированная прибыль. Лот мог измениться — проверьте цену перед покупкой.",
    ]
    if not has_link:
        lines.append("🔎 Прямая ссылка не настроена: найдите лот в Portals по названию и номеру.")
    return "\n".join(lines)


def alert_keyboard(link: str | None) -> list | None:
    return [[{"text": "🔗 Открыть объявление", "url": link}]] if link else None
