"""A stand-in for a model provider, so tests run the whole pipeline without a network."""

from __future__ import annotations

import json
import re
import threading

from epub_translator.providers.base import Completion

_ONLY = re.compile(r"^Translate ONLY these segments: (.+)$", re.M)


def read_request(user: str) -> tuple[dict[int, str], list[int]]:
    """The segments of the document in a request and the ids it asks for."""
    items, _ = json.JSONDecoder().raw_decode(user, user.index("[\n"))
    document = {item["id"]: item["text"] for item in items}
    only = _ONLY.search(user)
    wanted = [int(n) for n in only.group(1).split(",")] if only else list(document)
    return document, wanted


def mark(text: str) -> str:
    """The default "translation": the source with a Chinese marker in front."""
    return "译：" + text


def answer(translations: dict[int, str]) -> str:
    items = [{"id": i, "text": t} for i, t in translations.items()]
    return json.dumps({"translations": items}, ensure_ascii=False)


class FakeProvider:
    """Translates with `mark`, or answers with `script` when one is given.

    `script(document, wanted, call_number)` returns a JSON string, a
    Completion, or raises to simulate a failing request.
    """

    model = "fake-model"

    def __init__(self, script=None):
        self.script = script
        self.calls: list[tuple[str, str]] = []
        self._lock = threading.Lock()

    def complete(self, system, user, *, cancel=None):
        with self._lock:
            self.calls.append((system, user))
            number = len(self.calls)
        document, wanted = read_request(user)
        if self.script is not None:
            reply = self.script(document, wanted, number)
        else:
            reply = answer({i: mark(document[i]) for i in wanted})
        if isinstance(reply, Completion):
            return reply
        return Completion(reply, finish_reason="stop")
