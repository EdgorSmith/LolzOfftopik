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

        # The Lolz timeline API returns: { "data": [...], "data_total": N,
        # "user": {...}, "links": {...}, "system_info": {...} }
        # Each item is FLAT: it has top-level `content_type`, `post_body`,
        # `post_body_plain_text`, `post_create_date`, `post_id`, `thread_id`,
        # plus a nested `thread` object that carries `forum_id` and
        # `thread_title`. The previous parser looked for `data["contents"]`
        # and a non-existent `item["content"]` — both wrong, which is why
        # /learn_replies stopped at "0 pages, 0 posts" on page 1.
        items = data.get("data") or data.get("contents") or data.get("timeline") or []
        if not items:
            reason = "empty"
            break
        pages += 1

        page_saved = 0
        for item in items:
            ctype = (item.get("content_type") or "").lower()
            # Two style sources are useful: 'post' items (replies you wrote)
            # AND 'thread' items (the first post of threads YOU created — the
            # body is in item.first_post). Including both ~doubles the sample
            # pool because the Lolz timeline/search API hard-caps the index
            # to ~180 items per user, and skipping threads loses about a
            # third of that.
            if ctype == "post":
                # For 'post' items, `forum_id` lives in the nested `thread`
                # object, not at top level (top-level `forum_id` exists only
                # for 'thread' items). The nested `thread` also carries
                # `thread_title`.
                thread_obj = item.get("thread") or {}
                item_forum_id = (
                    int(thread_obj.get("forum_id") or 0)
                    or int(item.get("forum_id") or 0)
                )
                if item_forum_id != int(forum_id):
                    continue
                seen += 1
                body_plain = (
                    item.get("post_body_plain_text")
                    or _clean_body(item.get("post_body") or "")
                ).strip()
                post_id_v = int(item.get("post_id") or item.get("content_id") or 0)
                thread_id_v = int(
                    item.get("thread_id") or thread_obj.get("thread_id") or 0
                )
                thread_title = (
                    thread_obj.get("thread_title")
                    or item.get("thread_title")
                    or ""
                )
                posted_at = int(item.get("post_create_date") or 0)
            elif ctype == "thread":
                # Top-level `forum_id` exists for thread items.
                if int(item.get("forum_id") or 0) != int(forum_id):
                    continue
                first_post = item.get("first_post") or {}
                # Skip if the thread's first post wasn't authored by this
                # user (e.g. timeline can include threads they posted IN).
                if user_id and int(first_post.get("poster_user_id") or 0) != int(user_id):
                    continue
                seen += 1
                body_plain = (
                    first_post.get("post_body_plain_text")
                    or _clean_body(first_post.get("post_body") or "")
                ).strip()
                post_id_v = int(first_post.get("post_id") or 0)
                thread_id_v = int(item.get("thread_id") or 0)
                thread_title = item.get("thread_title") or ""
                posted_at = int(
                    first_post.get("post_create_date")
                    or item.get("thread_create_date")
                    or 0
                )
            else:
                continue
            if not body_plain or len(body_plain) < 3:
                continue
            if not post_id_v:
                continue
            await store.upsert_my_reply(
                post_id=post_id_v,
                thread_id=thread_id_v,
                thread_title=str(thread_title)[:200],
                body_plain=body_plain[:1000],
                posted_at=posted_at,
            )
            page_saved += 1

        # Track the running total locally rather than re-querying the DB
        # after each page. The DB count is correct but the previous version
        # appeared to report the same final value on every progress message
        # (likely Telegram message reordering on rapid bursts), which made
        # the progress bar useless. Local accumulation is also faster.
        saved_total += page_saved
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

    # Reconcile with the DB once at the end (page_saved counts upserts but
    # INSERT OR REPLACE may have updated rows that already existed).
    saved_total = await store.count_my_replies()

    return LearnResult(
        pages_scanned=pages,
        posts_seen=seen,
        saved_total=saved_total,
        stopped_reason=reason,
    )
