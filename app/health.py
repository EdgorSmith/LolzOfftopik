"""Tiny aiohttp HTTP server with a /health endpoint for Render and cron-job.org."""

from __future__ import annotations

import logging

from aiohttp import web

log = logging.getLogger(__name__)


def build_app() -> web.Application:
    async def health(_: web.Request) -> web.Response:
        return web.json_response({"status": "ok", "service": "lolzofftopik"})

    async def root(_: web.Request) -> web.Response:
        return web.Response(text="LolzOfftopik is running. See /health.\n")

    app = web.Application()
    app.router.add_get("/health", health)
    app.router.add_get("/", root)
    return app


async def run_http_server(port: int) -> web.AppRunner:
    app = build_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=port)
    await site.start()
    log.info("HTTP /health listening on 0.0.0.0:%s", port)
    return runner
