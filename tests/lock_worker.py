"""Spawn target for exercising the real file lock without loading the studio."""

from pathlib import Path

from core.storage import project_lock


def hold_project_lock(path, acquired, release, scope):
    with project_lock(Path(path), timeout=0, scope=scope):
        acquired.set()
        release.wait(20)
