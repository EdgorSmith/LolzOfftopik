"""Helpers to extract media URLs and a clean text rendition from a forum post."""

from __future__ import annotations

import html
import json
import os
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
    creator_user_id: int
    permalink: str
    first_post_id: int
    first_post_body: str
    first_post_body_html: str
    first_post_body_plain: str
    like_count: int
    is_liked: bool
    can_like: bool
    can_reply: bool


_VIDEO_EXT = (".mp4", ".webm", ".mov")
_SMILIE_CLASS_HINTS = ("mcesmilie", "smilie", "smiley", "emoji")
_FORUM_BASE = "https://lolz.live/"


def _is_smilie(img_class: str | None) -> bool:
    if not img_class:
        return False
    cl = img_class.lower()
    return any(hint in cl for hint in _SMILIE_CLASS_HINTS)


def _is_real_attachment(img) -> bool:
    """A real user-uploaded image inserted as [IMG]...[/IMG] gets class 'bbCodeImage'."""
    cl = (img.attributes.get("class") or "").lower()
    return "bbcodeimage" in cl


def _normalize_image_url(src: str) -> str | None:
    """Convert relative / proxy URLs into something Telegram can fetch."""
    from urllib.parse import parse_qs, unquote, urlparse

    src = (src or "").strip()
    if not src:
        return None
    src = src.replace("&amp;", "&")
    # XenForo proxy: proxy.php?image=<urlencoded>&hash=...
    if src.startswith("proxy.php") or "/proxy.php?" in src:
        try:
            qs = parse_qs(urlparse(src).query)
            inner = (qs.get("image") or [""])[0]
            if inner:
                return unquote(inner)
        except (ValueError, KeyError):
            pass
        # Couldn't decode — at least return the absolute forum URL.
        if src.startswith("proxy.php"):
            return _FORUM_BASE + src
        return src
    if src.startswith("//"):
        return "https:" + src
    if src.startswith("/"):
        return _FORUM_BASE.rstrip("/") + src
    if not src.startswith(("http://", "https://", "data:")):
        return _FORUM_BASE + src
    return src


def extract_media(post_body_html: str) -> ThreadMedia:
    """Pull image and video URLs out of the rendered HTML body of a post.

    Filters out forum smileys/emoji (class contains 'mceSmilie', 'smilie', etc.).
    """
    media = ThreadMedia()
    if not post_body_html:
        return media
    tree = HTMLParser(post_body_html)

    for img in tree.css("img"):
        cl = img.attributes.get("class") or ""
        if _is_smilie(cl):
            continue
        # Skip user avatars / decorative icons.
        if not _is_real_attachment(img):
            # Allow images that look like uploaded files even without a class hint.
            src_check = (img.attributes.get("data-url") or img.attributes.get("src") or "").lower()
            if "lztcdn.com/files/" not in src_check:
                continue
        raw_src = img.attributes.get("data-url") or img.attributes.get("src")
        src = _normalize_image_url(raw_src or "")
        if not src:
            continue
        if src.lower().endswith(_VIDEO_EXT):
            if src not in media.videos:
                media.videos.append(src)
        else:
            if src not in media.photos:
                media.photos.append(src)

    for video in tree.css("video"):
        src = _normalize_image_url(video.attributes.get("src") or "")
        if src and src not in media.videos:
            media.videos.append(src)
        for source in video.css("source"):
            s = _normalize_image_url(source.attributes.get("src") or "")
            if s and s not in media.videos:
                media.videos.append(s)

    return media


def _replace_smileys_with_shortcodes(post_body_html: str) -> str:
    """Replace <img class="mceSmilie" alt=":foo:"> with the alt text so it survives strip-tags."""

    def repl(match: re.Match[str]) -> str:
        tag = match.group(0)
        cl_match = re.search(r'class="([^"]*)"', tag)
        if not cl_match or not _is_smilie(cl_match.group(1)):
            return tag
        alt_match = re.search(r'alt="([^"]*)"', tag)
        if alt_match and alt_match.group(1).strip():
            return alt_match.group(1)
        title_match = re.search(r'title="([^"]*)"', tag)
        if title_match and title_match.group(1).strip():
            return title_match.group(1)
        return ""

    return re.sub(r"<img[^>]*>", repl, post_body_html)


def render_text_for_telegram(post_body_html: str, max_len: int = 3500) -> str:
    """Best-effort plain-text render of a post body, with line breaks preserved."""
    if not post_body_html:
        return ""
    # Replace smiley images with their shortcodes so they survive stripping.
    s = _replace_smileys_with_shortcodes(post_body_html)
    # Normalize <br> and block-ish closers to newlines before stripping tags.
    s = re.sub(r"(?i)<br\s*/?>", "\n", s)
    s = re.sub(r"(?i)</p>", "\n\n", s)
    s = re.sub(r"(?i)</div>", "\n", s)
    tree = HTMLParser(s)
    text = tree.text(separator="").strip()
    text = html.unescape(text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    if len(text) > max_len:
        text = text[: max_len - 1].rstrip() + "…"
    return text


# ---- optional Telegram custom-emoji mapping --------------------------------------
#
# When `EMOJI_MAP` env var is set to a JSON like {":bush:": "5444444444444444444"},
# every occurrence of the shortcode in the rendered text is wrapped with a marker
# that bot/cards.py converts into a MessageEntity(custom_emoji_id=...). Since we
# render via parse_mode=HTML and Telegram does not have an HTML tag for custom
# emoji, we use <tg-emoji emoji-id="..."> which IS supported by Telegram Bot API.


def _load_emoji_map() -> dict[str, str]:
    raw = os.environ.get("EMOJI_MAP", "").strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): str(v) for k, v in data.items()}


_EMOJI_MAP_CACHE: dict[str, str] | None = None


def emoji_map() -> dict[str, str]:
    global _EMOJI_MAP_CACHE
    if _EMOJI_MAP_CACHE is None:
        _EMOJI_MAP_CACHE = _load_emoji_map()
    return _EMOJI_MAP_CACHE


_FALLBACK_EMOJI = "🙂"


def apply_emoji_map_to_escaped_html(escaped_text: str) -> str:
    """After hd.quote / html.escape, swap shortcodes for Telegram <tg-emoji> tags.

    Operates on already-escaped text so the resulting HTML is valid alongside other
    escaped content. The <tg-emoji> tags are intentionally NOT escaped — Telegram parses
    them as entities.
    """
    mapping = emoji_map()
    if not mapping:
        return escaped_text
    for shortcode, emoji_id in mapping.items():
        if not shortcode or shortcode not in escaped_text:
            continue
        escaped_text = escaped_text.replace(
            shortcode,
            f'<tg-emoji emoji-id="{html.escape(emoji_id, quote=True)}">{_FALLBACK_EMOJI}</tg-emoji>',
        )
    return escaped_text


def parse_thread(raw: dict) -> Thread:
    fp = raw.get("first_post") or {}
    return Thread(
        thread_id=int(raw["thread_id"]),
        title=str(raw.get("thread_title", "")).strip(),
        creator_username=str(raw.get("creator_username", "")),
        creator_user_id=int(raw.get("creator_user_id", 0) or 0),
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
