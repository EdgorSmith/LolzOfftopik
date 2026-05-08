"""Inline keyboards used by the bot."""

from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup

START_BUTTON_TEXT = "▶ Начать оффтопить"
STOP_BUTTON_TEXT = "⏹ Окончить оффтоп"
CREATE_THREAD_BUTTON_TEXT = "📝 Создать тему"
TRANSFER_BUTTON_TEXT = "💰 Перевести деньги"
BALANCE_BUTTON_TEXT = "💼 Мой баланс"
NOTIFS_ON_BUTTON_TEXT = "🔔 Уведомления: вкл"
NOTIFS_OFF_BUTTON_TEXT = "🔕 Уведомления: выкл"
HELP_BUTTON_TEXT = "❓ Команды"
# Either label triggers the toggle handler.
NOTIFS_TOGGLE_BUTTON_TEXTS = (NOTIFS_ON_BUTTON_TEXT, NOTIFS_OFF_BUTTON_TEXT)


def main_menu(
    polling_enabled: bool,
    *,
    notifs_enabled: bool = True,
) -> ReplyKeyboardMarkup:
    """Bottom reply keyboard.

    Layout:
      [▶ / ⏹ оффтоп]
      [💰 Перевести]   [💼 Мой баланс]
      [📝 Создать тему] [🔔/🔕 Уведомления]
      [❓ Команды]
    """
    poll_label = STOP_BUTTON_TEXT if polling_enabled else START_BUTTON_TEXT
    notif_label = NOTIFS_ON_BUTTON_TEXT if notifs_enabled else NOTIFS_OFF_BUTTON_TEXT
    rows = [
        [KeyboardButton(text=poll_label)],
        [
            KeyboardButton(text=TRANSFER_BUTTON_TEXT),
            KeyboardButton(text=BALANCE_BUTTON_TEXT),
        ],
        [
            KeyboardButton(text=CREATE_THREAD_BUTTON_TEXT),
            KeyboardButton(text=notif_label),
        ],
        [KeyboardButton(text=HELP_BUTTON_TEXT)],
    ]
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


def confirm_transfer_kb(yes_data: str, no_data: str = "noop") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(text="❌ Отмена", callback_data=no_data),
            InlineKeyboardButton(text="✅ Перевести", callback_data=yes_data),
        ]]
    )


def profile_actions_kb(user_id: int, *, profile_url: str | None = None) -> InlineKeyboardMarkup:
    """Action menu shown under a user's profile card.

    Lets the bot owner transfer money to that user, leave a profile-post on
    their wall, check their own balance, or open the profile page on lolz.
    """
    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(text="💰 Перевести деньги", callback_data=f"transfer:{user_id}"),
            InlineKeyboardButton(text="✍ На стене", callback_data=f"wallpost:{user_id}"),
        ],
        [
            InlineKeyboardButton(text="💼 Мой баланс", callback_data="balance"),
        ],
    ]
    if profile_url:
        rows[1].append(InlineKeyboardButton(text="🌐 Открыть", url=profile_url))
    return InlineKeyboardMarkup(inline_keyboard=rows)


def comment_notif_kb(post_id: int, *, creator_user_id: int = 0) -> InlineKeyboardMarkup:
    """Keyboard under a "X прокомментировал/упомянул/ответил" notification."""
    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(text="↩ Ответить", callback_data=f"creply:{post_id}"),
            InlineKeyboardButton(text="🌐 Открыть", url=f"https://lolz.live/posts/{post_id}/"),
        ],
    ]
    if creator_user_id:
        rows.append(
            [InlineKeyboardButton(text="👤 Профиль", callback_data=f"profile:{creator_user_id}")]
        )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def post_notif_kb(post_id: int, *, creator_user_id: int = 0) -> InlineKeyboardMarkup:
    """Keyboard under a regular post notification (reply / mention / quote)."""
    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(text="🌐 Открыть", url=f"https://lolz.live/posts/{post_id}/"),
        ],
    ]
    if creator_user_id:
        rows[0].insert(
            0, InlineKeyboardButton(text="👤 Профиль", callback_data=f"profile:{creator_user_id}")
        )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def generic_notif_kb(url: str, *, creator_user_id: int = 0) -> InlineKeyboardMarkup:
    """Keyboard under non-post notifications (profile_post / payment / follow / …)."""
    rows: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton(text="🌐 Открыть", url=url)],
    ]
    if creator_user_id:
        rows[0].insert(
            0, InlineKeyboardButton(text="👤 Профиль", callback_data=f"profile:{creator_user_id}")
        )
    return InlineKeyboardMarkup(inline_keyboard=rows)
