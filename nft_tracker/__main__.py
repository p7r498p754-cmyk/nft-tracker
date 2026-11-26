"""CLI: python -m nft_tracker {run|loop|diagnose|commands|register-commands|serve-once|scan-once|scan-loop}"""
from __future__ import annotations

import logging
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Event

from . import __version__
from .bot import process_updates, register_commands
from .config import ConfigError, Settings
from .catalog import cache_preview, fetch_catalog, refresh_catalog_if_due
from .floors import refresh
from .monitor import run_cycle, utcnow
from .portals import PortalsClient, PortalsError
from .redact import RedactFilter
from .storage import Storage
from .telegram import TelegramClient, TelegramError

LOG = logging.getLogger("nft_tracker")
ACTION_LISTEN_SECONDS = 600


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
    market_ok = scan_market(cfg, store, tg, portals)
    return command_ok and market_ok


def scan_market(cfg, store, tg, portals):
    if cfg.setup_mode:
        LOG.info("Режим настройки (OWNER_CHAT_ID=0): мониторинг выключен, работает только /id")
        return True
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
    return not bool(rep.error)


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


def warm_catalog(cfg):
    if cfg.setup_mode:
        return
    store = Storage(cfg.db_path)
    try:
        refresh_catalog_if_due(store, utcnow())
    except (OSError, ValueError) as error:
        LOG.warning("Загрузка каталога цен: %s", error)
    finally:
        store.close()


def market_worker(cfg, portals, stop, once):
    store = Storage(cfg.db_path)
    tg = TelegramClient(cfg.telegram_token)
    next_floors = 0.0
    try:
        while not stop.is_set():
            started = time.monotonic()
            ok = scan_market(cfg, store, tg, portals)
            if not cfg.setup_mode and started >= next_floors:
                try:
                    count = refresh(store, tg, cfg, fetch_catalog(), utcnow())
                    LOG.info("Свежих цен коллекций по площадкам: %s", count)
                except (OSError, ValueError, TelegramError) as error:
                    LOG.warning("Обновление цен коллекций: %s", error)
                    if not store.get("price_catalog"):
                        try:
                            cache_preview(store, utcnow())
                        except (OSError, ValueError):
                            LOG.warning("Резервный предпросмотр недоступен")
                    ok = False
                next_floors = started + cfg.scan_seconds
            if once:
                return ok
            stop.wait(max(0, started + cfg.scan_seconds - time.monotonic()))
        return True
    finally:
        store.close()


def verify_polling_host(tg):
    identity = tg.call("getMe", timeout=5)
    if not isinstance(identity, dict) or identity.get("is_bot") is not True or str(identity.get("username")).lower() != "nfttrecker_bot":
        raise TelegramError("Ожидался основной бот @nfttrecker_bot")
    status = tg.call("getWebhookInfo", timeout=5)
    if not isinstance(status, dict) or status.get("url") != "":
        raise TelegramError("Перед запуском polling проверьте активный webhook")


def run_loop(cfg, store, tg, portals, *, once=False):
    stop = Event()
    commands_ok = True
    until = time.monotonic() + ACTION_LISTEN_SECONDS
    with ThreadPoolExecutor(max_workers=1) as pool:
        worker = pool.submit(market_worker, cfg, portals, stop, once)
        try:
            while True:
                if worker.done():
                    market_ok = worker.result()
                    if not once or time.monotonic() >= until:
                        return 0 if market_ok and commands_ok else 1
                try:
                    process_updates(cfg, store, tg, utcnow(), timeout=5)
                    commands_ok = True
                except TelegramError as error:
                    LOG.warning("Ошибка Telegram: %s", error)
                    commands_ok = False
                    if error.code in (401, 403, 409):
                        return 1
                    delay = error.retry_after if isinstance(error.retry_after, int) else 2
                    if once and time.monotonic() + delay >= until:
                        return 1
                    time.sleep(max(1, delay))
        finally:
            stop.set()


def main(argv: list[str]) -> int:
    cmd = argv[1] if len(argv) > 1 else "run"
    if cmd not in {"run", "loop", "diagnose", "commands", "register-commands", "serve-once", "scan-once", "scan-loop"}:
        print("Использование: python -m nft_tracker {run|loop|diagnose|commands|register-commands|serve-once|scan-once|scan-loop}")
        return 2
    try:
        cfg = Settings.from_env(need_portals=cmd not in {"commands", "register-commands"})
    except ConfigError as e:
        print(f"Ошибка настройки: {e}")
        return 2
    if cfg.stars_payments and cmd not in {"loop", "scan-loop", "scan-once", "register-commands"}:
        print("Для приёма платежей запустите loop или отдельный webhook с scan-loop")
        return 2
    _setup_logging(cfg)
    LOG.info("NFT Tracker v%s, режим %s", __version__, cmd)
    if cmd == "diagnose" and not cfg.portals_key:
        LOG.error("Для диагностики необходим секрет PORTALS_API_KEY")
        return 2
    if cmd == "diagnose":
        from .diagnose import run_diagnose
        return run_diagnose(cfg)
    if cmd == "register-commands":
        try:
            tg = TelegramClient(cfg.telegram_token)
            result = tg.call("getMe", timeout=5)
            if not isinstance(result, dict) or result.get("username", "").lower() != "nfttrecker_bot":
                raise TelegramError("Токен принадлежит другому боту")
            register_commands(cfg, tg)
        except TelegramError as error:
            LOG.error("Подсказки команд Telegram: %s", error)
            return 1
        LOG.info("Telegram подтвердил подсказки команд в личном чате")
        return 0
    if cmd == "commands":
        store = Storage(cfg.db_path)
        try:
            with ThreadPoolExecutor(max_workers=1) as pool:
                catalog = pool.submit(warm_catalog, cfg)
                ok = listen_for_commands(cfg, store, TelegramClient(cfg.telegram_token), ACTION_LISTEN_SECONDS)
                catalog.result()
                return 0 if ok else 1
        finally:
            store.close()
    if cmd in {"scan-once", "scan-loop"}:
        portals = None if cfg.setup_mode else PortalsClient(cfg.portals_key, link_template=cfg.listing_url_template,
                                                            purchase_action=cfg.purchase_action)
        try:
            return 0 if market_worker(cfg, portals, Event(), once=cmd == "scan-once") else 1
        except KeyboardInterrupt:
            return 0
    store, tg, portals = _make(cfg)
    try:
        if cmd == "loop" and cfg.stars_payments:
            try:
                verify_polling_host(tg)
            except TelegramError as error:
                LOG.error("Запуск приёма платежей остановлен: %s", error)
                return 1
        if cmd == "run":
            ok = cycle_once(cfg, store, tg, portals)
            warm_catalog(cfg)
            return 0 if ok else 1
        return run_loop(cfg, store, tg, portals, once=cmd == "serve-once")
    except KeyboardInterrupt:
        return 0
    finally:
        store.close()


if __name__ == "__main__":
    sys.exit(main(sys.argv))
