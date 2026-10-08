"""Put translations back: into content documents, the table of contents and the metadata."""

from __future__ import annotations

from lxml import etree

from .epub import Book, parse_xml, serialize_xml
from .extract import (
    _TOKEN,
    _WS_RUN,
    HTML_WS,
    AttrLoc,
    Document,
    RunLoc,
    Segment,
    TextLoc,
    local_name,
    translatable,
)

XML_LANG = "{http://www.w3.org/XML/1998/namespace}lang"
VOID_TAGS = frozenset(
    "area base br col embed hr img input link meta param source track wbr".split()
)


def apply(seg: Segment, text: str) -> None:
    """Replace the source text of `seg` in its tree with `text`.

    Never produces broken markup: a translation whose placeholders do not add
    up loses its inline formatting instead.
    """
    text = text.strip(HTML_WS)
    loc = seg.loc
    if isinstance(loc, AttrLoc):
        loc.element.set(loc.name, _WS_RUN.sub(" ", _TOKEN.sub("", text)))
    elif isinstance(loc, TextLoc):
        loc.element.text = _TOKEN.sub("", text)
    else:
        _apply_run(seg, loc, text)


def _slot(parent: etree._Element, prev: etree._Element | None) -> str:
    return (parent.text if prev is None else prev.tail) or ""


def _set_slot(parent: etree._Element, prev: etree._Element | None, value: str) -> None:
    if prev is None:
        parent.text = value or None
    else:
        prev.tail = value or None


def _apply_run(seg: Segment, loc: RunLoc, text: str) -> None:
    if seg.check(text):
        text = seg.salvage(text)
    tokens = seg.tokens(text)
    parent, table = loc.parent, loc.table

    # Whitespace around the run is layout of the source file: keep it.
    before = _slot(parent, loc.prev)
    if loc.elements:
        after = loc.elements[-1].tail or ""
        index = parent.index(loc.elements[0])
        if loc.lead_text:
            before = before[: len(before) - len(before.lstrip(HTML_WS))]
        if loc.trail_text:
            after = after[len(after.rstrip(HTML_WS)):]
    else:
        index = 0
        after = before[len(before.rstrip(HTML_WS)):]
        before = before[: len(before) - len(before.lstrip(HTML_WS))]

    # Take the run apart; the elements are reused when it is put together again.
    for el in loc.elements:
        parent.remove(el)
    for n, el in table.items():
        el.tail = None
        if n in seg.paired:
            for child in list(el):
                el.remove(child)
            el.text = None

    # A salvaged translation has lost its wrapping elements. Those that carry
    # an id stay, empty, at the front, so links pointing at them keep working.
    present = {n for kind, n in tokens if kind != "text"}
    for n in sorted(seg.paired - present, reverse=True):
        if table[n].get("id") or table[n].get("name"):
            tokens[:0] = [("open", n), ("close", n)]

    lead: list[str] = []
    top: list[etree._Element] = []
    stack: list[etree._Element] = []
    for kind, value in tokens:
        if kind == "close":
            stack.pop()
        elif kind == "text":
            if stack:
                box = stack[-1]
                if len(box):
                    box[-1].tail = (box[-1].tail or "") + value
                else:
                    box.text = (box.text or "") + value
            elif top:
                top[-1].tail = (top[-1].tail or "") + value
            else:
                lead.append(value)
        else:
            el = table[value]
            if stack:
                stack[-1].append(el)
            else:
                top.append(el)
            if kind == "open":
                stack.append(el)

    before += "".join(lead)
    if top:
        _set_slot(parent, loc.prev, before)
        for offset, el in enumerate(top):
            parent.insert(index + offset, el)
        top[-1].tail = (top[-1].tail or "") + after or None
    else:
        _set_slot(parent, loc.prev, before + after)


def serialize_document(doc: Document, lang: str | None = None) -> bytes:
    root = doc.root
    if lang and local_name(root) == "html":
        root.set("lang", lang)
        root.set(XML_LANG, lang)
    # Write <p></p>, never <p/>: readers that parse the file as HTML would
    # take a self-closed non-void element for an unclosed one.
    for el in root.iter():
        name = local_name(el)
        if name is not None and name not in VOID_TAGS and el.text is None and not len(el):
            el.text = ""
    return serialize_xml(doc.tree, doc.original)


class Ncx:
    """The EPUB 2 table of contents."""

    def __init__(self, data: bytes):
        self.original = data
        self.tree = parse_xml(data)
        self.segments: list[Segment] = []
        for el in self.tree.getroot().iter():
            if not isinstance(el.tag, str) or etree.QName(el).localname != "text":
                continue
            parent = el.getparent()
            if parent is None or etree.QName(parent).localname not in ("navLabel", "docTitle"):
                continue
            if translatable(el.text):
                source = _WS_RUN.sub(" ", el.text).strip(HTML_WS)
                self.segments.append(Segment(0, source, "heading", loc=TextLoc(el)))

    def serialize(self) -> bytes:
        return serialize_xml(self.tree, self.original)


def title_segments(book: Book) -> list[Segment]:
    """The book titles in the package metadata."""
    return [
        Segment(0, _WS_RUN.sub(" ", el.text).strip(HTML_WS), "heading", loc=TextLoc(el))
        for el in book.title_elements
        if translatable(el.text)
    ]
