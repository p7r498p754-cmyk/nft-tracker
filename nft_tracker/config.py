"""Settings loaded from environment variables only (no secrets in files)."""
from __future__ import annotations

import os
import re
from urllib.parse import urlsplit
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Mapping

from .links import validate_template


class ConfigError(ValueError):
    pass


def _int(env: Mapping[str, str], name: str, default: int, lo: int, hi: int) -> int:
    raw = env.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ConfigError(f"{name} должен быть целым числом") from None
    if not lo <= value <= hi:
        raise ConfigError(f"{name} должен быть в диапазоне {lo}..{hi}")
    return value


def _flag(env: Mapping[str, str], name: str) -> bool:
    return env.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    telegram_token: str
    portals_key: str
    owner_chat_id: int
    db_path: Path
    price_unit: str = "GRAM"
    timezone: str = "UTC"
    listing_url_template: str = ""
    purchase_action: str = "buy"
    max_event_age_min: int = 45
    default_discount: int = 30
    model_extra_margin: Decimal = Decimal("10")
    allow_no_link_alerts: bool = False
    scan_seconds: int = 600
    listing_pages: int = 12   # 20 events/page; Portals allows offsets through 700
    on_actions: bool = False
    github_event: str = ""
    closed_beta: bool = False
    public_users: bool = False
    owner_lifetime_ultra: bool = False
    stars_payments: bool = False
    stars_support: str = ""
    stars_terms: str = ""

    @property
    def setup_mode(self) -> bool:
        """OWNER_CHAT_ID=0: only /id works, nothing is monitored."""
        return self.owner_chat_id == 0

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None, *, need_portals: bool = True) -> "Settings":
        env = os.environ if env is None else env
        token = env.get("TELEGRAM_BOT_TOKEN", "").strip()
        if not token:
            raise ConfigError("Не задан секрет TELEGRAM_BOT_TOKEN")
        key = env.get("PORTALS_API_KEY", "").strip()
        owner = env.get("OWNER_CHAT_ID", "").strip()
        if not owner.isdecimal():
            raise ConfigError("OWNER_CHAT_ID должен быть числом (узнать: команда /id у бота; пока не знаете — 0)")
        if need_portals and int(owner) != 0 and not key:
            raise ConfigError("Не задан секрет PORTALS_API_KEY")
        template = env.get("PORTALS_LISTING_URL_TEMPLATE", "").strip()
        if template:
            validate_template(template)
        try:
            margin = Decimal(env.get("MODEL_EXTRA_MARGIN", "10").strip() or "10")
        except InvalidOperation:
            raise ConfigError("MODEL_EXTRA_MARGIN должен быть числом") from None
        if not margin.is_finite() or not Decimal(0) <= margin <= Decimal(50):
            raise ConfigError("MODEL_EXTRA_MARGIN должен быть в диапазоне 0..50")
        db_path = Path(env.get("DB_PATH", "state/tracker.sqlite3"))
        stars_payments = _flag(env, "STARS_PAYMENTS")
        support = env.get("STARS_SUPPORT", "").strip().removeprefix("@")
        terms = env.get("STARS_TERMS_URL", "").strip()
        beta = _flag(env, "CLOSED_BETA")
        if beta and (stars_payments or _flag(env, "PUBLIC_USERS") or _flag(env, "GITHUB_ACTIONS") or int(owner) == 0 or str(db_path) == ":memory:"):
            raise ConfigError("Закрытой бете нужны владелец, приватная постоянная база и выключенные платежи.")
        if stars_payments:
            if _flag(env, "GITHUB_ACTIONS") or not _flag(env, "PUBLIC_USERS") or int(owner) == 0 or str(db_path) == ":memory:":
                raise ConfigError("Оплате требуется непрерывный запуск с приватной базой и PUBLIC_USERS=1")
            if not re.fullmatch(r"[A-Za-z0-9_]{5,32}", support):
                raise ConfigError("Задайте Telegram username поддержки в STARS_SUPPORT")
            try:
                url = urlsplit(terms)
                if url.scheme != "https" or not url.hostname or url.username or url.password:
                    raise ValueError
            except ValueError:
                raise ConfigError("Задайте HTTPS-ссылку на условия покупки в STARS_TERMS_URL") from None
        return cls(
            telegram_token=token, portals_key=key, owner_chat_id=int(owner),
            db_path=db_path,
            stars_payments=stars_payments, stars_support=support, stars_terms=terms,
            price_unit=(env.get("PRICE_UNIT", "GRAM").strip() or "GRAM")[:12],
            timezone=env.get("TIMEZONE", "UTC").strip() or "UTC",
            listing_url_template=template,
            purchase_action=env.get("PORTALS_PURCHASE_ACTION", "buy").strip() or "buy",
            max_event_age_min=_int(env, "MAX_EVENT_AGE_MIN", 45, 5, 720),
            default_discount=_int(env, "DEFAULT_DISCOUNT", 30, 30, 90),
            model_extra_margin=margin,
            allow_no_link_alerts=_flag(env, "ALLOW_NO_LINK_ALERTS"),
            scan_seconds=_int(env, "SCAN_SECONDS", 600, 60, 86400),
            listing_pages=_int(env, "LISTING_PAGES", 12, 1, 36),
            owner_lifetime_ultra=_flag(env, "OWNER_LIFETIME_ULTRA"),
            closed_beta=beta,
            public_users=_flag(env, "PUBLIC_USERS") and not _flag(env, "GITHUB_ACTIONS") and int(owner) > 0,
            github_event=env.get("GITHUB_EVENT_NAME", ""),
            on_actions=env.get("GITHUB_ACTIONS", "").lower() == "true",
        )
