"""Run the reproducible local quality gate without invoking a model provider.

Usage: python scripts/check.py [--audit]
The optional vulnerability audit needs network access; ordinary checks do not.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

_SOURCE_DIRS = (
    ".github", "agents", "api", "config", "core", "pipeline", "services", "examples", "scripts", "tests", "web",
)
_ROOT_INPUTS = (
    "main.py", "web_ui.py", "pyproject.toml", "requirements.txt", "requirements-dev.txt",
    "requirements.lock", "requirements-dev.lock", "package.json", "package-lock.json",
    "playwright.config.mjs",
)
_IGNORED_DIRS = {"__pycache__", ".pytest_cache", ".ruff_cache", "node_modules"}


def input_fingerprints(root: Path) -> dict[str, str]:
    """Hash gate inputs only; never inspect keys, user novels or tool caches."""
    paths = [root / name for name in _ROOT_INPUTS if (root / name).is_file()]
    for directory in _SOURCE_DIRS:
        paths.extend(path for path in (root / directory).rglob("*")
                     if path.is_file() and not (_IGNORED_DIRS & set(path.relative_to(root).parts)))
    result = {}
    for path in sorted(paths):
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        result[path.relative_to(root).as_posix()] = digest.hexdigest()
    return result


def save_report(report: dict) -> Path:
    directory = ROOT / ".quality"
    directory.mkdir(exist_ok=True)
    path = directory / f"check-py{sys.version_info.major}.{sys.version_info.minor}.json"
    fd, temporary = tempfile.mkstemp(prefix=".check-", suffix=".json", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            json.dump(report, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audit", action="store_true", help="Query published dependency vulnerabilities")
    args = parser.parse_args()
    baseline = input_fingerprints(ROOT)
    report = {
        "report_version": 1, "started_at": datetime.now(UTC).isoformat(),
        "python": sys.version, "audit_requested": args.audit, "status": "running",
        "inputs": baseline, "checks": [], "skipped_checks": [],
    }

    def finish(code: int) -> int:
        current = input_fingerprints(ROOT)
        changed = sorted(name for name in baseline.keys() | current.keys()
                         if baseline.get(name) != current.get(name))
        report["changed_inputs"] = changed
        if changed:
            print("FAILED: gate inputs changed during execution: " + ", ".join(changed), file=sys.stderr)
            code = 1
        report["status"] = "failed" if code else ("partial" if report["skipped_checks"] else "passed")
        report["finished_at"] = datetime.now(UTC).isoformat()
        report["exit_code"] = code
        report_path = save_report(report)
        print(f"Quality report: {report_path.relative_to(ROOT)}", flush=True)
        return code

    save_report(report)
    commands = [
        ("Ruff", [sys.executable, "-m", "ruff", "check", "."]),
        (
            "Static security analysis",
            [
                sys.executable,
                "-m",
                "bandit",
                "-q",
                "-r",
                "main.py",
                "web_ui.py",
                "api",
                "services",
                "agents",
                "config",
                "core",
                "pipeline",
                "examples",
                "scripts",
                "-x",
                "tests",
                "-ll",
            ],
        ),
        ("Compile", [sys.executable, "-m", "compileall", "-q", "main.py", "web_ui.py", "api", "services", "agents", "config", "core", "pipeline", "examples", "tests", "scripts"]),
        ("Tests and coverage", [sys.executable, "-m", "pytest", "--cov", "--cov-branch", "--cov-report=term-missing", "-q"]),
        ("Dependency consistency", [sys.executable, "-m", "pip", "check"]),
        ("CLI contract", [sys.executable, "main.py", "--help"]),
    ]
    node = shutil.which("node")
    if node:
        commands.append(("Browser state regression tests", [node, "--test", "tests/web_state.test.mjs"]))
        commands.append(("Browser JavaScript syntax", [node, "-e", (
            "const fs=require('fs'),s=fs.readFileSync('web/index.html','utf8');"
            "for(const p of ['web/assets/tailwind.min.css','web/assets/cytoscape.min.js',"
            "'web/assets/fontawesome/all.min.css','web/assets/webfonts/fa-solid-900.woff2'])"
            "if(!fs.existsSync(p)||fs.statSync(p).size<1000)throw Error('Missing web asset: '+p);"
            "for(const host of ['cdn.tailwindcss.com','cdnjs.cloudflare.com','fonts.googleapis.com'])"
            "if(s.includes(host))throw Error('Remote UI dependency remains: '+host);"
            "const blocks=[...s.matchAll(/<script(?:\\s[^>]*)?>([\\s\\S]*?)<\\/script>/gi)]"
            ".map(x=>x[1]).filter(x=>x.trim());"
            "if(!blocks.length)throw Error('No inline JavaScript found');"
            "for(const code of blocks)new Function(code);"
            "console.log('JavaScript syntax OK: '+blocks.length+' blocks');"
        )]))
        commands.append(("Real-browser test syntax", [
            node, "--check", "playwright.config.mjs",
        ]))
        commands.append(("Real-browser spec syntax", [
            node, "--check", "tests/browser/novel_studio.spec.mjs",
        ]))
    else:
        report["skipped_checks"].append("Browser state/syntax, real-browser syntax and asset checks: Node.js unavailable")
        print("Node.js is unavailable; browser JavaScript checks were NOT run.", flush=True)
    if args.audit:
        commands.extend(
            (f"Published dependency vulnerabilities ({lock})", [
                sys.executable, "-m", "pip_audit", "-r", lock,
                "--require-hashes", "--disable-pip", "--strict", "--progress-spinner", "off",
            ])
            for lock in ("requirements.lock", "requirements-dev.lock")
        )

    # Each invocation (including concurrent Python versions) owns its coverage
    # database and child-process fragments. Never erase/combine another run's data.
    with tempfile.TemporaryDirectory(prefix="novelengine-quality-") as coverage_dir:
        environment = {**os.environ, "PYTHONUTF8": "1", "COVERAGE_FILE": str(Path(coverage_dir) / ".coverage")}
        for label, command in commands:
            print(f"\n[{label}]", flush=True)
            try:
                completed = subprocess.run(command, cwd=ROOT, env=environment, check=False, timeout=600)
            except subprocess.TimeoutExpired:
                report["checks"].append({"name": label, "status": "timeout"})
                print(f"FAILED: {label} timed out", file=sys.stderr)
                return finish(1)
            except OSError as exc:
                report["checks"].append({"name": label, "status": "launch_failed", "error_type": type(exc).__name__})
                print(f"FAILED: {label} could not start ({type(exc).__name__})", file=sys.stderr)
                return finish(1)
            report["checks"].append({"name": label, "status": "passed" if not completed.returncode else "failed",
                                     "exit_code": completed.returncode})
            if completed.returncode:
                print(f"FAILED: {label}", file=sys.stderr)
                return finish(completed.returncode)
            if input_fingerprints(ROOT) != baseline:
                return finish(1)
    result = finish(0)
    if result:
        return result
    print("\nAll executed checks passed.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
