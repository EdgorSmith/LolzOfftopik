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
* Single sequential reader (one "tab"). Multi-tab parallelism is the
  obvious automation pattern when paired with constant traffic — a real
  forum scroller reads one thread at a time.
* Small variable "click delay" between threads (≤ 5 s) instead of
  stitching threads back-to-back. Real users hesitate, scan the title
  list, hover a bit before clicking.
* Occasional second/third-page loads on the same thread to mimic
  scroll-down on long threads.
* Occasional return-to-forum-index hit (``/forums/8/page-N``) between
  threads to mimic scrolling the listing.
* Browser-shaped HTTP fingerprint: stable Chrome UA, modern
  Sec-Fetch-* / Sec-CH-UA / Priority / Accept-Encoding (br, zstd).
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
DWELL_MEAN = 9.0           # mean of exponential dwell-time distribution
DWELL_MIN = 2.5            # short visits ("opened, glanced, closed")
DWELL_MAX = 45.0           # long visits ("got hooked, read it fully")
QUICK_BOUNCE_PROB = 0.15   # chance a thread is bounced in <2.5s
QUICK_BOUNCE_MIN = 0.8     # "misclicked / not interesting" floor
QUICK_BOUNCE_MAX = 2.4     # ≤ 2.4s reads as a clear bounce
PAGE_TWO_PROB = 0.22       # chance we load /page-2 mid-dwell
PAGE_THREE_PROB = 0.10     # chance we also load /page-3 (only on longer reads)
FORUM_BROWSE_PROB = 0.18   # chance we hit /forums/8/page-N between threads
INTER_THREAD_MIN = 0.4     # min "click-next" gap (s)
INTER_THREAD_MAX = 4.8     # max "click-next" gap (s) — keep ≤ 5s per spec
LIST_REFRESH_LIMIT = 100   # threads pulled per listing pass
RECENT_VIEWED_WINDOW = 30  # don't repeat a thread within this many views
COOKIE_FAIL_THRESHOLD = 3  # consecutive 401/403 → disable + notify


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
        # The most recently viewed thread URL — used as Referer for the
        # *next* thread navigation, so the request chain matches what a
        # browser actually sends when you click forward through a forum.
        self._last_thread_url: str | None = None
        # Cycling through forum-index pages so the periodic "scroll" hits
        # different parts of the listing.
        self._forum_browse_page: int = 1

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
        # Single-reader model. Concurrency-N looked more human in code
        # review ("opens N tabs") but in practice on a forum a real user
        # scrolls one thread at a time; parallel HTML hits to /threads/...
        # against the same XenForo session is more anomalous, not less.
        self._workers = [asyncio.create_task(self._loop(0), name="viewer-0")]

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
        while not self._stop_event.is_set():
            try:
                await self._tick(worker_id)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("viewer[%d] iteration failed", worker_id)
                # Backoff on unknown failure but stay within human range.
                await self._sleep_or_stop(random.uniform(2.0, 5.0))

    async def _tick(self, worker_id: int) -> None:
        if not self.configured:
            await self._sleep_or_stop(60)
            return
        if not await self.is_enabled():
            await self._sleep_or_stop(5)
            return

        thread_id = await self._next_thread_id()
        if not thread_id:
            # Empty pool — give the candidate-refill a moment and retry.
            # Capped at 5s per spec.
            await self._sleep_or_stop(random.uniform(3.0, 5.0))
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
            dwell = random.uniform(QUICK_BOUNCE_MIN, QUICK_BOUNCE_MAX)
        else:
            dwell = max(
                DWELL_MIN,
                min(DWELL_MAX, random.expovariate(1.0 / DWELL_MEAN)),
            )

        # Page 2/3 "scroll-down". Only triggers on longer dwells where it
        # makes sense for a human to scroll past the first page.
        if random.random() < PAGE_TWO_PROB and dwell > 4.0:
            # Slice the dwell: read p1, then p2, then optionally p3.
            p1 = dwell * random.uniform(0.30, 0.45)
            await self._sleep_or_stop(p1)
            await self._view_thread(thread_id, worker_id, page=2)
            if dwell > 12.0 and random.random() < PAGE_THREE_PROB:
                p2 = dwell * random.uniform(0.20, 0.30)
                await self._sleep_or_stop(p2)
                await self._view_thread(thread_id, worker_id, page=3)
                await self._sleep_or_stop(max(0.0, dwell - p1 - p2))
            else:
                await self._sleep_or_stop(max(0.0, dwell - p1))
        else:
            await self._sleep_or_stop(dwell)

        # Sometimes "go back to the forum" before opening the next thread —
        # mimics the natural click → back → scroll → click loop. The forum-
        # index hit also rotates the candidate pool indirectly (next refill
        # comes from a fresh listing).
        if random.random() < FORUM_BROWSE_PROB:
            await self._view_forum_index(worker_id)

        # "Click-next" gap: short variable pause before the next thread.
        # Capped at 5s — the user explicitly said no long breaks.
        await self._sleep_or_stop(
            random.uniform(INTER_THREAD_MIN, INTER_THREAD_MAX)
        )

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

    def _sec_ch_ua(self) -> str:
        # Match the major Chrome version baked into the picked UA so the
        # Sec-CH-UA hint is internally consistent. Real Chrome sends a
        # 3-brand string; ours mirrors that.
        ua = self._ua
        major = "122"
        for v in ("120", "121", "122", "123"):
            if f"Chrome/{v}" in ua:
                major = v
                break
        return (
            f'"Not(A:Brand";v="24", "Chromium";v="{major}", '
            f'"Google Chrome";v="{major}"'
        )

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
                    # aiohttp doesn't transparently decode br/zstd, but
                    # advertising them matches what Chrome actually sends.
                    "Accept-Encoding": "gzip, deflate, br, zstd",
                    "Cache-Control": "no-cache",
                    "Pragma": "no-cache",
                    "Sec-Ch-Ua": self._sec_ch_ua(),
                    "Sec-Ch-Ua-Mobile": "?0",
                    "Sec-Ch-Ua-Platform": '"Windows"',
                    "Sec-Fetch-Dest": "document",
                    "Sec-Fetch-Mode": "navigate",
                    "Sec-Fetch-Site": "same-origin",
                    "Sec-Fetch-User": "?1",
                    "Upgrade-Insecure-Requests": "1",
                    "Priority": "u=0, i",
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
        # Referer chain reflects the user's actual click path:
        #   - Page 2/3 of *this* thread → page 1 of *this* thread
        #   - Page 1 of a thread → whatever we last visited (forum index
        #     or the previous thread; matches "clicked back, then a new
        #     thread title")
        if page > 1:
            referer = base
        elif self._last_thread_url:
            referer = self._last_thread_url
        else:
            referer = forum_url
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
                if resp.status >= 500:
                    log.warning(
                        "viewer[%d]: GET %s -> %s (server side)",
                        worker_id, url, resp.status,
                    )
                    # Short backoff on 5xx; capped at 5s per spec.
                    await self._sleep_or_stop(random.uniform(2.0, 5.0))
                    return
                if resp.status >= 400:
                    log.warning(
                        "viewer[%d]: GET %s -> %s",
                        worker_id, url, resp.status,
                    )
                    return
                # Success: clear the streak and remember this URL as the
                # next request's Referer.
                self._auth_fail_streak = 0
                self._last_thread_url = url
                log.info(
                    "viewer[%d]: viewed thread %s%s (%s)",
                    worker_id,
                    thread_id,
                    "" if page == 1 else f" p{page}",
                    resp.status,
                )
        except (aiohttp.ClientError, TimeoutError) as e:
            log.warning("viewer[%d]: GET %s failed: %s", worker_id, url, e)
            # Capped backoff on transport error.
            await self._sleep_or_stop(random.uniform(2.0, 5.0))

    async def _view_forum_index(self, worker_id: int) -> None:
        """Hit ``/forums/{id}/page-N`` to mimic scrolling the listing.

        We rotate through pages 1..5 so the periodic "back to forum"
        navigation isn't always landing on the same page. Failures are
        soft — this is just texture, not a critical hit.
        """
        http = await self._ensure_http()
        page = self._forum_browse_page
        # Cycle 1 → 5 → 1
        self._forum_browse_page = (page % 5) + 1
        forum_url = (
            f"{self._config.lolz_web_base}/forums/"
            f"{self._config.lolz_offtop_forum_id}/"
        )
        url = forum_url if page == 1 else f"{forum_url}page-{page}"
        # Referer is the previous thread (just "clicked back").
        referer = self._last_thread_url or forum_url
        try:
            async with http.get(
                url,
                headers={"Referer": referer},
                allow_redirects=True,
            ) as resp:
                await resp.read()
                if resp.status >= 400:
                    log.debug(
                        "viewer[%d]: forum index %s -> %s",
                        worker_id, url, resp.status,
                    )
                    return
                self._last_thread_url = url
                log.info(
                    "viewer[%d]: browsed forum index page %d (%s)",
                    worker_id, page, resp.status,
                )
        except (aiohttp.ClientError, TimeoutError) as e:
            log.debug("viewer[%d]: forum index %s failed: %s", worker_id, url, e)

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
