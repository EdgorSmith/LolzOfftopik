"""Entry point: run aiogram bot, lolz poller, and HTTP /health server in one process."""

from __future__ import annotations

import asyncio
import logging
import signal

import aiohttp
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties

from app.bot.handlers import build_router
from app.config import Config
from app.db import Store
from app.file_proxy import add_routes as add_proxy_routes
from app.health import build_app as build_http_app
from app.health import run_http_server
from app.lolz import LolzClient
from app.notif_poller import NotifPoller
from app.poller import Poller


async def amain() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    config = Config.from_env()
    store = Store(config.db_path)
    lolz = LolzClient(config.lolz_api_base, config.lolz_api_token)

    # Fetch /users/me to learn our own username/id, used for "is this my thread/post"
    # checks (delete buttons, etc.). Best-effort — failure is non-fatal.
    try:
        me = await lolz.me()
        if me:
            store.set_self_user(int(me.get("user_id", 0) or 0), str(me.get("username", "")))
            logging.getLogger(__name__).info(
                "Authenticated as user_id=%s username=%s",
                store.self_user_id, store.self_username,
            )
    except Exception as e:  # noqa: BLE001
        logging.getLogger(__name__).warning("/users/me lookup failed: %s", e)

    bot = Bot(
        config.telegram_bot_token,
        default=DefaultBotProperties(parse_mode="HTML"),
    )
    dp = Dispatcher()
    dp.include_router(build_router(config, store, lolz))

    poller = Poller(config, store, lolz, bot)
    poller.start()

    notif_poller = NotifPoller(config, store, lolz, bot)
    notif_poller.start()

    http_app = build_http_app()
    add_proxy_routes(
        http_app,
        config,
        store,
        session_factory=lambda: aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60)),
    )
    http_runner = await run_http_server(config.http_port, app=http_app)

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:  # Windows
            pass

    polling_task = asyncio.create_task(dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types()))

    try:
        await stop_event.wait()
    finally:
        await dp.stop_polling()
        polling_task.cancel()
        try:
            await polling_task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
        await poller.stop()
        await notif_poller.stop()
        await lolz.close()
        await bot.session.close()
        await http_runner.cleanup()


def main() -> None:
    asyncio.run(amain())


if __name__ == "__main__":
    main()
