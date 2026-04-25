"""Telegram bot handlers: password gate, start/stop polling, like/reply/edit flows."""

from __future__ import annotations

import logging
import time
from datetime import UTC

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    CallbackQuery,
    ForceReply,
    Message,
    ReplyKeyboardRemove,
)
from aiogram.utils.text_decorations import html_decoration as hd

from app.bot.cards import (
    send_replies,
    transition_card_to_replied,
    update_replied_card_text,
)
from app.bot.keyboards import (
    CREATE_THREAD_BUTTON_TEXT,
    START_BUTTON_TEXT,
    STATS_BUTTON_TEXT,
    STOP_BUTTON_TEXT,
    cancel_kb,
    confirm_kb,
    main_menu,
    reply_like_kb,
    thread_card_kb,
)
from app.config import Config
from app.db import Store
from app.lolz import LolzClient
from app.lolz.client import LolzApiError

log = logging.getLogger(__name__)

# Hard cap on /users/{me}/timeline pages walked when computing stats.
# 20 pages * 50 items * ~3.1s rate-limit ≈ 60 s upper bound on a cold call.
_STATS_PAGE_LIMIT = 20


def build_router(config: Config, store: Store, lolz: LolzClient) -> Router:
    router = Router(name="lolzofftopik")

    def is_owner(message_or_cq) -> bool:
        from_user = getattr(message_or_cq, "from_user", None)
        if not from_user:
            return False
        return from_user.id == config.telegram_owner_id

    @router.message(F.from_user.id != config.telegram_owner_id)
    async def reject_strangers(message: Message) -> None:
        # Silently ignore everyone except the owner.
        log.info("Ignoring message from non-owner uid=%s", message.from_user.id if message.from_user else "?")

    @router.callback_query(F.from_user.id != config.telegram_owner_id)
    async def reject_strangers_cb(cq: CallbackQuery) -> None:
        await cq.answer("Доступ запрещён.", show_alert=False)

    # ----- password gate -------------------------------------------------------

    @router.message(CommandStart())
    async def cmd_start(message: Message) -> None:
        if not await store.is_unlocked():
            await message.answer(
                "🔒 Введите пароль, чтобы пользоваться ботом.",
                reply_markup=ReplyKeyboardRemove(),
            )
            return
        polling = await store.is_polling_enabled()
        await message.answer(
            "Привет. Готов оффтопить.",
            reply_markup=main_menu(polling),
        )

    @router.message(Command("lock"))
    async def cmd_lock(message: Message) -> None:
        await store.set_unlocked(False)
        await store.set_polling_enabled(False)
        await message.answer(
            "🔒 Заблокировано. Введи пароль, чтобы продолжить.", reply_markup=ReplyKeyboardRemove()
        )

    # Plain text (NOT a ForceReply response) — password gate or main menu actions.
    @router.message(F.text, F.reply_to_message.is_(None))
    async def handle_text(message: Message, bot: Bot) -> None:
        # Locked state — accept password only.
        if not await store.is_unlocked():
            if (message.text or "").strip().lower() == config.bot_password.strip().lower():
                await store.set_unlocked(True)
                await message.answer(
                    "✅ Разблокировано. Жми «Начать оффтопить», когда будешь готов.",
                    reply_markup=main_menu(False),
                )
            # Wrong password: stay silent.
            return

        text = (message.text or "").strip()
        if text in (START_BUTTON_TEXT, "/offtop_on"):
            await _start_polling(message, bot)
            return
        if text in (STOP_BUTTON_TEXT, "/offtop_off"):
            await _stop_polling(message)
            return
        if text in (CREATE_THREAD_BUTTON_TEXT, "/new_thread"):
            await _begin_create_thread(message)
            return
        if text in (STATS_BUTTON_TEXT, "/stats"):
            await _send_stats(message)
            return

        # Otherwise — show the menu.
        polling = await store.is_polling_enabled()
        await message.answer(
            "Используй кнопку ниже, чтобы включить/выключить оффтоп.",
            reply_markup=main_menu(polling),
        )

    # ----- start / stop polling -----------------------------------------------

    async def _start_polling(message: Message, bot: Bot) -> None:
        # Set baseline to the current max thread_id so older threads are not delivered.
        try:
            threads = await lolz.list_threads(
                config.lolz_offtop_forum_id, limit=1, order="post_date", direction="desc"
            )
            baseline = threads[0].thread_id if threads else 0
        except LolzApiError as e:
            log.exception("Could not establish baseline: %s", e)
            await message.answer("⚠ Не удалось обратиться к API lolz, проверь токен. Включение отменено.")
            return
        await store.set_baseline_thread_id(baseline)
        await store.set_polling_enabled(True)
        await message.answer(
            f"▶ Оффтопим. Слежу за новыми темами в "
            f"{hd.link('разделе', config.lolz_offtop_url)} (после thread_id={baseline}).",
            reply_markup=main_menu(True),
            parse_mode="HTML",
            disable_web_page_preview=True,
        )

    async def _stop_polling(message: Message) -> None:
        await store.set_polling_enabled(False)
        await message.answer("⏹ Оффтоп остановлен.", reply_markup=main_menu(False))

    async def _send_stats(message: Message) -> None:
        """Compute and send the user's offtop stats over 12h / 7d / all-time.

        Walks through ``/users/{me}/timeline?forum_id=8`` page by page, summing
        ``post_like_count`` (likes received) and counting posts. Stops early
        once we've covered enough history for all three windows. Bounded at
        ``_STATS_PAGE_LIMIT`` pages so a runaway account doesn't burn through
        rate-limit.
        """
        if not store.self_user_id:
            await message.answer("⚠ Профиль ещё не подгружен — попробуй через минуту.")
            return
        progress = await message.answer("📊 Считаю статистику... (~10–20 сек)")
        try:
            stats = await _compute_offtop_stats(store.self_user_id)
        except LolzApiError as e:
            log.exception("stats failed: %s", e)
            await _safe_delete(message.bot, message.chat.id, progress.message_id)
            await message.answer(f"⚠ Не удалось получить статистику: {e}")
            return
        text = (
            f"📊 <b>Статистика в оффтопе</b>\n\n"
            f"<b>Получено лайков</b>\n"
            f"  • за 12 ч — {stats['likes_12h']}\n"
            f"  • за 7 дней — {stats['likes_7d']}\n"
            f"  • за всё время — {stats['likes_total']}\n\n"
            f"<b>Ответов в оффтопике</b>\n"
            f"  • за 12 ч — {stats['posts_12h']}\n"
            f"  • за 7 дней — {stats['posts_7d']}\n"
            f"  • за всё время — {stats['posts_total']}"
        )
        if stats.get("truncated"):
            from datetime import datetime as _dt
            oldest = _dt.fromtimestamp(stats["oldest_seen"], tz=UTC).strftime("%Y-%m-%d")
            text += (
                f"\n\n<i>Прошёл {stats['pages_walked']} стр., "
                f"посчитано до {oldest}. "
                f"«Всё время» — оценка снизу.</i>"
            )
        try:
            await message.bot.edit_message_text(
                text,
                chat_id=message.chat.id,
                message_id=progress.message_id,
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
        except TelegramBadRequest:
            await message.answer(text, parse_mode="HTML", disable_web_page_preview=True)

    async def _compute_offtop_stats(user_id: int) -> dict:
        now = int(time.time())
        cutoff_12h = now - 12 * 3600
        cutoff_7d = now - 7 * 86400
        likes_12h = likes_7d = likes_total = 0
        posts_12h = posts_7d = posts_total = 0
        oldest_seen = now
        pages_walked = 0
        truncated = False
        page = 1
        while True:
            data = await lolz.list_user_timeline(
                user_id, forum_id=config.lolz_offtop_forum_id, page=page, limit=50
            )
            items = data.get("data") or []
            # Only count replies, not the first-post of threads we created.
            posts_in_page = [
                it for it in items
                if it.get("content_type") == "post" and not it.get("post_is_first_post")
            ]
            for p in posts_in_page:
                ts = int(p.get("post_create_date") or 0)
                likes = int(p.get("post_like_count") or 0)
                posts_total += 1
                likes_total += likes
                if ts >= cutoff_7d:
                    posts_7d += 1
                    likes_7d += likes
                if ts >= cutoff_12h:
                    posts_12h += 1
                    likes_12h += likes
                if ts and ts < oldest_seen:
                    oldest_seen = ts
            pages_walked += 1
            links = data.get("links") or {}
            total_pages = int(links.get("pages") or page)
            if page >= total_pages:
                break
            if pages_walked >= _STATS_PAGE_LIMIT:
                truncated = True
                break
            page += 1
        return {
            "likes_12h": likes_12h,
            "likes_7d": likes_7d,
            "likes_total": likes_total,
            "posts_12h": posts_12h,
            "posts_7d": posts_7d,
            "posts_total": posts_total,
            "pages_walked": pages_walked,
            "oldest_seen": oldest_seen,
            "truncated": truncated,
        }

    async def _begin_create_thread(message: Message) -> None:
        prompt = await message.answer(
            "📝 Пришли заголовок темы (одной строкой):",
            reply_markup=ForceReply(input_field_placeholder="Заголовок..."),
        )
        cancel_msg = await message.answer(
            "Передумал?", reply_markup=cancel_kb(prompt.message_id)
        )
        await store.set_pending_create_title(
            message.chat.id, prompt.message_id, cancel_message_id=cancel_msg.message_id
        )

    # ----- inline buttons: like / reply / edit --------------------------------

    @router.callback_query(F.data.startswith("like:"))
    async def cb_like(cq: CallbackQuery, bot: Bot) -> None:
        if not await store.is_unlocked():
            await cq.answer("Бот заблокирован, отправь пароль.", show_alert=True)
            return
        try:
            _, post_id_str, is_liked_str = cq.data.split(":", 2)
            post_id = int(post_id_str)
            currently_liked = bool(int(is_liked_str))
        except (ValueError, AttributeError):
            await cq.answer("Битые данные.", show_alert=True)
            return
        try:
            if currently_liked:
                await lolz.unlike_post(post_id)
                await cq.answer("💔 Лайк убран")
            else:
                await lolz.like_post(post_id)
                await cq.answer("❤ Лайкнул")
        except LolzApiError as e:
            log.warning("Like failed: %s", e)
            await cq.answer(f"Ошибка: {e}", show_alert=True)
            return

        # Toggle the like button on the existing card if it still has a keyboard.
        card = await store.get_card(cq.message.chat.id, cq.message.message_id) if cq.message else None
        if not card or card.state != "pending":
            return
        is_own = bool(
            store.self_user_id and card.creator_user_id and store.self_user_id == card.creator_user_id
        )
        prev_count = _read_like_count_from_kb(cq.message.reply_markup)
        new_count = max(0, prev_count + (-1 if currently_liked else 1))
        new_kb = thread_card_kb(
            card.thread_id,
            post_id,
            is_liked=not currently_liked,
            creator_user_id=card.creator_user_id,
            is_own=is_own,
            like_count=new_count,
        )
        try:
            await bot.edit_message_reply_markup(
                chat_id=cq.message.chat.id, message_id=cq.message.message_id, reply_markup=new_kb
            )
        except TelegramBadRequest:
            pass

    @router.callback_query(F.data == "noop")
    async def cb_noop(cq: CallbackQuery) -> None:
        # The "no" half of confirm dialogs etc. — just dismiss.
        if cq.message:
            try:
                await cq.message.delete()
            except TelegramBadRequest:
                pass
        await cq.answer()

    @router.callback_query(F.data.startswith("cancel:"))
    async def cb_cancel(cq: CallbackQuery, bot: Bot) -> None:
        try:
            _, prompt_msg_id_str = cq.data.split(":", 1)
            prompt_msg_id = int(prompt_msg_id_str)
        except (ValueError, AttributeError):
            await cq.answer("Битые данные.", show_alert=True)
            return
        if not cq.message:
            await cq.answer()
            return
        # Drop the pending action and clean up both messages.
        await store.pop_pending(cq.message.chat.id, prompt_msg_id)
        await _safe_delete(bot, cq.message.chat.id, prompt_msg_id)
        await _safe_delete(bot, cq.message.chat.id, cq.message.message_id)
        await cq.answer("❌ Отменено")
        await _reattach_main_menu(bot, store, cq.message.chat.id)

    # ----- profile / delete buttons -------------------------------------------

    @router.callback_query(F.data.startswith("profile:"))
    async def cb_profile(cq: CallbackQuery) -> None:
        if not await store.is_unlocked():
            await cq.answer("Бот заблокирован.", show_alert=True)
            return
        try:
            _, user_id_str = cq.data.split(":", 1)
            user_id = int(user_id_str)
        except (ValueError, AttributeError):
            await cq.answer("Битые данные.", show_alert=True)
            return
        if not cq.message:
            await cq.answer()
            return
        await cq.answer("Гружу профиль…")
        try:
            user = await lolz.get_user(user_id)
        except LolzApiError as e:
            await cq.message.answer(f"⚠ Не удалось получить профиль: {e}")
            return
        if not user:
            await cq.message.answer("Профиль не найден.")
            return
        text, avatar_url = _format_profile(user)
        if avatar_url:
            try:
                await cq.message.answer_photo(avatar_url, caption=text, parse_mode="HTML")
                return
            except TelegramBadRequest as e:
                log.warning("profile photo failed: %s", e)
        await cq.message.answer(text, parse_mode="HTML", disable_web_page_preview=True)

    @router.callback_query(F.data.startswith("delpost:"))
    async def cb_delpost(cq: CallbackQuery) -> None:
        if not await store.is_unlocked():
            await cq.answer("Бот заблокирован.", show_alert=True)
            return
        try:
            _, post_id_str = cq.data.split(":", 1)
            post_id = int(post_id_str)
        except (ValueError, AttributeError):
            await cq.answer("Битые данные.", show_alert=True)
            return
        if not cq.message:
            await cq.answer()
            return
        await cq.message.answer(
            f"🗑 Удалить пост #{post_id}?",
            reply_markup=confirm_kb(yes_data=f"delpost_yes:{post_id}:{cq.message.message_id}"),
        )
        await cq.answer()

    @router.callback_query(F.data.startswith("delpost_yes:"))
    async def cb_delpost_yes(cq: CallbackQuery, bot: Bot) -> None:
        if not await store.is_unlocked():
            await cq.answer("Бот заблокирован.", show_alert=True)
            return
        try:
            _, post_id_str, source_msg_id_str = cq.data.split(":", 2)
            post_id = int(post_id_str)
            source_msg_id = int(source_msg_id_str)
        except (ValueError, AttributeError):
            await cq.answer("Битые данные.", show_alert=True)
            return
        try:
            await lolz.delete_post(post_id)
        except LolzApiError as e:
            await cq.answer(f"Ошибка: {e}", show_alert=True)
            return
        await cq.answer("🗑 Пост удалён")
        # Drop the confirm dialog itself.
        if cq.message:
            await _safe_delete(bot, cq.message.chat.id, cq.message.message_id)
            # And the original card whose Delete button we clicked.
            await _safe_delete(bot, cq.message.chat.id, source_msg_id)

    @router.callback_query(F.data.startswith("delthread:"))
    async def cb_delthread(cq: CallbackQuery) -> None:
        if not await store.is_unlocked():
            await cq.answer("Бот заблокирован.", show_alert=True)
            return
        try:
            _, thread_id_str = cq.data.split(":", 1)
            thread_id = int(thread_id_str)
        except (ValueError, AttributeError):
            await cq.answer("Битые данные.", show_alert=True)
            return
        if not cq.message:
            await cq.answer()
            return
        await cq.message.answer(
            f"🗑 Удалить тему #{thread_id}?",
            reply_markup=confirm_kb(yes_data=f"delthread_yes:{thread_id}:{cq.message.message_id}"),
        )
        await cq.answer()

    @router.callback_query(F.data.startswith("delthread_yes:"))
    async def cb_delthread_yes(cq: CallbackQuery, bot: Bot) -> None:
        if not await store.is_unlocked():
            await cq.answer("Бот заблокирован.", show_alert=True)
            return
        try:
            _, thread_id_str, source_msg_id_str = cq.data.split(":", 2)
            thread_id = int(thread_id_str)
            source_msg_id = int(source_msg_id_str)
        except (ValueError, AttributeError):
            await cq.answer("Битые данные.", show_alert=True)
            return
        try:
            await lolz.delete_thread(thread_id)
        except LolzApiError as e:
            await cq.answer(f"Ошибка: {e}", show_alert=True)
            return
        await cq.answer("🗑 Тема удалена")
        if cq.message:
            await _safe_delete(bot, cq.message.chat.id, cq.message.message_id)
            await _safe_delete(bot, cq.message.chat.id, source_msg_id)

    @router.callback_query(F.data.startswith("rlike:"))
    async def cb_rlike(cq: CallbackQuery, bot: Bot) -> None:
        """Compact ❤ button under each reply in the 'view replies' view."""
        if not await store.is_unlocked():
            await cq.answer("Бот заблокирован, отправь пароль.", show_alert=True)
            return
        try:
            _, post_id_str, is_liked_str = cq.data.split(":", 2)
            post_id = int(post_id_str)
            currently_liked = bool(int(is_liked_str))
        except (ValueError, AttributeError):
            await cq.answer("Битые данные.", show_alert=True)
            return
        try:
            if currently_liked:
                await lolz.unlike_post(post_id)
                await cq.answer("💔 Лайк убран")
            else:
                await lolz.like_post(post_id)
                await cq.answer("❤ Лайкнул")
        except LolzApiError as e:
            log.warning("rlike failed: %s", e)
            await cq.answer(f"Ошибка: {e}", show_alert=True)
            return
        if not cq.message:
            return
        prev_count = _read_like_count_from_kb(cq.message.reply_markup)
        new_count = max(0, prev_count + (-1 if currently_liked else 1))
        new_kb = reply_like_kb(post_id, is_liked=not currently_liked, like_count=new_count)
        try:
            await bot.edit_message_reply_markup(
                chat_id=cq.message.chat.id, message_id=cq.message.message_id, reply_markup=new_kb
            )
        except TelegramBadRequest:
            pass

    @router.callback_query(F.data.startswith("rreply:"))
    async def cb_rreply(cq: CallbackQuery) -> None:
        """Reply to a specific post (from the View Replies stream)."""
        if not await store.is_unlocked():
            await cq.answer("Бот заблокирован.", show_alert=True)
            return
        try:
            _, post_id_str = cq.data.split(":", 1)
            post_id = int(post_id_str)
        except (ValueError, AttributeError):
            await cq.answer("Битые данные.", show_alert=True)
            return
        if not cq.message:
            await cq.answer()
            return
        # Need to know which thread this post belongs to so the reply lands in
        # the right place. We fetch it from the API on demand.
        try:
            post_data = await lolz._request("GET", f"/posts/{post_id}")
        except LolzApiError as e:
            await cq.answer(f"Ошибка: {e}", show_alert=True)
            return
        post = post_data.get("post") or {}
        thread_id = int(post.get("thread_id", 0) or 0)
        if not thread_id:
            await cq.answer("Не удалось определить тему.", show_alert=True)
            return
        prompt = await cq.message.answer(
            f"↩ Напиши ответ на пост #{post_id}.\n"
            f"Можно текстом, фото, видео или гифкой (с подписью).",
            reply_markup=ForceReply(input_field_placeholder="Ответ на пост..."),
        )
        cancel_msg = await cq.message.answer(
            "Передумал?", reply_markup=cancel_kb(prompt.message_id)
        )
        await store.set_pending_reply(
            cq.message.chat.id,
            prompt.message_id,
            thread_id,
            card_chat_id=cq.message.chat.id,
            card_message_id=cq.message.message_id,
            quote_post_id=post_id,
            cancel_message_id=cancel_msg.message_id,
        )
        await cq.answer()

    @router.callback_query(F.data.startswith("reply:"))
    async def cb_reply(cq: CallbackQuery) -> None:
        if not await store.is_unlocked():
            await cq.answer("Бот заблокирован.", show_alert=True)
            return
        try:
            _, thread_id_str = cq.data.split(":", 1)
            thread_id = int(thread_id_str)
        except (ValueError, AttributeError):
            await cq.answer("Битые данные.", show_alert=True)
            return
        if not cq.message:
            await cq.answer()
            return
        prompt = await cq.message.answer(
            f"✍ Напиши ответ для темы #{thread_id}.\n"
            f"Можно текстом, фото, видео или гифкой (можно с подписью).",
            reply_markup=ForceReply(input_field_placeholder="Ответ в тему..."),
        )
        cancel_msg = await cq.message.answer(
            "Передумал?", reply_markup=cancel_kb(prompt.message_id)
        )
        await store.set_pending_reply(
            cq.message.chat.id,
            prompt.message_id,
            thread_id,
            card_chat_id=cq.message.chat.id,
            card_message_id=cq.message.message_id,
            cancel_message_id=cancel_msg.message_id,
        )
        await cq.answer()

    @router.callback_query(F.data.startswith("edit:"))
    async def cb_edit(cq: CallbackQuery) -> None:
        if not await store.is_unlocked():
            await cq.answer("Бот заблокирован.", show_alert=True)
            return
        try:
            _, post_id_str = cq.data.split(":", 1)
            post_id = int(post_id_str)
        except (ValueError, AttributeError):
            await cq.answer("Битые данные.", show_alert=True)
            return
        if not cq.message:
            await cq.answer()
            return
        prompt = await cq.message.answer(
            f"✏ Введи новый текст ответа (post_id={post_id}).\n"
            f"Можно текстом, фото, видео или гифкой (с подписью).",
            reply_markup=ForceReply(input_field_placeholder="Новый текст ответа..."),
        )
        cancel_msg = await cq.message.answer(
            "Передумал?", reply_markup=cancel_kb(prompt.message_id)
        )
        await store.set_pending_edit(
            cq.message.chat.id,
            prompt.message_id,
            post_id,
            card_chat_id=cq.message.chat.id,
            card_message_id=cq.message.message_id,
            cancel_message_id=cancel_msg.message_id,
        )
        await cq.answer()

    # ----- view replies -------------------------------------------------------

    @router.callback_query(F.data.startswith("replies:"))
    async def cb_replies(cq: CallbackQuery, bot: Bot) -> None:
        if not await store.is_unlocked():
            await cq.answer("Бот заблокирован.", show_alert=True)
            return
        try:
            _, thread_id_str = cq.data.split(":", 1)
            thread_id = int(thread_id_str)
        except (ValueError, AttributeError):
            await cq.answer("Битые данные.", show_alert=True)
            return
        if not cq.message:
            await cq.answer()
            return
        await cq.answer("Ищу ответы…")
        try:
            posts = await lolz.list_thread_posts(thread_id, limit=20, order="natural_reverse")
            try:
                thread = await lolz.get_thread(thread_id)
                first_post_id = thread.first_post_id
            except LolzApiError:
                first_post_id = None
        except LolzApiError as e:
            log.warning("replies fetch failed: %s", e)
            await cq.message.answer(f"⚠ Не удалось получить ответы: {e}")
            return
        await send_replies(bot, cq.message.chat.id, thread_id, posts, first_post_id=first_post_id)

    # ----- ForceReply consumer -------------------------------------------------

    @router.message(F.reply_to_message)
    async def handle_force_reply(message: Message, bot: Bot) -> None:
        if not await store.is_unlocked():
            return
        if not message.reply_to_message:
            return
        pending = await store.pop_pending(message.chat.id, message.reply_to_message.message_id)
        if not pending:
            return

        caption = (message.text or message.caption or "").strip()
        media_bbcode = await _build_media_bbcode(message, store, config)
        body = _join_caption_and_media(caption, media_bbcode)
        if not body:
            await message.answer("Пустой ответ — отменено.")
            return

        # The TG-side preview should not include BBCode noise.
        display_text = caption or _media_kind_label(message)
        action = pending["action"]

        # Multi-step thread-creation: title is captured in step 1, body in step 2.
        if action == "create_title":
            title_text = (message.text or message.caption or "").strip()
            if not title_text:
                await message.answer("Заголовок пустой — отменено.")
                return
            prompt = await message.answer(
                "📝 Теперь пришли текст темы (можно с фото / видео / гифкой и подписью):",
                reply_markup=ForceReply(input_field_placeholder="Текст темы..."),
            )
            cancel_msg = await message.answer(
                "Передумал?", reply_markup=cancel_kb(prompt.message_id)
            )
            await store.set_pending_create_body(
                message.chat.id, prompt.message_id, title_text,
                cancel_message_id=cancel_msg.message_id,
            )
            # Drop the previous step's prompt and its cancel hint, plus the
            # title message we just consumed.
            prev_cancel = pending.get("cancel_message_id")
            if prev_cancel:
                await _safe_delete(bot, message.chat.id, int(prev_cancel))
            await _safe_delete(bot, message.chat.id, message.reply_to_message.message_id)
            return

        cancel_msg_id = pending.get("cancel_message_id")

        async def _finalize_force_reply() -> None:
            """Drop the prompt, the cancel hint and the user's message; reattach menu."""
            if cancel_msg_id:
                await _safe_delete(bot, message.chat.id, int(cancel_msg_id))
            await _safe_delete(bot, message.chat.id, message.reply_to_message.message_id)
            await _safe_delete(bot, message.chat.id, message.message_id)
            await _reattach_main_menu(bot, store, message.chat.id)

        try:
            if action == "create_body":
                title_text = pending.get("payload") or "(без заголовка)"
                try:
                    new_thread_id = await lolz.create_thread(
                        config.lolz_offtop_forum_id, title_text, body
                    )
                except LolzApiError as e:
                    log.exception("create_thread failed: %s", e)
                    await message.answer(f"⚠ Не удалось создать тему: {e}")
                    return
                if not new_thread_id:
                    await message.answer("⚠ API не вернул thread_id, тема могла не создаться.")
                    return
                link = f"https://lolz.live/threads/{new_thread_id}/"
                await message.answer(
                    f"✅ {hd.bold('Тема создана')}: {hd.link(hd.quote(title_text), link)}",
                    parse_mode="HTML",
                    disable_web_page_preview=False,
                )
                await _finalize_force_reply()
                return
            if action == "reply":
                thread_id = int(pending["target_thread_id"])
                # If this reply was triggered from a per-post ↩ button, payload
                # holds the original post_id we should quote.
                quote_pid_raw = pending.get("payload") or ""
                if quote_pid_raw.isdigit():
                    body = await _build_quoted_body(lolz, int(quote_pid_raw), body)
                post_id = await lolz.reply(thread_id, body)
                if not post_id:
                    raise LolzApiError(0, "API не вернул post_id")
                # Try to fetch thread title for the replied card; fall back to thread_id.
                try:
                    t = await lolz.get_thread(thread_id)
                    title = t.title
                except LolzApiError:
                    title = ""
                card = await store.get_card(pending["card_chat_id"], pending["card_message_id"])
                is_photo_card = bool(card.is_photo_card) if card else False
                await transition_card_to_replied(
                    bot,
                    store,
                    chat_id=pending["card_chat_id"],
                    card_message_id=pending["card_message_id"],
                    is_photo_card=is_photo_card,
                    thread_id=thread_id,
                    thread_title=title,
                    reply_text=display_text,
                    post_id=post_id,
                )
                await _finalize_force_reply()
            elif action == "edit":
                post_id = int(pending["target_post_id"])
                await lolz.edit_post(post_id, body)
                card = await store.get_card(pending["card_chat_id"], pending["card_message_id"])
                thread_id_for_link = card.thread_id if card else 0
                await update_replied_card_text(
                    bot,
                    chat_id=pending["card_chat_id"],
                    message_id=pending["card_message_id"],
                    thread_id=thread_id_for_link,
                    new_reply_text=display_text,
                    post_id=post_id,
                )
                await _finalize_force_reply()
        except LolzApiError as e:
            log.exception("Force-reply action failed: %s", e)
            await message.answer(f"⚠ Ошибка lolz API: {e}")

    return router


async def _safe_delete(bot: Bot, chat_id: int, message_id: int) -> None:
    try:
        await bot.delete_message(chat_id, message_id)
    except TelegramBadRequest:
        pass


def _read_like_count_from_kb(reply_markup) -> int:
    """Extract the numeric like count from the first button on an inline kb.

    Buttons are rendered as '❤ 12' / '💔 12' / '❤'. We just find the first
    integer in the first button's text. Returns 0 when not present.
    """
    try:
        text = reply_markup.inline_keyboard[0][0].text or ""
    except (AttributeError, IndexError):
        return 0
    digits = "".join(ch for ch in text if ch.isdigit())
    return int(digits) if digits else 0


async def _build_quoted_body(lolz: LolzClient, quote_post_id: int, my_body: str) -> str:
    """Prepend a forum [QUOTE] block to the user's body so the new post quotes the target.

    Best-effort — if the lookup fails we just submit the body without a quote.
    """
    try:
        data = await lolz._request("GET", f"/posts/{quote_post_id}")
    except LolzApiError as e:
        log.warning("quote lookup failed for post %s: %s", quote_post_id, e)
        return my_body
    post = data.get("post") or {}
    username = str(post.get("poster_username") or "").strip()
    member_id = int(post.get("poster_user_id", 0) or 0)
    plain = str(post.get("post_body_plain_text") or "").strip()
    # Truncate the quoted body so we don't blow up the post.
    if len(plain) > 500:
        plain = plain[:500].rstrip() + "…"
    if not plain:
        plain = "(вложение)"
    head = (
        f'[QUOTE="{username}, post: {quote_post_id}, member: {member_id}"]'
        if username and member_id
        else "[QUOTE]"
    )
    return f"{head}\n{plain}\n[/QUOTE]\n\n{my_body}"


def _format_profile(user: dict) -> tuple[str, str | None]:
    """Render a lolz user dict into HTML for Telegram. Returns (text, avatar_url|None)."""
    username = str(user.get("username", "?"))
    user_id = int(user.get("user_id", 0) or 0)
    title = str(user.get("user_title") or "")
    msg_count = user.get("user_message_count")
    like_count = user.get("user_like_count")
    register_ts = user.get("user_register_date")
    last_seen_ts = user.get("user_last_seen_date")
    is_banned = bool(user.get("user_is_banned"))
    profile_url = (
        ((user.get("links") or {}).get("permalink"))
        or f"https://lolz.live/members/{user_id}/"
    )
    avatar = (user.get("links") or {}).get("avatar_big") or (user.get("links") or {}).get("avatar")
    if avatar and avatar.startswith("//"):
        avatar = "https:" + avatar
    elif avatar and avatar.startswith("/"):
        avatar = "https://lolz.live" + avatar

    def _fmt_ts(ts) -> str:
        try:
            from datetime import datetime
            return datetime.fromtimestamp(int(ts), tz=UTC).strftime("%Y-%m-%d")
        except Exception:  # noqa: BLE001
            return "?"

    lines = [
        f"👤 <b>{hd.quote(username)}</b>" + (" 🚫" if is_banned else ""),
        f"id: <code>{user_id}</code>" + (f" · {hd.quote(title)}" if title else ""),
    ]
    stats: list[str] = []
    if msg_count is not None:
        stats.append(f"💬 {msg_count}")
    if like_count is not None:
        stats.append(f"❤ {like_count}")
    if stats:
        lines.append(" · ".join(stats))
    if register_ts:
        lines.append(f"📅 рег: {_fmt_ts(register_ts)}")
    if last_seen_ts:
        lines.append(f"👁 был: {_fmt_ts(last_seen_ts)}")
    lines.append(f'🌐 <a href="{profile_url}">Открыть профиль</a>')
    return "\n".join(lines), (avatar or None)


async def _reattach_main_menu(bot: Bot, store: Store, chat_id: int) -> None:
    """Send a tiny confirmation that re-attaches the persistent reply keyboard."""
    polling = await store.is_polling_enabled()
    try:
        await bot.send_message(chat_id, "✅ Готово.", reply_markup=main_menu(polling))
    except TelegramBadRequest as e:
        log.warning("reattach main menu failed: %s", e)


def _media_kind_label(message: Message) -> str:
    if message.photo:
        return "📷 photo"
    if message.video:
        return "🎥 video"
    if message.animation:
        return "🎞 gif"
    if message.document:
        return "📎 file"
    return ""


def _join_caption_and_media(caption: str, media_bbcode: str) -> str:
    parts = [p for p in (caption, media_bbcode) if p]
    return "\n\n".join(parts)


_PHOTO_EXT = "jpg"
_VIDEO_EXT = "mp4"
_ANIM_EXT = "mp4"
_DOC_EXT_BY_MIME = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/gif": "gif",
    "image/webp": "webp",
    "video/mp4": "mp4",
    "video/webm": "webm",
    "video/quicktime": "mov",
}


async def _build_media_bbcode(message: Message, store: Store, config: Config) -> str:
    """Inspect the TG message for media; if any, host it via /m/<token> and return BBCode.

    Returns an empty string if no media.

    For photos: ``[IMG]<url>[/IMG]``.
    For videos / animations / documents: bare URL on its own line (XenForo will linkify).
    """
    import secrets

    file_id: str | None = None
    mime_type: str | None = None
    ext = ""

    if message.photo:
        # Largest size last.
        file_id = message.photo[-1].file_id
        mime_type = "image/jpeg"
        ext = _PHOTO_EXT
        kind = "photo"
    elif message.video:
        file_id = message.video.file_id
        mime_type = message.video.mime_type or "video/mp4"
        ext = _DOC_EXT_BY_MIME.get(mime_type, _VIDEO_EXT)
        kind = "video"
    elif message.animation:
        file_id = message.animation.file_id
        mime_type = message.animation.mime_type or "video/mp4"
        ext = _DOC_EXT_BY_MIME.get(mime_type, _ANIM_EXT)
        kind = "animation"
    elif message.document:
        file_id = message.document.file_id
        mime_type = message.document.mime_type or "application/octet-stream"
        ext = _DOC_EXT_BY_MIME.get(mime_type, "bin")
        kind = "document" if not (mime_type or "").startswith("image/") else "photo"
    else:
        return ""

    if not config.public_url:
        log.warning("PUBLIC_URL is not set — cannot build a hostable media URL")
        return ""

    token = secrets.token_urlsafe(16)
    await store.add_file_token(token, file_id, mime_type)
    url = f"{config.public_url}/m/{token}.{ext}"

    if kind == "photo":
        return f"[IMG]{url}[/IMG]"
    return url
