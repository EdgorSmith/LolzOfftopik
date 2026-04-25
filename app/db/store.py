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
    PRIMARY KEY (chat_id, message_id)
);

CREATE INDEX IF NOT EXISTS idx_cards_thread ON cards(thread_id);

CREATE TABLE IF NOT EXISTS pending_actions (
    chat_id INTEGER NOT NULL,
    prompt_message_id INTEGER NOT NULL,
    action TEXT NOT NULL,        -- 'reply' | 'edit'
    target_thread_id INTEGER,    -- for 'reply'
    target_post_id INTEGER,      -- for 'edit'
    card_chat_id INTEGER NOT NULL,
    card_message_id INTEGER NOT NULL,
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
        with self._connect() as conn:
            conn.executescript(_SCHEMA)
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

    async def add_card(self, chat_id: int, message_id: int, thread_id: int, is_photo_card: bool) -> None:
        async with self._lock:
            with self._connect() as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO cards(chat_id,message_id,thread_id,post_id,state,is_photo_card)"
                    " VALUES(?,?,?,NULL,'pending',?)",
                    (chat_id, message_id, thread_id, 1 if is_photo_card else 0),
                )
                conn.commit()

    async def get_card(self, chat_id: int, message_id: int) -> CardRecord | None:
        async with self._lock:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT chat_id,message_id,thread_id,post_id,state,is_photo_card "
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

    # ----- pending actions ------------------------------------------------------

    async def set_pending_reply(
        self,
        chat_id: int,
        prompt_message_id: int,
        thread_id: int,
        card_chat_id: int,
        card_message_id: int,
    ) -> None:
        async with self._lock:
            with self._connect() as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO pending_actions"
                    "(chat_id,prompt_message_id,action,target_thread_id,target_post_id,card_chat_id,card_message_id)"
                    " VALUES(?,?,?,?,?,?,?)",
                    (
                        chat_id,
                        prompt_message_id,
                        "reply",
                        thread_id,
                        None,
                        card_chat_id,
                        card_message_id,
                    ),
                )
                conn.commit()

    async def set_pending_edit(
        self,
        chat_id: int,
        prompt_message_id: int,
        post_id: int,
        card_chat_id: int,
        card_message_id: int,
    ) -> None:
        async with self._lock:
            with self._connect() as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO pending_actions"
                    "(chat_id,prompt_message_id,action,target_thread_id,target_post_id,card_chat_id,card_message_id)"
                    " VALUES(?,?,?,?,?,?,?)",
                    (chat_id, prompt_message_id, "edit", None, post_id, card_chat_id, card_message_id),
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
                    "SELECT action,target_thread_id,target_post_id,card_chat_id,card_message_id "
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
