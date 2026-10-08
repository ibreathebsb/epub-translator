from __future__ import annotations

import hashlib
import zipfile
from pathlib import Path

import checks
import pytest
from conftest import CHAPTERS, build_epub
from fakes import FakeProvider, answer, mark

from epub_translator.epub import EpubError
from epub_translator.pipeline import state_dir, translate_book
from epub_translator.providers.base import ProviderError


def run(src: Path, provider=None, **kwargs):
    provider = provider or FakeProvider()
    out = src.with_name("out.epub")
    report = translate_book(src, out, provider, target="zh-CN", **kwargs)
    return report, out, provider


def read(path: Path, name: str) -> str:
    with zipfile.ZipFile(path) as z:
        return z.read(name).decode()


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_whole_book(epub):
    before = digest(epub)
    report, out, provider = run(epub)

    assert report.ok and report.output == out
    assert digest(epub) == before  # the source is never touched
    assert checks.problems(out) == []
    assert checks.compare(epub, out) == []

    ch1 = read(out, "OEBPS/ch1.xhtml")
    assert 'lang="zh-CN" xml:lang="zh-CN"' in ch1
    assert '<h1 id="c1">译：Chapter One</h1>' in ch1
    assert "译：It was a <em>dark</em> and stormy night" in ch1
    assert 'opening line.<a id="fnref1" href="notes.xhtml#fn1"><sup>1</sup></a></p>' in ch1
    assert '译：See <a href="ch2.xhtml#c2">the next chapter</a> for' in ch1
    assert '<pre>print("never translated")</pre>' in ch1
    assert '<img src="cover.png" alt="译：A lighthouse in the rain"/>' in ch1

    opf = read(out, "OEBPS/content.opf")
    assert "<dc:title>译：The Stormy Night</dc:title>" in opf
    assert "<dc:language>zh-CN</dc:language>" in opf

    # three chapters, plus one request for the labels no chapter contains
    assert len(provider.calls) == 4
    assert [d.status for d in report.docs] == ["translated"] * 4


def test_table_of_contents_uses_the_wording_of_the_headings(epub):
    """A title translated three different ways ends up the way the heading has it."""

    def script(document, wanted, number):
        out = {}
        for i in wanted:
            text = document[i]
            if text == "Chapter One":
                # the <title> comes before the <h1> in the document
                out[i] = "第一章（标题栏）" if i == 1 else "第一章"
            else:
                out[i] = mark(text)
        return answer(out)

    report, out, provider = run(epub, FakeProvider(script))
    assert report.ok
    assert "<title>第一章</title>" in read(out, "OEBPS/ch1.xhtml")
    assert '<h1 id="c1">第一章</h1>' in read(out, "OEBPS/ch1.xhtml")
    assert '<a href="ch1.xhtml">第一章</a>' in read(out, "OEBPS/nav.xhtml")
    assert "<text>第一章</text>" in read(out, "OEBPS/toc.ncx")
    # the labels request only held what no chapter had translated
    labels = provider.calls[-1][1]
    assert '"Contents"' in labels and '"The Stormy Night"' in labels
    assert '"Chapter One"' not in labels
    assert "<h2>译：Contents</h2>" in read(out, "OEBPS/nav.xhtml")
    assert "<text>译：The Stormy Night</text>" in read(out, "OEBPS/toc.ncx")


def test_second_run_sends_nothing(epub):
    _, out, _ = run(epub)
    first = out.read_bytes()
    assert (state_dir(epub) / "state.db").is_file()
    report, out, provider = run(epub)
    assert provider.calls == []
    assert out.read_bytes() == first
    assert {d.status for d in report.docs} == {"cached"}


def test_cache_is_per_model(epub):
    run(epub)
    other = FakeProvider()
    other.model = "another-model"
    _, _, provider = run(epub, other)
    assert len(provider.calls) == 4


def test_chapters_limits_the_run(epub):
    report, out, provider = run(epub, chapters={2})
    assert len(provider.calls) == 1
    assert [d.index for d in report.docs] == [2]
    assert "译：The morning came slowly" in read(out, "OEBPS/ch2.xhtml")
    with zipfile.ZipFile(epub) as a, zipfile.ZipFile(out) as b:
        for name in ("OEBPS/ch1.xhtml", "OEBPS/notes.xhtml", "OEBPS/content.opf"):
            assert a.read(name) == b.read(name)
    nav = read(out, "OEBPS/nav.xhtml")
    assert '<a href="ch2.xhtml">译：Chapter Two</a>' in nav
    assert '<a href="ch1.xhtml">Chapter One</a>' in nav
    assert "<h2>Contents</h2>" in nav and 'lang="en"' in nav
    assert checks.problems(out) == [] and checks.compare(epub, out) == []


def test_chapters_out_of_range(epub):
    with pytest.raises(EpubError, match="超出范围"):
        run(epub, chapters={2, 9})


def test_document_with_persistent_errors(epub):
    """Bad placeholders in one chapter: it is salvaged and reported, the rest is fine."""

    def script(document, wanted, number):
        out = {}
        for i in wanted:
            text = document[i]
            out[i] = "见下一章<x1>" if "the next chapter" in text else mark(text)
        return answer(out)

    report, out, _ = run(epub, FakeProvider(script))
    assert not report.ok
    flagged = [(d.name, [(i.seg, i.kind) for i in d.issues]) for d in report.docs if d.issues]
    assert flagged == [("OEBPS/ch1.xhtml", [(4, "placeholder")])]
    ch1 = read(out, "OEBPS/ch1.xhtml")
    assert "<p>见下一章</p>" in ch1  # the words survive, the link around them is gone
    assert "译：It was a <em>dark</em> and stormy night" in ch1
    assert checks.problems(out) == []

    # the salvaged chapter is not cached: only it is requested again
    fixed = FakeProvider()
    report, out, _ = run(epub, fixed)
    assert report.ok and len(fixed.calls) == 1
    assert '译：See <a href="ch2.xhtml#c2">the next chapter</a>' in read(out, "OEBPS/ch1.xhtml")


def test_missing_segment_keeps_the_source_text(epub):
    def script(document, wanted, number):
        return answer({i: mark(document[i]) for i in wanted if "morning" not in document[i]})

    report, out, _ = run(epub, FakeProvider(script))
    assert not report.ok
    ch2 = read(out, "OEBPS/ch2.xhtml")
    assert "<p>The morning came slowly, and with it" in ch2
    assert '<h1 id="c2">译：Chapter Two</h1>' in ch2


def test_fatal_error_stops_the_run_and_keeps_what_is_done(epub):
    def script(document, wanted, number):
        if any("morning" in text for text in document.values()):
            raise ProviderError("HTTP 401: bad key", kind="fatal")
        return answer({i: mark(document[i]) for i in wanted})

    with pytest.raises(ProviderError, match="401"):
        run(epub, FakeProvider(script), concurrency=1)
    assert not epub.with_name("out.epub").exists()

    report, _, provider = run(epub)
    assert report.ok
    assert len(provider.calls) == 3  # chapter one came from the cache


def test_requests_refused_from_the_start_stop_the_run(tmp_path):
    chapters = {f"c{i}.xhtml": (f"Part {i}", f"<p>Paragraph number {i} of the book</p>") for i in range(6)}
    src = build_epub(tmp_path / "book.epub", chapters)

    def script(document, wanted, number):
        raise ProviderError("HTTP 400: no such model", kind="request")

    provider = FakeProvider(script)
    with pytest.raises(ProviderError, match="no such model"):
        run(src, provider, concurrency=1)
    assert not src.with_name("out.epub").exists()


def test_one_refused_document_does_not_stop_the_others(epub):
    def script(document, wanted, number):
        if any("morning" in text for text in document.values()):
            raise ProviderError("HTTP 400: blocked", kind="request")
        return answer({i: mark(document[i]) for i in wanted})

    report, out, _ = run(epub, FakeProvider(script), concurrency=1)
    assert not report.ok
    with zipfile.ZipFile(epub) as a, zipfile.ZipFile(out) as b:
        assert a.read("OEBPS/ch2.xhtml") == b.read("OEBPS/ch2.xhtml")
    assert "译：Chapter One" in read(out, "OEBPS/ch1.xhtml")
    refused = next(d for d in report.docs if d.name == "OEBPS/ch2.xhtml")
    assert refused.status == "failed"
    assert [(i.seg, i.kind) for i in refused.issues] == [(None, "request")]


def test_unparseable_document_is_left_as_it_is(tmp_path):
    chapters = dict(CHAPTERS)
    chapters["bad.xhtml"] = ("Bad", "<p>An unclosed paragraph")
    src = build_epub(tmp_path / "book.epub", chapters)
    report, out, _ = run(src)
    assert not report.ok
    bad = next(d for d in report.docs if d.name == "OEBPS/bad.xhtml")
    assert bad.status == "unparseable"
    with zipfile.ZipFile(src) as a, zipfile.ZipFile(out) as b:
        assert a.read("OEBPS/bad.xhtml") == b.read("OEBPS/bad.xhtml")
    assert "译：Chapter One" in read(out, "OEBPS/ch1.xhtml")


def test_document_without_text_is_not_sent(tmp_path):
    chapters = {"cover.xhtml": ("", '<p><img src="cover.png"/></p>'), **CHAPTERS}
    src = build_epub(tmp_path / "book.epub", chapters)
    report, out, provider = run(src)
    assert report.docs[0].status == "empty"
    assert len(provider.calls) == 4
    with zipfile.ZipFile(src) as a, zipfile.ZipFile(out) as b:
        assert a.read("OEBPS/cover.xhtml") == b.read("OEBPS/cover.xhtml")


def test_navigation_document_in_the_spine_is_translated_once(tmp_path):
    src = build_epub(tmp_path / "book.epub")
    with zipfile.ZipFile(src) as z:
        data = {name: z.read(name) for name in z.namelist()}
    data["OEBPS/content.opf"] = data["OEBPS/content.opf"].replace(
        b'<itemref idref="d0"/>', b'<itemref idref="nav"/>\n    <itemref idref="d0"/>'
    )
    with zipfile.ZipFile(src, "w") as z:
        z.writestr(zipfile.ZipInfo("mimetype"), data.pop("mimetype"))
        for name, content in data.items():
            z.writestr(name, content)
    report, out, provider = run(src)
    assert report.ok and len(provider.calls) == 4
    assert '<a href="ch1.xhtml">译：Chapter One</a>' in read(out, "OEBPS/nav.xhtml")
    assert checks.compare(src, out) == []


def test_documents_are_translated_in_parallel(tmp_path):
    import threading

    chapters = {f"c{i}.xhtml": (f"Part {i}", f"<p>Paragraph number {i} of the book</p>") for i in range(8)}
    src = build_epub(tmp_path / "book.epub", chapters)
    gate = threading.Barrier(4, timeout=5)

    def script(document, wanted, number):
        if number <= 4:
            gate.wait()  # passes only when four requests are in flight together
        return answer({i: mark(document[i]) for i in wanted})

    report, out, provider = run(src, FakeProvider(script), concurrency=4)
    assert report.ok and len(provider.calls) == 9
    assert checks.compare(src, out) == []


def test_progress_events(epub):
    events = []
    run(epub, emit=lambda kind, **data: events.append((kind, data)))
    kinds = [kind for kind, _ in events]
    assert kinds[0] == "plan"
    assert events[0][1] == {"documents": 3, "translatable": 3, "cached": 0, "segments": 11}
    assert kinds.count("start") == kinds.count("done") == 4


def test_output_must_differ_from_the_source(epub):
    with pytest.raises(EpubError):
        translate_book(epub, epub, FakeProvider(), target="zh-CN")
