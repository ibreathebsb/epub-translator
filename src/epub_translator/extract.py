"""Find the translatable text of an XHTML content document.

The text is cut into *segments*. A segment is one run of inline content: a
paragraph, a heading, a list item, a table cell, or loose text sitting between
block elements. Inline markup inside a segment is replaced by numbered
placeholders so the model never sees real tags or attributes:

    He said <em>no</em> to <a href="#n1">her</a>.<br/>
    He said <x1>no</x1> to <x2>her</x2>.<x3/>

``<xN>…</xN>`` wraps text the model must translate; ``<xN/>`` stands for an
opaque inline object (line break, image, note reference, code) that it must
keep. `writeback` puts the real elements back.
"""

from __future__ import annotations

import html.entities
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from lxml import etree

from .epub import parse_xml

XHTML_NS = "http://www.w3.org/1999/xhtml"

INLINE_TAGS = frozenset(
    "a abbr acronym b bdi bdo big cite data del dfn em font i ins label mark nobr q "
    "rb rp rt rtc ruby s small span strike strong sub sup time tt u".split()
)
# Inline objects that are carried along untouched.
ATOM_TAGS = frozenset(
    "area audio br button canvas code embed iframe img input kbd map meter object "
    "output picture progress samp script select style textarea var video wbr".split()
)
# Block-level content that is never translated.
SKIP_TAGS = frozenset("pre template noscript".split())
HEADING_TAGS = frozenset("h1 h2 h3 h4 h5 h6".split())

BLOCK, INLINE, ATOM, SKIP = "block", "inline", "atom", "skip"

HTML_WS = " \t\n\r\f"
_WS_RUN = re.compile(r"[ \t\n\r\f]+")
_LETTER = re.compile(r"[^\W\d_]")
_URL = re.compile(r"(?:https?://|ftp://|www\.)\S+|[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_TOKEN = re.compile(r"<\s*(/?)\s*[xX](\d+)\s*(/?)\s*>")
_ENTITY = re.compile(rb"&([A-Za-z][A-Za-z0-9]{1,31});")
_XML_ENTITIES = {b"amp", b"lt", b"gt", b"quot", b"apos"}


class ParseError(Exception):
    """The document is not well-formed XML."""


def translatable(text: str | None) -> bool:
    """True when `text` holds words: letters outside of URLs and e-mail addresses."""
    return bool(text) and bool(_LETTER.search(_URL.sub(" ", text)))


def local_name(el: etree._Element) -> str | None:
    """Tag name of an HTML element; None for comments, PIs and foreign markup (SVG, MathML)."""
    tag = el.tag
    if not isinstance(tag, str):
        return None
    if tag.startswith("{"):
        ns, _, name = tag[1:].partition("}")
        return name.lower() if ns == XHTML_NS else None
    return tag.lower()


def tokenize(text: str) -> list[tuple[str, Any]]:
    """Split a segment into ("text", str), ("open", n), ("close", n) and ("atom", n) tokens."""
    tokens: list[tuple[str, Any]] = []
    pos = 0
    for m in _TOKEN.finditer(text):
        if m.start() > pos:
            tokens.append(("text", text[pos:m.start()]))
        n = int(m.group(2))
        if m.group(3):
            tokens.append(("atom", n))
        else:
            tokens.append(("close" if m.group(1) else "open", n))
        pos = m.end()
    if pos < len(text):
        tokens.append(("text", text[pos:]))
    return tokens


@dataclass(eq=False)
class Segment:
    id: int
    source: str
    kind: str = "text"  # text | heading | title | attr
    paired: frozenset[int] = frozenset()
    atoms: frozenset[int] = frozenset()
    # placeholder -> the placeholder it sits in (0 at the top level)
    nesting: dict[int, int] = field(default_factory=dict)
    loc: Any = None  # where the segment lives in the tree, for writeback

    def tokens(self, text: str) -> list[tuple[str, Any]]:
        """Tokens of a translation, with ``<xN></xN>`` read as ``<xN/>`` for opaque objects."""
        raw = tokenize(text)
        out: list[tuple[str, Any]] = []
        i = 0
        while i < len(raw):
            kind, value = raw[i]
            if (
                kind == "open"
                and value in self.atoms
                and i + 1 < len(raw)
                and raw[i + 1] == ("close", value)
            ):
                out.append(("atom", value))
                i += 2
                continue
            out.append((kind, value))
            i += 1
        return out

    def check(self, text: str) -> list[str]:
        """Problems with the placeholders of a translation; empty when it is sound."""
        errors: list[str] = []
        seen: Counter[int] = Counter()
        parent: dict[int, int] = {}
        stack: list[int] = []
        filled: set[int] = set()
        for kind, n in self.tokens(text):
            if kind == "text":
                if n.strip(HTML_WS):
                    filled.update(stack)
                continue
            if n not in self.paired and n not in self.atoms:
                errors.append(f"<x{n}> does not exist in the source segment")
                continue
            if kind == "close":
                if stack and stack[-1] == n:
                    stack.pop()
                else:
                    errors.append(f"</x{n}> closes a placeholder that is not open at that point")
                continue
            seen[n] += 1
            parent[n] = stack[-1] if stack else 0
            filled.update(stack)
            if kind == "atom" and n in self.paired:
                errors.append(f"<x{n}> must wrap text as <x{n}>...</x{n}>, not be self-closing")
            elif kind == "open" and n in self.atoms:
                errors.append(f"<x{n}/> is self-closing and cannot wrap text")
            elif kind == "open":
                stack.append(n)
        errors.extend(f"<x{n}> is never closed" for n in stack)
        for n in sorted(self.paired | self.atoms):
            if seen[n] == 0:
                errors.append(f"placeholder x{n} is missing")
            elif seen[n] > 1:
                errors.append(f"placeholder x{n} appears {seen[n]} times instead of once")
        if errors:
            return list(dict.fromkeys(errors))
        for n in sorted(self.paired - filled):
            errors.append(f"<x{n}>...</x{n}> wraps no text")
        for n, outer in sorted(self.nesting.items()):
            if parent[n] != outer:
                where = f"inside <x{outer}>" if outer else "outside of the other placeholders"
                errors.append(f"x{n} must stay {where}")
        return errors

    def salvage(self, text: str) -> str:
        """Make a translation with broken placeholders usable.

        The words are kept, wrapping placeholders are dropped, and every opaque
        object is kept exactly once (appended when the model lost it).
        """
        out: list[str] = []
        used: set[int] = set()
        for kind, value in self.tokens(text):
            if kind == "text":
                out.append(value)
            elif kind == "atom" and value in self.atoms and value not in used:
                used.add(value)
                out.append(f"<x{value}/>")
        out.extend(f"<x{n}/>" for n in sorted(self.atoms - used))
        return "".join(out).strip(HTML_WS)


@dataclass(eq=False)
class RunLoc:
    """A run of inline content inside `parent`."""

    parent: etree._Element
    prev: etree._Element | None  # node whose tail is the text before the run; None: parent.text
    elements: list[etree._Element]  # top-level elements of the run
    lead_text: bool  # the text before the first element belongs to the run
    trail_text: bool  # the text after the last element belongs to the run
    table: dict[int, etree._Element]  # placeholder number -> element


@dataclass(eq=False)
class AttrLoc:
    element: etree._Element
    name: str


@dataclass(eq=False)
class TextLoc:
    """An element whose whole text is the segment (NCX labels, OPF titles)."""

    element: etree._Element


def _named_entities_to_numeric(data: bytes) -> bytes:
    """Rewrite HTML entities such as ``&nbsp;`` that an XML parser does not know."""

    def repl(m: re.Match[bytes]) -> bytes:
        name = m.group(1)
        if name in _XML_ENTITIES:
            return m.group(0)
        chars = html.entities.html5.get(name.decode("ascii") + ";")
        if chars is None:
            return m.group(0)
        return "".join(f"&#{ord(c)};" for c in chars).encode("ascii")

    return _ENTITY.sub(repl, data)


def parse_xhtml(data: bytes) -> etree._ElementTree:
    try:
        return parse_xml(_named_entities_to_numeric(data))
    except etree.XMLSyntaxError as exc:
        raise ParseError(str(exc)) from exc


def _no_translate(el: etree._Element) -> bool:
    return el.get("translate") == "no" or "notranslate" in (el.get("class") or "").split()


class Document:
    """A parsed content document and its translatable segments."""

    def __init__(self, data: bytes):
        self.original = data
        self.tree = parse_xhtml(data)
        self.root = self.tree.getroot()
        # Pin every node: lxml would otherwise hand out fresh proxy objects and
        # the element references kept in segments would lose their identity.
        self._nodes = list(self.root.iter())
        self._kinds: dict[etree._Element, str] = {}
        self._heading = False
        self.segments: list[Segment] = []
        self._extract()

    @property
    def title(self) -> str | None:
        """A human-readable name: the first heading, else the <title>."""
        for kind in ("heading", "title"):
            for seg in self.segments:
                if seg.kind == kind:
                    return _TOKEN.sub("", seg.source)
        return None

    # -- classification ----------------------------------------------------

    def _kind(self, el: etree._Element) -> str:
        kind = self._kinds.get(el)
        if kind is None:
            kind = self._kinds[el] = self._compute_kind(el)
        return kind

    def _compute_kind(self, el: etree._Element) -> str:
        name = local_name(el)
        if name is None:
            return ATOM
        inline = name in INLINE_TAGS
        if _no_translate(el):
            return ATOM if inline or name in ATOM_TAGS else SKIP
        if name in ATOM_TAGS:
            return ATOM
        if name in SKIP_TAGS:
            return SKIP
        if not inline:
            return BLOCK
        # An inline element is a container when it holds block content, opaque
        # when it holds no words (note markers, page anchors, bare URLs).
        has_words = translatable(el.text)
        for child in el:
            kind = self._kind(child)
            if kind in (BLOCK, SKIP):
                return BLOCK
            if kind == INLINE or translatable(child.tail):
                has_words = True
        return INLINE if has_words else ATOM

    # -- walking -----------------------------------------------------------

    def _extract(self) -> None:
        root = self.root
        if local_name(root) != "html":
            self._walk(root)
            return
        for child in root:
            name = local_name(child)
            if name == "head":
                for el in child:
                    if local_name(el) == "title" and translatable(el.text) and not len(el):
                        self._add("title", el.text, RunLoc(el, None, [], True, True, {}))
            elif name == "body":
                self._attrs(child)
                self._walk(child)

    def _add(self, kind: str, source: str, loc: Any, **extra: Any) -> None:
        source = _WS_RUN.sub(" ", source).strip(HTML_WS)
        self.segments.append(Segment(len(self.segments) + 1, source, kind, loc=loc, **extra))

    def _attrs(self, el: etree._Element) -> None:
        name = local_name(el)
        if name in ("img", "area", "input") and translatable(el.get("alt")):
            self._add("attr", el.get("alt"), AttrLoc(el, "alt"))
        # A tooltip shows on something visible. On an empty element (a page
        # marker, an anchor) the title is bookkeeping, not text for the reader.
        visible = name in ATOM_TAGS or len(el) or (el.text or "").strip(HTML_WS)
        if visible and translatable(el.get("title")):
            self._add("attr", el.get("title"), AttrLoc(el, "title"))

    def _attrs_deep(self, el: etree._Element) -> None:
        if local_name(el) is None or _no_translate(el):
            return
        self._attrs(el)
        for child in el:
            self._attrs_deep(child)

    def _walk(self, parent: etree._Element) -> None:
        children = list(parent)
        start = 0
        for i, child in enumerate(children):
            kind = self._kind(child)
            if kind in (BLOCK, SKIP):
                self._run(parent, children, start, i)
                if kind == BLOCK:
                    self._attrs(child)
                    heading = self._heading
                    self._heading = heading or local_name(child) in HEADING_TAGS
                    self._walk(child)
                    self._heading = heading
                start = i + 1
        self._run(parent, children, start, len(children))

    def _run(self, parent: etree._Element, children: list, a: int, b: int) -> None:
        """Handle the inline run made of children[a:b] and the text around them."""

        def slot(k: int) -> str:
            return (parent.text if k == 0 else children[k - 1].tail) or ""

        # Opaque objects and whitespace at either end stay where they are.
        lo, hi = a, b
        while not slot(lo).strip(HTML_WS) and lo < hi and self._kind(children[lo]) == ATOM:
            lo += 1
        lead_text = bool(slot(lo).strip(HTML_WS))
        while not slot(hi).strip(HTML_WS) and hi > lo and self._kind(children[hi - 1]) == ATOM:
            hi -= 1
        trail_text = bool(slot(hi).strip(HTML_WS))

        for el in children[a:lo]:
            self._attrs_deep(el)
        elements = children[lo:hi]
        if len(elements) == 1 and not lead_text and not trail_text:
            # The run is one element and nothing else: its content is the segment.
            self._attrs(elements[0])
            self._walk(elements[0])
        elif elements or lead_text:
            self._segment(parent, children, lo, hi, lead_text, trail_text)
            for el in elements:
                self._attrs_deep(el)
        for el in children[hi:b]:
            self._attrs_deep(el)

    def _segment(
        self, parent: etree._Element, children: list, lo: int, hi: int,
        lead_text: bool, trail_text: bool,
    ) -> None:
        parts: list[str] = []
        table: dict[int, etree._Element] = {}
        paired: set[int] = set()
        atoms: set[int] = set()
        nesting: dict[int, int] = {}
        words: list[str] = []

        def text(value: str | None) -> None:
            if value:
                parts.append(value)
                words.append(value)

        def element(el: etree._Element, outer: int) -> None:
            n = len(table) + 1
            table[n] = el
            nesting[n] = outer
            if self._kind(el) == ATOM:
                atoms.add(n)
                parts.append(f"<x{n}/>")
                return
            paired.add(n)
            parts.append(f"<x{n}>")
            text(el.text)
            for child in el:
                element(child, n)
                text(child.tail)
            parts.append(f"</x{n}>")

        if lead_text:
            text(parent.text if lo == 0 else children[lo - 1].tail)
        for k in range(lo, hi):
            element(children[k], 0)
            if k + 1 < hi or trail_text:
                text(children[k].tail)

        if not paired and not translatable(" ".join(words)):
            return
        if any(_TOKEN.search(w) for w in words):
            return  # the text itself looks like a placeholder; leave it alone
        loc = RunLoc(
            parent=parent,
            prev=children[lo - 1] if lo > 0 else None,
            elements=children[lo:hi],
            lead_text=lead_text,
            trail_text=trail_text,
            table=table,
        )
        self._add(
            "heading" if self._heading else "text", "".join(parts), loc,
            paired=frozenset(paired), atoms=frozenset(atoms), nesting=nesting,
        )
