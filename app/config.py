"""Runtime configuration loaded from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


def _required(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"Environment variable {name} is required")
    return value


@dataclass(frozen=True)
class Config:
    telegram_bot_token: str
    telegram_owner_id: int
    lolz_api_token: str
    lolz_api_base: str
    lolz_offtop_forum_id: int
    lolz_offtop_url: str
    poll_interval_seconds: int
    notif_poll_interval_seconds: int
    http_port: int
    db_path: str
    public_url: str  # e.g. "https://lolzofftopik.onrender.com" — used to host attachments for replies
    # Browser-fingerprinted "view" mode — hits HTML thread pages on lolz.live
    # with the user's session cookies, so XenForo's "members currently
    # viewing this thread" widget shows them as present. Disabled if the
    # required cookies are empty.
    lolz_web_base: str
    lolz_xf_user_cookie: str
    lolz_xf_session_cookie: str
    lolz_xf_csrf_cookie: str

    @classmethod
    def from_env(cls) -> Config:
        public_url = os.environ.get("PUBLIC_URL", "").rstrip("/")
        if not public_url:
            host = os.environ.get("RENDER_EXTERNAL_HOSTNAME", "")
            if host:
                public_url = f"https://{host}"
        return cls(
            telegram_bot_token=_required("TELEGRAM_BOT_TOKEN"),
            telegram_owner_id=int(_required("TELEGRAM_OWNER_ID")),
            lolz_api_token=_required("LOLZ_API_TOKEN"),
            lolz_api_base=os.environ.get("LOLZ_API_BASE", "https://prod-api.lolz.live"),
            lolz_offtop_forum_id=int(os.environ.get("LOLZ_OFFTOP_FORUM_ID", "8")),
            lolz_offtop_url=os.environ.get("LOLZ_OFFTOP_URL", "https://lolz.live/forums/8/"),
            poll_interval_seconds=int(os.environ.get("POLL_INTERVAL_SECONDS", "25")),
            notif_poll_interval_seconds=int(os.environ.get("NOTIF_POLL_INTERVAL_SECONDS", "60")),
            http_port=int(os.environ.get("PORT", "10000")),
            db_path=os.environ.get("DB_PATH", "data/state.sqlite3"),
            public_url=public_url,
            lolz_web_base=os.environ.get("LOLZ_WEB_BASE", "https://lolz.live").rstrip("/"),
            lolz_xf_user_cookie=os.environ.get("LOLZ_XF_USER_COOKIE", "").strip(),
            lolz_xf_session_cookie=os.environ.get("LOLZ_XF_SESSION_COOKIE", "").strip(),
            lolz_xf_csrf_cookie=os.environ.get("LOLZ_XF_CSRF_COOKIE", "").strip(),
        )
