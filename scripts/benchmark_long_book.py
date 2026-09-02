"""Offline 1,000-chapter publication benchmark; no model or user project access."""

from __future__ import annotations

import argparse
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

def main() -> int:
    from core.project_export import export_markdown, export_platform_chapter, export_platform_text
    from core.schemas import ChapterContent, NovelProjectState

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--chapters", type=int, default=1_000)
    parser.add_argument("--chars", type=int, default=2_500)
    args = parser.parse_args()
    if not 1 <= args.chapters <= 10_000 or not 1 <= args.chars <= 20_000:
        parser.error("chapters must be 1..10000 and chars must be 1..20000")

    state = NovelProjectState(novel_id="benchmark")
    start = time.perf_counter()
    with tempfile.TemporaryDirectory(prefix="novelengine-benchmark-") as temporary:
        root = Path(temporary)
        delivery = root / "delivery_package"
        for number in range(1, args.chapters + 1):
            text = (f"第{number}章基准正文" + "文" * args.chars)[: args.chars]
            chapter = ChapterContent(
                chapter_number=number, volume_number=1, title=f"基准 {number}",
                word_count=len(text), full_text=text,
            )
            state.chapters[number] = chapter
            export_platform_chapter(delivery, chapter)
        markdown = export_markdown(root, "benchmark", state)
        export_platform_text(delivery, "benchmark", state, sync_chapters=False)
        full_text = delivery / "benchmark_全本发布稿.txt"
        release_count = len(list((delivery / "platform_txt_release").glob("第*.txt")))
        elapsed = time.perf_counter() - start
        expected_tail = f"第{args.chapters}章 基准 {args.chapters}"
        if release_count != args.chapters or expected_tail not in full_text.read_text(encoding="utf-8"):
            raise RuntimeError("benchmark export is incomplete")
        print(
            f"chapters={args.chapters} chars_per_chapter={args.chars} elapsed={elapsed:.3f}s "
            f"markdown_bytes={markdown.stat().st_size} txt_bytes={full_text.stat().st_size} "
            f"release_files={release_count}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
