"""Inline keyboards used by the bot."""

from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup

START_BUTTON_TEXT = "▶ Начать оффтопить"
STOP_BUTTON_TEXT = "⏹ Окончить оффтоп"
CREATE_THREAD_BUTTON_TEXT = "📝 Создать тему"
AI_ON_BUTTON_TEXT = "🤖 Нейросеть: вкл"
AI_OFF_BUTTON_TEXT = "🤖 Нейросеть: выкл"
HELP_BUTTON_TEXT = "❓ Команды"
# Either label triggers the toggle handler.
AI_TOGGLE_BUTTON_TEXTS = (AI_ON_BUTTON_TEXT, AI_OFF_BUTTON_TEXT)


def main_menu(
    polling_enabled: bool,
    *,
    ai_available: bool = False,
    ai_enabled: bool = False,
) -> ReplyKeyboardMarkup:
    """Bottom reply keyboard.

    The AI row is rendered only when ``ai_available`` is true (i.e. a Gemini
    key is configured). Without a key the row is hidden so the user doesn't see
    a button that does nothing.
    """
    poll_label = STOP_BUTTON_TEXT if polling_enabled else START_BUTTON_TEXT
    ai_label = AI_ON_BUTTON_TEXT if ai_enabled else AI_OFF_BUTTON_TEXT
    rows = [[KeyboardButton(text=poll_label)]]
    if ai_available:
        rows.append([KeyboardButton(text=ai_label)])
    rows.append([
        KeyboardButton(text=CREATE_THREAD_BUTTON_TEXT),
        KeyboardButton(text=HELP_BUTTON_TEXT),
    ])
    return ReplyKeyboardMarkup(
        keyboard=rows,
        resize_keyboard=True,
        one_time_keyboard=False,
    )


def thread_card_kb(
    thread_id: int,
    post_id: int,
    is_liked: bool,
    *,
    creator_user_id: int = 0,
    is_own: bool = False,
    like_count: int = 0,
) -> InlineKeyboardMarkup:
    icon = "💔" if is_liked else "❤"
    like_text = f"{icon} {like_count}" if like_count else icon
    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(text=like_text, callback_data=f"like:{post_id}:{int(is_liked)}"),
            InlineKeyboardButton(text="✍ Ответить", callback_data=f"reply:{thread_id}"),
            InlineKeyboardButton(text="💬 Ответы", callback_data=f"replies:{thread_id}"),
        ],
        [
            InlineKeyboardButton(text="🌐 Открыть", url=f"https://lolz.live/threads/{thread_id}/"),
        ],
    ]
    if creator_user_id:
        rows[1].insert(0, InlineKeyboardButton(text="👤 Профиль", callback_data=f"profile:{creator_user_id}"))
    if is_own:
        rows.append([InlineKeyboardButton(text="🗑 Удалить тему", callback_data=f"delthread:{thread_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def reply_like_kb(post_id: int, is_liked: bool, like_count: int = 0) -> InlineKeyboardMarkup:
    """Compact keyboard under each reply: ❤ (with count) + ↩ reply-to-this-post."""
    icon = "💔" if is_liked else "❤"
    like_text = f"{icon} {like_count}" if like_count else icon
    return InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(text=like_text, callback_data=f"rlike:{post_id}:{int(is_liked)}"),
            InlineKeyboardButton(text="↩", callback_data=f"rreply:{post_id}"),
        ]]
    )


def replied_kb(thread_id: int, post_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✏ Изменить", callback_data=f"edit:{post_id}"),
                InlineKeyboardButton(text="🗑 Удалить", callback_data=f"delpost:{post_id}"),
                InlineKeyboardButton(text="💬 Ответы", callback_data=f"replies:{thread_id}"),
            ],
            [InlineKeyboardButton(text="🌐 Мой ответ", url=f"https://lolz.live/posts/{post_id}/")],
        ]
    )


def cancel_kb(prompt_message_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(text="❌ Отмена", callback_data=f"cancel:{prompt_message_id}")
        ]]
    )


def confirm_kb(yes_data: str, no_data: str = "noop") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(text="❌ Нет", callback_data=no_data),
            InlineKeyboardButton(text="🗑 Да, удалить", callback_data=yes_data),
        ]]
    )
