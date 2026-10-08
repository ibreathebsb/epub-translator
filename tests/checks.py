"""Structural checks on an EPUB file: a small stand-in for epubcheck.

Run it by hand to compare a translation with its source:

    python tests/checks.py original.epub translated.epub
"""

from __future__ import annotations

import posixpath
import sys
import zipfile
from collections import Counter
from pathlib import Path
from urllib.parse import unquote, urlsplit

from lxml import etree

from epub_translator.epub import Book
from epub_translator.extract import local_name, parse_xhtml


def _target(base: str, ref: str) -> tuple[str, str] | None:
    """(member name, fragment) for a link inside the book; None for external links."""
    parts = urlsplit(ref)
    if parts.scheme or parts.netloc:
        return None
    path = unquote(parts.path)
    name = posixpath.normpath(posixpath.join(posixpath.dirname(base), path)) if path else base
    return name, unquote(parts.fragment)


def problems(path: Path) -> list[str]:
    """Everything that is wrong with the container, the markup and the internal links."""
    found: list[str] = []
    with zipfile.ZipFile(path) as z:
        infos = z.infolist()
        if not infos or infos[0].filename != "mimetype":
            found.append("mimetype is not the first member")
        elif infos[0].compress_type != zipfile.ZIP_STORED:
            found.append("mimetype is compressed")
        elif z.read("mimetype") != b"application/epub+zip":
            found.append("mimetype has the wrong content")
        if z.testzip() is not None:
            found.append("corrupt zip member")
    with Book(path) as book:
        names = set(zipfile.ZipFile(path).namelist())
        for item in book.manifest.values():
            if item.path not in names:
                found.append(f"manifest item missing from the archive: {item.path}")
        trees = {}
        for item in book.manifest.values():
            if not item.is_content or item.path not in names:
                continue
            try:
                trees[item.path] = parse_xhtml(book.read(item.path)).getroot()
            except Exception as exc:  # noqa: BLE001
                found.append(f"{item.path}: not well-formed: {exc}")
        ids = {}
        for name, root in trees.items():
            seen = Counter(el.get("id") for el in root.iter() if isinstance(el.tag, str) and el.get("id"))
            ids[name] = set(seen)
            found.extend(f"{name}: duplicate id {i!r}" for i, n in seen.items() if n > 1)
        for name, root in trees.items():
            for el in root.iter():
                if not isinstance(el.tag, str):
                    continue
                for attr in ("href", "src"):
                    ref = el.get(attr)
                    if not ref:
                        continue
                    target = _target(name, ref)
                    if target is None:
                        continue
                    member, fragment = target
                    if member not in names:
                        found.append(f"{name}: broken link {ref!r}")
                    elif fragment and member in ids and fragment not in ids[member]:
                        found.append(f"{name}: link to a missing id {ref!r}")
        if book.ncx is not None and book.ncx.path in names:
            try:
                ncx = etree.fromstring(book.read(book.ncx.path))
            except etree.XMLSyntaxError as exc:
                found.append(f"{book.ncx.path}: not well-formed: {exc}")
            else:
                for el in ncx.iter():
                    if isinstance(el.tag, str) and etree.QName(el).localname == "content":
                        target = _target(book.ncx.path, el.get("src") or "")
                        if target and target[0] not in names:
                            found.append(f"{book.ncx.path}: broken link {el.get('src')!r}")
    return found


def skeleton(path: Path) -> dict[str, tuple]:
    """Per content document: everything except its text.

    Tags, ids, link targets and image sources, each as a sorted list. A
    translation must leave all of this exactly as it was.
    """
    result = {}
    with Book(path) as book:
        for item in book.manifest.values():
            if not item.is_content:
                continue
            root = parse_xhtml(book.read(item.path)).getroot()
            tags, ids, refs = [], [], []
            for el in root.iter():
                if not isinstance(el.tag, str):
                    continue
                tags.append(local_name(el) or el.tag)
                if el.get("id"):
                    ids.append(el.get("id"))
                refs.extend(el.get(a) for a in ("href", "src") if el.get(a))
            result[item.path] = (sorted(tags), sorted(ids), sorted(refs))
    return result


def spine(path: Path) -> list[str]:
    with Book(path) as book:
        return [item.path for item in book.spine]


def compare(original: Path, translated: Path) -> list[str]:
    """Structural differences between a book and its translation."""
    found = []
    if spine(original) != spine(translated):
        found.append("the spine changed")
    with zipfile.ZipFile(original) as a, zipfile.ZipFile(translated) as b:
        if sorted(a.namelist()) != sorted(b.namelist()):
            found.append("the set of files changed")
    before, after = skeleton(original), skeleton(translated)
    for name in before:
        if name not in after:
            found.append(f"{name}: missing")
            continue
        for what, x, y in zip(("tags", "ids", "links"), before[name], after[name]):
            if x != y:
                gone = list((Counter(x) - Counter(y)).elements())
                new = list((Counter(y) - Counter(x)).elements())
                found.append(f"{name}: {what} changed, lost {gone[:5]}, gained {new[:5]}")
    return found


if __name__ == "__main__":
    source, result = Path(sys.argv[1]), Path(sys.argv[2])
    before = problems(source)
    after = problems(result)
    print(f"original:   {len(before)} problems")
    print(f"translated: {len(after)} problems, {len(set(after) - set(before))} of them new")
    for line in sorted(set(after) - set(before))[:40]:
        print("  NEW", line)
    changes = compare(source, result)
    print(f"structural differences: {len(changes)}")
    for line in changes[:40]:
        print("  ", line)
    sys.exit(1 if changes or set(after) - set(before) else 0)
