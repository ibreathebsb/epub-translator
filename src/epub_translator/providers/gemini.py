"""Google Gemini through the google-genai SDK."""

from __future__ import annotations

import threading

import httpx
from google import genai
from google.genai import errors, types

from .base import Cancelled, Completion, ProviderError, kind_for_status

# Finish reasons that mean the model refused the content rather than ran out of room.
_BLOCKED = {"SAFETY", "RECITATION", "BLOCKLIST", "PROHIBITED_CONTENT", "SPII", "LANGUAGE"}


class GeminiProvider:
    def __init__(
        self,
        *,
        model: str,
        api_key: str,
        base_url: str | None = None,
        json_mode: bool = True,
        max_output_tokens: int | None = None,
        temperature: float | None = None,
        timeout: float | None = None,
        extra: dict | None = None,
    ):
        self.model = model
        self._json_mode = json_mode
        self._max_output_tokens = max_output_tokens
        self._temperature = temperature
        self._extra = extra or {}
        http_options = types.HttpOptions(
            base_url=base_url,
            timeout=int(timeout * 1000) if timeout is not None else None,  # milliseconds
        )
        self._client = genai.Client(api_key=api_key, http_options=http_options)

    def _config(self, system: str) -> types.GenerateContentConfig:
        fields: dict = {"system_instruction": system}
        if self._json_mode:
            fields["response_mime_type"] = "application/json"
        if self._max_output_tokens:
            fields["max_output_tokens"] = self._max_output_tokens
        if self._temperature is not None:
            fields["temperature"] = self._temperature
        fields.update(self._extra)
        return types.GenerateContentConfig(**fields)

    def complete(
        self, system: str, user: str, *, cancel: threading.Event | None = None
    ) -> Completion:
        parts: list[str] = []
        finish: str | None = None
        blocked: str | None = None
        try:
            stream = self._client.models.generate_content_stream(
                model=self.model, contents=user, config=self._config(system)
            )
            for chunk in stream:
                if cancel is not None and cancel.is_set():
                    raise Cancelled()
                if chunk.prompt_feedback is not None and chunk.prompt_feedback.block_reason:
                    blocked = _name(chunk.prompt_feedback.block_reason)
                for candidate in (chunk.candidates or [])[:1]:
                    if candidate.finish_reason:
                        finish = _name(candidate.finish_reason)
                    if candidate.content is None:
                        continue
                    for part in candidate.content.parts or []:
                        if part.text and not part.thought:
                            parts.append(part.text)
        except errors.APIError as exc:
            status = exc.code if isinstance(exc.code, int) else None
            kind = kind_for_status(status)
            if "api key" in (exc.message or "").lower():
                kind = "fatal"  # Gemini answers a bad key with a plain 400
            raise ProviderError(
                f"HTTP {exc.code} {exc.status or ''}: {exc.message}", kind=kind
            ) from exc
        except httpx.HTTPError as exc:
            raise ProviderError(f"{type(exc).__name__}: {exc}", kind="retry") from exc

        text = "".join(parts)
        if not text and (blocked or finish in _BLOCKED):
            raise ProviderError(f"内容被 Gemini 的过滤器拦截（{blocked or finish}）", kind="request")
        if not text and finish is None:
            raise ProviderError("服务端没有返回任何内容", kind="retry")
        return Completion(text, truncated=finish == "MAX_TOKENS", finish_reason=finish)


def _name(value) -> str:
    return getattr(value, "name", None) or str(value)
