"""'Просмотр' (browse) mode — hit HTML thread pages on lolz.live with the
user's browser session so XenForo's 'members currently viewing this thread'
widget includes them.

The lolz API endpoint (``prod-api.lolz.live``) authenticates with a Bearer
token; that path bumps the user's 'last activity' but does NOT update
XenForo's session table for the public viewing widget. To populate that
widget we have to hit the actual HTML page (``https://lolz.live/threads/…``)
with the same ``xf_user`` / ``xf_session`` / ``xf_csrf`` cookies a real
browser would send.

A separate :mod:`aiohttp` session is used (not ``LolzClient``) for two
reasons:

1. Different base URL (web vs API) and totally different auth (cookies vs
   Bearer).
2. Requests are fingerprinted like a real Chrome session — UA,
   Accept-Language, Accept, Sec-Fetch-* headers, Referer — without
   polluting the API client.

Behaviour goals:

* No fixed cadence between threads. Real humans do not click every N
  seconds. Per-thread "dwell time" is drawn from an exponential
  distribution clamped to a sensible range; some threads are read briefly,
  some are read longer, some are opened and immediately closed.
* Multiple concurrent "tabs" (configurable, default 2). A real reader
  opens a thread, scrolls a bit, opens another in a new tab while the
  first stays loaded. Concurrency-1 is the obvious bot pattern.
* Occasional second-page loads (``?page=2``) on the same thread to mimic
  scroll-down behaviour.
* If the session cookies expire (3 consecutive 401/403), the viewer
  disables itself and notifies the bot owner.
"""

from __future__ import annotations

import asyncio
import logging
import random
from collections import deque

import aiohttp
from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
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
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/123.0.0.0 Safari/537.36",
)

# Tunables: kept module-level so they're easy to spot in code review.
# All times are in seconds.
DWELL_MEAN = 6.0          # mean of exponential dwell-time distribution
DWELL_MIN = 1.0           # short visits ("opened, closed")
DWELL_MAX = 22.0          # long visits ("read attentively")
QUICK_BOUNCE_PROB = 0.10  # chance a thread is closed in <2s
PAGE_TWO_PROB = 0.25      # chance we load /page-2 mid-dwell
CONCURRENCY = 2           # number of parallel "open tabs"
LIST_REFRESH_LIMIT = 100  # threads pulled per listing pass
RECENT_VIEWED_WINDOW = 30 # don't repeat a thread within this many views
COOKIE_FAIL_THRESHOLD = 3 # consecutive 401/403 → disable + notify


class Viewer:
    """Background task that opens forum threads under the user's session.

    Toggled via store setting ``viewer_enabled`` (string ``"1"`` / ``"0"``).
    Resilient: a failed request just gets logged; the loop never crashes.
    """

    SETTING_KEY = "viewer_enabled"

    def __init__(
        self,
        config: Config,
        store: Store,
        lolz: LolzClient,
        bot: Bot | None = None,
    ) -> None:
        self._config = config
        self._store = store
        self._lolz = lolz
        self._bot = bot
        self._stop_event = asyncio.Event()
        self._workers: list[asyncio.Task] = []
        # Avoid hammering the same thread back-to-back across all workers.
        self._recent_viewed: deque[int] = deque(maxlen=RECENT_VIEWED_WINDOW)
        # Pool of candidate thread_ids; refreshed when empty.
        self._candidates: list[int] = []
        self._candidates_lock = asyncio.Lock()
        # Picked once per process, kept stable across requests.
        self._ua: str = random.choice(_CHROME_UAS)
        self._http: aiohttp.ClientSession | None = None
        # Consecutive auth failures — used to detect cookie expiry.
        self._auth_fail_streak = 0
        self._views_total = 0

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
        # Reset the auth-fail counter when the user explicitly re-enables
        # the viewer — they probably just rotated the cookies.
        if value:
            self._auth_fail_streak = 0

    # ---------------------------------------------------------------- lifecycle

    def start(self) -> None:
        if self._workers and any(not t.done() for t in self._workers):
            return
        self._stop_event.clear()
        self._workers = [
            asyncio.create_task(self._loop(i), name=f"viewer-{i}")
            for i in range(CONCURRENCY)
        ]

    async def stop(self) -> None:
        self._stop_event.set()
        for t in self._workers:
            try:
                await asyncio.wait_for(t, timeout=5)
            except TimeoutError:
                t.cancel()
            except asyncio.CancelledError:
                pass
        self._workers = []
        if self._http and not self._http.closed:
            await self._http.close()
        self._http = None

    # ---------------------------------------------------------------- loop

    async def _loop(self, worker_id: int) -> None:
        # Stagger workers slightly so they don't fire in lock-step.
        if worker_id > 0:
            await self._sleep_or_stop(random.uniform(0.5, 2.5) * worker_id)
        while not self._stop_event.is_set():
            try:
                await self._tick(worker_id)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("viewer[%d] iteration failed", worker_id)
                await self._sleep_or_stop(15)

    async def _tick(self, worker_id: int) -> None:
        if not self.configured:
            await self._sleep_or_stop(60)
            return
        if not await self.is_enabled():
            await self._sleep_or_stop(10)
            return

        thread_id = await self._next_thread_id()
        if not thread_id:
            # Empty pool — give the candidate-refill a moment and retry.
            await self._sleep_or_stop(random.uniform(3.0, 8.0))
            return

        await self._view_thread(thread_id, worker_id)
        self._recent_viewed.append(thread_id)
        self._views_total += 1
        if self._views_total % 25 == 0:
            log.info("viewer: %d threads viewed so far", self._views_total)

        # Per-thread dwell time. Exponential is heavy-tailed, which matches
        # how real readers spend time on threads — most are quick, a few
        # are long. Clamped so we don't sit on one thread for hours.
        if random.random() < QUICK_BOUNCE_PROB:
            dwell = random.uniform(0.5, 2.0)
        else:
            dwell = max(DWELL_MIN, min(DWELL_MAX, random.expovariate(1.0 / DWELL_MEAN)))

        # Halfway through the dwell, sometimes load page 2 (scroll-down).
        if random.random() < PAGE_TWO_PROB and dwell > 3.0:
            await self._sleep_or_stop(dwell * 0.4)
            await self._view_thread(thread_id, worker_id, page=2)
            await self._sleep_or_stop(dwell * 0.6)
        else:
            await self._sleep_or_stop(dwell)

    # ---------------------------------------------------------------- candidates

    async def _next_thread_id(self) -> int | None:
        async with self._candidates_lock:
            if not self._candidates:
                try:
                    threads = await self._lolz.list_threads(
                        self._config.lolz_offtop_forum_id,
                        limit=LIST_REFRESH_LIMIT,
                        order="post_date",
                        direction="desc",
                        include_sticky=False,
                    )
                except Exception as e:  # noqa: BLE001
                    log.warning("viewer: list_threads failed: %s", e)
                    return None
                ids = [t.thread_id for t in threads if t.thread_id]
                if not ids:
                    return None
                random.shuffle(ids)
                self._candidates = ids
                log.debug("viewer: refreshed candidate pool, %d threads", len(ids))

            # Pop until we find one not in the recent window.
            while self._candidates:
                tid = self._candidates.pop()
                if tid not in self._recent_viewed:
                    return tid
            return None

    # ---------------------------------------------------------------- HTTP

    async def _ensure_http(self) -> aiohttp.ClientSession:
        if self._http is None or self._http.closed:
            jar = aiohttp.CookieJar(unsafe=False)
            cookies: dict[str, str] = {
                "xf_user": self._config.lolz_xf_user_cookie,
                "xf_session": self._config.lolz_xf_session_cookie,
            }
            if self._config.lolz_xf_csrf_cookie:
                cookies["xf_csrf"] = self._config.lolz_xf_csrf_cookie
            jar.update_cookies(cookies, response_url=URL(self._config.lolz_web_base))
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

    async def _view_thread(
        self, thread_id: int, worker_id: int, *, page: int = 1
    ) -> None:
        http = await self._ensure_http()
        base = f"{self._config.lolz_web_base}/threads/{thread_id}/"
        url = base if page == 1 else f"{base}page-{page}"
        forum_url = (
            f"{self._config.lolz_web_base}/forums/"
            f"{self._config.lolz_offtop_forum_id}/"
        )
        # Page 1 referer is the forum listing (clicked from list); page 2
        # referer is page 1 (in-thread navigation). This matches what a
        # browser sends.
        referer = forum_url if page == 1 else base
        try:
            async with http.get(
                url,
                headers={"Referer": referer},
                allow_redirects=True,
            ) as resp:
                # Drain body to avoid keep-alive starvation. We don't parse
                # — this is a presence ping, not a scrape.
                await resp.read()
                if resp.status in (401, 403):
                    self._auth_fail_streak += 1
                    log.warning(
                        "viewer[%d]: GET %s -> %s (auth fail %d/%d)",
                        worker_id, url, resp.status,
                        self._auth_fail_streak, COOKIE_FAIL_THRESHOLD,
                    )
                    if self._auth_fail_streak >= COOKIE_FAIL_THRESHOLD:
                        await self._handle_cookie_expiry()
                    return
                if resp.status >= 400:
                    log.warning(
                        "viewer[%d]: GET %s -> %s",
                        worker_id, url, resp.status,
                    )
                    return
                # Success: clear the streak.
                self._auth_fail_streak = 0
                log.info(
                    "viewer[%d]: viewed thread %s%s (%s)",
                    worker_id,
                    thread_id,
                    "" if page == 1 else f" p{page}",
                    resp.status,
                )
        except (aiohttp.ClientError, TimeoutError) as e:
            log.warning("viewer[%d]: GET %s failed: %s", worker_id, url, e)
            await self._sleep_or_stop(random.uniform(5.0, 12.0))

    async def _handle_cookie_expiry(self) -> None:
        """Disable the viewer and notify the owner that cookies expired."""
        await self.set_enabled(False)
        log.error("viewer: cookies appear to be expired; disabled.")
        if self._bot is None:
            return
        try:
            await self._bot.send_message(
                self._config.telegram_owner_id,
                "❗ Куки lolz протухли — просмотр сам себя выключил. "
                "Обнови <code>LOLZ_XF_USER_COOKIE</code>, "
                "<code>LOLZ_XF_SESSION_COOKIE</code>, "
                "<code>LOLZ_XF_CSRF_COOKIE</code> в Render env "
                "и снова жми «👀 Просмотр».",
                parse_mode="HTML",
            )
        except TelegramBadRequest as e:
            log.warning("viewer: failed to notify owner about cookie expiry: %s", e)

    # ---------------------------------------------------------------- helpers

    async def _sleep_or_stop(self, seconds: float) -> None:
        if seconds <= 0:
            return
        try:
            await asyncio.wait_for(self._stop_event.wait(), timeout=seconds)
        except TimeoutError:
            pass
