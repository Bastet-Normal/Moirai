"""Open Moirai or its isolated demo with the locked runtime."""

import argparse
import hashlib
import os
import subprocess
import sys
import threading
import time
import venv
import webbrowser
from pathlib import Path
from urllib.error import URLError
from urllib.request import HTTPHandler, ProxyHandler, build_opener

ROOT = Path(__file__).resolve().parents[1]
LOCK = ROOT / "requirements.lock"


def prepare_environment(directory: Path) -> Path:
    executable = directory / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    stamp = directory / ".moirai-runtime"
    expected = hashlib.sha256(LOCK.read_bytes()).hexdigest() + f"\n{sys.version_info.major}.{sys.version_info.minor}\n"
    if executable.is_file() and stamp.is_file() and stamp.read_text(encoding="utf-8") == expected:
        return executable
    if not executable.is_file():
        venv.EnvBuilder(with_pip=True).create(directory)
    print("正在安装或更新 Moirai 的锁定依赖。首次运行需要联网。", flush=True)
    subprocess.run(
        [str(executable), "-m", "pip", "install", "--require-hashes", "-r", str(LOCK)],
        check=True, cwd=ROOT,
    )
    stamp.write_text(expected, encoding="utf-8")
    return executable


def open_browser_when_ready(url: str, finished: threading.Event) -> None:
    opener = build_opener(ProxyHandler({}), HTTPHandler())
    for _ in range(100):
        if finished.is_set():
            return
        try:
            with opener.open(url, timeout=0.4) as result:
                if result.status == 200 and result.headers.get("X-Moirai") == "studio":
                    webbrowser.open(url)
                    return
        except (OSError, URLError):
            time.sleep(0.15)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--demo", action="store_true", help="打开不调用外部模型的临时样书")
    parser.add_argument("--port", type=int, help="本机端口，正式创作台默认 8000，样书默认 8766")
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--current-python", action="store_true", help="使用已安装锁定依赖的当前解释器")
    args = parser.parse_args()
    if sys.version_info < (3, 11):  # noqa: UP036 - users can launch with an older system interpreter
        parser.error("Moirai 需要 Python 3.11 或更新版本")
    if args.port is not None and not 1 <= args.port <= 65535:
        parser.error("端口必须在 1–65535 之间")
    port = args.port or (8766 if args.demo else 8000)
    executable = Path(sys.executable) if args.current_python else prepare_environment(ROOT / ".venv")
    script = ROOT / ("scripts/serve_demo.py" if args.demo else "web_ui.py")
    url = f"http://127.0.0.1:{port}/"
    print(f"Moirai{' 离线样书' if args.demo else ''} · {url}", flush=True)
    finished = threading.Event()
    child = subprocess.Popen([str(executable), str(script), "--port", str(port)], cwd=ROOT)
    if not args.no_browser:
        threading.Thread(target=open_browser_when_ready, args=(url, finished), daemon=True).start()
    try:
        return child.wait()
    except KeyboardInterrupt:
        # The terminal also sends Ctrl-C to the child, allowing Uvicorn and the
        # demo's temporary directory to finish their normal shutdown.
        try:
            child.wait(timeout=10)
        except subprocess.TimeoutExpired:
            child.terminate()
            child.wait()
        return 130
    finally:
        finished.set()


if __name__ == "__main__":
    raise SystemExit(main())
