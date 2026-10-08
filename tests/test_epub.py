from __future__ import annotations

import zipfile

import pytest
from conftest import build_epub

from epub_translator.epub import Book, EpubError, parse_xml, serialize_xml


def members(path) -> dict[str, bytes]:
    with zipfile.ZipFile(path) as z:
        return {name: z.read(name) for name in z.namelist()}


def test_open_reads_the_package(epub):
    with Book(epub) as book:
        assert book.opf_path == "OEBPS/content.opf"
        assert [item.path for item in book.spine] == [
            "OEBPS/ch1.xhtml", "OEBPS/ch2.xhtml", "OEBPS/notes.xhtml",
        ]
        assert book.nav.path == "OEBPS/nav.xhtml"
        assert book.ncx.path == "OEBPS/toc.ncx"
        assert book.title == "The Stormy Night"
        assert book.language == "en"


def test_write_without_replacements_keeps_every_member(epub, tmp_path):
    out = tmp_path / "copy.epub"
    with Book(epub) as book:
        book.write(out, {})
    assert members(out) == members(epub)
    with zipfile.ZipFile(out) as z:
        first = z.infolist()[0]
        assert first.filename == "mimetype" and first.compress_type == zipfile.ZIP_STORED
        assert not first.extra
        png = z.getinfo("OEBPS/cover.png")
        assert png.compress_type == zipfile.ZIP_STORED  # stored in the source, stored in the copy
        assert z.getinfo("OEBPS/ch1.xhtml").compress_type == zipfile.ZIP_DEFLATED


def test_write_replaces_only_what_it_is_given(epub, tmp_path):
    out = tmp_path / "copy.epub"
    with Book(epub) as book:
        book.write(out, {"OEBPS/ch2.xhtml": b"<html/>"})
    before, after = members(epub), members(out)
    assert after.pop("OEBPS/ch2.xhtml") == b"<html/>"
    before.pop("OEBPS/ch2.xhtml")
    assert after == before


def test_write_puts_mimetype_first(tmp_path):
    src = build_epub(tmp_path / "book.epub")
    shuffled = tmp_path / "shuffled.epub"
    with zipfile.ZipFile(src) as a, zipfile.ZipFile(shuffled, "w", zipfile.ZIP_DEFLATED) as b:
        for info in sorted(a.infolist(), key=lambda i: i.filename == "mimetype"):
            b.writestr(info.filename, a.read(info.filename))
    out = tmp_path / "fixed.epub"
    with Book(shuffled) as book:
        book.write(out, {})
    with zipfile.ZipFile(out) as z:
        first = z.infolist()[0]
        assert first.filename == "mimetype" and first.compress_type == zipfile.ZIP_STORED


def test_write_refuses_to_overwrite_the_source(epub):
    with Book(epub) as book, pytest.raises(EpubError):
        book.write(epub, {})


def test_not_an_epub(tmp_path):
    path = tmp_path / "book.epub"
    path.write_bytes(b"this is not a zip file")
    with pytest.raises(EpubError):
        Book(path)


ENCRYPTION = """<?xml version="1.0"?>
<encryption xmlns="urn:oasis:names:tc:opendocument:xmlns:container"
            xmlns:enc="http://www.w3.org/2001/04/xmlenc#">
  <enc:EncryptedData>
    <enc:EncryptionMethod Algorithm="{algorithm}"/>
    <enc:CipherData><enc:CipherReference URI="OEBPS/ch1.xhtml"/></enc:CipherData>
  </enc:EncryptedData>
</encryption>
"""


def test_drm_is_refused(tmp_path):
    drm = ENCRYPTION.format(algorithm="http://www.w3.org/2001/04/xmlenc#aes128-cbc")
    path = build_epub(tmp_path / "book.epub", extra={"META-INF/encryption.xml": drm})
    with pytest.raises(EpubError, match="DRM"):
        Book(path)


def test_font_obfuscation_is_not_drm(tmp_path):
    fonts = ENCRYPTION.format(algorithm="http://www.idpf.org/2008/embedding")
    path = build_epub(tmp_path / "book.epub", extra={"META-INF/encryption.xml": fonts})
    with Book(path) as book:
        assert len(book.spine) == 3


def test_hrefs_are_decoded_and_resolved(tmp_path):
    chapters = {"text/my chapter.xhtml": ("One", "<p>Some words here</p>")}
    path = build_epub(tmp_path / "book.epub", chapters)
    # the manifest of the fixture writes the href as it is; make it percent-encoded
    with zipfile.ZipFile(path) as z:
        data = {name: z.read(name) for name in z.namelist()}
    data["OEBPS/content.opf"] = data["OEBPS/content.opf"].replace(
        b"text/my chapter.xhtml", b"./text/../text/my%20chapter.xhtml"
    )
    with zipfile.ZipFile(path, "w") as z:
        for name, content in data.items():
            z.writestr(name, content)
    with Book(path) as book:
        assert [item.path for item in book.spine] == ["OEBPS/text/my chapter.xhtml"]


@pytest.mark.parametrize(
    "data",
    [
        b"<?xml version='1.0' encoding='utf-8'?>\n<a><b/></a>\n",
        b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<a><b/></a>',
        b"<a><b/></a>\n",
    ],
)
def test_serialize_xml_keeps_the_declaration_as_it_was(data):
    out = serialize_xml(parse_xml(data), data)
    assert out.startswith(b"<?xml") == data.startswith(b"<?xml")
    assert (b"standalone" in out) == (b"standalone" in data)
    assert out.endswith(b"\n") == data.endswith(b"\n")
    assert b"<a><b/></a>" in out
