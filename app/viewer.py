"""'Просмотр' (browse) mode — hit HTML thread pages on lolz.live with the
user's browser session so XenForo's 'members currently viewing this thread'
widget includes them.

The lolz API endpoint (`prod-api.lolz.live`) authenticates with a Bearer
token; that path bumps the user's 'last activity' but does NOT update
XenForo's session table for the public viewing widget. To populate that
widget we have to hit the actual HTML page (`https://lolz.live/threads/...`)
with the same `xf_user` / `xf_session` cookies a real browser would send.

We use a separate aiohttp session here (not the LolzClient) for two reasons:
1. Different base URL (web vs API) and totally different auth (cookies vs
   Bearer).
2. We want to fingerprint requests like a real Chrome session — UA,
   Accept-Language, Accept, Referer — without polluting the API client.
"""

from __future__ import annotations

import asyncio
import logging
import random
from collections import deque

import aiohttp
from yarl import URL

from app.config import Config
from app.db import Store
from app.lolz import LolzClient

log = logging.getLogger(__name__)

# A single realistic Chrome-on-Windows UA. Picked once at startup so all
# requests within a session share the same fingerprint (rotating UA mid-
# session is itself anomalous).
_CHROME_UAS = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/121.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36",
)


class Viewer:
    """Background task that opens forum threads under the user's session.

    Toggled via store setting ``viewer_enabled`` (string ``"1"``/``"0"``).
    Resilient: a failed request just gets logged and we move on; we never
    crash the loop.
    """

    SETTING_KEY = "viewer_enabled"

    def __init__(self, config: Config, store: Store, lolz: LolzClient) -> None:
        self._config = config
        self._store = store
        self._lolz = lolz
        self._task: asyncio.Task | None = None
        self._stop_event = asyncio.Event()
        # Avoid hammering the same thread back-to-back.
        self._recent_viewed: deque[int] = deque(maxlen=20)
        # Pool of candidate thread_ids; refreshed every full pass.
        self._candidates: list[int] = []
        # Pagination cursor for the candidate refresh.
        self._next_page: int = 1
        # Picked once per process, kept stable across requests (rotating UA
        # within the same session is a fingerprinting red flag).
        self._ua: str = random.choice(_CHROME_UAS)
        self._http: aiohttp.ClientSession | None = None

    # ---------------------------------------------------------------- state

    @property
    def configured(self) -> bool:
        return bool(
            self._config.lolz_xf_user_cookie
            and self._config.lolz_xf_session_cookie
        )

    async def is_enabled(self) -> bool:
        v = await self._store.get_setting(self.SETTING_KEY)
        return v == "1"

    async def set_enabled(self, value: bool) -> None:
        await self._store.set_setting(self.SETTING_KEY, "1" if value else "0")

    # ---------------------------------------------------------------- lifecycle

    def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(self._loop(), name="viewer")

    async def stop(self) -> None:
        self._stop_event.set()
        if self._task:
            try:
                await asyncio.wait_for(self._task, timeout=5)
            except TimeoutError:
                self._task.cancel()
        self._task = None
        if self._http and not self._http.closed:
            await self._http.close()
        self._http = None

    # ---------------------------------------------------------------- loop

    async def _loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("viewer iteration failed")
                await self._sleep_or_stop(15)

    async def _tick(self) -> None:
        # Gate: feature off / bot locked / cookies missing → idle.
        if not self.configured:
            await self._sleep_or_stop(60)
            return
        if not await self._store.is_unlocked():
            await self._sleep_or_stop(15)
            return
        if not await self.is_enabled():
            await self._sleep_or_stop(10)
            return

        thread_id = await self._next_thread_id()
        if not thread_id:
            await self._sleep_or_stop(20)
            return

        await self._view_thread(thread_id)
        self._recent_viewed.append(thread_id)

        # Tiny human-like jitter on top of the lolz client's hard 3.1s rate
        # limit. Floor request → request distance ≈ 3.1 + uniform(0, 2.5)
        # seconds; identical fixed intervals are the obvious script tell.
        await self._sleep_or_stop(random.uniform(0.0, 2.5))

    # ---------------------------------------------------------------- candidate selection

    async def _next_thread_id(self) -> int | None:
        # Refill the pool when empty by paginating through the offtop forum
        # listing — we use the API for the *list* (cheap, indexed) and only
        # use the cookie-bearing HTML hits for the actual "view" event.
        if not self._candidates:
            try:
                threads = await self._lolz.list_threads(
                    self._config.lolz_offtop_forum_id,
                    limit=50,
                    order="post_date",
                    direction="desc",
                    include_sticky=False,
                )
            except Exception as e:  # noqa: BLE001
                log.warning("viewer: list_threads failed: %s", e)
                return None
            ids = [t.thread_id for t in threads if t.thread_id]
            if not ids:
                # No threads in offtop? Wait it out, don't loop hot.
                return None
            random.shuffle(ids)
            self._candidates = ids

        # Pop until we find one that wasn't viewed recently.
        while self._candidates:
            tid = self._candidates.pop()
            if tid not in self._recent_viewed:
                return tid
        return None

    # ---------------------------------------------------------------- HTTP / "view"

    async def _ensure_http(self) -> aiohttp.ClientSession:
        if self._http is None or self._http.closed:
            jar = aiohttp.CookieJar(unsafe=False)
            # Build cookies anchored to the lolz.live host so they ride along
            # only on lolz.live requests.
            jar.update_cookies(
                {
                    "xf_user": self._config.lolz_xf_user_cookie,
                    "xf_session": self._config.lolz_xf_session_cookie,
                },
                response_url=URL(self._config.lolz_web_base),
            )
            self._http = aiohttp.ClientSession(
                cookie_jar=jar,
                headers={
                    "User-Agent": self._ua,
                    "Accept": (
                        "text/html,application/xhtml+xml,application/xml;"
                        "q=0.9,image/avif,image/webp,*/*;q=0.8"
                    ),
                    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
                    "Accept-Encoding": "gzip, deflate, br",
                    "Cache-Control": "no-cache",
                    "Pragma": "no-cache",
                    "Sec-Fetch-Dest": "document",
                    "Sec-Fetch-Mode": "navigate",
                    "Sec-Fetch-Site": "same-origin",
                    "Sec-Fetch-User": "?1",
                    "Upgrade-Insecure-Requests": "1",
                    "DNT": "1",
                },
                timeout=aiohttp.ClientTimeout(total=20),
            )
        return self._http

    async def _view_thread(self, thread_id: int) -> None:
        http = await self._ensure_http()
        url = f"{self._config.lolz_web_base}/threads/{thread_id}/"
        forum_url = f"{self._config.lolz_web_base}/forums/{self._config.lolz_offtop_forum_id}/"
        try:
            async with http.get(url, headers={"Referer": forum_url}, allow_redirects=True) as resp:
                # Drain body to avoid keep-alive starvation; we don't parse
                # it (this is a presence ping, not a scrape).
                await resp.read()
                if resp.status >= 400:
                    log.warning(
                        "viewer: GET %s -> %s (cookies stale?)", url, resp.status
                    )
                    # If the session cookie is invalid, back off so we don't
                    # spam dead requests.
                    if resp.status in (401, 403):
                        await self._sleep_or_stop(120)
                else:
                    log.debug("viewer: viewed thread %s (%s)", thread_id, resp.status)
        except (aiohttp.ClientError, TimeoutError) as e:
            log.warning("viewer: GET %s failed: %s", url, e)
            await self._sleep_or_stop(10)

    # ---------------------------------------------------------------- helpers

    async def _sleep_or_stop(self, seconds: float) -> None:
        if seconds <= 0:
            return
        try:
            await asyncio.wait_for(self._stop_event.wait(), timeout=seconds)
        except TimeoutError:
            pass
