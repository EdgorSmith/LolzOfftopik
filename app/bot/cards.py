"""Render and deliver thread cards to Telegram."""

from __future__ import annotations

import logging

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import Message
from aiogram.utils.text_decorations import html_decoration as hd

from app.bot.keyboards import replied_kb, thread_card_kb
from app.db import Store
from app.lolz import (
    Thread,
    apply_emoji_map_to_escaped_html,
    extract_media,
    render_text_for_telegram,
)

log = logging.getLogger(__name__)

_TG_CAPTION_LIMIT = 1024
_TG_TEXT_LIMIT = 4096


def _format_card_html(thread: Thread, *, for_caption: bool) -> str:
    text_body = render_text_for_telegram(
        thread.first_post_body_html, max_len=2800 if not for_caption else 800
    )
    title = hd.bold(apply_emoji_map_to_escaped_html(hd.quote(thread.title or "(без заголовка)")))
    author = hd.italic(f"@{hd.quote(thread.creator_username)}")
    likes = thread.like_count
    likes_line = f"❤ {likes}" if likes else ""
    head = f"{title}\n{author}{(' · ' + likes_line) if likes_line else ''}"
    link = hd.link("Открыть тему", thread.permalink)

    sections: list[str] = [head]
    if text_body:
        sections.append(apply_emoji_map_to_escaped_html(hd.quote(text_body)))
    sections.append(link)
    full = "\n\n".join(sections)

    limit = _TG_CAPTION_LIMIT if for_caption else _TG_TEXT_LIMIT
    if len(full) > limit:
        overflow = len(full) - limit + 1
        if text_body:
            body = apply_emoji_map_to_escaped_html(hd.quote(text_body))
            trimmed_body = body[: max(0, len(body) - overflow - 1)] + "…"
            full = f"{head}\n\n{trimmed_body}\n\n{link}"
        else:
            # No body to trim; truncate the whole thing.
            full = full[: limit - 1] + "…"
    return full


async def send_thread_card(bot: Bot, store: Store, chat_id: int, thread: Thread) -> Message | None:
    """Send a single Telegram message for the thread, with media if any. Returns the sent message."""
    media = extract_media(thread.first_post_body_html)
    kb = thread_card_kb(thread.thread_id, thread.first_post_id, is_liked=thread.is_liked)

    msg: Message | None = None

    # Try video first if present, otherwise photo, otherwise text.
    if media.videos:
        caption = _format_card_html(thread, for_caption=True)
        try:
            msg = await bot.send_video(
                chat_id, media.videos[0], caption=caption, reply_markup=kb, parse_mode="HTML"
            )
        except TelegramBadRequest as e:
            log.warning("send_video failed for %s: %s; falling back to photo", thread.thread_id, e)
            msg = None
    if msg is None and media.photos:
        caption = _format_card_html(thread, for_caption=True)
        try:
            msg = await bot.send_photo(
                chat_id, media.photos[0], caption=caption, reply_markup=kb, parse_mode="HTML"
            )
        except TelegramBadRequest as e:
            log.warning("send_photo failed for %s: %s; falling back to text", thread.thread_id, e)
            msg = None
    if msg is None:
        text = _format_card_html(thread, for_caption=False)
        msg = await bot.send_message(
            chat_id, text, reply_markup=kb, parse_mode="HTML", disable_web_page_preview=False
        )

    if msg is not None:
        is_photo_card = msg.photo is not None or msg.video is not None
        await store.add_card(chat_id, msg.message_id, thread.thread_id, is_photo_card=is_photo_card)
    return msg


async def transition_card_to_replied(
    bot: Bot,
    store: Store,
    *,
    chat_id: int,
    card_message_id: int,
    is_photo_card: bool,
    thread_id: int,
    thread_title: str,
    reply_text: str,
    post_id: int,
) -> int:
    """Convert a thread card into a 'replied' state. Returns the message_id of the replied card."""
    body = (
        f"✅ {hd.bold('Ответил в теме')} «{hd.quote(thread_title or str(thread_id))}»\n\n"
        f"💬 {hd.quote(reply_text)}\n\n"
        f"{hd.link('Открыть тему', f'https://lolz.live/threads/{thread_id}/')}"
    )
    if len(body) > _TG_TEXT_LIMIT:
        body = body[: _TG_TEXT_LIMIT - 1] + "…"

    kb = replied_kb(thread_id, post_id)

    if is_photo_card:
        # Cannot turn a photo+caption message into text-only; delete and resend.
        try:
            await bot.delete_message(chat_id, card_message_id)
        except TelegramBadRequest as e:
            log.warning("delete_message failed for %s/%s: %s", chat_id, card_message_id, e)
        new_msg = await bot.send_message(chat_id, body, reply_markup=kb, parse_mode="HTML")
        await store.update_card_thread(chat_id, new_msg.message_id, thread_id, post_id)
        # Remove the old row (if it still exists).
        return new_msg.message_id

    await bot.edit_message_text(
        body, chat_id=chat_id, message_id=card_message_id, reply_markup=kb, parse_mode="HTML"
    )
    await store.update_card_thread(chat_id, card_message_id, thread_id, post_id)
    return card_message_id


async def update_replied_card_text(
    bot: Bot,
    *,
    chat_id: int,
    message_id: int,
    thread_id: int,
    new_reply_text: str,
    post_id: int,
) -> None:
    body = (
        f"✏ {hd.bold('Ответ обновлён')}\n\n"
        f"💬 {hd.quote(new_reply_text)}\n\n"
        f"{hd.link('Открыть тему', f'https://lolz.live/threads/{thread_id}/')}"
    )
    if len(body) > _TG_TEXT_LIMIT:
        body = body[: _TG_TEXT_LIMIT - 1] + "…"
    await bot.edit_message_text(
        body,
        chat_id=chat_id,
        message_id=message_id,
        reply_markup=replied_kb(thread_id, post_id),
        parse_mode="HTML",
    )


_REPLY_TYPE_LABELS = {
    "photo": "photo",
    "video": "video",
    "animation": "gif",
}


def _detect_media_kind(post_body_html: str) -> str | None:
    """Best-effort guess of media type by inspecting tags in the post body html.

    Only real BBCode attachments count. Mentions, avatars, smileys and other
    decorative <img> tags are explicitly ignored.
    """
    if not post_body_html:
        return None
    s = post_body_html.lower()
    if "<video" in s:
        return "video"
    if "bbcodeimage" not in s:
        return None
    if ".gif" in s:
        return "animation"
    return "photo"


async def send_replies(
    bot: Bot,
    chat_id: int,
    thread_id: int,
    posts: list[dict],
    first_post_id: int | None = None,
) -> None:
    """Render replies as a sequence of Telegram messages.

    - Text-only replies are batched into one or more text messages.
    - Each reply that has a real attachment is sent as its own photo / video /
      animation message, captioned with ``<b>nick</b>\\ntext photo``.
    """
    candidates = [p for p in posts if int(p.get("post_id", 0)) != int(first_post_id or 0)]
    candidates.sort(key=lambda p: int(p.get("post_id", 0)), reverse=True)
    candidates = candidates[:20]
    candidates.sort(key=lambda p: int(p.get("post_id", 0)))

    if not candidates:
        await bot.send_message(
            chat_id,
            f"💬 Ответов пока нет в {hd.link('теме', f'https://lolz.live/threads/{thread_id}/')}.",
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
        return

    header = f"💬 {hd.bold('Ответы в теме')} ({len(candidates)})"
    text_buffer: list[str] = [header]

    async def flush_text_buffer() -> None:
        nonlocal text_buffer
        if len(text_buffer) <= 0:
            return
        # Avoid sending an empty buffer (only header).
        if len(text_buffer) == 1 and text_buffer[0] == header:
            return
        chunk = "\n\n".join(text_buffer)
        if len(chunk) > _TG_TEXT_LIMIT:
            chunk = chunk[: _TG_TEXT_LIMIT - 4] + "…"
        await bot.send_message(chat_id, chunk, parse_mode="HTML", disable_web_page_preview=True)
        text_buffer = []  # subsequent batches start without the header

    for p in candidates:
        username = (p.get("poster_username") or "unknown").strip() or "unknown"
        body_html = p.get("post_body_html") or ""
        body_text = render_text_for_telegram(body_html, max_len=400).strip()
        kind = _detect_media_kind(body_html)
        label = _REPLY_TYPE_LABELS.get(kind or "", "")
        media = extract_media(body_html)

        head = hd.bold(hd.quote(username))
        if body_text:
            content = apply_emoji_map_to_escaped_html(hd.quote(body_text))
            if label:
                content = f"{content} {hd.italic(label)}"
        else:
            content = hd.italic(label) if label else ""

        block = f"{head}\n{content}".rstrip()

        # Try to send this reply as media when we have a usable URL.
        media_url: str | None = None
        send_kind: str | None = None
        if kind == "video" and media.videos:
            media_url = media.videos[0]
            send_kind = "video"
        elif kind == "animation" and media.photos:
            media_url = media.photos[0]
            send_kind = "animation"
        elif kind == "photo" and media.photos:
            media_url = media.photos[0]
            send_kind = "photo"

        if media_url and send_kind:
            await flush_text_buffer()
            caption = block if len(block) <= 1024 else block[:1023] + "…"
            try:
                if send_kind == "video":
                    await bot.send_video(chat_id, media_url, caption=caption, parse_mode="HTML")
                elif send_kind == "animation":
                    await bot.send_animation(chat_id, media_url, caption=caption, parse_mode="HTML")
                else:
                    await bot.send_photo(chat_id, media_url, caption=caption, parse_mode="HTML")
            except TelegramBadRequest as e:
                log.warning("send media for reply failed: %s; falling back to text", e)
                text_buffer.append(block)
        else:
            text_buffer.append(block)

    await flush_text_buffer()
