"""Background poller for /notifications.

Watches the authenticated user's notification feed and forwards new
notifications (replies, mentions, likes…) to the Telegram owner.

Dedup is done via the highest-seen ``notification_id``: only newer ones are
sent. The high-water mark is persisted in the ``settings`` table so that a
restart doesn't re-send anything the user has already received.
"""

from __future__ import annotations

import asyncio
import html
import logging
from datetime import UTC, datetime

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.config import Config
from app.db import Store
from app.lolz import LolzClient
from app.lolz.client import LolzApiError

log = logging.getLogger(__name__)

_SETTING_KEY = "last_notification_id"


class NotifPoller:
    def __init__(self, config: Config, store: Store, lolz: LolzClient, bot: Bot) -> None:
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
        self._task = asyncio.create_task(self._loop(), name="lolz-notif-poller")

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
                log.exception("Notif-poller iteration failed")
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=self._config.notif_poll_interval_seconds,
                )
            except TimeoutError:
                pass

    async def _poll_once(self) -> None:
        if not await self._store.is_unlocked():
            return
        try:
            data = await self._lolz.list_notifications(limit=20, page=1)
        except LolzApiError as e:
            log.warning("list_notifications failed: %s", e)
            return

        notifications = data.get("notifications") or []
        if not notifications:
            return

        last_seen_str = await self._store.get_setting(_SETTING_KEY)
        last_seen = int(last_seen_str) if last_seen_str else 0

        # Bootstrap: on the very first run we don't have a baseline; record the
        # newest id and don't spam the user with everything in their inbox.
        if last_seen == 0:
            highest = max(int(n.get("notification_id") or 0) for n in notifications)
            await self._store.set_setting(_SETTING_KEY, str(highest))
            return

        new_ones = [
            n for n in notifications if int(n.get("notification_id") or 0) > last_seen
        ]
        # Send oldest-first.
        new_ones.sort(key=lambda n: int(n.get("notification_id") or 0))

        for n in new_ones:
            try:
                await self._send_one(n)
            except TelegramBadRequest as e:
                log.warning(
                    "Failed to deliver notification %s: %s",
                    n.get("notification_id"),
                    e,
                )
                continue
            highest = int(n.get("notification_id") or 0)
            if highest > last_seen:
                last_seen = highest
                await self._store.set_setting(_SETTING_KEY, str(last_seen))

    async def _send_one(self, n: dict) -> None:
        text = _format(n)
        kb = _kb_for(n)
        await self._bot.send_message(
            self._config.telegram_owner_id,
            text,
            parse_mode="HTML",
            disable_web_page_preview=True,
            reply_markup=kb,
        )


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def _kb_for(n: dict) -> InlineKeyboardMarkup | None:
    url = _link_for(n)
    if not url:
        return None
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="🌐 Открыть", url=url)]]
    )


def _link_for(n: dict) -> str:
    ctype = (n.get("content_type") or "").lower()
    cid = n.get("content_id")
    if ctype == "post" and cid:
        return f"https://lolz.live/posts/{cid}/"
    if ctype == "thread" and cid:
        return f"https://lolz.live/threads/{cid}/"
    if ctype == "profile_post" and cid:
        return f"https://lolz.live/profile-posts/{cid}/"
    if ctype == "user" and cid:
        return f"https://lolz.live/members/{cid}/"
    return ""


def _format(n: dict) -> str:
    """Render a single notification as a TG HTML message."""
    creator = html.escape(str(n.get("creator_username") or "?"))
    when = _fmt_ts(int(n.get("notification_create_date") or 0))
    icon, action = _label(n)
    head = f"{icon} <b>{creator}</b> {action}"
    tail = f"\n<i>{when}</i>"
    return head + tail


def _label(n: dict) -> tuple[str, str]:
    """Pick an emoji + verb based on (content_type, content_action)."""
    ctype = (n.get("content_type") or "").lower()
    action = (n.get("content_action") or "").lower()

    if ctype == "post":
        if action == "insert":
            return "💬", "ответил в твоей теме"
        if action == "like":
            return "❤", "лайкнул твой пост"
        if action == "mention":
            return "📣", "упомянул тебя"
    if ctype == "thread":
        if action == "insert":
            return "📌", "создал тему"
        if action == "watch":
            return "👀", "подписался на твою тему"
    if ctype == "profile_post":
        return "📝", "написал на твоей странице"
    if ctype == "user" and action == "follow":
        return "➕", "подписался на тебя"
    if ctype == "conversation":
        return "✉", "написал в личку"
    return "🔔", f"{ctype}/{action}".strip("/")


def _fmt_ts(ts: int) -> str:
    if not ts:
        return "—"
    return datetime.fromtimestamp(ts, tz=UTC).strftime("%Y-%m-%d %H:%M UTC")
