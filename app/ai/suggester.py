"""Build & deliver Gemini-drafted replies under each new thread card."""

from __future__ import annotations

import asyncio
import logging
import random

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.text_decorations import html_decoration as hd

from app.ai.gemini import GeminiClient, GeminiError, GenerationParams
from app.config import Config
from app.db import Store
from app.lolz import Thread, render_text_for_telegram

log = logging.getLogger(__name__)


_SYSTEM_PROMPT = (
    "Ты помогаешь автору вести оффтоп-беседы на русскоязычном форуме lolz.live. "
    "Тебе передадут заголовок и тело темы. Сгенерируй один короткий ответ "
    "от лица автора в его стиле, чтобы он мог постить как есть. Никаких "
    "приветствий, подписей, кавычек, разметки markdown и пояснений — только "
    "сам текст ответа, как реплика в чате. 1–3 предложения, разговорный "
    "русский, можно с лёгким сарказмом — НО без оскорблений, без политики, "
    "без рекламы, без раскрытия того что ты ИИ."
)


def draft_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(text="✅ Разрешить", callback_data="aiok"),
            InlineKeyboardButton(text="🔄 Другой вариант", callback_data="aire"),
            InlineKeyboardButton(text="❌ Закрыть", callback_data="aino"),
        ]]
    )


def format_draft(thread_title: str, suggestion: str) -> str:
    title = hd.quote((thread_title or "(без заголовка)").strip())
    text = hd.quote(suggestion.strip())
    return (
        f"🤖 {hd.bold('Черновик ответа')} для темы «{title}»\n\n"
        f"{text}\n\n"
        f"<i>Ничего не отправлено. Решай: разрешить, ещё вариант или закрыть.</i>"
    )


class AISuggester:
    """Owns the Gemini client and the orchestration of draft messages."""

    def __init__(
        self,
        config: Config,
        store: Store,
        bot: Bot,
        client: GeminiClient | None = None,
    ) -> None:
        self._config = config
        self._store = store
        self._bot = bot
        self._client = client
        self._inflight: set[int] = set()  # thread_ids currently being generated

    @property
    def configured(self) -> bool:
        return bool(self._client and self._client.configured)

    async def is_enabled(self) -> bool:
        if not self.configured:
            return False
        v = await self._store.get_setting("ai_reply_enabled")
        if v is None:
            return self._config.ai_reply_enabled_default
        return v == "1"

    async def set_enabled(self, value: bool) -> None:
        await self._store.set_setting("ai_reply_enabled", "1" if value else "0")

    async def close(self) -> None:
        if self._client:
            await self._client.close()

    # ---- main entry: schedule a draft after a card was sent ------------------

    def schedule(
        self,
        chat_id: int,
        card_message_id: int,
        thread: Thread,
    ) -> asyncio.Task | None:
        """Kick off a background draft generation.

        Returns the spawned task (or None if AI is disabled).
        """
        if not self.configured:
            return None
        if thread.thread_id in self._inflight:
            return None
        self._inflight.add(thread.thread_id)
        return asyncio.create_task(
            self._run_and_send(chat_id, card_message_id, thread),
            name=f"ai-draft-{thread.thread_id}",
        )

    async def _run_and_send(
        self, chat_id: int, card_message_id: int, thread: Thread
    ) -> None:
        try:
            if not await self.is_enabled():
                return
            try:
                draft = await self._generate(thread)
            except GeminiError as e:
                log.warning("ai draft failed for %s: %s", thread.thread_id, e)
                return
            if not draft:
                return
            try:
                msg = await self._bot.send_message(
                    chat_id,
                    format_draft(thread.title, draft),
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                    reply_markup=draft_kb(),
                    reply_to_message_id=card_message_id,
                    allow_sending_without_reply=True,
                )
            except TelegramBadRequest as e:
                log.warning("send draft failed for %s: %s", thread.thread_id, e)
                return
            await self._store.add_ai_suggestion(
                chat_id=chat_id,
                message_id=msg.message_id,
                thread_id=thread.thread_id,
                suggestion_text=draft,
                card_chat_id=chat_id,
                card_message_id=card_message_id,
            )
        finally:
            self._inflight.discard(thread.thread_id)

    # ---- regeneration (called from the 'другой вариант' button) -------------

    async def regenerate_for_thread(self, thread_id: int) -> str:
        """Re-generate a draft for an existing thread_id (no card context needed)."""
        if not self.configured:
            raise GeminiError(0, "Gemini API key not configured")
        # Best-effort: we keep enough context in the existing draft to ask for
        # a different angle. Caller passes the original prompt indirectly via
        # `_generate_from_text`. For simplicity we just regenerate based on
        # what we know — caller will overwrite the suggestion text.
        raise NotImplementedError  # unused, see regenerate()

    async def regenerate(self, thread_title: str, thread_body: str) -> str:
        return await self._generate_from_text(
            thread_title,
            thread_body,
            params=GenerationParams(temperature=1.05, top_p=0.95, max_output_tokens=256),
        )

    # ---- internals -----------------------------------------------------------

    async def _generate(self, thread: Thread) -> str:
        body_text = (
            thread.first_post_body_plain
            or render_text_for_telegram(thread.first_post_body_html, max_len=1200)
            or ""
        ).strip()
        return await self._generate_from_text(thread.title, body_text)

    async def _generate_from_text(
        self,
        title: str,
        body: str,
        *,
        params: GenerationParams | None = None,
    ) -> str:
        examples = await self._store.sample_my_replies(limit=8)
        random.shuffle(examples)
        prompt = _build_prompt(title, body, examples)
        assert self._client is not None  # configured implies non-None
        text = await self._client.generate(
            prompt,
            system_instruction=_SYSTEM_PROMPT,
            params=params,
        )
        return _postprocess(text)


def _build_prompt(title: str, body: str, examples: list[str]) -> str:
    title = (title or "(без заголовка)").strip()
    body = (body or "").strip()
    if len(body) > 1500:
        body = body[:1500].rstrip() + "…"

    parts: list[str] = []
    if examples:
        parts.append("Примеры моих прошлых ответов в оффтопе (для стиля):")
        for ex in examples:
            ex = ex.strip()
            if not ex:
                continue
            parts.append(f"— {ex}")
        parts.append("")

    parts.append(f"Тема: «{title}»")
    if body:
        parts.append(f"Тело темы: {body}")
    parts.append("")
    parts.append("Сгенерируй один ответ в духе моих примеров.")
    return "\n".join(parts)


def _postprocess(text: str) -> str:
    """Trim quoting, leading bullets, code fences, etc."""
    s = (text or "").strip()
    # Common Gemini boilerplate: backticks / triple quotes / leading "Ответ:".
    s = s.strip("`")
    if s.startswith(('"', "«")) and s.endswith(('"', "»")):
        s = s[1:-1].strip()
    for prefix in ("Ответ:", "Reply:", "Response:"):
        if s.lower().startswith(prefix.lower()):
            s = s[len(prefix):].lstrip(" :—-")
    # Drop trailing "—Я"-style sign-offs.
    return s.strip()
