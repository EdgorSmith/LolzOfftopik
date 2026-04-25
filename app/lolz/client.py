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
                    "User-Agent": "LolzOfftopik/0.1 (+personal-tg-client)",
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
