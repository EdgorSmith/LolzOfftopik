"""Background poller that watches a forum and sends new threads to Telegram."""

from __future__ import annotations

import asyncio
import logging

from aiogram import Bot

from app.bot.cards import send_thread_card
from app.config import Config
from app.db import Store
from app.lolz import LolzClient

log = logging.getLogger(__name__)


class Poller:
    def __init__(
        self,
        config: Config,
        store: Store,
        lolz: LolzClient,
        bot: Bot,
    ) -> None:
        self._config = config
        self._store = store
        self._lolz = lolz
        self._bot = bot
        self._task: asyncio.Task | None = None
        self._stop_event = asyncio.Event()

    def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(self._loop(), name="lolz-poller")

    async def stop(self) -> None:
        self._stop_event.set()
        if self._task:
            try:
                await asyncio.wait_for(self._task, timeout=5)
            except TimeoutError:
                self._task.cancel()
        self._task = None

    async def _loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                await self._poll_once()
            except Exception:  # noqa: BLE001
                log.exception("Poller iteration failed")
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=self._config.poll_interval_seconds)
            except TimeoutError:
                pass

    async def _poll_once(self) -> None:
        if not await self._store.is_polling_enabled():
            return

        baseline = await self._store.get_baseline_thread_id()
        threads = await self._lolz.list_threads(
            self._config.lolz_offtop_forum_id,
            limit=20,
            order="post_date",
            direction="desc",
            include_sticky=False,
        )
        # Process oldest-first so the user gets natural ordering in TG.
        new_threads = [t for t in threads if t.thread_id > baseline]
        new_threads.sort(key=lambda t: t.thread_id)
        if not new_threads:
            return

        for thread in new_threads:
            if await self._store.is_seen(thread.thread_id):
                continue
            try:
                await send_thread_card(
                    self._bot, self._store, self._config.telegram_owner_id, thread
                )
            except Exception:  # noqa: BLE001
                log.exception("Failed to send card for thread %s", thread.thread_id)
                continue
            await self._store.mark_seen(thread.thread_id)
            await self._store.set_baseline_thread_id(thread.thread_id)
