"""Send one explicitly marked, read-only owner diagnostic using two Portals responses."""
from datetime import datetime, timedelta, timezone
from html import escape
from .config import Settings
from .links import safe_link
from .portals import PortalsClient, parse_event, dec
from .telegram import TelegramClient, alert_keyboard


def main():
    cfg = Settings.from_env()
    tg = TelegramClient(cfg.telegram_token)
    if not cfg.owner_chat_id or tg.call("getMe").get("username", "").lower() != "nfttrecker_bot":
        raise RuntimeError("Unexpected diagnostic destination")
    client = PortalsClient(cfg.portals_key)
    now = datetime.now(timezone.utc)
    events = client.listings(pages=2, since=now-timedelta(minutes=45))
    candidates = {}
    for event in sorted(events, key=lambda e: e.event_time, reverse=True):
        if now-timedelta(minutes=45) <= event.event_time <= now:
            candidates.setdefault(event.gift_id, event)
    ids = list(candidates)[:20]
    if not ids:
        print("No recent listings")
        return
    rows = client.get("/nfts/search", {"ids": ids, "status": "listed", "limit": len(ids)})["results"]
    for row in rows:
        event = candidates.get(row.get("id"))
        if not event or row.get("status") != "listed" or dec(row.get("price")) != event.price:
            continue
        fresh = parse_event(dict(nft=row, type="listing", created_at=now.isoformat(), amount=row["price"]))
        if not fresh or not all(getattr(fresh,k) and getattr(fresh,k)==getattr(event,k)
                                for k in ("number","name","model","backdrop","symbol","photo_url")):
            continue
        link = safe_link(row.get("listing_url"))
        if link != event.link:
            link = None
        caption = (f"🧪 Тест доставки · реальное объявление Portals\n{escape(fresh.name)} #{escape(fresh.number)}"
                   f"\nМодель: {escape(fresh.model)}\nФон: {escape(fresh.backdrop)}\nУзор: {escape(fresh.symbol)}"
                   f"\nЦена: {fresh.price} {escape(cfg.price_unit)}")
        if not link:
            caption += "\nПрямая ссылка на этот лот не подтверждена."
        msg = tg.send_photo(cfg.owner_chat_id, fresh.photo_url, caption, alert_keyboard(link))
        if not (msg.get("message_id") and msg.get("photo") and msg.get("chat",{}).get("id")==cfg.owner_chat_id):
            raise RuntimeError("Delivery not confirmed")
        print("Telegram accepted matched owner card; listing_url=" + str(bool(link)))
        return
    print("No card matched across both responses; no message sent")


if __name__ == "__main__":
    main()
