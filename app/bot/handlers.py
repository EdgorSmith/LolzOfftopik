"""Telegram bot handlers: password gate, start/stop polling, like/reply/edit flows."""

from __future__ import annotations

import logging
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
    HELP_BUTTON_TEXT,
    NOTIFS_TOGGLE_BUTTON_TEXTS,
    START_BUTTON_TEXT,
    STOP_BUTTON_TEXT,
    TRANSFER_BUTTON_TEXT,
    cancel_kb,
    confirm_kb,
    confirm_transfer_kb,
    main_menu,
    profile_actions_kb,
    reply_like_kb,
    thread_card_kb,
)
from app.config import Config
from app.db import Store
from app.lolz import LolzClient
from app.lolz.client import LolzApiError

log = logging.getLogger(__name__)


def build_router(
    config: Config,
    store: Store,
    lolz: LolzClient,
) -> Router:
    router = Router(name="lolzofftopik")
    # Cache pending money-transfer details between the "enter amount" step
    # and the confirm-button click. Keyed by (chat_id, confirm_message_id);
    # entries are dropped after the confirm/cancel callback fires.
    transfer_pending: dict[tuple[int, int], dict] = {}

    @router.message(F.from_user.id != config.telegram_owner_id)
    async def reject_strangers(message: Message) -> None:
        log.info(
            "Rejecting non-owner uid=%s",
            message.from_user.id if message.from_user else "?",
        )
        try:
            await message.answer(
                "⛔ Вы не создатель.", reply_markup=ReplyKeyboardRemove()
            )
        except TelegramBadRequest:
            pass

    @router.callback_query(F.from_user.id != config.telegram_owner_id)
    async def reject_strangers_cb(cq: CallbackQuery) -> None:
        await cq.answer("⛔ Вы не создатель.", show_alert=True)

    # ----- start / help --------------------------------------------------------

    @router.message(CommandStart())
    async def cmd_start(message: Message) -> None:
        polling = await store.is_polling_enabled()
        await message.answer(
            "Привет. Готов оффтопить.",
            reply_markup=await _menu(polling),
        )

    @router.message(Command("help", "commands"))
    async def cmd_help(message: Message) -> None:
        await message.answer(_help_text(), parse_mode="HTML")

    @router.message(Command("offtop_on"))
    async def cmd_offtop_on(message: Message, bot: Bot) -> None:
        await _start_polling(message, bot)

    @router.message(Command("offtop_off"))
    async def cmd_offtop_off(message: Message) -> None:
        await _stop_polling(message)

    @router.message(Command("new_thread"))
    async def cmd_new_thread(message: Message) -> None:
        await _begin_create_thread(message)

    @router.message(Command("notifs_on"))
    async def cmd_notifs_on(message: Message) -> None:
        await _set_notifications(message, True)

    @router.message(Command("notifs_off"))
    async def cmd_notifs_off(message: Message) -> None:
        await _set_notifications(message, False)

    @router.message(Command("transfer"))
    async def cmd_transfer(message: Message) -> None:
        await _begin_transfer_open(message)

    # Plain text (NOT a ForceReply response, NOT a slash-command) — main-menu
    # reply-keyboard buttons. Slash-commands are intentionally excluded here so
    # they fall through to their dedicated Command() handlers below; otherwise
    # this catch-all would swallow them.
    @router.message(F.text, F.reply_to_message.is_(None), ~F.text.startswith("/"))
    async def handle_text(message: Message, bot: Bot) -> None:
        text = (message.text or "").strip()
        if text == START_BUTTON_TEXT:
            await _start_polling(message, bot)
            return
        if text == STOP_BUTTON_TEXT:
            await _stop_polling(message)
            return
        if text == CREATE_THREAD_BUTTON_TEXT:
            await _begin_create_thread(message)
            return
        if text in NOTIFS_TOGGLE_BUTTON_TEXTS:
            new_state = not await store.is_notifications_enabled()
            await _set_notifications(message, new_state)
            return
        if text == TRANSFER_BUTTON_TEXT:
            await _begin_transfer_open(message)
            return
        if text == HELP_BUTTON_TEXT:
            await message.answer(_help_text(), parse_mode="HTML")
            return

        # Otherwise — show the menu.
        await message.answer(
            "Не понял. Жми «❓ Команды» или /help.",
            reply_markup=await _menu(),
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
            reply_markup=await _menu(),
            parse_mode="HTML",
            disable_web_page_preview=True,
        )

    async def _stop_polling(message: Message) -> None:
        await store.set_polling_enabled(False)
        await message.answer("⏹ Оффтоп остановлен.", reply_markup=await _menu())

    async def _set_notifications(message: Message, new_state: bool) -> None:
        await store.set_notifications_enabled(new_state)
        await message.answer(
            (
                "🔔 Уведомления включены. Буду присылать ответы, упоминания, комментарии и переводы."
                if new_state
                else "🔕 Уведомления выключены. Новых сообщений от форума присылать не буду."
            ),
            reply_markup=await _menu(),
        )

    async def _begin_transfer_open(message: Message) -> None:
        if not config.lolz_secret_answer:
            await message.answer(
                "⚠ Для переводов нужен <code>LOLZ_SECRET_ANSWER</code> "
                "(секретный ответ из настроек безопасности на lolz). Добавь в ENV и перезапусти.",
                parse_mode="HTML",
            )
            return
        balance_line = await _build_balance_line(lolz)
        prompt = await message.answer(
            f"{balance_line}\n"
            "💰 Кому и сколько перевести?\n"
            "Формат: <code>@username сумма [комментарий]</code>\n"
            "Например: <code>@HvHpasta 100 спасибо</code>",
            parse_mode="HTML",
            reply_markup=ForceReply(input_field_placeholder="@username сумма комментарий..."),
        )
        cancel_msg = await message.answer(
            "Передумал?", reply_markup=cancel_kb(prompt.message_id)
        )
        await store.set_pending_transfer_username(
            message.chat.id, prompt.message_id, cancel_message_id=cancel_msg.message_id
        )

    async def _menu():
        """Build the bottom keyboard with current toggle states."""
        polling = await store.is_polling_enabled()
        notifs = await store.is_notifications_enabled()
        return main_menu(polling, notifs_enabled=notifs)

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
        text, avatar_url, profile_url = _format_profile(user)
        kb = profile_actions_kb(user_id, profile_url=profile_url)
        if avatar_url:
            try:
                await cq.message.answer_photo(
                    avatar_url, caption=text, parse_mode="HTML", reply_markup=kb,
                )
                return
            except TelegramBadRequest as e:
                log.warning("profile photo failed: %s", e)
        await cq.message.answer(
            text, parse_mode="HTML", disable_web_page_preview=True, reply_markup=kb,
        )

    @router.callback_query(F.data.startswith("transfer:"))
    async def cb_transfer(cq: CallbackQuery) -> None:
        try:
            _, user_id_str = cq.data.split(":", 1)
            user_id = int(user_id_str)
        except (ValueError, AttributeError):
            await cq.answer("Битые данные.", show_alert=True)
            return
        if not cq.message:
            await cq.answer()
            return
        if not config.lolz_secret_answer:
            await cq.answer(
                "⚠ Не задан LOLZ_SECRET_ANSWER — переводы отключены.",
                show_alert=True,
            )
            return
        balance_line = await _build_balance_line(lolz)
        prompt = await cq.message.answer(
            f"{balance_line}\n"
            f"💰 Сколько перевести юзеру #{user_id}?\n"
            "Формат: <code>сумма [комментарий]</code>\n"
            "Например: <code>100 спасибо за помощь</code>",
            parse_mode="HTML",
            reply_markup=ForceReply(input_field_placeholder="сумма [комментарий]..."),
        )
        cancel_msg = await cq.message.answer(
            "Передумал?", reply_markup=cancel_kb(prompt.message_id)
        )
        await store.set_pending_transfer(
            cq.message.chat.id, prompt.message_id, user_id,
            cancel_message_id=cancel_msg.message_id,
        )
        await cq.answer()

    @router.callback_query(F.data.startswith("wallpost:"))
    async def cb_wallpost(cq: CallbackQuery) -> None:
        try:
            _, user_id_str = cq.data.split(":", 1)
            user_id = int(user_id_str)
        except (ValueError, AttributeError):
            await cq.answer("Битые данные.", show_alert=True)
            return
        if not cq.message:
            await cq.answer()
            return
        prompt = await cq.message.answer(
            f"✍ Что написать на стене юзера #{user_id}?",
            reply_markup=ForceReply(input_field_placeholder="Сообщение на стену..."),
        )
        cancel_msg = await cq.message.answer(
            "Передумал?", reply_markup=cancel_kb(prompt.message_id)
        )
        await store.set_pending_wallpost(
            cq.message.chat.id, prompt.message_id, user_id,
            cancel_message_id=cancel_msg.message_id,
        )
        await cq.answer()

    @router.callback_query(F.data.startswith("transfer_yes:"))
    async def cb_transfer_yes(cq: CallbackQuery, bot: Bot) -> None:
        if not cq.message:
            await cq.answer()
            return
        key = (cq.message.chat.id, cq.message.message_id)
        details = transfer_pending.pop(key, None)
        if not details:
            await cq.answer("Истекло время подтверждения.", show_alert=True)
            return
        try:
            await lolz.transfer_money(
                amount=details["amount"],
                secret_answer=config.lolz_secret_answer,
                user_id=details.get("user_id"),
                username=details.get("username"),
                comment=details.get("comment", ""),
            )
        except LolzApiError as e:
            log.warning("transfer_money failed: %s", e)
            await cq.answer(f"Ошибка: {e}", show_alert=True)
            await _safe_delete(bot, cq.message.chat.id, cq.message.message_id)
            return
        recipient = details.get("username") or f"#{details.get('user_id')}"
        await cq.answer("✅ Переведено")
        await cq.message.edit_text(
            f"✅ Переведено <b>{details['amount']:.2f} ₽</b> → "
            f"<code>{hd.quote(str(recipient))}</code>",
            parse_mode="HTML",
        )

    @router.callback_query(F.data.startswith("delpost:"))
    async def cb_delpost(cq: CallbackQuery) -> None:
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

    @router.callback_query(F.data.startswith("creply:"))
    async def cb_creply(cq: CallbackQuery) -> None:
        """Reply to a post-comment notification by posting a comment under that post."""
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
            f"💬 Напиши комментарий под постом #{post_id}.",
            reply_markup=ForceReply(input_field_placeholder="Комментарий..."),
        )
        cancel_msg = await cq.message.answer(
            "Передумал?", reply_markup=cancel_kb(prompt.message_id)
        )
        await store.set_pending_comment_reply(
            cq.message.chat.id,
            prompt.message_id,
            post_id,
            cancel_message_id=cancel_msg.message_id,
        )
        await cq.answer()

    @router.callback_query(F.data.startswith("rreply:"))
    async def cb_rreply(cq: CallbackQuery) -> None:
        """Reply to a specific post (from the View Replies stream)."""
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
            post = await lolz.get_post(post_id)
        except LolzApiError as e:
            await cq.answer(f"Ошибка: {e}", show_alert=True)
            return
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
            elif action == "comment_reply":
                post_id = int(pending["target_post_id"])
                comment_id = await lolz.create_post_comment(post_id, body)
                preview_url = (
                    f"https://lolz.live/posts/comments/{comment_id}/"
                    if comment_id else f"https://lolz.live/posts/{post_id}/"
                )
                await message.answer(
                    f"✓ Комментарий отправлен.\n{preview_url}",
                    disable_web_page_preview=True,
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
            elif action == "transfer_open":
                # User typed "@username amount [comment]" — parse and ask for confirmation.
                parsed = _parse_transfer_input(message.text or message.caption or "")
                if parsed is None:
                    await message.answer(
                        "⚠ Не понял. Формат: <code>@username сумма [комментарий]</code>",
                        parse_mode="HTML",
                    )
                    return
                amount, username, comment = parsed
                confirm = await message.answer(
                    f"💰 Перевести <b>{amount:.2f} ₽</b> юзеру <code>@{hd.quote(username)}</code>?"
                    + (f"\nКомментарий: {hd.quote(comment)}" if comment else ""),
                    parse_mode="HTML",
                    reply_markup=confirm_transfer_kb(
                        yes_data="transfer_yes:open", no_data="noop"
                    ),
                )
                transfer_pending[(confirm.chat.id, confirm.message_id)] = {
                    "amount": amount,
                    "username": username,
                    "comment": comment,
                }
                await _finalize_force_reply()
            elif action == "transfer":
                # User typed "amount [comment]" for a known user_id — confirm.
                target_user_id = int(pending.get("target_post_id") or 0)
                parsed = _parse_amount_and_comment(message.text or message.caption or "")
                if parsed is None or not target_user_id:
                    await message.answer(
                        "⚠ Не понял сумму. Пример: <code>100 спасибо</code>",
                        parse_mode="HTML",
                    )
                    return
                amount, comment = parsed
                confirm = await message.answer(
                    f"💰 Перевести <b>{amount:.2f} ₽</b> юзеру #{target_user_id}?"
                    + (f"\nКомментарий: {hd.quote(comment)}" if comment else ""),
                    parse_mode="HTML",
                    reply_markup=confirm_transfer_kb(
                        yes_data=f"transfer_yes:{target_user_id}", no_data="noop"
                    ),
                )
                transfer_pending[(confirm.chat.id, confirm.message_id)] = {
                    "amount": amount,
                    "user_id": target_user_id,
                    "comment": comment,
                }
                await _finalize_force_reply()
            elif action == "wallpost":
                target_user_id = int(pending.get("target_post_id") or 0)
                if not target_user_id:
                    await message.answer("⚠ Неизвестный юзер для стены.")
                    return
                pp_id = await lolz.create_profile_post(target_user_id, body)
                if pp_id:
                    url = f"https://lolz.live/profile-posts/{pp_id}/"
                    await message.answer(
                        f"✅ Сообщение отправлено на стену: {url}",
                        disable_web_page_preview=True,
                    )
                else:
                    await message.answer(
                        f"✅ Сообщение отправлено на стену юзера #{target_user_id}.",
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
        post = await lolz.get_post(quote_post_id)
    except LolzApiError as e:
        log.warning("quote lookup failed for post %s: %s", quote_post_id, e)
        return my_body
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


def _format_profile(user: dict) -> tuple[str, str | None, str]:
    """Render a lolz user dict into HTML for Telegram.

    Returns ``(text, avatar_url|None, profile_url)``. The profile URL is
    handed back to the caller so it can be wired into an inline button.
    """
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
    return "\n".join(lines), (avatar or None), profile_url


_BALANCE_FIELD_NAMES = (
    "balance",
    "user_money",
    "user_balance",
    "user_balance_format",
    "user_balance_short",
    "money",
    "value",
)


def _extract_balance_value(data: dict) -> str | None:
    """Pull a printable balance string out of an API response.

    Walks the canonical fields (``balance``, ``user_money``…) at the top
    level and inside ``raw`` (where ``/users/me`` data lives after our
    fallback). Returns ``None`` if nothing useful is there.
    """
    if not isinstance(data, dict):
        return None
    for source in (data, data.get("raw") if isinstance(data.get("raw"), dict) else None):
        if not source:
            continue
        for key in _BALANCE_FIELD_NAMES:
            v = source.get(key)
            if isinstance(v, str) and v.strip():
                return v.strip()
            if isinstance(v, (int, float)):
                return f"{float(v):.2f} ₽"
    return None


def _format_balance(data: dict) -> str:
    """Pretty-print whatever ``LolzClient.get_balance()`` returned.

    Different lolz hosts surface the balance in different shapes — the
    canonical one is ``{"balance": "1234.56 ₽", ...}`` but some return a
    nested ``{currency: amount}`` map. We handle both.
    """
    if not data:
        return "💼 Баланс пуст или недоступен."
    bal = _extract_balance_value(data)
    if bal is not None:
        return f"💼 Баланс: <b>{hd.quote(bal)}</b>"
    # Some endpoints return per-currency dicts: {"rub": 100, "usd": 5}
    parts: list[str] = []
    for k, v in data.items():
        if k in {"raw", "links", "permissions"}:
            continue
        if isinstance(v, (int, float, str)) and str(v).strip():
            parts.append(f"{hd.quote(str(k))}: <b>{hd.quote(str(v))}</b>")
    if parts:
        return "💼 Баланс\n" + "\n".join(parts)
    return "💼 Баланс не определён."


async def _build_balance_line(lolz: LolzClient) -> str:
    """Inline balance summary for the transfer prompts.

    Returns a single short line like ``💼 Баланс: <b>123.45 ₽</b>``,
    or a fallback notice when the API didn't surface a numeric value.
    """
    try:
        data = await lolz.get_balance()
    except LolzApiError as e:
        log.warning("get_balance failed: %s", e)
        return "💼 Баланс: недоступен (нет права 'payment' у токена)"
    bal = _extract_balance_value(data)
    if bal is not None:
        return f"💼 Баланс: <b>{hd.quote(bal)}</b>"
    return "💼 Баланс: не определён (скорее всего у токена нет права 'payment')"


def _parse_transfer_input(text: str) -> tuple[float, str, str] | None:
    """Parse ``@username 100 [комментарий]`` → ``(amount, username, comment)``."""
    import re as _re

    m = _re.match(
        r"\s*@?(?P<u>[A-Za-z0-9_А-Яа-яёЁ\-\.]+)\s+(?P<a>[\d.,]+)\s*(?P<c>.*)",
        text or "",
        _re.DOTALL,
    )
    if not m:
        return None
    try:
        amount = float(m.group("a").replace(",", "."))
    except ValueError:
        return None
    if amount <= 0:
        return None
    return amount, m.group("u").strip(), m.group("c").strip()


def _parse_amount_and_comment(text: str) -> tuple[float, str] | None:
    """Parse ``100 [комментарий]`` → ``(amount, comment)``."""
    import re as _re

    m = _re.match(r"\s*(?P<a>[\d.,]+)\s*(?P<c>.*)", text or "", _re.DOTALL)
    if not m:
        return None
    try:
        amount = float(m.group("a").replace(",", "."))
    except ValueError:
        return None
    if amount <= 0:
        return None
    return amount, m.group("c").strip()


async def _reattach_main_menu(
    bot: Bot,
    store: Store,
    chat_id: int,
) -> None:
    """Send a tiny confirmation that re-attaches the persistent reply keyboard."""
    polling = await store.is_polling_enabled()
    notifs = await store.is_notifications_enabled()
    try:
        await bot.send_message(
            chat_id,
            "✅ Готово.",
            reply_markup=main_menu(polling, notifs_enabled=notifs),
        )
    except TelegramBadRequest as e:
        log.warning("reattach main menu failed: %s", e)


def _help_text() -> str:
    """Pretty-printed list of all bot commands."""
    lines: list[str] = [
        "<b>Команды</b>",
        "",
        "<b>Базовые</b>",
        "  /start — поприветствовать, показать клавиатуру",
        "  /help, /commands — этот список",
        "",
        "<b>Оффтоп-поллер</b>",
        "  /offtop_on — слежу за новыми темами в оффтопе",
        "  /offtop_off — приостановить",
        "  /new_thread — создать тему (запросит заголовок)",
        "",
        "<b>Уведомления и финансы</b>",
        "  /notifs_on, /notifs_off — вкл/выкл уведомлений с форума",
        "  /transfer — перевести деньги юзеру (требует LOLZ_SECRET_ANSWER, покажет баланс)",
        "",
        "<b>Кнопки</b>",
        "  ▶/⏹ Оффтопить · 💰 Перевести · 📝 Новая тема · 🔔/🔕 Уведомления · ❓ Команды",
        "",
        "<b>На профиле юзера</b>",
        "  💰 Перевести деньги · ✍ Написать на стене",
        "",
        "При нажатии «Перевести деньги» бот сразу покажет текущий баланс в окне ввода.",
    ]
    return "\n".join(lines)


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
