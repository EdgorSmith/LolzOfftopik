"""Background poller for /notifications.

Watches the authenticated user's notification feed and forwards new
notifications (replies, mentions, likes, comments, money transfers…) to the
Telegram owner.

Dedup is done via the highest-seen ``notification_id``: only newer ones are
sent. The high-water mark is persisted in the ``settings`` table so that a
restart doesn't re-send anything the user has already received.
"""

from __future__ import annotations

import asyncio
import html
import logging
import re

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from selectolax.parser import HTMLParser

from app.config import Config
from app.db import Store
from app.lolz import LolzClient
from app.lolz.client import LolzApiError

log = logging.getLogger(__name__)

_SETTING_KEY = "last_notification_id"

# Things that look like money in a notification body. We use this both to pick
# a wallet emoji and to surface the amount in the title line.
_MONEY_HINT_RE = re.compile(
    r"(\d[\d\s]{0,8}(?:[.,]\d+)?)\s*(?:₽|руб(?:лей|ля|\.)?|rub|р\.?)",
    re.IGNORECASE,
)


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
            nid = int(n.get("notification_id") or 0)
            try:
                classified = await self._classify(n)
                if classified is not None:
                    await self._send_one(n, classified)
                else:
                    log.info(
                        "Skip notification %s (%s/%s) — not for me",
                        nid,
                        n.get("content_type"),
                        n.get("content_action"),
                    )
            except TelegramBadRequest as e:
                # Don't get stuck retrying a malformed notification — record it
                # as seen and move on.
                log.warning("Failed to deliver notification %s: %s", nid, e)
            except Exception:  # noqa: BLE001
                log.exception("Notification %s processing failed", nid)
            if nid > last_seen:
                last_seen = nid
                await self._store.set_setting(_SETTING_KEY, str(last_seen))

    async def _classify(self, n: dict) -> dict | None:
        """Decide whether a notification is relevant and gather render data.

        Returns a dict ``{reason, body, post_id, action}`` or ``None`` when the
        notification should be dropped. ``reason`` is one of:
        ``my_thread``, ``quote``, ``mention``, ``post_comment``, ``other``.
        """
        ctype = (n.get("content_type") or "").lower()
        action = (n.get("content_action") or "").lower()
        my_uid = self._store.self_user_id
        my_username = (self._store.self_username or "").lower()

        if ctype == "post_comment":
            # Any post-comment notification (your_post / tag / reply / mention /
            # quote) — extract the comment body and parent post_id from the
            # ready-made HTML preview.
            body, post_id = _parse_comment_html(
                n.get("notification_html") or "", my_username
            )
            return {
                "reason": "post_comment",
                "body": body,
                "post_id": post_id,
                "action": action,
            }

        if ctype != "post":
            # Profile-post / conversation / follow / payment / market / etc.
            # Always render the HTML preview as text so the user actually sees
            # the content (incl. ₽ amounts).
            body = _strip_html_to_text(n.get("notification_html") or "")
            return {"reason": "other", "body": body, "post_id": 0, "action": action}

        post_id = int(n.get("content_id") or 0)
        if not post_id or not my_uid:
            return None
        try:
            post = await self._lolz.get_post(post_id)
        except LolzApiError as e:
            log.warning("get_post(%s) failed: %s", post_id, e)
            return None

        if int(post.get("poster_user_id") or 0) == my_uid:
            return None

        body_bb = post.get("post_body") or ""
        body_text = _strip_bbcode(body_bb)

        thread = post.get("thread") or {}
        if int(thread.get("creator_user_id") or 0) == my_uid:
            return {"reason": "my_thread", "body": body_text, "post_id": post_id, "action": action}
        if re.search(rf"\[QUOTE=[^\]]*member:\s*{my_uid}\b", body_bb, re.IGNORECASE):
            return {"reason": "quote", "body": body_text, "post_id": post_id, "action": action}
        if my_username and re.search(
            rf"@(?:\[user=\d+\])?{re.escape(my_username)}\b", body_bb, re.IGNORECASE
        ):
            return {"reason": "mention", "body": body_text, "post_id": post_id, "action": action}
        return None

    async def _send_one(self, n: dict, classified: dict) -> None:
        text = _format(n, classified)
        kb = _kb_for(n, classified)
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


def _kb_for(n: dict, classified: dict) -> InlineKeyboardMarkup | None:
    rows: list[list[InlineKeyboardButton]] = []
    reason = classified.get("reason")
    post_id = int(classified.get("post_id") or 0)

    if reason == "post_comment" and post_id:
        rows.append([
            InlineKeyboardButton(text="↩ Ответить", callback_data=f"creply:{post_id}"),
            InlineKeyboardButton(text="🌐 Открыть",
                                 url=f"https://lolz.live/posts/{post_id}/"),
        ])
        return InlineKeyboardMarkup(inline_keyboard=rows)

    url = _link_for(n)
    if not url:
        return None
    rows.append([InlineKeyboardButton(text="🌐 Открыть", url=url)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


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


def _format(n: dict, classified: dict) -> str:
    """Render a single notification as a TG HTML message."""
    creator = html.escape(str(n.get("creator_username") or "?"))
    icon, action = _label(n, classified)
    body = (classified.get("body") or "").strip()
    head = f"{icon} <b>{creator}</b> {action}".rstrip()
    if body:
        snippet = body if len(body) <= 600 else body[:600].rstrip() + "…"
        return f"{head}\n\n<i>{html.escape(snippet)}</i>"
    return head


def _label(n: dict, classified: dict) -> tuple[str, str]:
    """Pick an emoji + verb based on the classified reason / action."""
    reason = classified.get("reason")
    action = (classified.get("action") or "").lower()

    if reason == "my_thread":
        return "💬", "ответил в твоей теме"
    if reason == "quote":
        return "↩", "ответил на твой пост"
    if reason == "mention":
        return "📣", "упомянул тебя"
    if reason == "post_comment":
        if action == "your_post":
            return "💬", "прокомментировал твой пост"
        if action in {"tag", "mention"}:
            return "📣", "упомянул тебя в комментарии"
        if action in {"reply", "quote"}:
            return "↩", "ответил на твой комментарий"
        return "💬", "ответил под постом"

    # reason == "other" — non-post notifications. Try to recognise common
    # ones and surface useful info (e.g. ₽ amount).
    body = classified.get("body") or ""
    money = _detect_money(body)
    if money:
        return "💰", f"перевёл {money}"

    ctype = (n.get("content_type") or "").lower()
    if ctype == "profile_post":
        return "📝", "написал на твоей странице"
    if ctype == "user" and action == "follow":
        return "➕", "подписался на тебя"
    if ctype == "conversation":
        return "✉", "написал в личку"
    suffix = f"{ctype}/{action}".strip("/")
    return "🔔", suffix


def _detect_money(text: str) -> str:
    m = _MONEY_HINT_RE.search(text or "")
    if not m:
        return ""
    amount = re.sub(r"\s+", "", m.group(1))
    return f"{amount} ₽"


_BBCODE_TAG_RE = re.compile(r"\[/?[A-Za-z][^\]]*\]")


def _strip_bbcode(s: str) -> str:
    """Best-effort strip of BBCode for a short preview snippet."""
    s = _BBCODE_TAG_RE.sub(" ", s or "")
    s = re.sub(r"\s+", " ", s)
    return s.strip()


def _strip_html_to_text(notif_html: str) -> str:
    """Render an HTML notification preview to readable plain text."""
    if not notif_html:
        return ""
    # Normalize <br> to newlines so multi-line previews stay legible.
    s = re.sub(r"(?i)<br\s*/?>", "\n", notif_html)
    text = HTMLParser(s).text(separator=" ").strip()
    text = html.unescape(text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _parse_comment_html(notif_html: str, my_username: str) -> tuple[str, int]:
    """Pull the comment body and parent post_id out of ``notification_html``.

    Lolz includes a ready-to-render snippet that already contains the comment
    text after a ``<br>`` tag, plus a ``/posts/{post_id}/preview`` link. We
    parse both with selectolax and fall back to regex on the raw HTML.
    """
    if not notif_html:
        return "", 0

    # Parent post_id from /posts/<id>/preview link.
    m = re.search(r"/posts/(\d+)/preview", notif_html)
    post_id = int(m.group(1)) if m else 0

    # The comment body is the part after the first <br>.
    parts = re.split(r"<br\s*/?>", notif_html, maxsplit=1)
    raw_body = parts[1] if len(parts) > 1 else notif_html
    text = HTMLParser(raw_body).text(separator="").strip()
    text = html.unescape(text)

    # Drop our own @-mention prefix ("MyNick, ...").
    if my_username:
        prefix = re.match(
            rf"^@?{re.escape(my_username)}\s*,\s*", text, re.IGNORECASE
        )
        if prefix:
            text = text[prefix.end():]
    return text, post_id
