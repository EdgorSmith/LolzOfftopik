"""Inline keyboards used by the bot."""

from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup

START_BUTTON_TEXT = "▶ Начать оффтопить"
STOP_BUTTON_TEXT = "⏹ Окончить оффтоп"
CREATE_THREAD_BUTTON_TEXT = "📝 Создать тему"


def main_menu(polling_enabled: bool) -> ReplyKeyboardMarkup:
    label = STOP_BUTTON_TEXT if polling_enabled else START_BUTTON_TEXT
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=label)],
            [KeyboardButton(text=CREATE_THREAD_BUTTON_TEXT)],
        ],
        resize_keyboard=True,
        one_time_keyboard=False,
    )


def thread_card_kb(thread_id: int, post_id: int, is_liked: bool) -> InlineKeyboardMarkup:
    like_text = "💔 Убрать лайк" if is_liked else "❤ Лайк"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=like_text, callback_data=f"like:{post_id}:{int(is_liked)}"),
                InlineKeyboardButton(text="✍ Ответить", callback_data=f"reply:{thread_id}"),
            ],
            [
                InlineKeyboardButton(text="💬 Посмотреть ответы", callback_data=f"replies:{thread_id}"),
                InlineKeyboardButton(text="🔗 Тема", url=f"https://lolz.live/threads/{thread_id}/"),
            ],
        ]
    )


def reply_like_kb(post_id: int, is_liked: bool) -> InlineKeyboardMarkup:
    """Compact one-button keyboard for liking a single reply post."""
    icon = "💔" if is_liked else "❤"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=icon, callback_data=f"rlike:{post_id}:{int(is_liked)}")]
        ]
    )


def replied_kb(thread_id: int, post_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✏ Изменить ответ", callback_data=f"edit:{post_id}"),
                InlineKeyboardButton(text="💬 Ответы", callback_data=f"replies:{thread_id}"),
            ],
            [InlineKeyboardButton(text="🔗 Мой ответ", url=f"https://lolz.live/posts/{post_id}/")],
        ]
    )
