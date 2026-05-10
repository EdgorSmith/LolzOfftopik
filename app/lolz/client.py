"""Async lolz.live API client (Bearer-authenticated).

The lolz forum API rate-limits at 20 req/min (3s between requests). We enforce a
client-side floor of 3.1s between calls per client instance.
"""

from __future__ import annotations

import asyncio
import logging
import time

import aiohttp

from app.lolz.parser import Thread, parse_thread

log = logging.getLogger(__name__)

_MIN_REQUEST_INTERVAL = 3.1  # seconds


class LolzClient:
    def __init__(self, base_url: str, token: str) -> None:
        self._base = base_url.rstrip("/")
        self._token = token
        self._session: aiohttp.ClientSession | None = None
        self._last_request_at: float = 0.0
        self._gate = asyncio.Lock()

    async def __aenter__(self) -> LolzClient:
        await self._ensure_session()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers={
                    "Authorization": f"Bearer {self._token}",
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/122.0.0.0 Safari/537.36"
                    ),
                    "Accept": "application/json",
                },
                timeout=aiohttp.ClientTimeout(total=30),
            )
        return self._session

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None

    async def _wait_rate_limit(self) -> None:
        async with self._gate:
            elapsed = time.monotonic() - self._last_request_at
            sleep_for = _MIN_REQUEST_INTERVAL - elapsed
            if sleep_for > 0:
                await asyncio.sleep(sleep_for)
            self._last_request_at = time.monotonic()

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        data: dict | None = None,
        json: dict | None = None,
    ) -> dict:
        await self._wait_rate_limit()
        session = await self._ensure_session()
        url = f"{self._base}{path}"
        async with session.request(method, url, params=params, data=data, json=json) as resp:
            text = await resp.text()
            if resp.status == 429:
                # Honour 20rpm just in case.
                log.warning("Got 429 from lolz, sleeping 5s. body=%s", text[:200])
                await asyncio.sleep(5)
                async with session.request(method, url, params=params, data=data, json=json) as r2:
                    text = await r2.text()
                    if r2.status >= 400:
                        raise LolzApiError(r2.status, text)
                    return _safe_json(text)
            if resp.status >= 400:
                raise LolzApiError(resp.status, text)
            return _safe_json(text)

    # ----- threads -------------------------------------------------------------

    async def list_threads(
        self,
        forum_id: int,
        *,
        limit: int = 20,
        order: str = "post_date",
        direction: str = "desc",
        include_sticky: bool = False,
    ) -> list[Thread]:
        params: dict = {
            "forum_id": forum_id,
            "limit": limit,
            "order": order,
            "direction": direction,
        }
        if not include_sticky:
            params["sticky"] = "false"
        data = await self._request("GET", "/threads", params=params)
        threads = [parse_thread(t) for t in data.get("threads", [])]
        return threads

    async def get_thread(self, thread_id: int) -> Thread:
        data = await self._request("GET", f"/threads/{thread_id}")
        return parse_thread(data.get("thread") or data)

    async def get_post(self, post_id: int) -> dict:
        """Fetch a single post (with embedded ``thread`` field)."""
        data = await self._request("GET", f"/posts/{post_id}")
        return data.get("post") or {}

    async def list_thread_posts(
        self,
        thread_id: int,
        *,
        limit: int = 20,
        order: str = "natural_reverse",
    ) -> list[dict]:
        """Fetch posts in a thread. Returns the raw post dicts (the bot renders them)."""
        params = {"thread_id": thread_id, "limit": limit, "order": order}
        data = await self._request("GET", "/posts", params=params)
        return list(data.get("posts") or [])

    async def list_post_comments(
        self,
        post_id: int,
        *,
        limit: int = 20,
    ) -> list[dict]:
        """Fetch comments under a post. Used to recover the real text of a
        comment notification when the rendered preview is truncated or
        replaced with ``[Скрытый контент]``.
        """
        data = await self._request(
            "GET", f"/posts/{post_id}/comments", params={"limit": limit}
        )
        return list(data.get("comments") or [])

    # ----- posts ---------------------------------------------------------------

    async def reply(self, thread_id: int, body: str) -> int:
        """Create a reply post in the given thread. Returns new post_id."""
        data = await self._request(
            "POST",
            "/posts",
            data={"thread_id": thread_id, "post_body": body},
        )
        post = data.get("post") or {}
        return int(post.get("post_id", 0))

    async def create_thread(self, forum_id: int, title: str, body: str) -> int:
        """Create a new thread in the given forum. Returns new thread_id.

        The lolz prod-api expects the title param under both 'thread_title'
        and 'title' depending on the route version — we send both.
        """
        data = await self._request(
            "POST",
            "/threads",
            data={
                "forum_id": forum_id,
                "thread_title": title,
                "title": title,
                "post_body": body,
            },
        )
        thread = data.get("thread") or {}
        return int(thread.get("thread_id", 0))

    async def create_post_comment(self, post_id: int, body: str) -> int:
        """Post a comment under an existing post. Returns the new comment_id."""
        data = await self._request(
            "POST",
            f"/posts/{post_id}/comments",
            data={"post_comment_body": body, "comment_body": body},
        )
        c = data.get("post_comment") or data.get("comment") or {}
        return int(c.get("post_comment_id", 0) or c.get("comment_id", 0) or 0)

    async def edit_post(self, post_id: int, body: str) -> None:
        await self._request("PUT", f"/posts/{post_id}", data={"post_body": body})

    async def like_post(self, post_id: int) -> None:
        await self._request("POST", f"/posts/{post_id}/likes")

    async def unlike_post(self, post_id: int) -> None:
        await self._request("DELETE", f"/posts/{post_id}/likes")

    async def delete_post(self, post_id: int, reason: str = "") -> None:
        params = {"reason": reason} if reason else None
        await self._request("DELETE", f"/posts/{post_id}", params=params)

    async def delete_thread(self, thread_id: int, reason: str = "") -> None:
        params = {"reason": reason} if reason else None
        await self._request("DELETE", f"/threads/{thread_id}", params=params)

    # ----- notifications -------------------------------------------------------

    async def list_notifications(self, *, limit: int = 20, page: int = 1) -> dict:
        """Fetch the authenticated user's notifications (alerts). Returns the
        raw response with a ``notifications`` list."""
        return await self._request(
            "GET", "/notifications", params={"limit": limit, "page": page}
        )

    # ----- users ---------------------------------------------------------------

    async def me(self) -> dict:
        """Returns the authenticated user's record."""
        data = await self._request("GET", "/users/me")
        return data.get("user") or {}

    async def get_user(self, user_id: int) -> dict:
        data = await self._request("GET", f"/users/{user_id}")
        return data.get("user") or {}

    async def list_user_timeline(
        self,
        user_id: int,
        *,
        page: int = 1,
        limit: int = 20,
    ) -> dict:
        """Paginated content stream for a user (posts, threads, profile-posts).

        Returns the raw API payload so callers can inspect ``contents`` and
        ``links`` (for pagination).
        """
        return await self._request(
            "GET",
            f"/users/{user_id}/timeline",
            params={"page": page, "limit": limit},
        )

    # ----- payments / wallet ---------------------------------------------------

    async def get_balance(self) -> dict:
        """Read my own forum/market balance.

        Tries ``/payments/balance`` first (returns multiple currencies on
        lolz prod-api), then falls back to ``/users/me`` and reads the
        ``user_money`` / ``user_balance`` fields. Returns a dict like
        ``{"balance": "1234.56 ₽", "raw": {...}}``.
        """
        try:
            data = await self._request("GET", "/payments/balance")
        except LolzApiError as e:
            log.info("/payments/balance unavailable (%s); falling back to /users/me", e)
            data = None
        if data:
            return data

        me = await self.me()
        balance = (
            me.get("user_money")
            or me.get("user_balance")
            or me.get("user_balance_format")
            or me.get("user_balance_short")
            or ""
        )
        return {"balance": balance, "raw": me}

    async def transfer_money(
        self,
        *,
        amount: float,
        secret_answer: str,
        user_id: int | None = None,
        username: str | None = None,
        comment: str = "",
        transfer_hold: int | bool = False,
        currency: str = "rub",
    ) -> dict:
        """Transfer ``amount`` rubles to ``user_id`` / ``username``.

        ``secret_answer`` is your forum security-question answer
        (configured in ``LOLZ_SECRET_ANSWER``). ``transfer_hold`` of 1 / 2
        applies a 24h / 48h hold; ``False`` / ``0`` sends instantly.

        The endpoint is ``POST /payments/transfer`` on the bdApi base; if
        the host returns 404 we retry against the canonical zelenka market
        host. Both speak the same form-encoded contract.
        """
        if not user_id and not username:
            raise ValueError("transfer_money: user_id or username is required")
        payload: dict = {
            "amount": amount,
            "secret_answer": secret_answer,
            "currency": currency,
        }
        if user_id:
            payload["user_id"] = user_id
        if username:
            payload["username"] = username
        if comment:
            payload["comment"] = comment
        if transfer_hold:
            payload["transfer_hold"] = int(transfer_hold) if transfer_hold is not True else 1
        try:
            return await self._request("POST", "/payments/transfer", data=payload)
        except LolzApiError as e:
            if e.status != 404:
                raise
            # Some lolz API hosts mount payments under /zelenka/payments/transfer.
            return await self._request("POST", "/zelenka/payments/transfer", data=payload)

    # ----- conversations / private messages -----------------------------------

    async def list_conversations(
        self,
        *,
        folder: str = "all",
        page: int = 1,
        limit: int = 10,
    ) -> dict:
        """List my conversations.

        ``folder`` is one of: all / unread / groups / market / market_replacements
        / staff / giveaways / p2p. The raw response carries a ``conversations``
        list and pagination ``links``.
        """
        return await self._request(
            "GET",
            "/conversations",
            params={"folder": folder, "page": page, "limit": limit},
        )

    async def get_conversation(self, conversation_id: int) -> dict:
        """Fetch a single conversation (participants, last message, …)."""
        data = await self._request("GET", f"/conversations/{conversation_id}")
        return data.get("conversation") or data

    async def list_conversation_messages(
        self,
        conversation_id: int,
        *,
        page: int = 1,
        limit: int = 10,
        order: str = "natural_reverse",
    ) -> list[dict]:
        """Fetch messages of a conversation. Newest-first by default."""
        data = await self._request(
            "GET",
            f"/conversations/{conversation_id}/messages",
            params={"page": page, "limit": limit, "order": order},
        )
        return list(data.get("messages") or [])

    async def send_conversation_message(
        self,
        conversation_id: int,
        body: str,
        *,
        reply_message_id: int | None = None,
    ) -> dict:
        """Append a message to a conversation. Returns the raw API response."""
        payload: dict = {"message_body": body}
        if reply_message_id:
            payload["reply_message_id"] = int(reply_message_id)
        return await self._request(
            "POST",
            f"/conversations/{conversation_id}/messages",
            data=payload,
        )

    async def start_conversation(self, user_id: int) -> int:
        """Open / re-open a 1‑on‑1 conversation with ``user_id``.

        Returns the ``conversation_id`` so the caller can post a message in
        it. Used by the "💬 Личные сообщения → Новый диалог" flow.
        """
        data = await self._request(
            "POST", "/conversations/start", data={"user_id": int(user_id)}
        )
        c = data.get("conversation") or {}
        return int(c.get("conversation_id", 0) or data.get("conversation_id", 0) or 0)

    async def create_conversation_with_username(
        self,
        username: str,
        body: str,
    ) -> int:
        """Send the very first message in a brand-new conversation by
        username. The forum endpoint accepts an array of recipients; we
        always pass exactly one for the personal-PM flow.

        Returns the new ``conversation_id`` (or 0 if the API didn't echo it).
        """
        data = await self._request(
            "POST",
            "/conversations",
            data={
                "recipients[]": username,
                "is_group": "false",
                "message_body": body,
            },
        )
        c = data.get("conversation") or {}
        return int(c.get("conversation_id", 0) or 0)

    # ----- profile posts (wall comments) --------------------------------------

    async def create_profile_post(self, user_id: int, body: str) -> int:
        """Post a message on ``user_id``'s profile wall. Returns the new
        ``profile_post_id`` (0 if the API didn't echo it back).
        """
        data = await self._request(
            "POST",
            "/profile-posts",
            data={"user_id": user_id, "post_body": body},
        )
        pp = data.get("profile_post") or {}
        return int(pp.get("profile_post_id", 0) or 0)


class LolzApiError(RuntimeError):
    def __init__(self, status: int, body: str) -> None:
        super().__init__(f"lolz api error {status}: {body[:300]}")
        self.status = status
        self.body = body


def _safe_json(text: str) -> dict:
    import json

    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise LolzApiError(0, f"invalid JSON: {text[:200]}") from exc
