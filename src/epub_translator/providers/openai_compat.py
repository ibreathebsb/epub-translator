"""Any service that speaks the OpenAI chat completions API."""

from __future__ import annotations

import threading

import httpx
import openai

from .base import Cancelled, Completion, ProviderError, kind_for_status


class OpenAICompatProvider:
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
        self._limit_param = "max_tokens"
        self._temperature = temperature
        self._extra = extra or None
        # Retries are handled by the translator, for every provider alike.
        self._client = openai.OpenAI(
            api_key=api_key,
            base_url=base_url,
            max_retries=0,
            timeout=timeout if timeout is not None else openai.DEFAULT_TIMEOUT,
        )

    def _kwargs(self, system: str, user: str) -> dict:
        kwargs: dict = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "stream": True,
        }
        if self._json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        if self._max_output_tokens:
            kwargs[self._limit_param] = self._max_output_tokens
        if self._temperature is not None:
            kwargs["temperature"] = self._temperature
        if self._extra:
            kwargs["extra_body"] = self._extra
        return kwargs

    def _adapt(self, message: str) -> bool:
        """Drop an option the server just refused. Compatible servers differ in what they accept."""
        message = message.lower()
        if "max_completion_tokens" in message and self._limit_param == "max_tokens":
            self._limit_param = "max_completion_tokens"
            return True
        if self._json_mode and ("response_format" in message or "json" in message):
            self._json_mode = False
            return True
        return False

    def _open(self, system: str, user: str):
        while True:
            try:
                return self._client.chat.completions.create(**self._kwargs(system, user))
            except (openai.BadRequestError, openai.UnprocessableEntityError) as exc:
                if not self._adapt(str(exc)):
                    raise

    def complete(
        self, system: str, user: str, *, cancel: threading.Event | None = None
    ) -> Completion:
        parts: list[str] = []
        finish: str | None = None
        try:
            stream = self._open(system, user)
            try:
                for chunk in stream:
                    if cancel is not None and cancel.is_set():
                        raise Cancelled()
                    if not chunk.choices:
                        continue
                    choice = chunk.choices[0]
                    if choice.delta is not None and choice.delta.content:
                        parts.append(choice.delta.content)
                    if choice.finish_reason:
                        finish = choice.finish_reason
            finally:
                stream.close()
        except openai.APIStatusError as exc:
            raise _status_error(exc) from exc
        except (openai.APIError, httpx.HTTPError) as exc:
            # Connection problems, timeouts, and streams that break midway.
            raise ProviderError(f"{type(exc).__name__}: {exc}", kind="retry") from exc

        text = "".join(parts)
        if not text and finish == "content_filter":
            raise ProviderError("内容被服务商的过滤器拦截", kind="request")
        if not text and finish is None:
            raise ProviderError("服务端没有返回任何内容", kind="retry")
        return Completion(text, truncated=finish == "length", finish_reason=finish)


def _status_error(exc: openai.APIStatusError) -> ProviderError:
    status = exc.status_code
    kind = kind_for_status(status)
    if getattr(exc, "code", None) == "insufficient_quota":
        kind = "fatal"
    retry_after = None
    try:
        retry_after = float(exc.response.headers.get("retry-after", ""))
    except ValueError:
        pass
    # The SDK hands over the "error" object of the response body when there is one.
    body = exc.body
    detail = body.get("message") if isinstance(body, dict) else None
    message = detail if isinstance(detail, str) and detail else exc.message
    return ProviderError(f"HTTP {status}: {message}", kind=kind, retry_after=retry_after)
