"""Produce a launchable repository snapshot without data or local configuration."""

import argparse
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_DIRECTORIES = ("agents", "api", "config", "core", "pipeline", "services", "web/assets")
_ROOT_FILES = ("README.md", "ARCHITECTURE.md", ".env.example", "main.py", "web_ui.py", "requirements.lock", "requirements.txt")
_LAUNCH_FILES = ("launch.py", "launch.command", "launch.bat", "serve_demo.py")


def package_release(destination: Path) -> None:
    files = [ROOT / item for item in _ROOT_FILES]
    files.extend(ROOT / "scripts" / item for item in _LAUNCH_FILES)
    files.append(ROOT / "web" / "index.html")
    files.append(ROOT / "core" / "han_repertoire.json")
    for directory in _DIRECTORIES:
        files.extend(
            path for path in (ROOT / directory).rglob("*")
            if path.is_file() and (directory == "web/assets" or path.suffix == ".py")
            and "__pycache__" not in path.parts and not path.name.startswith(".")
        )
    with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in sorted(files):
            if path.is_symlink() or not path.resolve().is_relative_to(ROOT):
                raise ValueError(f"Release contains an unsupported link: {path.name}")
            archive.write(path, Path("Moirai") / path.relative_to(ROOT))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    if args.output.suffix.casefold() != ".zip":
        parser.error("输出文件必须以 .zip 结尾")
    if args.output.exists():
        parser.error("输出文件已存在，请指定新的 ZIP 路径")
    package_release(args.output)


if __name__ == "__main__":
    main()
