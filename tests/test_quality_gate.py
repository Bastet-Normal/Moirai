"""The quality report must identify exactly the inputs that were tested."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from scripts import check


@pytest.fixture
def gate(tmp_path, monkeypatch):
    monkeypatch.setattr(check, "ROOT", tmp_path)
    monkeypatch.setattr(sys, "argv", ["check.py"])
    monkeypatch.setattr(check.shutil, "which", lambda name: "node")
    (tmp_path / "core").mkdir()
    (tmp_path / "core" / "example.py").write_text("value = 1\n", encoding="utf-8")
    return tmp_path


def report(root):
    path = root / ".quality" / f"check-py{sys.version_info.major}.{sys.version_info.minor}.json"
    return json.loads(path.read_text(encoding="utf-8"))


def test_quality_gate_report_covers_static_test_cli_and_web_steps(gate, monkeypatch):
    monkeypatch.setattr(check.subprocess, "run", lambda *args, **kwargs: subprocess.CompletedProcess(args, 0))
    assert check.main() == 0
    saved = report(gate)
    assert saved["status"] == "passed"
    assert saved["changed_inputs"] == []
    assert len(saved["checks"]) == 10
    assert saved["inputs"] == check.input_fingerprints(gate)


@pytest.mark.parametrize("returncode", [0, 3])
def test_quality_gate_owns_and_cleans_coverage_data_per_invocation(gate, monkeypatch, returncode):
    existing = gate / ".coverage"
    existing.write_bytes(b"other run's evidence")
    monkeypatch.setenv("COVERAGE_FILE", str(existing))
    paths = []

    def run(*args, **kwargs):
        path = Path(kwargs["env"]["COVERAGE_FILE"])
        assert path.parent.is_dir()
        assert path != existing
        path.write_bytes(b"this invocation")
        paths.append(path)
        return subprocess.CompletedProcess(args, returncode)

    monkeypatch.setattr(check.subprocess, "run", run)
    for _ in range(2):
        assert check.main() == returncode
        assert all(not path.parent.exists() for path in paths)
    assert len(set(paths)) == 2
    assert existing.read_bytes() == b"other run's evidence"
    assert os.environ["COVERAGE_FILE"] == str(existing)


def test_quality_gate_rejects_mutation_even_when_every_check_passes(gate, monkeypatch):
    def run(*args, **kwargs):
        (gate / "core" / "example.py").write_text("value = 2\n", encoding="utf-8")
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(check.subprocess, "run", run)
    assert check.main() == 1
    assert report(gate)["status"] == "failed"
    assert report(gate)["changed_inputs"] == ["core/example.py"]


@pytest.mark.parametrize("outcome", ["failed", "timeout", "launch_failed"])
def test_quality_gate_retains_failure_evidence(gate, monkeypatch, outcome):
    def run(*args, **kwargs):
        if outcome == "timeout":
            raise subprocess.TimeoutExpired("fake", 600)
        if outcome == "launch_failed":
            raise FileNotFoundError("fake")
        return subprocess.CompletedProcess(args, 3)

    monkeypatch.setattr(check.subprocess, "run", run)
    assert check.main() != 0
    assert report(gate)["status"] == "failed"
    assert report(gate)["checks"][0]["status"] == outcome


def test_quality_gate_does_not_claim_full_validation_without_node(gate, monkeypatch):
    monkeypatch.setattr(check.shutil, "which", lambda name: None)
    monkeypatch.setattr(check.subprocess, "run", lambda *args, **kwargs: subprocess.CompletedProcess(args, 0))
    assert check.main() == 0
    assert report(gate)["status"] == "partial"
    assert report(gate)["skipped_checks"]


def test_gate_input_fingerprints_exclude_user_data_and_generated_caches(gate):
    before = check.input_fingerprints(gate)
    (gate / "novel_projects").mkdir()
    (gate / "novel_projects" / "secret.json").write_text("private", encoding="utf-8")
    (gate / ".env").write_text("API_KEY=private", encoding="utf-8")
    (gate / "core" / "__pycache__").mkdir()
    (gate / "core" / "__pycache__" / "fake.pyc").write_bytes(b"compiled")
    assert check.input_fingerprints(gate) == before
