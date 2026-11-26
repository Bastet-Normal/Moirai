"""Plain-text manuscript interchange through the main Word document part."""

import io
import re
import xml.etree.ElementTree as ET
import zipfile
import zlib
from pathlib import Path

from core.schemas import NovelProjectState

DOCX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
MAX_DOCUMENT_BYTES = 16 * 1024 * 1024
_WORD_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_STRICT_WORD_NS = "http://purl.oclc.org/ooxml/wordprocessingml/main"
_PART = "word/document.xml"
_INVALID_XML = re.compile(r"[^\x09\x0a\x0d\x20-\ud7ff\ue000-\ufffd\U00010000-\U0010ffff]")


class InvalidManuscriptDocument(ValueError):
    """The uploaded container cannot be used as a Word manuscript."""


def read_docx(data: bytes) -> list[dict]:
    """Extract paragraphs and heading hints without resolving relationships."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            matches = [entry for entry in archive.infolist() if entry.filename == _PART]
            if len(matches) != 1 or matches[0].file_size > MAX_DOCUMENT_BYTES:
                raise InvalidManuscriptDocument("DOCX 缺少正文，或解压后的正文超过 16 MiB")
            with archive.open(matches[0]) as document:
                xml = document.read(MAX_DOCUMENT_BYTES + 1)
        encoding = "utf-16" if xml.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8-sig"
        source = xml.decode(encoding)
        if len(xml) > MAX_DOCUMENT_BYTES or "\x00" in source or re.search(r"<!\s*(DOCTYPE|ENTITY)", source, re.I):
            raise InvalidManuscriptDocument("DOCX 正文使用了不支持的 XML 声明")
        # The decoded XML above rejects DTDs and entity declarations.
        root = ET.fromstring(source)  # nosec B314
    except (zipfile.BadZipFile, NotImplementedError, RuntimeError, UnicodeError, ET.ParseError, OSError, EOFError, zlib.error) as exc:
        raise InvalidManuscriptDocument("无法读取 DOCX，请用 Word 或兼容编辑器另存后重试") from exc
    ns = next((uri for uri in (_WORD_NS, _STRICT_WORD_NS) if root.tag == f"{{{uri}}}document"), None)
    body = root.find(f"{{{ns}}}body") if ns else None
    if body is None:
        raise InvalidManuscriptDocument("DOCX 缺少可读的正文")
    word = f"{{{ns}}}"
    paragraphs = []
    for paragraph in body.iter(word + "p"):
        parts: list[str] = []
        for node in paragraph.iter():
            if node.tag == word + "t" and node.text:
                parts.append(node.text)
            elif node.tag == word + "tab":
                parts.append("\t")
            elif node.tag in {word + "br", word + "cr"}:
                parts.append("\n")
        style = paragraph.find(f"{word}pPr/{word}pStyle")
        style_name = style.get(word + "val", "") if style is not None else ""
        heading = bool(re.fullmatch(r"(heading|标题|titre|überschrift)[ _-]*[1-9]", style_name, re.I))
        text = "".join(parts)
        is_title = not paragraphs and style_name.casefold() == "title" and bool(text.strip())
        paragraphs.append({"text": text, "heading": heading, "document_title": is_title})
    if not any(p["text"].strip() and not p["document_title"] for p in paragraphs):
        raise InvalidManuscriptDocument("DOCX 没有可导入的文字")
    return paragraphs


def export_docx(directory: Path, project_id: str, state: NovelProjectState) -> Path:
    """Create an editable Word document with a title and navigable chapters."""
    ns = f"{{{_WORD_NS}}}"
    ET.register_namespace("w", _WORD_NS)
    doc = ET.Element(ns + "document")
    body = ET.SubElement(doc, ns + "body")

    def paragraph(value: str, style: str = "") -> None:
        node = ET.SubElement(body, ns + "p")
        if style:
            props = ET.SubElement(node, ns + "pPr")
            ET.SubElement(props, ns + "pStyle", {ns + "val": style})
        if value:
            run = ET.SubElement(node, ns + "r")
            for index, part in enumerate(_INVALID_XML.sub("�", value).split("\t")):
                if index:
                    ET.SubElement(run, ns + "tab")
                ET.SubElement(run, ns + "t", {"{http://www.w3.org/XML/1998/namespace}space": "preserve"}).text = part

    paragraph(state.concept.title if state.concept else project_id, "Title")
    for chapter in sorted(state.chapters.values(), key=lambda item: item.chapter_number):
        paragraph(f"第{chapter.chapter_number}章 {chapter.title}", "Heading1")
        for line in chapter.full_text.split("\n"):
            paragraph(line.removesuffix("\r"))

    output = directory / f"{project_id}_交稿.docx"
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", _CONTENT_TYPES)
        archive.writestr("_rels/.rels", _ROOT_RELS)
        archive.writestr("word/_rels/document.xml.rels", _DOC_RELS)
        archive.writestr("word/styles.xml", _STYLES)
        archive.writestr(_PART, ET.tostring(doc, encoding="utf-8", xml_declaration=True))
    return output


_CONTENT_TYPES = """<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
<Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>
</Types>"""
_ROOT_RELS = """<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>"""
_DOC_RELS = """<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>
</Relationships>"""
_STYLES = f"""<w:styles xmlns:w="{_WORD_NS}">
<w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="Normal"/></w:style>
<w:style w:type="paragraph" w:styleId="Title"><w:name w:val="Title"/><w:rPr><w:b/><w:sz w:val="38"/></w:rPr></w:style>
<w:style w:type="paragraph" w:styleId="Heading1"><w:name w:val="heading 1"/><w:pPr><w:keepNext/><w:outlineLvl w:val="0"/></w:pPr><w:rPr><w:b/><w:sz w:val="30"/></w:rPr></w:style>
</w:styles>"""
