from __future__ import annotations

import pytest
from conftest import page

from epub_translator.extract import Document, ParseError, Segment, tokenize
from epub_translator.writeback import apply, serialize_document


def parse(body: str, title: str = "T") -> Document:
    return Document(page(title, body))


def sources(doc: Document) -> list[str]:
    return [seg.source for seg in doc.segments if seg.kind != "title"]


def body_of(doc: Document) -> str:
    text = serialize_document(doc).decode()
    return text[text.index("<body>") + 6 : text.index("</body>")]


def translate(doc: Document, text: str, index: int = 0) -> str:
    """Apply `text` to the index-th body segment and return the new body."""
    segments = [seg for seg in doc.segments if seg.kind != "title"]
    apply(segments[index], text)
    return body_of(doc)


# -- what becomes a segment --------------------------------------------------


def test_inline_markup_becomes_placeholders():
    doc = parse('<p>He said <em>no</em> to <a href="#n1">her</a>.<br/></p>')
    assert sources(doc) == ["He said <x1>no</x1> to <x2>her</x2>."]
    seg = doc.segments[-1]
    assert seg.paired == {1, 2} and seg.atoms == set()


def test_line_break_inside_text_is_an_opaque_placeholder():
    doc = parse("<p>Line one<br/>line two</p>")
    assert sources(doc) == ["Line one<x1/>line two"]
    assert doc.segments[-1].atoms == {1}


def test_nested_inline_markup_records_nesting():
    doc = parse("<p>A <b>bold <i>and italic</i> text</b> end</p>")
    assert sources(doc) == ["A <x1>bold <x2>and italic</x2> text</x1> end"]
    assert doc.segments[-1].nesting == {1: 0, 2: 1}


def test_element_that_is_the_whole_block_is_unwrapped():
    doc = parse('<ul><li><a href="x.html">Chapter One</a></li><li><b><i>Deep</i></b></li></ul>')
    assert sources(doc) == ["Chapter One", "Deep"]


def test_inline_content_between_blocks_is_a_segment():
    doc = parse('<span class="date">Oct 1st 2026</span><br/>\n<p>Text here</p>')
    assert sources(doc) == ["Oct 1st 2026", "Text here"]


def test_table_cells_are_separate_segments():
    doc = parse("<table><tr><td>Cell one</td><td>Cell two</td></tr></table>")
    assert sources(doc) == ["Cell one", "Cell two"]


def test_block_content_inside_an_inline_element():
    doc = parse('<a href="x.html"><div><p>Inner text</p></div></a>')
    assert sources(doc) == ["Inner text"]
    assert translate(doc, "内文") == '<a href="x.html"><div><p>内文</p></div></a>'


def test_headings_and_title_are_marked():
    doc = parse("<h2>Part <em>One</em></h2><p>Body text</p>", title="My Book")
    assert [(s.kind, s.source) for s in doc.segments] == [
        ("title", "My Book"),
        ("heading", "Part <x1>One</x1>"),
        ("text", "Body text"),
    ]
    assert doc.title == "Part One"


def test_image_alt_and_title_attributes_are_segments():
    doc = parse('<p><img src="a.png" alt="A lighthouse"/></p><p title="A tooltip">Text</p>')
    assert [(s.kind, s.source) for s in doc.segments if s.kind != "title"] == [
        ("attr", "A lighthouse"),
        ("attr", "A tooltip"),
        ("text", "Text"),
    ]


def test_title_of_an_empty_element_is_not_text():
    doc = parse('<p>Some words<span class="pageno" title="{vii}" id="p7"/> on a page</p>')
    assert sources(doc) == ["Some words<x1/> on a page"]


def test_drop_classes_removes_elements_and_keeps_the_text_after_them():
    data = page("T", '<p>Body text</p><p class="x footer">Downloaded by <a href="u">someone</a></p> tail'
                     '<div>Keep <span class="footer">drop</span> this</div>')
    doc = Document(data, frozenset({"footer"}))
    assert doc.dropped == 2
    assert sources(doc) == ["Body text", "tail", "Keep this"]
    assert body_of(doc) == "<p>Body text</p> tail<div>Keep  this</div>"
    assert Document(data).dropped == 0


# -- what is left alone ------------------------------------------------------


def test_code_and_preformatted_text_are_not_translated():
    doc = parse("<p>Run <code>ls -la</code> now</p><pre>some code here</pre>")
    assert sources(doc) == ["Run <x1/> now"]


@pytest.mark.parametrize(
    "markup",
    [
        '<p translate="no">Keep me as I am</p>',
        '<div class="x notranslate"><p>Keep me as I am</p></div>',
        "<script>var keep = 'me';</script>",
        "<style>p { color: red }</style>",
        '<svg xmlns="http://www.w3.org/2000/svg"><text>Label here</text></svg>',
        '<math xmlns="http://www.w3.org/1998/Math/MathML"><mi>speed</mi></math>',
        "<p>***</p>",
        "<p>42</p>",
        "<p>https://example.com/some/path</p>",
        "<p>   </p>",
    ],
)
def test_nothing_to_translate(markup):
    assert sources(parse(markup)) == []


def test_inline_no_translate_is_carried_as_opaque():
    doc = parse('<p>The command <span translate="no">make all</span> builds it</p>')
    assert sources(doc) == ["The command <x1/> builds it"]


def test_wordless_inline_elements_are_opaque():
    doc = parse(
        '<p>A claim<sup><a href="#fn1">1</a></sup> and a page mark<a id="p5"/> in a sentence.</p>'
    )
    assert sources(doc) == ["A claim<x1/> and a page mark<x2/> in a sentence."]


def test_opaque_objects_at_the_edges_stay_out_of_the_segment():
    doc = parse(
        '<p><img src="d.png"/>The end of the article. <span class="mark">■</span></p>'
        '<p>Downloaded from <a href="https://e.com/a">https://e.com/a</a> </p>'
    )
    assert sources(doc) == ["The end of the article.", "Downloaded from"]
    assert translate(doc, "文章结束。") .startswith(
        '<p><img src="d.png"/>文章结束。 <span class="mark">■</span></p>'
    )


def test_text_that_looks_like_a_placeholder_is_left_alone():
    assert sources(parse("<p>Write &lt;x1&gt; to open the tag</p>")) == []


def test_comment_inside_a_paragraph_survives():
    doc = parse("<p>Hello <!-- note --> big world</p>")
    assert sources(doc) == ["Hello <x1/> big world"]
    assert translate(doc, "你好<x1/>大世界") == "<p>你好<!-- note -->大世界</p>"


# -- parsing -----------------------------------------------------------------


def test_html_entities_are_understood():
    data = (
        b'<?xml version="1.0"?><!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.1//EN" '
        b'"http://www.w3.org/TR/xhtml11/DTD/xhtml11.dtd">'
        b'<html xmlns="http://www.w3.org/1999/xhtml"><body>'
        b"<p>Mr.&nbsp;Smith &amp; Sons&mdash;est. 1900</p></body></html>"
    )
    doc = Document(data)
    assert doc.segments[0].source == "Mr. Smith & Sons—est. 1900"


def test_document_without_namespace():
    doc = Document(b"<html><head><title>T</title></head><body><p>Plain <b>old</b> page</p></body></html>")
    assert [s.source for s in doc.segments] == ["T", "Plain <x1>old</x1> page"]


def test_malformed_document_is_rejected():
    with pytest.raises(ParseError):
        Document(b"<html><body><p>Unclosed</body></html>")


# -- writing back ------------------------------------------------------------


def test_writing_the_source_back_changes_nothing():
    body = (
        '\n    <h1 id="c1">Chapter <em>One</em></h1>\n'
        '    <p class="first">It was a <b>dark <i>and</i> stormy</b> night.<a id="r1" href="#f1"><sup>1</sup></a></p>\n'
        '    <span class="date">Oct 1st</span><br/>\n'
        "    <ul>\n      <li><a href=\"a.html\">First entry</a></li>\n      <li>Second <code>x</code> entry</li>\n    </ul>\n"
        '    <p><img src="a.png" alt="An image"/></p>\n'
        "    <blockquote><p>Quoted words<br/>on two lines</p></blockquote>\n  "
    )
    data = page("A title", body)
    doc = Document(data)
    for seg in doc.segments:
        apply(seg, seg.source)
    assert serialize_document(doc) == data


def test_untouched_document_serializes_to_the_same_bytes():
    data = page("A title", "<p>One <b>two</b></p>\n<hr/>\n<p>\n</p>")
    assert serialize_document(Document(data)) == data


def test_placeholders_can_move():
    doc = parse('<p>He said <em>no</em> to <a href="#n1">her</a>.<br/></p>')
    assert (
        translate(doc, "<x2>她</x2>被他<x1>拒绝</x1>了。")
        == '<p><a href="#n1">她</a>被他<em>拒绝</em>了。<br/></p>'
    )


def test_nested_placeholders_are_rebuilt():
    doc = parse('<p>A <b class="k">bold <i>and italic</i> text</b> end</p>')
    assert (
        translate(doc, "一段<x1><x2>又斜</x2>又粗的字</x1>结束")
        == '<p>一段<b class="k"><i>又斜</i>又粗的字</b>结束</p>'
    )


def test_note_reference_moves_with_the_sentence():
    doc = parse('<p>A claim<sup><a href="#fn1">1</a></sup> was made, and then more was said.</p>')
    assert (
        translate(doc, "有人提出了一个说法<x1/>，随后又说了更多。")
        == '<p>有人提出了一个说法<sup><a href="#fn1">1</a></sup>，随后又说了更多。</p>'
    )


def test_layout_whitespace_is_kept():
    doc = parse("<p>\n      Hello   wide\n      world\n    </p>")
    assert sources(doc) == ["Hello wide world"]
    assert translate(doc, "你好，世界") == "<p>\n      你好，世界\n    </p>"


def test_attribute_is_written_back():
    doc = parse('<p><img src="a.png" alt="A lighthouse"/></p>')
    assert translate(doc, "一座灯塔") == '<p><img src="a.png" alt="一座灯塔"/></p>'


def test_title_is_written_back():
    doc = parse("<p>Body text</p>", title="My Book")
    apply(doc.segments[0], "我的书")
    assert "<title>我的书</title>" in serialize_document(doc).decode()


def test_language_attributes_are_updated():
    doc = parse("<p>Body text</p>")
    out = serialize_document(doc, "zh-CN").decode()
    assert 'lang="zh-CN" xml:lang="zh-CN"' in out


def test_empty_elements_are_not_self_closed():
    doc = parse('<p><a id="p1"/>Some words here<br/></p><div/>')
    assert body_of(doc) == '<p><a id="p1"></a>Some words here<br/></p><div></div>'


# -- translations with broken placeholders -----------------------------------


def test_broken_placeholders_lose_formatting_but_never_break_the_markup():
    doc = parse(
        '<p>See <a href="n.html#a" id="back1">the note</a> and <em>this</em> picture'
        '<img src="i.png"/> for more.</p>'
    )
    seg = doc.segments[-1]
    assert seg.source == "See <x1>the note</x1> and <x2>this</x2> picture<x3/> for more."
    # x1 never closed, x2 missing, the image lost
    out = translate(doc, "参见<x1>注释和这张图片，了解更多。")
    assert out == (
        '<p><a href="n.html#a" id="back1"></a>参见注释和这张图片，了解更多。<img src="i.png"/></p>'
    )
    Document(serialize_document(doc))  # still well-formed


CHECKS = [
    ("译<x1>文</x1>和<x2/>", []),
    ("译<x2/>和<x1>文</x1>", []),
    ("译<x1>文</x1>和<x2></x2>", []),
    ("译文和<x2/>", ["placeholder x1 is missing"]),
    ("译<x1>文</x1>", ["placeholder x2 is missing"]),
    ("译<x1>文</x1><x1>文</x1>和<x2/>", ["placeholder x1 appears 2 times instead of once"]),
    ("译<x1>文</x1>和<x2/><x9/>", ["<x9> does not exist in the source segment"]),
    ("译<x1>文和<x2/>", ["<x1> is never closed"]),
    ("译<x1/>文和<x2/>", ["<x1> must wrap text as <x1>...</x1>, not be self-closing"]),
    ("译<x1></x1>文和<x2/>", ["<x1>...</x1> wraps no text"]),
    ("译<x1>文<x2/></x1>和", ["x2 must stay outside of the other placeholders"]),
]


@pytest.mark.parametrize("text,expected", CHECKS)
def test_check(text, expected):
    seg = Segment(1, "Source <x1>text</x1> and <x2/>", paired=frozenset({1}),
                  atoms=frozenset({2}), nesting={1: 0, 2: 0})
    assert seg.check(text) == expected


def test_check_nesting_must_be_kept():
    seg = Segment(1, "<x1>a <x2>b</x2></x1>", paired=frozenset({1, 2}), nesting={1: 0, 2: 1})
    assert seg.check("<x1>甲<x2>乙</x2></x1>") == []
    assert seg.check("<x1>甲</x1><x2>乙</x2>") == ["x2 must stay inside <x1>"]


def test_tokenize_tolerates_spacing():
    assert tokenize("a< x1 >b</ x1 >c<x2 />") == [
        ("text", "a"), ("open", 1), ("text", "b"), ("close", 1), ("text", "c"), ("atom", 2),
    ]


def test_salvage_keeps_words_and_opaque_objects():
    seg = Segment(1, "A <x1>b</x1> c<x2/>", paired=frozenset({1}), atoms=frozenset({2}),
                  nesting={1: 0, 2: 0})
    assert seg.salvage("甲<x1>乙</x1><x1>丙<x7/>") == "甲乙丙<x2/>"
    assert seg.salvage("甲<x2/>乙<x2/>") == "甲<x2/>乙"
