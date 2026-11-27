"""Channel mode: sell signals through a paid Telegram channel.

The paid channel is joined through a Telegram Stars *subscription invite link*
(createChatSubscriptionInviteLink). Telegram charges subscribers monthly,
removes them when they stop paying and credits Stars to the channel balance.
This module never touches money and stores no subscriber data, so it runs on
public GitHub Actions without a server.

Flow per cycle:
1. Fresh verified signals go to the paid channel immediately (no daily cap).
2. After FREE_DELAY_MIN minutes the best few are repeated in the free channel
   with their current status ("still available" / "no longer at this price")
   and a button to the paid channel. This is the autonomous marketing loop.
3. Once a day the free channel gets yesterday's statistics.
"""
from __future__ import annotations

import html
import json
import logging
import statistics
from datetime import datetime, timedelta
from decimal import Decimal

from .storage import Storage, iso
from .telegram import TelegramClient, TelegramError, format_alert
from .valuation import Estimate, MarketEvent, Signal

LOG = logging.getLogger(__name__)
TEASER_WINDOW = timedelta(hours=3)   # older paid posts are not repeated as teasers
NOTICE_COOLDOWN = timedelta(hours=12)
PENDING, TEASED, SKIPPED = 0, 1, 2

SCHEMA = """
CREATE TABLE IF NOT EXISTS channel_posts (
  signal_id TEXT PRIMARY KEY, posted_at TEXT NOT NULL, discount TEXT NOT NULL,
  data TEXT NOT NULL, teaser INTEGER NOT NULL DEFAULT 0, teaser_at TEXT);
CREATE INDEX IF NOT EXISTS channel_posts_time ON channel_posts(posted_at);
CREATE TABLE IF NOT EXISTS channel_copies (
  tier TEXT NOT NULL, signal_id TEXT NOT NULL, day TEXT NOT NULL, PRIMARY KEY (tier, signal_id));
"""


def enabled(cfg) -> bool:
    return bool(cfg.paid_channel)


def ensure_schema(store: Storage) -> None:
    store.db.executescript(SCHEMA)


def already_posted(store: Storage, signal_id: str) -> bool:
    ensure_schema(store)
    return store.db.execute("SELECT 1 FROM channel_posts WHERE signal_id=?", (signal_id,)).fetchone() is not None


# --- serialisation -----------------------------------------------------------
def _dump(sig: Signal) -> str:
    e, est = sig.event, sig.estimate
    return json.dumps({
        "event": [e.market, e.gift_id, e.name, e.model, e.backdrop, e.symbol, str(e.price),
                  iso(e.event_time), e.event_type, e.number, e.link, e.photo_url],
        "estimate": [str(est.value), est.tier, est.sales, iso(est.newest)],
        "discount": str(sig.discount)}, ensure_ascii=False)


def _load(raw: str) -> Signal:
    d = json.loads(raw)
    e = d["event"]
    ev = MarketEvent(e[0], e[1], e[2], e[3], e[4], e[5], Decimal(e[6]), datetime.fromisoformat(e[7]),
                     e[8], e[9], e[10], e[11])
    v, tier, n, newest = d["estimate"]
    return Signal(ev, Estimate(Decimal(v), tier, int(n), datetime.fromisoformat(newest)), Decimal(d["discount"]))


# --- delivery helpers ----------------------------------------------------------
def _post(tg: TelegramClient, chat, text: str, keyboard: list | None, photo: str | None) -> None:
    if photo and len(text) <= 1024:
        try:
            tg.send_photo(chat, photo, text, keyboard)
            return
        except TelegramError as e:
            if e.code != 400:
                raise
    tg.send(chat, text, keyboard)


def _notice(store: Storage, tg: TelegramClient, cfg, key: str, text: str, now: datetime) -> None:
    last = store.get("notice:" + key)
    if last and now - datetime.fromisoformat(last) < NOTICE_COOLDOWN:
        return
    try:
        tg.send(cfg.owner_chat_id, text)
        store.set("notice:" + key, iso(now))
    except TelegramError:
        pass


def _cta(cfg) -> list:
    return [[{"text": "⭐ Сигналы сразу, без задержки", "url": cfg.paid_channel_invite}]] if cfg.paid_channel_invite else []


def publish_paid(cfg, store: Storage, tg: TelegramClient, sig: Signal, signal_id: str,
                 link: str | None, now: datetime) -> bool:
    """Post one verified signal to the paid channel. Returns True on success.

    The row is reserved before sending: an ambiguous network failure keeps it
    (no duplicate post), a definite Telegram rejection releases it for retry.
    """
    ensure_schema(store)
    with store.db:
        cur = store.db.execute("INSERT OR IGNORE INTO channel_posts(signal_id,posted_at,discount,data) VALUES (?,?,?,?)",
                               (signal_id, iso(now), str(sig.discount), _dump(sig)))
    if cur.rowcount == 0:
        return False
    text = format_alert(sig, cfg.price_unit, now, cfg.timezone, bool(link))
    kb = [[{"text": "🔗 Открыть объявление", "url": link}]] if link else None
    try:
        _post(tg, cfg.paid_channel, text, kb, sig.event.photo_url)
    except TelegramError as e:
        if e.code is not None:
            with store.db:
                store.db.execute("DELETE FROM channel_posts WHERE signal_id=?", (signal_id,))
            if e.code in (400, 403):
                _notice(store, tg, cfg, "paid_channel",
                        "⚠️ Бот не может публиковать в платном канале. Проверьте PAID_CHANNEL_ID и что бот — "
                        f"администратор с правом публикации. Ответ Telegram: {e}", now)
        raise
    return True


def _teaser_text(cfg, sig: Signal, posted_at: datetime, now: datetime, status: bool | None) -> str:
    minutes = max(1, int((now - posted_at).total_seconds() // 60))
    body = format_alert(sig, cfg.price_unit, posted_at, cfg.timezone, True)
    lines = [f"⏳ <b>Сигнал с задержкой {minutes} мин</b>", "", body, ""]
    if status is True:
        lines.append("🟢 Лот всё ещё продаётся по этой цене.")
    elif status is False:
        lines.append(f"❌ По этой цене лот уже недоступен (куплен или снят). "
                     f"Подписчики платного канала увидели его {minutes} мин назад.")
    lines.append("⭐ В платном канале — все сигналы сразу после появления лота.")
    return "\n".join(lines)


def _verify(portals, sigs: list[Signal]) -> dict:
    verify_many = getattr(portals, "verify_many", None)
    if not callable(verify_many) or not sigs:
        return {}
    try:
        return verify_many([s.event for s in sigs][:20])
    except Exception as e:  # noqa: BLE001 - status is optional decoration
        LOG.warning("Статус лотов для бесплатного канала: %s", e)
        return {}


def publish_teasers(cfg, store: Storage, tg: TelegramClient, portals, now: datetime) -> int:
    """Repeat the best delayed signals in the free channel. Returns posts sent."""
    if not (enabled(cfg) and cfg.free_channel):
        return 0
    ensure_schema(store)
    ready = now - timedelta(minutes=cfg.free_delay_min)
    with store.db:
        store.db.execute("UPDATE channel_posts SET teaser=? WHERE teaser=? AND posted_at < ?",
                         (SKIPPED, PENDING, iso(ready - TEASER_WINDOW)))
    day = now.date().isoformat()
    used = store.db.execute("SELECT count(*) FROM channel_posts WHERE teaser=? AND substr(teaser_at,1,10)=?",
                            (TEASED, day)).fetchone()[0]
    quota = cfg.free_max_per_day - used
    if quota <= 0:
        return 0
    rows = store.db.execute(
        "SELECT signal_id, posted_at, data FROM channel_posts WHERE teaser=? AND posted_at <= ? "
        "ORDER BY CAST(discount AS REAL) DESC, posted_at LIMIT ?", (PENDING, iso(ready), quota)).fetchall()
    if not rows:
        return 0
    sigs = [_load(r[2]) for r in rows]
    status = _verify(portals, sigs)
    sent = 0
    for (sid, posted_at, _), sig in zip(rows, sigs):
        key = (sig.event.gift_id, sig.event.price)
        listed = status.get(key)
        text = _teaser_text(cfg, sig, datetime.fromisoformat(posted_at), now, listed)
        kb = ([[{"text": "🔗 Открыть объявление", "url": sig.event.link}]] if listed and sig.event.link else []) + _cta(cfg)
        try:
            _post(tg, cfg.free_channel, text, kb or None, sig.event.photo_url)
        except TelegramError as e:
            if e.code is None:   # unknown outcome: never risk a duplicate
                _mark(store, sid, now)
            elif e.code in (400, 403):
                _notice(store, tg, cfg, "free_channel",
                        "⚠️ Бот не может публиковать в бесплатном канале. Проверьте FREE_CHANNEL_ID и права "
                        f"администратора. Ответ Telegram: {e}", now)
            LOG.warning("Публикация в бесплатный канал: %s", e)
            break
        _mark(store, sid, now)
        sent += 1
    return sent


def _mark(store: Storage, sid: str, now: datetime) -> None:
    with store.db:
        store.db.execute("UPDATE channel_posts SET teaser=?, teaser_at=? WHERE signal_id=?", (TEASED, iso(now), sid))


def daily_stats(cfg, store: Storage, tg: TelegramClient, now: datetime) -> bool:
    """Post yesterday's numbers to the free channel once per UTC day."""
    if not (enabled(cfg) and cfg.free_channel):
        return False
    ensure_schema(store)
    yesterday = (now - timedelta(days=1)).date()
    if store.get("channel_stats_day") == yesterday.isoformat():
        return False
    rows = store.db.execute("SELECT discount, teaser FROM channel_posts WHERE substr(posted_at,1,10)=?",
                            (yesterday.isoformat(),)).fetchall()
    if not rows:
        store.set("channel_stats_day", yesterday.isoformat())
        return False
    discounts = [Decimal(r[0]) for r in rows]
    free = sum(1 for r in rows if r[1] == TEASED)
    avg = (sum(discounts) / len(discounts)).quantize(Decimal("1"))
    top = max(discounts).quantize(Decimal("1"))
    text = (f"📊 <b>Итоги {yesterday:%d.%m.%Y}</b>\n\n"
            f"Сигналов в платном канале: <b>{len(rows)}</b>\n"
            f"Средняя скидка к рыночной оценке: <b>{avg}%</b>\n"
            f"Лучшая скидка: <b>{top}%</b>\n"
            f"Показано здесь с задержкой: {free}\n\n"
            "Оценка — медиана недавних продаж на Portals, не гарантия прибыли.")
    try:
        tg.send(cfg.free_channel, text, _cta(cfg) or None)
    except TelegramError as e:
        LOG.warning("Итоги дня в бесплатный канал: %s", e)
        if e.code is not None:
            return False
    store.set("channel_stats_day", yesterday.isoformat())
    return True


def prune(store: Storage, now: datetime, days: int = 14) -> None:
    ensure_schema(store)
    with store.db:
        store.db.execute("DELETE FROM channel_posts WHERE posted_at < ?", (iso(now - timedelta(days=days)),))
        store.db.execute("DELETE FROM channel_copies WHERE day < ?", ((now - timedelta(days=days)).date().isoformat(),))


# --- one-off setup (manual workflow) ------------------------------------------
def setup_report(cfg, tg: TelegramClient) -> tuple[bool, list[str]]:
    """Check bot rights in both channels; create the paid invite link if missing.

    Returns (ok, lines). The invite link (if created) is only sent to the
    owner in Telegram and never printed to the public Actions log.
    """
    lines: list[str] = []
    if not enabled(cfg):
        return False, ["PAID_CHANNEL_ID не задан — режим каналов выключен."]
    me = tg.call("getMe")
    ok = True
    for label, chat, need_invite in (("Канал PRO", cfg.paid_channel, True),
                                     ("Канал PLUS", cfg.plus_channel, True),
                                     ("Канал ULTRA", cfg.ultra_channel, True),
                                     ("Бесплатный канал", cfg.free_channel, False)):
        if not chat:
            lines.append(f"{label}: не задан")
            continue
        try:
            info = tg.call("getChat", {"chat_id": chat})
            member = tg.call("getChatMember", {"chat_id": chat, "user_id": me["id"]})
        except TelegramError as e:
            ok = False
            lines.append(f"❌ {label}: нет доступа ({e}). Добавьте бота администратором канала.")
            continue
        problems = []
        if info.get("type") != "channel":
            problems.append("это не канал")
        if member.get("status") != "administrator":
            problems.append("бот не администратор")
        else:
            if not member.get("can_post_messages"):
                problems.append("нет права «Публикация сообщений»")
            if need_invite and not member.get("can_invite_users"):
                problems.append("нет права «Пригласительные ссылки»")
        if problems:
            ok = False
            lines.append(f"❌ {label}: " + ", ".join(problems))
        else:
            lines.append(f"✅ {label}: права в порядке")
    return ok, lines


def create_paid_invite(cfg, tg: TelegramClient, chat=None, price=None, name="NFT Tracker") -> str:
    result = tg.call("createChatSubscriptionInviteLink", {
        "chat_id": chat or cfg.paid_channel, "name": name, "subscription_period": 2592000,
        "subscription_price": price or cfg.channel_price})
    link = result.get("invite_link") if isinstance(result, dict) else None
    if not isinstance(link, str) or not link.startswith("https://t.me/"):
        raise TelegramError("Telegram не вернул ссылку-подписку")
    return link


# --- paid tiers ---------------------------------------------------------------
def tiers(cfg) -> list[tuple[str, object, str, int]]:
    """(name, chat, invite, price) of configured paid tiers, cheapest first."""
    out = [("PLUS", cfg.plus_channel, cfg.plus_channel_invite, cfg.plus_price),
           ("PRO", cfg.paid_channel, cfg.paid_channel_invite, cfg.channel_price),
           ("ULTRA", cfg.ultra_channel, cfg.ultra_channel_invite, cfg.ultra_price)]
    return [t for t in out if t[1]]


def _detailed(cfg, sig: Signal, now: datetime, link) -> str:
    est = sig.estimate
    return (format_alert(sig, cfg.price_unit, now, cfg.timezone, bool(link)) +
            f"\n\n🔬 Уровень сравнения: {'точное совпадение атрибутов' if est.tier == 'exact' else 'та же модель'}"
            f"\nСопоставимых продаж: {est.sales}. Последняя: {est.newest:%d.%m.%Y %H:%M} UTC."
            "\nВыбросы отсечены по медианному отклонению; при большом разбросе сигнал не публикуется.")


def publish_tier_copies(cfg, store: Storage, tg: TelegramClient, sig: Signal, signal_id: str,
                        link: str | None, now: datetime) -> tuple[int, str | None]:
    """Copy a verified signal to PLUS (daily cap) and ULTRA (detailed). Never raises."""
    ensure_schema(store)
    sent, error = 0, None
    day = now.date().isoformat()
    kb = [[{"text": "🔗 Открыть объявление", "url": link}]] if link else None
    for name, chat, cap, detailed in (("PLUS", cfg.plus_channel, cfg.plus_daily, False),
                                      ("ULTRA", cfg.ultra_channel, None, True)):
        if not chat:
            continue
        with store.db:
            used = store.db.execute("SELECT count(*) FROM channel_copies WHERE tier=? AND day=?", (name, day)).fetchone()[0]
            if cap is not None and used >= cap:
                continue
            cur = store.db.execute("INSERT OR IGNORE INTO channel_copies VALUES (?,?,?)", (name, signal_id, day))
        if cur.rowcount == 0:
            continue
        text = _detailed(cfg, sig, now, link) if detailed else format_alert(sig, cfg.price_unit, now, cfg.timezone, bool(link))
        try:
            _post(tg, chat, text, kb, sig.event.photo_url)
            sent += 1
        except TelegramError as e:
            error = str(e)
            if e.code is not None:
                with store.db:
                    store.db.execute("DELETE FROM channel_copies WHERE tier=? AND signal_id=?", (name, signal_id))
                if e.code in (400, 403):
                    _notice(store, tg, cfg, name.lower() + "_channel",
                            f"⚠️ Бот не может публиковать в канале {name}. Проверьте {name}_CHANNEL_ID и права "
                            f"администратора. Ответ Telegram: {e}", now)
    return sent, error


def ultra_report(cfg, store: Storage, tg: TelegramClient, now: datetime) -> bool:
    """Once per UTC day: market analytics for yesterday in the ULTRA channel."""
    if not cfg.ultra_channel:
        return False
    ensure_schema(store)
    yesterday = (now - timedelta(days=1)).date().isoformat()
    if store.get("ultra_report_day") == yesterday:
        return False
    rows = store.db.execute("SELECT name, price FROM sales WHERE substr(event_time,1,10)=?", (yesterday,)).fetchall()
    signals = store.db.execute("SELECT discount FROM channel_posts WHERE substr(posted_at,1,10)=?", (yesterday,)).fetchall()
    if not rows and not signals:
        store.set("ultra_report_day", yesterday)
        return False
    by_name: dict[str, list[Decimal]] = {}
    for name, price in rows:
        by_name.setdefault(name, []).append(Decimal(price))
    top = sorted(by_name.items(), key=lambda kv: (-len(kv[1]), kv[0]))[:7]
    unit = html.escape(cfg.price_unit)
    lines = [f"📈 <b>Рынок Portals за {datetime.fromisoformat(yesterday):%d.%m.%Y}</b>", "",
             f"Продаж в выборке бота: <b>{len(rows)}</b>"]
    if top:
        lines.append("\n<b>Самые продаваемые коллекции</b> (продаж · медиана цены):")
        for name, prices in top:
            lines.append(f"• {html.escape(name)} — {len(prices)} · {statistics.median(prices).normalize():f} {unit}")
    if signals:
        ds = [Decimal(r[0]) for r in signals]
        lines.append(f"\nСигналов за день: <b>{len(ds)}</b>, средняя скидка {(sum(ds) / len(ds)).quantize(Decimal('1'))}%, "
                     f"лучшая {max(ds).quantize(Decimal('1'))}%.")
    lines.append("\nВыборка — последние продажи, которые видит бот, а не весь рынок.")
    try:
        tg.send(cfg.ultra_channel, "\n".join(lines))
    except TelegramError as e:
        LOG.warning("Аналитика ULTRA: %s", e)
        if e.code is not None:
            return False
    store.set("ultra_report_day", yesterday)
    return True


def free_link(cfg) -> str:
    chat = cfg.free_channel
    return f"https://t.me/{chat[1:]}" if isinstance(chat, str) and chat.startswith("@") else ""


TIER_TEXT = {
    "PLUS": "до {daily} сигналов в сутки сразу после проверки лота",
    "PRO": "все сигналы сразу, без лимита",
    "ULTRA": "все сигналы с подробным разбором оценки и ежедневная аналитика рынка",
}


def shop(cfg) -> tuple[str, list] | None:
    """Storefront: tiers sold as Telegram Stars channel subscriptions."""
    offers = [t for t in tiers(cfg) if t[2]]
    if not offers:
        return None
    lines = ["⭐ <b>Подписки NFT Tracker</b>", ""]
    buttons = []
    free = free_link(cfg)
    lines.append("<b>FREE</b> · бесплатно — до 3 лучших сигналов в сутки с задержкой 1 час и итоги дня" +
                 (" в открытом канале." if free else "."))
    if free:
        buttons.append([{"text": "FREE · открыть канал", "url": free}])
    for name, _, invite, price in offers:
        lines.append(f"<b>{name}</b> · {price} ⭐ / 30 дней — " + TIER_TEXT[name].format(daily=cfg.plus_daily) + ".")
        buttons.append([{"text": f"{name} · {price} ⭐", "url": invite}])
    lines += ["", "Оплата Stars проходит в Telegram при вступлении в канал. Подписка продлевается автоматически, "
              "отменить можно в любой момент в настройках канала.",
              "Рынок проверяется примерно раз в час. Сигнал — оценка по медиане недавних продаж Portals, а не гарантия прибыли."]
    return "\n".join(lines), buttons


def bot_description(cfg) -> str:
    """Plain text shown before /start (max 512 chars), so the shop works even while the bot is offline."""
    parts = ["Сигналы о подарках Telegram на Portals со скидкой от 30% к медиане недавних продаж."]
    free = free_link(cfg)
    if free:
        parts.append(f"FREE: {free}")
    for name, _, invite, price in tiers(cfg):
        if invite:
            parts.append(f"{name} {price}⭐/мес: {invite}")
    parts.append("Оценка, не гарантия прибыли.")
    return "\n".join(parts)[:512]
