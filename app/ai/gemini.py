"""Tiny async client for the Gemini Generative Language REST API.

We don't pull in `google-generativeai`/`google-genai` SDKs — a small JSON POST
over `aiohttp` is enough and avoids extra deps + auth complexity.

Docs: https://ai.google.dev/api/generate-content
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import aiohttp

log = logging.getLogger(__name__)

_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/models"


class GeminiError(RuntimeError):
    """Raised when the Gemini API returns a non-2xx response or a malformed body."""

    def __init__(self, status: int, body: str) -> None:
        super().__init__(f"gemini api error {status}: {body[:300]}")
        self.status = status
        self.body = body


@dataclass(frozen=True)
class GenerationParams:
    temperature: float = 0.95
    top_p: float = 0.95
    top_k: int = 40
    max_output_tokens: int = 256


class GeminiClient:
    """Thin async wrapper around `:generateContent`.

    A single instance is cheap and keeps an aiohttp session alive across calls.
    """

    def __init__(self, api_key: str, model: str = "gemini-2.0-flash") -> None:
        self._api_key = api_key
        self._model = model
        self._session: aiohttp.ClientSession | None = None

    async def __aenter__(self) -> GeminiClient:
        await self._ensure_session()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=30),
                headers={"User-Agent": "LolzOfftopik/0.1 (+gemini-client)"},
            )
        return self._session

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None

    @property
    def configured(self) -> bool:
        return bool(self._api_key)

    async def generate(
        self,
        prompt: str,
        *,
        system_instruction: str | None = None,
        params: GenerationParams | None = None,
    ) -> str:
        """Send a single-turn prompt and return the model's text reply."""
        if not self._api_key:
            raise GeminiError(0, "GEMINI_API_KEY is not configured")

        params = params or GenerationParams()
        payload: dict = {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": params.temperature,
                "topP": params.top_p,
                "topK": params.top_k,
                "maxOutputTokens": params.max_output_tokens,
            },
        }
        if system_instruction:
            payload["systemInstruction"] = {
                "role": "user",
                "parts": [{"text": system_instruction}],
            }

        url = f"{_BASE_URL}/{self._model}:generateContent"
        session = await self._ensure_session()
        async with session.post(url, params={"key": self._api_key}, json=payload) as resp:
            text = await resp.text()
            if resp.status >= 400:
                raise GeminiError(resp.status, text)
            try:
                data = await _safe_json(text)
            except ValueError as exc:
                raise GeminiError(0, str(exc)) from exc

        return _extract_text(data)


async def _safe_json(text: str) -> dict:
    import json

    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON: {text[:200]}") from exc


def _extract_text(data: dict) -> str:
    """Concatenate all text parts from the first candidate. Robust to schema drift."""
    candidates = data.get("candidates") or []
    if not candidates:
        # Sometimes the model is filtered with no candidate; surface promptFeedback.
        feedback = data.get("promptFeedback") or {}
        block_reason = feedback.get("blockReason") or ""
        raise GeminiError(0, f"no candidates (blockReason={block_reason!r})")
    parts = ((candidates[0].get("content") or {}).get("parts")) or []
    chunks: list[str] = []
    for p in parts:
        t = p.get("text")
        if t:
            chunks.append(str(t))
    return "".join(chunks).strip()
