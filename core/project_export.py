"""Publication exporters kept separate from project state orchestration."""

from __future__ import annotations

import html
import re
import zipfile
from datetime import UTC, datetime
from pathlib import Path

from core.schemas import ChapterContent, NovelProjectState
from core.storage import atomic_output_path, atomic_write_text, ensure_directory, remove_file


def sanitize_export_component(name: str, *, max_length: int = 80) -> str:
    value = re.sub(r'[\\/*?:"<>|\x00-\x1f]', "_", str(name)).strip(" .")
    value = re.sub(r"\s+", " ", value)
    return (value or "未命名")[:max_length].rstrip(" .")


def export_markdown(base_dir: Path, novel_id: str, state: NovelProjectState) -> Path:
    output_file = base_dir / f"{novel_id}_全本.md"
    title = state.concept.title if state.concept else novel_id
    parts = [f"# 《{title}》\n\n"]
    if state.concept:
        parts.extend(
            [
                f"**题材**：{state.concept.genre}  \n",
                f"**简介**：{state.concept.logline}  \n",
                f"**金手指**：{state.concept.golden_finger}  \n\n",
                "---\n\n",
            ]
        )

    chapter_numbers = sorted(state.chapters)
    total_words = 0
    for chapter_number in chapter_numbers:
        chapter = state.chapters[chapter_number]
        total_words += chapter.word_count
        parts.extend(
            [
                f"## 第{chapter.chapter_number}章 {chapter.title}\n\n",
                f"{chapter.full_text}\n\n",
                "---\n\n",
            ]
        )
    parts.append(f"\n*全书共计 {len(chapter_numbers)} 章，约 {total_words} 字*\n")
    atomic_write_text(output_file, "".join(parts))
    return output_file


def _chapter_release_pattern(chapter_number: int) -> str:
    return f"第{chapter_number:03d}章_*.txt"


def export_platform_chapter(delivery_dir: Path, chapter: ChapterContent) -> Path:
    """Replace one generated per-chapter release without touching other chapters."""
    platform_dir = delivery_dir / "platform_txt_release"
    ensure_directory(platform_dir)
    safe_title = sanitize_export_component(chapter.title)
    output = platform_dir / f"第{chapter.chapter_number:03d}章_{safe_title}.txt"
    for stale in platform_dir.glob(_chapter_release_pattern(chapter.chapter_number)):
        if stale != output:
            remove_file(stale)
    atomic_write_text(
        output,
        f"第{chapter.chapter_number}章 {chapter.title}\n\n{chapter.full_text}\n",
    )
    return output


def export_platform_text(
    delivery_dir: Path, novel_id: str, state: NovelProjectState, *, sync_chapters: bool = True,
) -> Path:
    platform_dir = delivery_dir / "platform_txt_release"
    ensure_directory(platform_dir)
    chapter_numbers = sorted(state.chapters)

    if sync_chapters:
        expected_numbers = set(chapter_numbers)
        for stale in platform_dir.glob("第*.txt"):
            match = re.fullmatch(r"第(\d+)章_.+\.txt", stale.name)
            if match and int(match.group(1)) not in expected_numbers:
                remove_file(stale)
        for chapter_number in chapter_numbers:
            export_platform_chapter(delivery_dir, state.chapters[chapter_number])

    title = state.concept.title if state.concept else novel_id
    parts = [f"《{title}》\n\n"]
    for chapter_number in chapter_numbers:
        chapter = state.chapters[chapter_number]
        parts.append(f"第{chapter.chapter_number}章 {chapter.title}\n\n{chapter.full_text}\n\n\n")
    atomic_write_text(delivery_dir / f"{novel_id}_全本发布稿.txt", "".join(parts))
    return delivery_dir


def export_epub(base_dir: Path, novel_id: str, state: NovelProjectState) -> Path:
    """Build an EPUB3 file and publish it atomically."""
    epub_path = base_dir / f"{novel_id}.epub"
    title = state.concept.title if state.concept else novel_id
    container_xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
        '<rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>'
        "</rootfiles></container>"
    )
    style_css = "body{font-family:serif;line-height:1.9;margin:1.2em}h1{font-size:1.35em}p{text-indent:2em;margin:.4em 0}"

    def chapter_xhtml(chapter_title: str, body_text: str) -> str:
        paragraphs = "".join(
            f"<p>{html.escape(paragraph.strip())}</p>"
            for paragraph in body_text.split("\n\n")
            if paragraph.strip()
        )
        return (
            '<?xml version="1.0" encoding="UTF-8"?>'
            "<!DOCTYPE html>"
            '<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="zh-CN"><head>'
            f"<title>{html.escape(chapter_title)}</title>"
            '<link rel="stylesheet" type="text/css" href="style.css"/>'
            "</head><body>"
            f"<h1>{html.escape(chapter_title)}</h1>{paragraphs}</body></html>"
        )

    chapter_numbers = sorted(state.chapters)
    manifest: list[str] = []
    spine: list[str] = []
    toc_items: list[str] = []
    for chapter_number in chapter_numbers:
        chapter = state.chapters[chapter_number]
        file_id = f"chap_{chapter_number:04d}"
        manifest.append(f'<item id="{file_id}" href="{file_id}.xhtml" media-type="application/xhtml+xml"/>')
        spine.append(f'<itemref idref="{file_id}"/>')
        label = html.escape(f"第{chapter.chapter_number}章 {chapter.title}")
        toc_items.append(f'<li><a href="{file_id}.xhtml">{label}</a></li>')

    modified = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    content_opf = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="bookid">'
        '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
        f'<dc:identifier id="bookid">urn:moirai:{html.escape(novel_id)}</dc:identifier>'
        f"<dc:title>{html.escape(title)}</dc:title>"
        "<dc:language>zh-CN</dc:language>"
        f'<meta property="dcterms:modified">{modified}</meta>'
        "</metadata><manifest>"
        '<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>'
        '<item id="css" href="style.css" media-type="text/css"/>'
        + "".join(manifest)
        + "</manifest><spine>"
        + "".join(spine)
        + "</spine></package>"
    )
    nav_xhtml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        "<!DOCTYPE html>"
        '<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">'
        '<head><title>目录</title><link rel="stylesheet" type="text/css" href="style.css"/></head>'
        '<body><nav epub:type="toc"><h1>目录</h1><ol>'
        + "".join(toc_items)
        + "</ol></nav></body></html>"
    )

    with atomic_output_path(epub_path) as temp_path, zipfile.ZipFile(temp_path, "w") as archive:
        archive.writestr(
            zipfile.ZipInfo("mimetype"),
            "application/epub+zip",
            compress_type=zipfile.ZIP_STORED,
        )
        archive.writestr("META-INF/container.xml", container_xml, compress_type=zipfile.ZIP_DEFLATED)
        archive.writestr("OEBPS/content.opf", content_opf, compress_type=zipfile.ZIP_DEFLATED)
        archive.writestr("OEBPS/nav.xhtml", nav_xhtml, compress_type=zipfile.ZIP_DEFLATED)
        archive.writestr("OEBPS/style.css", style_css, compress_type=zipfile.ZIP_DEFLATED)
        for chapter_number in chapter_numbers:
            chapter = state.chapters[chapter_number]
            archive.writestr(
                f"OEBPS/chap_{chapter_number:04d}.xhtml",
                chapter_xhtml(f"第{chapter.chapter_number}章 {chapter.title}", chapter.full_text),
                compress_type=zipfile.ZIP_DEFLATED,
            )
    return epub_path
