"""Read an EPUB container and write it back with some members replaced.

The EPUB is treated as a plain zip archive: only the members we are handed
replacements for change, every other member keeps its exact content.
"""

from __future__ import annotations

import io
import os
import posixpath
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote

from lxml import etree

CONTAINER_NS = "urn:oasis:names:tc:opendocument:xmlns:container"
OPF_NS = "http://www.idpf.org/2007/opf"
DC_NS = "http://purl.org/dc/elements/1.1/"
ENC_NS = "http://www.w3.org/2001/04/xmlenc#"

MIMETYPE = "mimetype"
CONTENT_TYPES = {"application/xhtml+xml", "text/html"}
CONTENT_EXTENSIONS = {".xhtml", ".html", ".htm", ".xml"}
# Font obfuscation is not DRM: the text stays readable.
FONT_OBFUSCATION = {
    "http://www.idpf.org/2008/embedding",
    "http://ns.adobe.com/pdf/enc#RC",
}


_DECLARATION = re.compile(rb"(?:\xef\xbb\xbf)?\s*<\?xml\s[^>]*\?>")
_STANDALONE = re.compile(rb"""standalone\s*=\s*["'](yes|no)["']""")


# href="..." and src="..." in markup, url(...) in style sheets
_REFERENCE = re.compile(r"""(?:href|src)\s*=\s*["']([^"']+)["']|url\(\s*["']?([^"')]+)""")


def _remove(el: etree._Element) -> None:
    """Remove an element together with the whitespace that followed it."""
    parent = el.getparent()
    before = el.getprevious()
    if before is not None:
        before.tail = el.tail
    else:
        parent.text = el.tail
    parent.remove(el)


class EpubError(Exception):
    """The file is not an EPUB we can work with."""


@dataclass(frozen=True)
class ManifestItem:
    id: str
    path: str  # member name inside the zip
    media_type: str
    properties: frozenset[str]

    @property
    def is_content(self) -> bool:
        if self.media_type in CONTENT_TYPES:
            return True
        ext = posixpath.splitext(self.path)[1].lower()
        return not self.media_type and ext in CONTENT_EXTENSIONS


def parse_xml(data: bytes) -> etree._ElementTree:
    parser = etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False)
    return etree.parse(io.BytesIO(data), parser)


def serialize_xml(tree: etree._ElementTree, original: bytes) -> bytes:
    """Serialize `tree`, keeping the declaration and trailing whitespace of `original`."""
    declaration = _DECLARATION.match(original)
    flag = _STANDALONE.search(declaration.group(0)) if declaration else None
    standalone = flag.group(1) == b"yes" if flag else None
    out = etree.tostring(
        tree,
        encoding="utf-8" if (tree.docinfo.encoding or "utf-8").islower() else "UTF-8",
        xml_declaration=declaration is not None,
        standalone=standalone,
    )
    tail = original[len(original.rstrip(b" \t\r\n")):]
    return out + tail


def resolve(base: str, href: str) -> str:
    """Zip member name for `href` as written in the file `base`."""
    href = unquote(href.split("#", 1)[0])
    return posixpath.normpath(posixpath.join(posixpath.dirname(base), href))


class Book:
    def __init__(self, path: Path):
        self.path = Path(path)
        try:
            self._zip = zipfile.ZipFile(self.path)
        except (zipfile.BadZipFile, OSError) as exc:
            raise EpubError(f"无法打开 {self.path.name}：{exc}") from exc
        self._names = set(self._zip.namelist())
        self._check_drm()
        self.opf_path = self._find_opf()
        self.opf_data = self.read(self.opf_path)
        try:
            self.opf = parse_xml(self.opf_data)
        except etree.XMLSyntaxError as exc:
            raise EpubError(f"OPF 文件无法解析：{exc}") from exc
        self.manifest = self._read_manifest()
        self.spine = self._read_spine()
        self.removed: set[str] = set()  # members left out when the book is written
        self.nav = next((i for i in self.manifest.values() if "nav" in i.properties), None)
        self.ncx = self._find_ncx()

    def close(self) -> None:
        self._zip.close()

    def __enter__(self) -> Book:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- reading -----------------------------------------------------------

    def has(self, name: str) -> bool:
        return name in self._names

    def read(self, name: str) -> bytes:
        try:
            return self._zip.read(name)
        except KeyError as exc:
            raise EpubError(f"EPUB 中缺少文件 {name}") from exc

    def _check_drm(self) -> None:
        name = "META-INF/encryption.xml"
        if name not in self._names:
            return
        try:
            root = etree.fromstring(self._zip.read(name))
        except etree.XMLSyntaxError as exc:
            raise EpubError(f"encryption.xml 无法解析：{exc}") from exc
        for method in root.iter(f"{{{ENC_NS}}}EncryptionMethod"):
            if method.get("Algorithm") not in FONT_OBFUSCATION:
                raise EpubError("这本书带有 DRM 加密，无法处理。请使用无 DRM 的 EPUB。")

    def _find_opf(self) -> str:
        try:
            root = etree.fromstring(self.read("META-INF/container.xml"))
        except etree.XMLSyntaxError as exc:
            raise EpubError(f"container.xml 无法解析：{exc}") from exc
        rootfile = root.find(f".//{{{CONTAINER_NS}}}rootfile")
        if rootfile is None or not rootfile.get("full-path"):
            raise EpubError("container.xml 中没有 rootfile")
        return rootfile.get("full-path")

    def _read_manifest(self) -> dict[str, ManifestItem]:
        items = {}
        for el in self.opf.getroot().iter(f"{{{OPF_NS}}}item"):
            if not el.get("id") or not el.get("href"):
                continue
            items[el.get("id")] = ManifestItem(
                id=el.get("id"),
                path=resolve(self.opf_path, el.get("href")),
                media_type=(el.get("media-type") or "").strip().lower(),
                properties=frozenset((el.get("properties") or "").split()),
            )
        return items

    def _read_spine(self) -> list[ManifestItem]:
        """Content documents in reading order."""
        spine = []
        for ref in self.opf.getroot().iter(f"{{{OPF_NS}}}itemref"):
            item = self.manifest.get(ref.get("idref") or "")
            if item is not None and item.is_content and self.has(item.path):
                spine.append(item)
        return spine

    def _find_ncx(self) -> ManifestItem | None:
        spine = self.opf.getroot().find(f"{{{OPF_NS}}}spine")
        if spine is not None and spine.get("toc") in self.manifest:
            return self.manifest[spine.get("toc")]
        return next(
            (i for i in self.manifest.values() if i.media_type == "application/x-dtbncx+xml"),
            None,
        )

    # -- metadata ----------------------------------------------------------

    def _dc(self, name: str) -> list[etree._Element]:
        return list(self.opf.getroot().iter(f"{{{DC_NS}}}{name}"))

    @property
    def title_elements(self) -> list[etree._Element]:
        return [el for el in self._dc("title") if (el.text or "").strip()]

    @property
    def title(self) -> str | None:
        titles = self.title_elements
        return titles[0].text.strip() if titles else None

    @property
    def language(self) -> str | None:
        langs = [el.text.strip() for el in self._dc("language") if (el.text or "").strip()]
        return langs[0] if langs else None

    def set_language(self, lang: str) -> None:
        langs = self._dc("language")
        if langs:
            langs[0].text = lang

    # -- removing ----------------------------------------------------------

    def drop_documents(self, names: set[str], missing_ok: bool = False) -> list[str]:
        """Take documents out of the book, by path or by file name.

        Images and other files that only those documents used go with them.
        Returns the members removed. A name the book does not have is an
        error unless `missing_ok`.
        """
        doomed = [
            item for item in self.spine
            if item.path in names or posixpath.basename(item.path) in names
        ]
        found = {item.path for item in doomed} | {posixpath.basename(i.path) for i in doomed}
        missing = sorted(names - found)
        if missing and not missing_ok:
            raise EpubError(f"书脊里没有这个文档：{', '.join(missing)}")
        gone = {item.path for item in doomed}
        # What the dropped documents point to, minus what anything else still uses.
        orphans = self._references(gone) - gone
        keepers = {i.path for i in self.manifest.values() if i.path not in gone}
        text_like = {i.path for i in self.manifest.values() if i.path not in gone and (
            i.is_content or i.media_type in ("text/css", "application/x-dtbncx+xml")
        )}
        orphans &= keepers
        orphans -= self._references(text_like)
        orphans -= {i.path for i in self.manifest.values() if "cover-image" in i.properties}
        cover = self.opf.getroot().find(f".//{{{OPF_NS}}}meta[@name='cover']")
        if cover is not None and cover.get("content") in self.manifest:
            orphans.discard(self.manifest[cover.get("content")].path)

        removed = gone | orphans
        ids = {i.id for i in self.manifest.values() if i.path in removed}
        for tag, attr in (("item", "id"), ("itemref", "idref")):
            for el in list(self.opf.getroot().iter(f"{{{OPF_NS}}}{tag}")):
                if el.get(attr) in ids:
                    _remove(el)
        for el in list(self.opf.getroot().iter(f"{{{OPF_NS}}}reference")):
            if resolve(self.opf_path, el.get("href") or "") in removed:
                _remove(el)
        self.manifest = {k: v for k, v in self.manifest.items() if k not in ids}
        self.spine = [item for item in self.spine if item.path not in removed]
        self.removed |= removed
        return sorted(removed)

    def _references(self, members: set[str]) -> set[str]:
        """Members of the archive that the given text files point to."""
        found: set[str] = set()
        for name in members:
            if not self.has(name):
                continue
            text = self.read(name).decode("utf-8", "replace")
            for ref in _REFERENCE.findall(text):
                ref = ref[0] or ref[1]
                if ref and "://" not in ref and not ref.startswith(("#", "data:", "mailto:")):
                    target = resolve(name, ref)
                    if self.has(target):
                        found.add(target)
        return found

    # -- writing -----------------------------------------------------------

    def write(self, out: Path, replacements: dict[str, bytes]) -> None:
        """Write a copy of the book to `out` with the given members replaced."""
        out = Path(out)
        if out.resolve() == self.path.resolve():
            raise EpubError("输出文件不能与原文件相同")
        # The mimetype member must come first and be stored uncompressed.
        infos = sorted(self._zip.infolist(), key=lambda info: info.filename != MIMETYPE)
        tmp = out.with_name(out.name + ".tmp")
        try:
            with zipfile.ZipFile(tmp, "w") as dst:
                for info in infos:
                    if info.is_dir() or info.filename in self.removed:
                        continue
                    data = replacements.get(info.filename)
                    if data is None:
                        data = self._zip.read(info.filename)
                    new = zipfile.ZipInfo(info.filename, date_time=info.date_time)
                    new.external_attr = info.external_attr
                    stored = info.filename == MIMETYPE or info.compress_type == zipfile.ZIP_STORED
                    new.compress_type = zipfile.ZIP_STORED if stored else zipfile.ZIP_DEFLATED
                    dst.writestr(new, data)
            os.replace(tmp, out)
        finally:
            tmp.unlink(missing_ok=True)
