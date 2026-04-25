"""Entry point: run aiogram bot, lolz poller, and HTTP /health server in one process."""

from __future__ import annotations

import asyncio
import logging
import signal

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties

from app.bot.handlers import build_router
from app.config import Config
from app.db import Store
from app.health import run_http_server
from app.lolz import LolzClient
from app.poller import Poller


async def amain() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    config = Config.from_env()
    store = Store(config.db_path)
    lolz = LolzClient(config.lolz_api_base, config.lolz_api_token)

    bot = Bot(
        config.telegram_bot_token,
        default=DefaultBotProperties(parse_mode="HTML"),
    )
    dp = Dispatcher()
    dp.include_router(build_router(config, store, lolz))

    poller = Poller(config, store, lolz, bot)
    poller.start()

    http_runner = await run_http_server(config.http_port)

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
        await lolz.close()
        await bot.session.close()
        await http_runner.cleanup()


def main() -> None:
    asyncio.run(amain())


if __name__ == "__main__":
    main()
