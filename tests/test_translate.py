from __future__ import annotations

import threading

import pytest
from fakes import FakeProvider, answer, read_request

from epub_translator.extract import Segment
from epub_translator.providers.base import Cancelled, Completion, ProviderError
from epub_translator.translate import (
    MAX_ATTEMPTS,
    MAX_TRIES,
    Translator,
    looks_untranslated,
    parse_response,
)

LINKED = "See <x1>the note</x1> for the long explanation of this rule."


def segments() -> list[Segment]:
    return [
        Segment(1, "Chapter One", "heading"),
        Segment(2, LINKED, paired=frozenset({1}), nesting={1: 0}),
        Segment(3, "A short closing line."),
    ]


GOOD = {1: "第一章", 2: "这条规则的详细解释见<x1>注释</x1>。", 3: "简短的结尾。"}


def translator(script=None, **kwargs) -> tuple[Translator, FakeProvider]:
    provider = FakeProvider(script)
    waits: list[float] = []
    tr = Translator(provider, "zh-CN", sleep=lambda s: waits.append(s) or False, **kwargs)
    tr.waits = waits
    return tr, provider


# -- reading the answer ------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        '{"translations": [{"id": 1, "text": "甲"}, {"id": 2, "text": "乙"}]}',
        '```json\n{"translations": [{"id": 1, "text": "甲"}, {"id": 2, "text": "乙"}]}\n```',
        '[{"id": 1, "text": "甲"}, {"id": 2, "text": "乙"}]',
        '{"translations": [{"id": "1", "text": "甲"}, {"id": "2", "text": "乙"}]}',
        '{"1": "甲", "2": "乙"}',
        'Here you go:\n{"translations": [{"id": 1, "text": "甲"}, {"id": 2, "text": "乙"}]}\nDone.',
        '{"translations": [{"id": 1, "text": "甲"}, {"id": 2, "text": "乙"}, {"id": 3, "text": "丙',
    ],
)
def test_parse_response_variants(text):
    assert parse_response(text) == {1: "甲", 2: "乙"}


def test_parse_response_keeps_escapes_and_ignores_rubbish():
    assert parse_response('{"translations": [{"id": 1, "text": "他说：\\"好\\"\\n"}]}') == {
        1: '他说："好"\n'
    }
    assert parse_response("I cannot help with that.") == {}
    assert parse_response("") == {}


def test_looks_untranslated():
    prose = "The morning came slowly, and with it the long walk back to the harbour."
    assert looks_untranslated(prose, prose, "zh-CN")
    assert not looks_untranslated(prose, "清晨来得很慢。", "zh-CN")
    assert not looks_untranslated("Stanislas Hutin", "Stanislas Hutin", "zh-CN")  # too short to judge
    assert not looks_untranslated("https://example.com/a/very/long/path/that/has/letters", "x", "zh-CN")
    assert not looks_untranslated("这一段本来就是中文，不需要再翻译了。", "这一段本来就是中文，不需要再翻译了。", "zh-CN")
    names = "Rheinmetall, Krauss-Maffei Wegmann and Thyssenkrupp Marine Systems"
    assert not looks_untranslated(names, names, "zh-CN")  # names may rightly stay as they are
    assert looks_untranslated(prose, prose, "fr")
    assert not looks_untranslated(prose, "Le matin est venu lentement.", "fr")


# -- one document ------------------------------------------------------------


def test_everything_right_the_first_time():
    tr, provider = translator(lambda doc, wanted, n: answer(GOOD))
    result = tr.translate(segments())
    assert result.translations == GOOD
    assert result.clean and not result.issues
    assert result.requests == len(provider.calls) == 1
    system, user = provider.calls[0]
    assert "Simplified Chinese" in system and "JSON" in system
    assert '{"id": 1, "kind": "heading", "text": "Chapter One"}' in user
    assert read_request(user) == ({1: "Chapter One", 2: LINKED, 3: "A short closing line."}, [1, 2, 3])


def test_book_and_languages_are_named_in_the_request():
    tr, provider = translator(lambda doc, wanted, n: answer(GOOD), book_title="The Stormy Night", source_lang="en")
    tr.translate(segments())
    user = provider.calls[0][1]
    assert "Book: The Stormy Night" in user
    assert "Source language: English" in user


def test_wrong_placeholders_are_asked_for_again():
    def script(doc, wanted, n):
        if n == 1:
            return answer({**GOOD, 2: "这条规则的详细解释见注释。"})
        return answer({2: GOOD[2]})

    events = []
    tr, provider = translator(script)
    result = tr.translate(segments(), notify=lambda kind, **d: events.append((kind, d)))
    assert result.translations == GOOD and result.clean
    assert result.requests == 2
    retry = provider.calls[1][1]
    # the whole document goes along again, but only the bad segment is asked for
    assert read_request(retry) == ({1: "Chapter One", 2: LINKED, 3: "A short closing line."}, [2])
    assert "placeholder x1 is missing" in retry
    assert "这条规则的详细解释见注释。" in retry  # the answer that was wrong
    assert '{"id": 1, "text": "第一章"}' in retry  # what is already done
    assert events == [("retry", {"attempt": 2, "pending": 1, "reason": "invalid"})]


def test_truncated_answer_is_continued():
    def script(doc, wanted, n):
        if n == 1:
            cut = '{"translations": [{"id": 1, "text": "第一章"}, {"id": 2, "text": "这条规则的详'
            return Completion(cut, truncated=True, finish_reason="length")
        return answer({i: GOOD[i] for i in wanted})

    events = []
    tr, provider = translator(script)
    result = tr.translate(segments(), notify=lambda kind, **d: events.append((kind, d)))
    assert result.translations == GOOD and result.clean
    assert read_request(provider.calls[1][1])[1] == [2, 3]
    assert events == [("retry", {"attempt": 2, "pending": 2, "reason": "truncated"})]


def test_placeholders_never_fixed_are_salvaged():
    tr, provider = translator(lambda doc, wanted, n: answer({**GOOD, 2: "见<x1>注释。"}))
    result = tr.translate(segments())
    assert len(provider.calls) == MAX_ATTEMPTS
    assert result.translations == {1: "第一章", 2: "见注释。", 3: "简短的结尾。"}
    assert not result.clean
    assert [(i.seg, i.kind, i.detail) for i in result.issues] == [
        (2, "placeholder", "<x1> is never closed")
    ]


def test_segment_never_returned_keeps_its_source():
    tr, provider = translator(lambda doc, wanted, n: answer({1: "第一章", 2: GOOD[2]}))
    result = tr.translate(segments())
    assert len(provider.calls) == MAX_ATTEMPTS
    assert result.translations == {1: "第一章", 2: GOOD[2]}
    assert not result.clean
    assert [(i.seg, i.kind) for i in result.issues] == [(3, "missing")]


def test_always_truncated_is_reported_as_such():
    cut = Completion('{"translations": [{"id": 1, "text": "第一章"}, {"id": 2, "te', truncated=True)
    tr, _ = translator(lambda doc, wanted, n: cut if 1 in wanted else Completion("", truncated=True))
    result = tr.translate(segments())
    assert result.translations == {1: "第一章"}
    assert [(i.seg, i.kind, i.detail) for i in result.issues] == [
        (2, "missing", "truncated"), (3, "missing", "truncated"),
    ]


def test_untranslated_prose_is_retried_then_accepted_with_a_note():
    tr, provider = translator(lambda doc, wanted, n: answer({**GOOD, 2: LINKED}))
    result = tr.translate(segments())
    assert len(provider.calls) == MAX_ATTEMPTS
    assert "returned untranslated" in provider.calls[1][1]
    assert result.translations[2] == LINKED
    assert [(i.seg, i.kind) for i in result.issues] == [(2, "untranslated")]
    assert result.clean  # nothing is broken, so it is worth caching


def test_empty_translation_counts_as_missing():
    def script(doc, wanted, n):
        return answer({**GOOD, 3: "  "}) if n == 1 else answer({3: GOOD[3]})

    tr, provider = translator(script)
    result = tr.translate(segments())
    assert result.translations == GOOD and len(provider.calls) == 2


# -- failing requests --------------------------------------------------------


def test_transient_failures_are_retried_with_growing_waits():
    def script(doc, wanted, n):
        if n <= 2:
            raise ProviderError("HTTP 429: slow down", kind="retry")
        return answer(GOOD)

    events = []
    tr, provider = translator(script)
    result = tr.translate(segments(), notify=lambda kind, **d: events.append(kind))
    assert result.translations == GOOD and result.requests == 1
    assert len(provider.calls) == 3
    assert events == ["wait", "wait"]
    assert len(tr.waits) == 2 and 2 <= tr.waits[0] <= 3 and 4 <= tr.waits[1] <= 6


def test_retry_after_is_honoured():
    def script(doc, wanted, n):
        if n == 1:
            raise ProviderError("HTTP 429", kind="retry", retry_after=17)
        return answer(GOOD)

    tr, _ = translator(script)
    tr.translate(segments())
    assert tr.waits == [17]


def test_persistent_failure_stops_the_run():
    def script(doc, wanted, n):
        raise ProviderError("HTTP 503: overloaded", kind="retry")

    tr, provider = translator(script)
    with pytest.raises(ProviderError) as info:
        tr.translate(segments())
    assert info.value.kind == "fatal" and "503" in str(info.value)
    assert len(provider.calls) == MAX_TRIES


def test_fatal_error_is_not_retried():
    def script(doc, wanted, n):
        raise ProviderError("HTTP 401: bad key", kind="fatal")

    tr, provider = translator(script)
    with pytest.raises(ProviderError):
        tr.translate(segments())
    assert len(provider.calls) == 1


def test_refused_request_fails_only_this_document():
    def script(doc, wanted, n):
        raise ProviderError("HTTP 400: too long", kind="request")

    tr, provider = translator(script)
    result = tr.translate(segments())
    assert len(provider.calls) == 1
    assert result.translations == {} and not result.clean
    assert [(i.seg, i.kind, i.detail) for i in result.issues] == [(None, "request", "HTTP 400: too long")]


def test_cancelled_run_sends_nothing():
    cancel = threading.Event()
    cancel.set()
    provider = FakeProvider()
    with pytest.raises(Cancelled):
        Translator(provider, "zh-CN", cancel=cancel).translate(segments())
    assert provider.calls == []


def test_cancel_during_a_wait():
    def script(doc, wanted, n):
        raise ProviderError("HTTP 429", kind="retry")

    provider = FakeProvider(script)
    tr = Translator(provider, "zh-CN", sleep=lambda seconds: True)
    with pytest.raises(Cancelled):
        tr.translate(segments())
    assert len(provider.calls) == 1


# -- cache key ---------------------------------------------------------------


def test_cache_key_depends_on_text_language_and_model():
    provider = FakeProvider()
    base = Translator(provider, "zh-CN").cache_key(segments())
    assert Translator(provider, "zh-CN").cache_key(segments()) == base
    assert Translator(provider, "ja").cache_key(segments()) != base
    changed = segments()
    changed[2].source += " More."
    assert Translator(provider, "zh-CN").cache_key(changed) != base
    other = FakeProvider()
    other.model = "another-model"
    assert Translator(other, "zh-CN").cache_key(segments()) != base
