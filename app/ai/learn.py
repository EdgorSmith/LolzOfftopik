"""Walk the authenticated user's timeline and store offtop replies for few-shot."""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass

from app.config import Config
from app.db import Store
from app.lolz import LolzClient
from app.lolz.client import LolzApiError

log = logging.getLogger(__name__)

_BBCODE_RE = re.compile(r"\[/?[A-Za-z][^\]]*\]")
_QUOTE_BLOCK_RE = re.compile(r"\[QUOTE[^\]]*\].*?\[/QUOTE\]", re.IGNORECASE | re.DOTALL)
_URL_RE = re.compile(r"https?://\S+")
_WS_RE = re.compile(r"\s+")


@dataclass
class LearnResult:
    pages_scanned: int
    posts_seen: int
    saved_total: int
    stopped_reason: str  # 'done', 'cap', 'empty', 'error'


def _clean_body(bb: str) -> str:
    """Strip BBCode + quoted blocks, collapse whitespace. Returns "" if too noisy."""
    if not bb:
        return ""
    s = _QUOTE_BLOCK_RE.sub(" ", bb)
    s = _BBCODE_RE.sub(" ", s)
    s = _URL_RE.sub(" ", s)
    s = _WS_RE.sub(" ", s).strip()
    return s


async def learn_user_replies(
    config: Config,
    store: Store,
    lolz: LolzClient,
    *,
    user_id: int,
    forum_id: int,
    max_pages: int = 50,
    target_count: int = 500,
    on_progress=None,  # async callable: await on_progress(message: str)
    stop_event: asyncio.Event | None = None,
) -> LearnResult:
    """Walk `/users/{user_id}/timeline` page by page and store offtop posts.

    - Filters `content_type == 'post'` and `thread.forum_id == forum_id`.
    - Persists clean plain-text bodies into ``my_replies``.
    - Calls ``on_progress`` with a short status string after each page.
    - Stops when:
      * the API returns no contents,
      * we hit ``max_pages`` or have ``target_count`` saved,
      * ``stop_event`` is set.
    """
    pages = 0
    seen = 0
    saved_total = await store.count_my_replies()
    saved_start = saved_total
    reason = "done"

    for page in range(1, max_pages + 1):
        if stop_event and stop_event.is_set():
            reason = "stopped"
            break
        try:
            data = await lolz.list_user_timeline(user_id, page=page, limit=20)
        except LolzApiError as e:
            log.warning("timeline page %s failed: %s", page, e)
            reason = "error"
            break

        contents = data.get("contents") or data.get("timeline") or []
        if not contents:
            reason = "empty"
            break
        pages += 1

        page_saved = 0
        for item in contents:
            ctype = (item.get("content_type") or "").lower()
            if ctype != "post":
                continue
            seen += 1
            content = item.get("content") or item.get("post") or {}
            thread = content.get("thread") or {}
            if int(thread.get("forum_id", 0) or 0) != int(forum_id):
                continue
            body_plain = (
                content.get("post_body_plain_text")
                or _clean_body(content.get("post_body") or "")
            ).strip()
            if not body_plain or len(body_plain) < 3:
                continue
            await store.upsert_my_reply(
                post_id=int(content.get("post_id") or 0),
                thread_id=int(thread.get("thread_id") or 0),
                thread_title=str(thread.get("thread_title") or "")[:200],
                body_plain=body_plain[:1000],
                posted_at=int(content.get("post_create_date") or 0),
            )
            page_saved += 1

        saved_total = await store.count_my_replies()
        if on_progress:
            try:
                await on_progress(
                    f"📚 стр. {page}: добавлено {page_saved}, всего сохранено {saved_total}"
                )
            except Exception:  # noqa: BLE001
                log.exception("on_progress failed")

        if saved_total - saved_start >= target_count:
            reason = "cap"
            break

    return LearnResult(
        pages_scanned=pages,
        posts_seen=seen,
        saved_total=saved_total,
        stopped_reason=reason,
    )
