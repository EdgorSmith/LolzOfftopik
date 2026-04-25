"""Helpers to extract media URLs and a clean text rendition from a forum post."""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field

from selectolax.parser import HTMLParser


@dataclass
class ThreadMedia:
    photos: list[str] = field(default_factory=list)
    videos: list[str] = field(default_factory=list)


@dataclass
class Thread:
    thread_id: int
    title: str
    creator_username: str
    permalink: str
    first_post_id: int
    first_post_body: str
    first_post_body_html: str
    first_post_body_plain: str
    like_count: int
    is_liked: bool
    can_like: bool
    can_reply: bool


_PHOTO_EXT = (".jpg", ".jpeg", ".png", ".gif", ".webp")
_VIDEO_EXT = (".mp4", ".webm", ".mov")


def extract_media(post_body_html: str) -> ThreadMedia:
    """Pull image and video URLs out of the rendered HTML body of a post."""
    media = ThreadMedia()
    if not post_body_html:
        return media
    tree = HTMLParser(post_body_html)

    for img in tree.css("img"):
        src = img.attributes.get("data-url") or img.attributes.get("src")
        if not src:
            continue
        src = src.strip()
        if src.lower().endswith(_VIDEO_EXT):
            if src not in media.videos:
                media.videos.append(src)
        elif src.lower().endswith(_PHOTO_EXT) or "lztcdn.com/files/" in src:
            if src not in media.photos:
                media.photos.append(src)

    for video in tree.css("video"):
        src = video.attributes.get("src")
        if src and src not in media.videos:
            media.videos.append(src.strip())
        for source in video.css("source"):
            s = source.attributes.get("src")
            if s and s not in media.videos:
                media.videos.append(s.strip())

    return media


def render_text_for_telegram(post_body_html: str, max_len: int = 3500) -> str:
    """Best-effort plain-text render of a post body, with line breaks preserved."""
    if not post_body_html:
        return ""
    # Normalize <br> and block-ish closers to newlines before stripping tags.
    s = re.sub(r"(?i)<br\s*/?>", "\n", post_body_html)
    s = re.sub(r"(?i)</p>", "\n\n", s)
    s = re.sub(r"(?i)</div>", "\n", s)
    tree = HTMLParser(s)
    text = tree.text(separator="").strip()
    text = html.unescape(text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    if len(text) > max_len:
        text = text[: max_len - 1].rstrip() + "…"
    return text


def parse_thread(raw: dict) -> Thread:
    fp = raw.get("first_post") or {}
    return Thread(
        thread_id=int(raw["thread_id"]),
        title=str(raw.get("thread_title", "")).strip(),
        creator_username=str(raw.get("creator_username", "")),
        permalink=str(
            ((raw.get("links") or {}).get("permalink")) or f"https://lolz.live/threads/{raw['thread_id']}/"
        ),
        first_post_id=int(fp.get("post_id", 0)) or 0,
        first_post_body=str(fp.get("post_body", "") or ""),
        first_post_body_html=str(fp.get("post_body_html", "") or ""),
        first_post_body_plain=str(fp.get("post_body_plain_text", "") or ""),
        like_count=int(fp.get("post_like_count", 0) or 0),
        is_liked=bool(fp.get("post_is_liked", False)),
        can_like=bool((fp.get("permissions") or {}).get("like", True)),
        can_reply=bool((fp.get("permissions") or {}).get("reply", True)),
    )
