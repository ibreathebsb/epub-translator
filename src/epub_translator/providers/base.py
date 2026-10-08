"""What the translator needs from a model provider."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Protocol


@dataclass
class Completion:
    text: str
    truncated: bool = False  # the model hit its output limit
    finish_reason: str | None = None


class Cancelled(Exception):
    """The run was cancelled while a request was in flight."""


class ProviderError(Exception):
    """A request failed.

    `kind` says what the caller can do about it:
      "retry"    transient (rate limit, overload, network): try again later
      "request"  this one request was refused (too long, blocked): give up on it
      "fatal"    nothing will work until the configuration is fixed (key, model, quota)
    """

    def __init__(self, message: str, *, kind: str = "fatal", retry_after: float | None = None):
        super().__init__(message)
        self.kind = kind
        self.retry_after = retry_after


def kind_for_status(status: int | None) -> str:
    if status in (401, 402, 403, 404):
        return "fatal"
    if status is None or status in (408, 409, 425, 429) or status >= 500:
        return "retry"
    return "request"


class Provider(Protocol):
    model: str

    def complete(
        self, system: str, user: str, *, cancel: threading.Event | None = None
    ) -> Completion: ...
