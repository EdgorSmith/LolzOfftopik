"""SQLite-backed persistent state."""

from __future__ import annotations

import asyncio
import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass


@dataclass
class CardRecord:
    """A row in the `cards` table — a Telegram message that represents a forum thread or a reply."""

    chat_id: int
    message_id: int
    thread_id: int
    post_id: int | None  # the user's reply post id (NULL until they reply)
    state: str  # 'pending' (waiting for action) or 'replied'
    is_photo_card: int  # 1 if the original card was sent as a photo (caption-based), else 0
    creator_user_id: int = 0  # user_id of the thread's OP (used for "is_own" UI checks)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS seen_threads (
    thread_id INTEGER PRIMARY KEY,
    seen_at INTEGER NOT NULL DEFAULT (strftime('%s','now'))
);

CREATE TABLE IF NOT EXISTS cards (
    chat_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    thread_id INTEGER NOT NULL,
    post_id INTEGER,
    state TEXT NOT NULL DEFAULT 'pending',
    is_photo_card INTEGER NOT NULL DEFAULT 0,
    creator_user_id INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (chat_id, message_id)
);

CREATE INDEX IF NOT EXISTS idx_cards_thread ON cards(thread_id);

CREATE TABLE IF NOT EXISTS pending_actions (
    chat_id INTEGER NOT NULL,
    prompt_message_id INTEGER NOT NULL,
    action TEXT NOT NULL,        -- 'reply' | 'edit' | 'create_title' | 'create_body'
    target_thread_id INTEGER,    -- for 'reply'
    target_post_id INTEGER,      -- for 'edit'
    card_chat_id INTEGER NOT NULL,
    card_message_id INTEGER NOT NULL,
    payload TEXT,                -- multi-step state (e.g. title between create_title and create_body)
    PRIMARY KEY (chat_id, prompt_message_id)
);

CREATE TABLE IF NOT EXISTS file_tokens (
    token TEXT PRIMARY KEY,
    file_id TEXT NOT NULL,
    mime_type TEXT,
    created_at INTEGER NOT NULL DEFAULT (strftime('%s','now'))
);
"""


class Store:
    def __init__(self, path: str) -> None:
        self.path = path
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self._lock = asyncio.Lock()
        # In-memory cache of /users/me — populated by main.py at startup.
        self._self_user_id: int = 0
        self._self_username: str = ""
        with self._connect() as conn:
            conn.executescript(_SCHEMA)
            # Safe additive migration for existing databases.
            cols = {row["name"] for row in conn.execute("PRAGMA table_info(pending_actions)").fetchall()}
            if "payload" not in cols:
                conn.execute("ALTER TABLE pending_actions ADD COLUMN payload TEXT")
            cards_cols = {row["name"] for row in conn.execute("PRAGMA table_info(cards)").fetchall()}
            if "creator_user_id" not in cards_cols:
                conn.execute("ALTER TABLE cards ADD COLUMN creator_user_id INTEGER NOT NULL DEFAULT 0")
            conn.commit()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    # ----- generic key/value ----------------------------------------------------

    async def get_setting(self, key: str) -> str | None:
        async with self._lock:
            with self._connect() as conn:
                row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
                return row["value"] if row else None

    async def set_setting(self, key: str, value: str) -> None:
        async with self._lock:
            with self._connect() as conn:
                conn.execute(
                    "INSERT INTO settings(key,value) VALUES(?,?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (key, value),
                )
                conn.commit()

    async def delete_setting(self, key: str) -> None:
        async with self._lock:
            with self._connect() as conn:
                conn.execute("DELETE FROM settings WHERE key=?", (key,))
                conn.commit()

    # ----- bot lock-state -------------------------------------------------------

    async def is_unlocked(self) -> bool:
        return (await self.get_setting("unlocked")) == "1"

    async def set_unlocked(self, value: bool) -> None:
        await self.set_setting("unlocked", "1" if value else "0")

    async def is_polling_enabled(self) -> bool:
        return (await self.get_setting("polling_enabled")) == "1"

    async def set_polling_enabled(self, value: bool) -> None:
        await self.set_setting("polling_enabled", "1" if value else "0")

    async def get_baseline_thread_id(self) -> int:
        v = await self.get_setting("baseline_thread_id")
        return int(v) if v else 0

    async def set_baseline_thread_id(self, value: int) -> None:
        await self.set_setting("baseline_thread_id", str(value))

    # ----- seen threads ---------------------------------------------------------

    async def is_seen(self, thread_id: int) -> bool:
        async with self._lock:
            with self._connect() as conn:
                row = conn.execute("SELECT 1 FROM seen_threads WHERE thread_id=?", (thread_id,)).fetchone()
                return row is not None

    async def mark_seen(self, thread_id: int) -> None:
        async with self._lock:
            with self._connect() as conn:
                conn.execute("INSERT OR IGNORE INTO seen_threads(thread_id) VALUES(?)", (thread_id,))
                conn.commit()

    # ----- cards ----------------------------------------------------------------

    async def add_card(
        self,
        chat_id: int,
        message_id: int,
        thread_id: int,
        is_photo_card: bool,
        creator_user_id: int = 0,
    ) -> None:
        async with self._lock:
            with self._connect() as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO cards"
                    "(chat_id,message_id,thread_id,post_id,state,is_photo_card,creator_user_id)"
                    " VALUES(?,?,?,NULL,'pending',?,?)",
                    (chat_id, message_id, thread_id, 1 if is_photo_card else 0, int(creator_user_id or 0)),
                )
                conn.commit()

    async def get_card(self, chat_id: int, message_id: int) -> CardRecord | None:
        async with self._lock:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT chat_id,message_id,thread_id,post_id,state,is_photo_card,creator_user_id "
                    "FROM cards WHERE chat_id=? AND message_id=?",
                    (chat_id, message_id),
                ).fetchone()
                if not row:
                    return None
                return CardRecord(**dict(row))

    async def update_card_after_reply(
        self,
        chat_id: int,
        old_message_id: int,
        new_message_id: int,
        post_id: int,
    ) -> None:
        """The card may have been redelivered as a fresh text message after a photo card."""
        async with self._lock:
            with self._connect() as conn:
                if old_message_id != new_message_id:
                    conn.execute(
                        "DELETE FROM cards WHERE chat_id=? AND message_id=?",
                        (chat_id, old_message_id),
                    )
                conn.execute(
                    "INSERT OR REPLACE INTO cards(chat_id,message_id,thread_id,post_id,state,is_photo_card)"
                    " VALUES(?,?,(SELECT thread_id FROM cards WHERE chat_id=? AND message_id=?),?,'replied',0)",
                    (chat_id, new_message_id, chat_id, old_message_id, post_id),
                )
                # If the previous SELECT returned NULL (race), fix thread_id later via update
                conn.commit()

    async def update_card_thread(self, chat_id: int, message_id: int, thread_id: int, post_id: int) -> None:
        async with self._lock:
            with self._connect() as conn:
                conn.execute(
                    "UPDATE cards SET thread_id=?, post_id=?, state='replied', is_photo_card=0 "
                    "WHERE chat_id=? AND message_id=?",
                    (thread_id, post_id, chat_id, message_id),
                )
                conn.commit()

    # ----- self-user cache (in-memory) -----------------------------------------

    def set_self_user(self, user_id: int, username: str) -> None:
        self._self_user_id = int(user_id or 0)
        self._self_username = str(username or "")

    @property
    def self_user_id(self) -> int:
        return self._self_user_id

    @property
    def self_username(self) -> str:
        return self._self_username

    # ----- pending actions ------------------------------------------------------

    async def set_pending_reply(
        self,
        chat_id: int,
        prompt_message_id: int,
        thread_id: int,
        card_chat_id: int,
        card_message_id: int,
    ) -> None:
        await self._set_pending(
            chat_id, prompt_message_id, "reply",
            target_thread_id=thread_id,
            target_post_id=None,
            card_chat_id=card_chat_id,
            card_message_id=card_message_id,
            payload=None,
        )

    async def set_pending_edit(
        self,
        chat_id: int,
        prompt_message_id: int,
        post_id: int,
        card_chat_id: int,
        card_message_id: int,
    ) -> None:
        await self._set_pending(
            chat_id, prompt_message_id, "edit",
            target_thread_id=None,
            target_post_id=post_id,
            card_chat_id=card_chat_id,
            card_message_id=card_message_id,
            payload=None,
        )

    async def set_pending_create_title(self, chat_id: int, prompt_message_id: int) -> None:
        await self._set_pending(
            chat_id, prompt_message_id, "create_title",
            target_thread_id=None,
            target_post_id=None,
            card_chat_id=chat_id,
            card_message_id=prompt_message_id,
            payload=None,
        )

    async def set_pending_create_body(
        self, chat_id: int, prompt_message_id: int, title: str
    ) -> None:
        await self._set_pending(
            chat_id, prompt_message_id, "create_body",
            target_thread_id=None,
            target_post_id=None,
            card_chat_id=chat_id,
            card_message_id=prompt_message_id,
            payload=title,
        )

    async def _set_pending(
        self,
        chat_id: int,
        prompt_message_id: int,
        action: str,
        *,
        target_thread_id: int | None,
        target_post_id: int | None,
        card_chat_id: int,
        card_message_id: int,
        payload: str | None,
    ) -> None:
        async with self._lock:
            with self._connect() as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO pending_actions"
                    "(chat_id,prompt_message_id,action,target_thread_id,target_post_id,"
                    " card_chat_id,card_message_id,payload)"
                    " VALUES(?,?,?,?,?,?,?,?)",
                    (
                        chat_id, prompt_message_id, action,
                        target_thread_id, target_post_id,
                        card_chat_id, card_message_id, payload,
                    ),
                )
                conn.commit()

    # ----- file-token mapping (Telegram file_id <-> proxy token) ---------------

    async def add_file_token(self, token: str, file_id: str, mime_type: str | None) -> None:
        async with self._lock:
            with self._connect() as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO file_tokens(token,file_id,mime_type) VALUES(?,?,?)",
                    (token, file_id, mime_type),
                )
                conn.commit()

    async def get_file_by_token(self, token: str) -> tuple[str, str | None] | None:
        async with self._lock:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT file_id, mime_type FROM file_tokens WHERE token=?",
                    (token,),
                ).fetchone()
                if not row:
                    return None
                return row["file_id"], row["mime_type"]

    async def pop_pending(self, chat_id: int, prompt_message_id: int) -> dict | None:
        async with self._lock:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT action,target_thread_id,target_post_id,card_chat_id,card_message_id,payload "
                    "FROM pending_actions WHERE chat_id=? AND prompt_message_id=?",
                    (chat_id, prompt_message_id),
                ).fetchone()
                if not row:
                    return None
                conn.execute(
                    "DELETE FROM pending_actions WHERE chat_id=? AND prompt_message_id=?",
                    (chat_id, prompt_message_id),
                )
                conn.commit()
                return dict(row)
