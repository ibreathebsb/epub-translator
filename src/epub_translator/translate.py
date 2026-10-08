"""Translate the segments of one document: ask the model, check the answer, ask again.

A document always goes to the model whole. When part of the answer is missing
or wrong, the next request carries the whole document again together with what
is already done, and asks only for the remaining segments.
"""

from __future__ import annotations

import hashlib
import json
import random
import re
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from . import prompts
from .extract import _TOKEN, _URL, Segment
from .providers.base import Cancelled, Completion, Provider, ProviderError

Notify = Callable[..., None]

MAX_ATTEMPTS = 3  # the first request plus two retries
MAX_TRIES = 6  # per request, for transient failures
_CJK = re.compile(r"[぀-ヿ㐀-䶿一-鿿가-힯]")
_LATIN = re.compile(r"[A-Za-z]")
_LOWER_WORD = re.compile(r"\b[a-z]{2,}\b")
_FENCE = re.compile(r"\A\s*```[A-Za-z]*[ \t]*\n?|\n?```\s*\Z")
_ITEM = re.compile(r'\{\s*"id"\s*:\s*"?(\d+)"?\s*,\s*"text"\s*:\s*(?=")')


@dataclass
class Issue:
    """Something the user should know about a document after the run."""

    seg: int | None  # segment id; None when it concerns the whole document
    kind: str  # placeholder | missing | untranslated | request
    detail: str = ""


@dataclass
class DocResult:
    translations: dict[int, str] = field(default_factory=dict)
    issues: list[Issue] = field(default_factory=list)
    requests: int = 0

    @property
    def clean(self) -> bool:
        """Every segment came back sound, so the result is worth keeping in the cache."""
        return all(issue.kind == "untranslated" for issue in self.issues)


def parse_response(text: str) -> dict[int, str]:
    """Read the model's answer. Tolerates code fences, chatter and truncated JSON."""
    text = _FENCE.sub("", text).strip()
    found = _items(_load(text))
    if found:
        return found
    # Broken or truncated JSON: take every item that is complete.
    decoder = json.JSONDecoder()
    for m in _ITEM.finditer(text):
        try:
            value, _ = decoder.raw_decode(text, m.end())
        except ValueError:
            continue
        if isinstance(value, str):
            found.setdefault(int(m.group(1)), value)
    return found


def _load(text: str):
    try:
        return json.loads(text)
    except ValueError:
        pass
    starts = [i for i in (text.find("{"), text.find("[")) if i >= 0]
    if starts:
        try:
            return json.JSONDecoder().raw_decode(text, min(starts))[0]
        except ValueError:
            pass
    return None


def _items(data) -> dict[int, str]:
    if isinstance(data, dict):
        lists = [v for v in data.values() if isinstance(v, list)]
        if lists:
            data = lists[0]
        else:
            data = [{"id": k, "text": v} for k, v in data.items()]
    found: dict[int, str] = {}
    if not isinstance(data, list):
        return found
    for item in data:
        if not isinstance(item, dict) or not isinstance(item.get("text"), str):
            continue
        try:
            found.setdefault(int(item.get("id")), item["text"])
        except (TypeError, ValueError):
            continue
    return found


def looks_untranslated(source: str, text: str, target: str) -> bool:
    """True when a sizeable piece of prose came back in the source language."""
    plain = _URL.sub(" ", _TOKEN.sub(" ", source))
    if target.lower().split("-")[0] in ("zh", "ja", "ko"):
        # Latin prose in, not one CJK character out. Names and titles alone do
        # not count: they are often rightly kept as they are.
        return (
            len(_LATIN.findall(plain)) >= 30
            and len(_LOWER_WORD.findall(plain)) >= 5
            and not _CJK.search(plain)
            and not _CJK.search(text)
        )
    return len(plain.split()) >= 8 and " ".join(text.split()) == " ".join(source.split())


class Translator:
    def __init__(
        self,
        provider: Provider,
        target: str,
        *,
        source_lang: str | None = None,
        book_title: str | None = None,
        cancel: threading.Event | None = None,
        sleep: Callable[[float], bool] | None = None,
    ):
        self.provider = provider
        self.target = target
        self.cancel = cancel or threading.Event()
        # Waits between tries end early, returning True, when the run is cancelled.
        self._sleep = sleep or self.cancel.wait
        self.system = prompts.system_prompt(target)
        self._context = {"target": target, "source": source_lang, "book": book_title}

    def cache_key(self, segments: Sequence[Segment]) -> str:
        """Identifies a request: same text, language, model and prompt give the same key."""
        payload = [self.system, self.target, self.provider.model, [s.source for s in segments]]
        return hashlib.sha256(json.dumps(payload, ensure_ascii=False).encode()).hexdigest()

    def translate(self, segments: Sequence[Segment], notify: Notify | None = None) -> DocResult:
        """Translate `segments`, one document. `notify` hears about retries and waits."""
        notify = notify or _quiet
        result = DocResult()
        done: dict[int, str] = {}
        answers: dict[int, str] = {}  # latest answer with broken placeholders, per segment
        faults: dict[int, str] = {}  # what was wrong with it
        wrong: dict[int, str] = {}  # the same, for the last request only
        echoes: dict[int, str] = {}  # sound answers that still read like the source
        problems: dict[int, list[str]] = {}
        pending = list(segments)
        completion = Completion("")

        for attempt in range(1, MAX_ATTEMPTS + 1):
            if attempt == 1:
                user = prompts.first_message(segments, **self._context)
            else:
                notify(
                    "retry",
                    attempt=attempt,
                    pending=len(pending),
                    reason=_why(completion, problems, pending),
                )
                user = prompts.retry_message(
                    segments, pending, done, problems, wrong, **self._context
                )
            try:
                completion = self._complete(user, notify)
            except ProviderError as exc:
                if exc.kind != "request":
                    raise
                result.issues.append(Issue(None, "request", str(exc)))
                break
            result.requests += 1

            received = parse_response(completion.text)
            problems, wrong = {}, {}
            for seg in pending:
                text = (received.get(seg.id) or "").strip()
                if not text:
                    problems[seg.id] = []
                    continue
                errors = seg.check(text)
                if errors:
                    problems[seg.id] = errors
                    answers[seg.id] = wrong[seg.id] = text
                    faults[seg.id] = errors[0]
                elif looks_untranslated(seg.source, text, self.target):
                    problems[seg.id] = ["it was returned untranslated; translate it"]
                    echoes[seg.id] = text
                else:
                    done[seg.id] = text
            pending = [seg for seg in pending if seg.id not in done]
            if not pending:
                break

        result.translations = done
        for seg in pending:
            if seg.id in answers:
                done[seg.id] = seg.salvage(answers[seg.id])
                result.issues.append(Issue(seg.id, "placeholder", faults[seg.id]))
            elif seg.id in echoes:
                done[seg.id] = echoes[seg.id]
                result.issues.append(Issue(seg.id, "untranslated"))
            elif not any(issue.kind == "request" for issue in result.issues):
                reason = "truncated" if completion.truncated else (completion.finish_reason or "")
                result.issues.append(Issue(seg.id, "missing", reason))
        return result

    def _complete(self, user: str, notify: Notify) -> Completion:
        delay = 2.0
        for attempt in range(1, MAX_TRIES + 1):
            if self.cancel.is_set():
                raise Cancelled()
            try:
                return self.provider.complete(self.system, user, cancel=self.cancel)
            except ProviderError as exc:
                if exc.kind != "retry":
                    raise
                if attempt == MAX_TRIES:
                    raise ProviderError(f"连续 {MAX_TRIES} 次请求失败：{exc}") from exc
                wait = exc.retry_after or delay * (1 + random.random() / 2)
                wait = min(wait, 120.0)
                delay = min(delay * 2, 30.0)
                notify("wait", seconds=wait, error=str(exc))
                if self._sleep(wait):
                    raise Cancelled()
        raise AssertionError("unreachable")


def _quiet(kind: str, **data) -> None:
    pass


def _why(completion: Completion, problems: dict[int, list[str]], pending: Sequence[Segment]) -> str:
    if completion.truncated:
        return "truncated"
    if all(not problems.get(seg.id) for seg in pending):
        return "missing"
    return "invalid"
