from __future__ import annotations

import sys
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

XHTML = """<?xml version='1.0' encoding='utf-8'?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" lang="en" xml:lang="en">
  <head>
    <title>{title}</title>
    <link href="style.css" rel="stylesheet" type="text/css"/>
  </head>
  <body>{body}</body>
</html>
"""

CONTAINER = """<?xml version="1.0" encoding="UTF-8"?>
<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container" version="1.0">
  <rootfiles>
    <rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>
  </rootfiles>
</container>
"""

CHAPTERS = {
    "ch1.xhtml": ("Chapter One", """
    <h1 id="c1">Chapter One</h1>
    <p>It was a <em>dark</em> and stormy night, wrote the author in the opening line.<a id="fnref1" href="notes.xhtml#fn1"><sup>1</sup></a></p>
    <p>See <a href="ch2.xhtml#c2">the next chapter</a> for what happened after the storm had passed.</p>
    <pre>print("never translated")</pre>
    <p><img src="cover.png" alt="A lighthouse in the rain"/></p>
  """),
    "ch2.xhtml": ("Chapter Two", """
    <h1 id="c2">Chapter Two</h1>
    <p>The morning came slowly, and with it the long walk back to the harbour town.</p>
  """),
    "notes.xhtml": ("Notes", """
    <h1>Notes</h1>
    <p id="fn1"><a href="ch1.xhtml#fnref1">1</a> A phrase borrowed from an old and much mocked novel.</p>
  """),
}


def page(title: str, body: str) -> bytes:
    return XHTML.format(title=title, body=body).encode()


def build_epub(path: Path, chapters: dict[str, tuple[str, str]] | None = None, *, extra=None) -> Path:
    """Write a small but complete EPUB 3 with a nav document and an NCX."""
    chapters = chapters or CHAPTERS
    names = list(chapters)
    manifest = "\n".join(
        f'    <item id="d{i}" href="{name}" media-type="application/xhtml+xml"/>'
        for i, name in enumerate(names)
    )
    spine = "\n".join(f'    <itemref idref="d{i}"/>' for i in range(len(names)))
    opf = f"""<?xml version="1.0" encoding="UTF-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="uid">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:identifier id="uid">urn:uuid:0001</dc:identifier>
    <dc:title>The Stormy Night</dc:title>
    <dc:language>en</dc:language>
  </metadata>
  <manifest>
{manifest}
    <item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>
    <item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>
    <item id="css" href="style.css" media-type="text/css"/>
    <item id="img" href="cover.png" media-type="image/png"/>
  </manifest>
  <spine toc="ncx">
{spine}
  </spine>
</package>
"""
    nav_items = "\n".join(
        f'        <li><a href="{name}">{title}</a></li>' for name, (title, _) in chapters.items()
    )
    nav = XHTML.format(
        title="The Stormy Night",
        body=f"""
    <nav epub:type="toc" id="toc">
      <h2>Contents</h2>
      <ol>
{nav_items}
      </ol>
    </nav>
  """,
    )
    points = "\n".join(
        f'    <navPoint id="n{i}" playOrder="{i + 1}"><navLabel><text>{title}</text></navLabel>'
        f'<content src="{name}"/></navPoint>'
        for i, (name, (title, _)) in enumerate(chapters.items())
    )
    ncx = f"""<?xml version="1.0" encoding="UTF-8"?>
<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">
  <head><meta name="dtb:uid" content="urn:uuid:0001"/></head>
  <docTitle><text>The Stormy Night</text></docTitle>
  <navMap>
{points}
  </navMap>
</ncx>
"""
    with zipfile.ZipFile(path, "w") as z:
        z.writestr(zipfile.ZipInfo("mimetype"), "application/epub+zip")
        z.writestr("META-INF/container.xml", CONTAINER, zipfile.ZIP_DEFLATED)
        z.writestr("OEBPS/content.opf", opf, zipfile.ZIP_DEFLATED)
        z.writestr("OEBPS/nav.xhtml", nav, zipfile.ZIP_DEFLATED)
        z.writestr("OEBPS/toc.ncx", ncx, zipfile.ZIP_DEFLATED)
        z.writestr("OEBPS/style.css", "p { margin: 0 }", zipfile.ZIP_DEFLATED)
        z.writestr("OEBPS/cover.png", b"\x89PNG\r\n\x1a\n" + bytes(range(64)))
        for name, (title, body) in chapters.items():
            z.writestr(f"OEBPS/{name}", page(title, body), zipfile.ZIP_DEFLATED)
        for name, data in (extra or {}).items():
            z.writestr(name, data, zipfile.ZIP_DEFLATED)
    return path


@pytest.fixture
def epub(tmp_path: Path) -> Path:
    return build_epub(tmp_path / "book.epub")
