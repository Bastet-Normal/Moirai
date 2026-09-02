"""Stored JSON cannot escape the regular-file and memory boundary."""

import json
import os

import pytest

from core.storage import read_json


def test_read_json_accepts_exact_boundary_and_rejects_one_byte_over(tmp_path):
    path = tmp_path / "data.json"
    path.write_text(json.dumps({"ok": True}), encoding="utf-8")
    size = path.stat().st_size
    assert read_json(path, max_bytes=size) == {"ok": True}
    with pytest.raises(ValueError, match="超过"):
        read_json(path, max_bytes=size - 1)


@pytest.mark.parametrize("value", [-1, 1.5, True, "10"])
def test_read_json_rejects_invalid_limits_before_open(tmp_path, value):
    with pytest.raises(ValueError, match="上限"):
        read_json(tmp_path / "missing.json", max_bytes=value)


def test_read_json_rejects_directories_and_fifos_without_blocking(tmp_path):
    with pytest.raises(ValueError, match="非普通"):
        read_json(tmp_path, max_bytes=100)
    if hasattr(os, "mkfifo"):
        fifo = tmp_path / "pipe.json"
        os.mkfifo(fifo)
        with pytest.raises(ValueError, match="非普通"):
            read_json(fifo, max_bytes=100)


def test_read_json_rejects_symlink_when_host_can_create_one(tmp_path):
    target = tmp_path / "target.json"
    target.write_text('{"secret": true}', encoding="utf-8")
    link = tmp_path / "link.json"
    try:
        link.symlink_to(target)
    except OSError as exc:
        pytest.skip(f"symlink privilege unavailable: {exc}")
    with pytest.raises(ValueError, match="非普通"):
        read_json(link)


def test_read_json_closes_descriptor_on_parse_failure(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text("{", encoding="utf-8")
    with pytest.raises(json.JSONDecodeError):
        read_json(path)
    # fdopen owns and closes the descriptor; reopening for write proves there is
    # no lingering Windows share/handle dependency.
    path.write_text("{}", encoding="utf-8")
    assert read_json(path) == {}
