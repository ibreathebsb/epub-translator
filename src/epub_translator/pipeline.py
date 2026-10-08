"""Translate a whole book: every document of the spine, then the table of contents."""

from __future__ import annotations

import dataclasses
import posixpath
import queue
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

from lxml import etree

from .epub import Book, EpubError, ManifestItem, serialize_xml
from .extract import Document, ParseError, Segment
from .providers.base import Provider, ProviderError
from .store import Store
from .translate import DocResult, Issue, Translator
from .writeback import Ncx, apply, serialize_document, title_segments

Emit = Callable[..., None]
TOC_NAME = "(目录)"


@dataclass
class DocReport:
    index: int  # position in the spine, from 1; 0 for the table of contents
    name: str  # file inside the EPUB
    title: str
    segments: int = 0
    status: str = "translated"  # translated | cached | failed | empty | unparseable
    issues: list[Issue] = field(default_factory=list)
    requests: int = 0
    seconds: float = 0.0


@dataclass
class Report:
    output: Path
    docs: list[DocReport]
    seconds: float = 0.0

    @property
    def requests(self) -> int:
        return sum(doc.requests for doc in self.docs)

    @property
    def ok(self) -> bool:
        """False when some text did not make it into the book as it should."""
        return not any(i.kind != "untranslated" for doc in self.docs for i in doc.issues)


@dataclass
class _Job:
    report: DocReport
    item: ManifestItem
    doc: Document
    key: str
    result: DocResult | None = None


def state_dir(book: Path) -> Path:
    return Path(book).with_suffix(".epubtr")


def translate_book(
    src: Path,
    out: Path,
    provider: Provider,
    *,
    target: str,
    chapters: set[int] | None = None,
    concurrency: int = 4,
    emit: Emit | None = None,
) -> Report:
    """Translate `src` into `target` and write the result to `out`.

    `chapters` limits the run to those spine positions (from 1). `emit` is
    called with progress events and may be called from worker threads.
    """
    emit = emit or _quiet
    started = time.monotonic()
    cancel = threading.Event()
    src, out = Path(src), Path(out)
    if out.resolve() == src.resolve():
        raise EpubError("输出文件不能与原文件相同")

    with Book(src) as book:
        translator = Translator(
            provider, target, source_lang=book.language, book_title=book.title, cancel=cancel
        )
        store = Store(state_dir(src) / "state.db")
        try:
            reports, jobs = _prepare(book, chapters, translator)
            todo = _from_cache(jobs, store)
            emit(
                "plan",
                documents=len(reports),
                translatable=len(jobs),
                cached=len(jobs) - len(todo),
                segments=sum(len(job.doc.segments) for job in jobs),
            )
            try:
                _translate(todo, translator, store, concurrency, cancel, emit)
            except BaseException:
                cancel.set()
                raise

            replacements: dict[str, bytes] = {}
            memory = _harmonize(jobs)
            for job in jobs:
                done = job.result.translations
                if not done:
                    continue
                for seg in job.doc.segments:
                    if seg.id in done:
                        apply(seg, memory[seg.source])
                replacements[job.item.path] = serialize_document(job.doc, target)

            full = chapters is None
            toc = _translate_toc(book, memory, translator, store, target, full, emit)
            replacements.update(toc.replacements)
            if toc.report is not None:
                reports.append(toc.report)
            if full:
                book.set_language(target)
                replacements[book.opf_path] = serialize_xml(book.opf, book.opf_data)
            book.write(out, replacements)
        finally:
            store.close()
    return Report(output=out, docs=reports, seconds=time.monotonic() - started)


def _quiet(kind: str, **data) -> None:
    pass


def _prepare(
    book: Book, chapters: set[int] | None, translator: Translator
) -> tuple[list[DocReport], list[_Job]]:
    spine = book.spine
    if chapters:
        outside = sorted(c for c in chapters if not 1 <= c <= len(spine))
        if outside:
            raise EpubError(
                f"--chapters 超出范围：这本书的书脊有 {len(spine)} 篇文档，"
                f"没有第 {', '.join(map(str, outside))} 篇"
            )
    reports: list[DocReport] = []
    jobs: list[_Job] = []
    for index, item in enumerate(spine, 1):
        if chapters and index not in chapters:
            continue
        if book.nav is not None and item.path == book.nav.path:
            continue  # translated with the table of contents
        report = DocReport(index, item.path, posixpath.basename(item.path))
        reports.append(report)
        try:
            doc = Document(book.read(item.path))
        except ParseError as exc:
            report.status = "unparseable"
            report.issues.append(Issue(None, "parse", str(exc)))
            continue
        report.title = doc.title or report.title
        report.segments = len(doc.segments)
        if not doc.segments:
            report.status = "empty"
            continue
        jobs.append(_Job(report, item, doc, translator.cache_key(doc.segments)))
    return reports, jobs


def _sound(segments: list[Segment], translations: dict[int, str]) -> bool:
    return all(seg.id in translations and not seg.check(translations[seg.id]) for seg in segments)


def _from_cache(jobs: list[_Job], store: Store) -> list[_Job]:
    """Fill in the jobs that were translated in an earlier run; return the rest."""
    todo = []
    for job in jobs:
        cached = store.get(job.key)
        if cached is not None and _sound(job.doc.segments, cached):
            job.result = DocResult(translations=cached)
            job.report.status = "cached"
        else:
            todo.append(job)
    return todo


def _translate(
    jobs: list[_Job],
    translator: Translator,
    store: Store,
    concurrency: int,
    cancel: threading.Event,
    emit: Emit,
) -> None:
    def work(job: _Job) -> DocResult:
        emit("start", doc=job.report)
        began = time.monotonic()
        result = translator.translate(
            job.doc.segments, notify=lambda kind, **data: emit(kind, doc=job.report, **data)
        )
        job.report.seconds = time.monotonic() - began
        return result

    refused = 0
    succeeded = 0
    for job, result, error in _parallel(jobs, work, concurrency, cancel):
        if error is not None:
            raise error
        job.result = result
        job.report.issues = result.issues
        job.report.requests = result.requests
        if result.clean:
            store.put(job.key, job.item.path, translator.provider.model, result.translations)
        if result.translations:
            succeeded += 1
        else:
            job.report.status = "failed"
            refused += 1
            # Nothing has worked yet: this is a setup problem, not a problem document.
            if refused >= 3 and not succeeded:
                detail = next((i.detail for i in result.issues if i.kind == "request"), "")
                raise ProviderError(f"前 {refused} 篇文档的请求都失败了：{detail}")
        emit("done", doc=job.report)


def _parallel(
    jobs: list[_Job], work: Callable[[_Job], DocResult], concurrency: int, cancel: threading.Event
) -> Iterator[tuple[_Job, DocResult | None, BaseException | None]]:
    """Run `work` on worker threads and yield the outcomes as they arrive.

    The threads are daemons and the caller polls, so Ctrl-C takes effect at
    once instead of waiting for requests in flight.
    """
    pending: queue.SimpleQueue[_Job] = queue.SimpleQueue()
    finished: queue.SimpleQueue = queue.SimpleQueue()
    for job in jobs:
        pending.put(job)

    def worker() -> None:
        while not cancel.is_set():
            try:
                job = pending.get_nowait()
            except queue.Empty:
                return
            try:
                finished.put((job, work(job), None))
            except BaseException as exc:
                finished.put((job, None, exc))

    for _ in range(max(1, min(concurrency, len(jobs)))):
        threading.Thread(target=worker, daemon=True).start()
    for _ in jobs:
        while True:
            try:
                outcome = finished.get(timeout=0.2)
                break
            except queue.Empty:
                continue
        yield outcome


def _harmonize(jobs: list[_Job]) -> dict[str, str]:
    """Pick one translation for every distinct source text in the book.

    Documents are translated independently, so the same title can come back
    worded differently in an article, on a section page and in the table of
    contents. The wording used where the text is a heading wins.
    """
    rank = {"heading": 0, "title": 1, "text": 2, "attr": 3}
    candidates = []
    for order, job in enumerate(jobs):
        for seg in job.doc.segments:
            text = job.result.translations.get(seg.id)
            if text is not None:
                broken = bool(seg.check(text))
                candidates.append((broken, rank[seg.kind], order, seg.id, seg.source, text))
    memory: dict[str, str] = {}
    for *_, source, text in sorted(candidates, key=lambda c: c[:4]):
        memory.setdefault(source, text)
    return memory


@dataclass
class _Toc:
    replacements: dict[str, bytes] = field(default_factory=dict)
    report: DocReport | None = None


def _translate_toc(
    book: Book,
    memory: dict[str, str],
    translator: Translator,
    store: Store,
    target: str,
    full: bool,
    emit: Emit,
) -> _Toc:
    """Translate the navigation document, the NCX and the book title.

    Labels that match text of a translated document reuse that translation.
    The others go to the model in one request, on full runs only.
    """
    toc = _Toc()
    nav = ncx = None
    if book.nav is not None and book.has(book.nav.path):
        try:
            nav = Document(book.read(book.nav.path))
        except ParseError:
            pass
    if book.ncx is not None and book.has(book.ncx.path):
        try:
            ncx = Ncx(book.read(book.ncx.path))
        except etree.XMLSyntaxError:
            pass
    titles = title_segments(book) if full else []
    labels = (nav.segments if nav else []) + (ncx.segments if ncx else []) + titles

    unknown: dict[str, Segment] = {}
    for seg in labels:
        if seg.source not in memory:
            unknown.setdefault(seg.source, seg)
    if unknown and full:
        batch = [
            dataclasses.replace(seg, id=i, loc=None) for i, seg in enumerate(unknown.values(), 1)
        ]
        report = toc.report = DocReport(0, TOC_NAME, "目录与书名", segments=len(batch))
        key = translator.cache_key(batch)
        cached = store.get(key)
        if cached is not None and _sound(batch, cached):
            translations = cached
            report.status = "cached"
        else:
            emit("start", doc=report)
            began = time.monotonic()
            result = translator.translate(
                batch, notify=lambda kind, **data: emit(kind, doc=report, **data)
            )
            report.seconds = time.monotonic() - began
            report.issues, report.requests = result.issues, result.requests
            translations = result.translations
            if result.clean:
                store.put(key, TOC_NAME, translator.provider.model, translations)
            emit("done", doc=report)
        for seg in batch:
            if seg.id in translations:
                memory[seg.source] = translations[seg.id]

    def fill(segments: list[Segment]) -> bool:
        known = [seg for seg in segments if seg.source in memory]
        for seg in known:
            apply(seg, memory[seg.source])
        return bool(known)

    if nav is not None and fill(nav.segments):
        toc.replacements[book.nav.path] = serialize_document(nav, target if full else None)
    if ncx is not None and fill(ncx.segments):
        toc.replacements[book.ncx.path] = ncx.serialize()
    fill(titles)
    return toc
