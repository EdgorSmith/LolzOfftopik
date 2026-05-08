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
    # Optional secret-answer for /payments/transfer. Without it the
    # "💰 Перевести деньги" flow refuses to send anything.
    lolz_secret_answer: str

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
            lolz_secret_answer=os.environ.get("LOLZ_SECRET_ANSWER", "").strip(),
        )
