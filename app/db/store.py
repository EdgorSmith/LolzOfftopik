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
    cancel_message_id INTEGER,   -- optional id of the small 'Передумал?' inline-kb message
    PRIMARY KEY (chat_id, prompt_message_id)
);

CREATE TABLE IF NOT EXISTS file_tokens (
    token TEXT PRIMARY KEY,
    file_id TEXT NOT NULL,
    mime_type TEXT,
    created_at INTEGER NOT NULL DEFAULT (strftime('%s','now'))
);

CREATE TABLE IF NOT EXISTS ai_suggestions (
    chat_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL,        -- the suggestion message we sent
    thread_id INTEGER NOT NULL,
    suggestion_text TEXT NOT NULL,
    card_chat_id INTEGER NOT NULL,
    card_message_id INTEGER NOT NULL,
    created_at INTEGER NOT NULL DEFAULT (strftime('%s','now')),
    PRIMARY KEY (chat_id, message_id)
);

CREATE TABLE IF NOT EXISTS my_replies (
    post_id INTEGER PRIMARY KEY,
    thread_id INTEGER NOT NULL DEFAULT 0,
    thread_title TEXT,
    body_plain TEXT NOT NULL,
    posted_at INTEGER NOT NULL DEFAULT 0,
    learned_at INTEGER NOT NULL DEFAULT (strftime('%s','now'))
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
            if "cancel_message_id" not in cols:
                conn.execute("ALTER TABLE pending_actions ADD COLUMN cancel_message_id INTEGER")
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
        quote_post_id: int | None = None,
        cancel_message_id: int | None = None,
    ) -> None:
        # The optional quote_post_id is stored in 'payload' as a string so the
        # submit step can prepend a [QUOTE] block when posting.
        await self._set_pending(
            chat_id, prompt_message_id, "reply",
            target_thread_id=thread_id,
            target_post_id=None,
            card_chat_id=card_chat_id,
            card_message_id=card_message_id,
            payload=str(quote_post_id) if quote_post_id else None,
            cancel_message_id=cancel_message_id,
        )

    async def set_pending_edit(
        self,
        chat_id: int,
        prompt_message_id: int,
        post_id: int,
        card_chat_id: int,
        card_message_id: int,
        cancel_message_id: int | None = None,
    ) -> None:
        await self._set_pending(
            chat_id, prompt_message_id, "edit",
            target_thread_id=None,
            target_post_id=post_id,
            card_chat_id=card_chat_id,
            card_message_id=card_message_id,
            payload=None,
            cancel_message_id=cancel_message_id,
        )

    async def set_pending_comment_reply(
        self,
        chat_id: int,
        prompt_message_id: int,
        post_id: int,
        cancel_message_id: int | None = None,
    ) -> None:
        # No card to mutate afterwards (notification messages are stand-alone),
        # so card_chat_id/card_message_id just point at the prompt itself.
        await self._set_pending(
            chat_id, prompt_message_id, "comment_reply",
            target_thread_id=None,
            target_post_id=post_id,
            card_chat_id=chat_id,
            card_message_id=prompt_message_id,
            payload=None,
            cancel_message_id=cancel_message_id,
        )

    async def set_pending_create_title(
        self,
        chat_id: int,
        prompt_message_id: int,
        cancel_message_id: int | None = None,
    ) -> None:
        await self._set_pending(
            chat_id, prompt_message_id, "create_title",
            target_thread_id=None,
            target_post_id=None,
            card_chat_id=chat_id,
            card_message_id=prompt_message_id,
            payload=None,
            cancel_message_id=cancel_message_id,
        )

    async def set_pending_create_body(
        self,
        chat_id: int,
        prompt_message_id: int,
        title: str,
        cancel_message_id: int | None = None,
    ) -> None:
        await self._set_pending(
            chat_id, prompt_message_id, "create_body",
            target_thread_id=None,
            target_post_id=None,
            card_chat_id=chat_id,
            card_message_id=prompt_message_id,
            payload=title,
            cancel_message_id=cancel_message_id,
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
        cancel_message_id: int | None = None,
    ) -> None:
        async with self._lock:
            with self._connect() as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO pending_actions"
                    "(chat_id,prompt_message_id,action,target_thread_id,target_post_id,"
                    " card_chat_id,card_message_id,payload,cancel_message_id)"
                    " VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        chat_id, prompt_message_id, action,
                        target_thread_id, target_post_id,
                        card_chat_id, card_message_id, payload, cancel_message_id,
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
                    "SELECT action,target_thread_id,target_post_id,card_chat_id,card_message_id,payload,cancel_message_id "
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

    # ----- AI draft suggestions -------------------------------------------------

    async def add_ai_suggestion(
        self,
        chat_id: int,
        message_id: int,
        thread_id: int,
        suggestion_text: str,
        card_chat_id: int,
        card_message_id: int,
    ) -> None:
        async with self._lock:
            with self._connect() as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO ai_suggestions"
                    "(chat_id,message_id,thread_id,suggestion_text,card_chat_id,card_message_id) "
                    "VALUES(?,?,?,?,?,?)",
                    (chat_id, message_id, thread_id, suggestion_text, card_chat_id, card_message_id),
                )
                conn.commit()

    async def get_ai_suggestion(self, chat_id: int, message_id: int) -> dict | None:
        async with self._lock:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT thread_id,suggestion_text,card_chat_id,card_message_id "
                    "FROM ai_suggestions WHERE chat_id=? AND message_id=?",
                    (chat_id, message_id),
                ).fetchone()
                return dict(row) if row else None

    async def update_ai_suggestion_text(
        self, chat_id: int, message_id: int, suggestion_text: str
    ) -> None:
        async with self._lock:
            with self._connect() as conn:
                conn.execute(
                    "UPDATE ai_suggestions SET suggestion_text=? "
                    "WHERE chat_id=? AND message_id=?",
                    (suggestion_text, chat_id, message_id),
                )
                conn.commit()

    async def delete_ai_suggestion(self, chat_id: int, message_id: int) -> None:
        async with self._lock:
            with self._connect() as conn:
                conn.execute(
                    "DELETE FROM ai_suggestions WHERE chat_id=? AND message_id=?",
                    (chat_id, message_id),
                )
                conn.commit()

    # ----- learned old replies (few-shot for the AI) ----------------------------

    async def upsert_my_reply(
        self,
        post_id: int,
        thread_id: int,
        thread_title: str,
        body_plain: str,
        posted_at: int,
    ) -> None:
        async with self._lock:
            with self._connect() as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO my_replies"
                    "(post_id,thread_id,thread_title,body_plain,posted_at) "
                    "VALUES(?,?,?,?,?)",
                    (post_id, thread_id, thread_title, body_plain, posted_at),
                )
                conn.commit()

    async def count_my_replies(self) -> int:
        async with self._lock:
            with self._connect() as conn:
                row = conn.execute("SELECT COUNT(*) AS c FROM my_replies").fetchone()
                return int(row["c"]) if row else 0

    async def sample_my_replies(
        self, limit: int = 8, *, min_chars: int = 5, max_chars: int = 400
    ) -> list[str]:
        """Return up to `limit` representative reply bodies for few-shot prompting."""
        async with self._lock:
            with self._connect() as conn:
                rows = conn.execute(
                    "SELECT body_plain FROM my_replies "
                    "WHERE LENGTH(body_plain) BETWEEN ? AND ? "
                    "ORDER BY RANDOM() LIMIT ?",
                    (min_chars, max_chars, limit),
                ).fetchall()
                return [r["body_plain"] for r in rows]
