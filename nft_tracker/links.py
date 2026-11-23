"""Link safety. A link is only ever built from a template the owner configured
or taken from an API field; nothing is guessed."""
from __future__ import annotations

import ipaddress
import urllib.parse
from typing import Any

LISTING_HOSTS = frozenset({"t.me", "telegram.me", "portal-market.com", "portals.tg", "app.portals.tg"})
IMAGE_HOST_SUFFIXES = ("fragment.com", "portal-market.com", "portals.tg")


def _clean_https(raw: Any) -> urllib.parse.SplitResult | None:
    if not isinstance(raw, str) or not raw or len(raw) > 2048:
        return None
    if any(ch.isspace() or ord(ch) < 32 for ch in raw):
        return None
    try:
        p = urllib.parse.urlsplit(raw)
        if p.scheme != "https" or not p.hostname or p.username or p.password:
            return None
        if p.port not in (None, 443) or p.fragment:
            return None
        try:
            ipaddress.ip_address(p.hostname)
            return None
        except ValueError:
            return p
    except ValueError:
        return None


def safe_link(raw: Any) -> str | None:
    """HTTPS link on an allow-listed host that is not just a homepage."""
    p = _clean_https(raw)
    if p is None or p.hostname.lower() not in LISTING_HOSTS:
        return None
    if not p.path or p.path == "/":
        return None
    return raw


def safe_image_url(raw: Any) -> str | None:
    p = _clean_https(raw)
    if p is None:
        return None
    host = p.hostname.lower()
    if any(host == s or host.endswith("." + s) for s in IMAGE_HOST_SUFFIXES):
        return raw
    return None


def build_listing_link(template: str, gift_id: str) -> str | None:
    if not template or not isinstance(gift_id, str) or not gift_id:
        return None
    return safe_link(template.replace("{id}", urllib.parse.quote(gift_id, safe="")))


def validate_template(template: str) -> None:
    from .config import ConfigError
    if template.count("{id}") != 1:
        raise ConfigError("PORTALS_LISTING_URL_TEMPLATE должен содержать ровно один {id}")
    if build_listing_link(template, "test-id-1") is None:
        raise ConfigError("PORTALS_LISTING_URL_TEMPLATE: нужен https-адрес разрешённого домена "
                          "(t.me, portal-market.com, portals.tg) с путём, не главная страница")
