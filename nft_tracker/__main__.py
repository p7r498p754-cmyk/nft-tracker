"""CLI: python -m nft_tracker {run|loop|diagnose|commands}"""
from __future__ import annotations

import logging
import sys
import time

from . import __version__
from .bot import process_updates
from .config import ConfigError, Settings
from .monitor import run_cycle, utcnow
from .portals import PortalsClient, PortalsError
from .redact import RedactFilter
from .storage import Storage
from .telegram import TelegramClient, TelegramError

LOG = logging.getLogger("nft_tracker")


def _setup_logging(cfg: Settings) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    flt = RedactFilter([cfg.telegram_token, cfg.portals_key])
    for handler in logging.getLogger().handlers:
        handler.addFilter(flt)


def _make(cfg: Settings) -> tuple[Storage, TelegramClient, PortalsClient | None]:
    # In /id-only setup mode a Portals key is not required; avoid building a
    # client that rejects the intentionally absent key before commands run.
    portals = (None if cfg.setup_mode else
               PortalsClient(cfg.portals_key, link_template=cfg.listing_url_template,
                             purchase_action=cfg.purchase_action))
    return Storage(cfg.db_path), TelegramClient(cfg.telegram_token), portals


def cycle_once(cfg: Settings, store: Storage, tg: TelegramClient, portals: PortalsClient | None) -> bool:
    command_ok = True
    try:
        n = process_updates(cfg, store, tg, utcnow())
        LOG.info("Обработано команд: %s", n)
    except TelegramError as e:
        LOG.warning("Команды не обработаны; повторим при следующем запуске: %s", e)
        command_ok = False
    if cfg.setup_mode:
        LOG.info("Режим настройки (OWNER_CHAT_ID=0): мониторинг выключен, работает только /id")
        return command_ok
    if portals is None:
        raise ConfigError("Не задан PORTALS_API_KEY для мониторинга")
    LOG.info("Активных получателей: %s", len(store.active_users()))
    rep = run_cycle(cfg, store, tg, portals)
    LOG.info("Проверка: объявлений=%s кандидатов=%s отправлено=%s без_ссылки=%s не_подтверждено=%s новых_продаж=%s",
             rep.listings, rep.candidates, rep.alerts, rep.skipped_no_link, rep.skipped_unverified, rep.new_sales)
    if rep.error:
        LOG.warning("Ошибка проверки: %s", rep.error)
    if rep.warning:
        LOG.warning("Предупреждение: %s", rep.warning)
    # A failed Portals scan must not appear as a green Actions run. State is
    # still saved by the workflow's `if: always()` cache step.
    return command_ok and not bool(rep.error)


def listen_for_commands(cfg, store, tg, seconds):
    last_poll_ok = False
    deadline = time.monotonic() + seconds
    while (remaining := int(deadline - time.monotonic())) > 0:
        try:
            process_updates(cfg, store, tg, utcnow(), timeout=min(20, remaining))
            last_poll_ok = True
        except TelegramError as error:
            last_poll_ok = False
            LOG.warning("Опрос команд Telegram: %s", error)
            if error.code in (401, 403, 409):
                return False
            delay = error.retry_after if isinstance(error.retry_after, int) else 3
            if delay >= remaining:
                return False
            time.sleep(max(1, delay))
    return last_poll_ok


def main(argv: list[str]) -> int:
    cmd = argv[1] if len(argv) > 1 else "run"
    if cmd not in {"run", "loop", "diagnose", "commands"}:
        print("Использование: python -m nft_tracker {run|loop|diagnose|commands}")
        return 2
    try:
        cfg = Settings.from_env(need_portals=cmd != "commands")
    except ConfigError as e:
        print(f"Ошибка настройки: {e}")
        return 2
    _setup_logging(cfg)
    LOG.info("NFT Tracker v%s, режим %s", __version__, cmd)
    if cmd == "diagnose" and not cfg.portals_key:
        LOG.error("Для диагностики необходим секрет PORTALS_API_KEY")
        return 2
    if cmd == "diagnose":
        from .diagnose import run_diagnose
        return run_diagnose(cfg)
    if cmd == "commands":
        store = Storage(cfg.db_path)
        try:
            return 0 if listen_for_commands(cfg, store, TelegramClient(cfg.telegram_token), 90) else 1
        finally:
            store.close()
    store, tg, portals = _make(cfg)
    try:
        if cmd == "run":
            return 0 if cycle_once(cfg, store, tg, portals) else 1
        while True:  # local / own-host mode
            cycle_once(cfg, store, tg, portals)
            if not listen_for_commands(cfg, store, tg, cfg.scan_seconds):
                return 1
    except KeyboardInterrupt:
        return 0
    finally:
        store.close()


if __name__ == "__main__":
    sys.exit(main(sys.argv))
