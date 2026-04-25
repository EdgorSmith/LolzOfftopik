"""HTTP routes that proxy a Telegram file through our public URL.

The flow:
1. User sends a photo/video/animation in a ForceReply.
2. Bot stores file_id under a random token (`Store.add_file_token`).
3. Bot inserts a public URL into the lolz post body: `<public_url>/m/<token>.<ext>`.
4. lolz / its readers fetch the URL.
5. This proxy resolves token -> file_id -> Telegram CDN URL and streams the bytes.
"""

from __future__ import annotations

import logging

import aiohttp
from aiohttp import web

from app.config import Config
from app.db import Store

log = logging.getLogger(__name__)


def add_routes(
    app: web.Application,
    config: Config,
    store: Store,
    session_factory,
) -> None:
    async def proxy_file(request: web.Request) -> web.StreamResponse:
        token = request.match_info["token"]
        # Strip optional extension: the public URL we generate looks like
        # /m/<token>.jpg so XenForo's image proxy recognises it as an image.
        if "." in token:
            token = token.split(".", 1)[0]

        record = await store.get_file_by_token(token)
        if not record:
            return web.Response(status=404, text="not found")
        file_id, mime_type = record

        # Resolve the file path from Telegram.
        async with session_factory() as session:
            async with session.get(
                f"https://api.telegram.org/bot{config.telegram_bot_token}/getFile",
                params={"file_id": file_id},
            ) as r:
                if r.status != 200:
                    return web.Response(status=502, text="getFile failed")
                payload = await r.json()
            if not payload.get("ok"):
                return web.Response(status=502, text="getFile error")
            file_path = payload["result"].get("file_path")
            if not file_path:
                return web.Response(status=502, text="no file_path")

            # Stream the actual bytes.
            tg_url = f"https://api.telegram.org/file/bot{config.telegram_bot_token}/{file_path}"
            async with session.get(tg_url) as upstream:
                if upstream.status != 200:
                    return web.Response(status=upstream.status, text="upstream error")
                resp = web.StreamResponse(
                    status=200,
                    headers={
                        "Content-Type": mime_type or upstream.headers.get("Content-Type", "application/octet-stream"),
                        "Cache-Control": "public, max-age=31536000, immutable",
                        "Access-Control-Allow-Origin": "*",
                    },
                )
                cl = upstream.headers.get("Content-Length")
                if cl:
                    resp.headers["Content-Length"] = cl
                await resp.prepare(request)
                async for chunk in upstream.content.iter_chunked(64 * 1024):
                    await resp.write(chunk)
                await resp.write_eof()
                return resp

    app.router.add_get("/m/{token}", proxy_file)


def session_factory_default():
    return aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60))
