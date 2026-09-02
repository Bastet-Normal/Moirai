"""The hosted quality workflow must preserve the local gate's safety contract."""

import json
import re
from pathlib import Path

from scripts import check

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "quality.yml"
DEPENDABOT = ROOT / ".github" / "dependabot.yml"
PACKAGE = ROOT / "package.json"
PACKAGE_LOCK = ROOT / "package-lock.json"
PLAYWRIGHT_CONFIG = ROOT / "playwright.config.mjs"


def workflow_text() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def test_quality_workflow_runs_real_gate_across_declared_matrix():
    text = workflow_text()
    for operating_system in ("ubuntu-latest", "macos-latest", "windows-latest"):
        assert f"- {operating_system}" in text
    for python in ('- "3.11"', '- "3.14"'):
        assert python in text
    assert 'runs-on: ${{ matrix.os }}' in text
    assert (
        'uv run --isolated --no-project --python "${{ matrix.python }}" '
        "--with-requirements requirements-dev.lock python scripts/check.py"
    ) in text


def test_quality_workflow_pins_actions_and_restricts_repository_access():
    text = workflow_text()
    uses = re.findall(r"^\s*uses:\s*([^\s#]+)", text, flags=re.MULTILINE)
    assert uses
    assert all(re.fullmatch(r"[^@\s]+@[0-9a-f]{40}", action) for action in uses)
    assert set(uses) == {
        "actions/checkout@de0fac2e4500dabe0009e67214ff5f5447ce83dd",
        "actions/setup-python@a309ff8b426b58ec0e2a45f0f869d46889d02405",
        "actions/setup-node@820762786026740c76f36085b0efc47a31fe5020",
        "astral-sh/setup-uv@c771a70e6277c0a99b617c7a806ffedaca235ff9",
    }
    assert "NODE_VERSION: 24.20.0" in text
    assert "UV_VERSION: 0.12.5" in text
    assert "permissions:\n  contents: read" in text
    checkout_steps = re.findall(
        r"- name: Checkout\n(?P<body>(?: {8}.*\n?)*)",
        text,
    )
    assert len(checkout_steps) == 3
    assert all("persist-credentials: false" in step for step in checkout_steps)


def test_quality_workflow_cannot_reach_a_configured_model_provider():
    text = workflow_text()
    assert 'OPENAI_API_KEY: ""' in text
    assert "OPENAI_BASE_URL: http://127.0.0.1:9/v1" in text
    assert "OUTPUT_DIR: ${{ runner.temp }}/novelengine-output" in text
    assert "secrets." not in text


def test_frontend_job_uses_locks_audit_and_rejects_asset_drift():
    text = workflow_text()
    for command in (
        "npm ci --ignore-scripts",
        "npm audit --audit-level=high",
        "npm run test:web",
        "npm run build:web",
        "git diff --exit-code -- web/assets",
    ):
        assert command in text


def test_real_browser_job_is_locked_isolated_and_runs_all_engines():
    text = workflow_text()
    for command in (
        "npx --no-install playwright install --with-deps chromium firefox webkit",
        "npm run test:browser:all",
    ):
        assert command in text
    assert "PLAYWRIGHT_BASE_URL: http://127.0.0.1:8769" in text
    assert "PLAYWRIGHT_BROWSERS_PATH: ${{ runner.temp }}/playwright-browsers" in text
    assert "OUTPUT_DIR: ${{ runner.temp }}/novelengine-browser-output" in text


def test_playwright_dependency_and_local_server_contract_are_exact():
    package = json.loads(PACKAGE.read_text(encoding="utf-8"))
    lock = json.loads(PACKAGE_LOCK.read_text(encoding="utf-8"))
    assert package["devDependencies"]["@playwright/test"] == "1.62.1"
    assert lock["packages"][""]["devDependencies"]["@playwright/test"] == "1.62.1"
    assert lock["packages"]["node_modules/@playwright/test"]["version"] == "1.62.1"
    assert package["scripts"]["test:browser"] == "playwright test --project=chromium"
    assert package["scripts"]["test:browser:all"] == "playwright test"

    config = PLAYWRIGHT_CONFIG.read_text(encoding="utf-8")
    assert "http://127.0.0.1:8769" in config
    assert "target.hostname !== '127.0.0.1'" in config
    assert "reuseExistingServer: false" in config
    assert "OPENAI_API_KEY: ''" in config
    assert "OPENAI_BASE_URL: 'http://127.0.0.1:9/v1'" in config
    for engine in ("chromium", "firefox", "webkit"):
        assert f"name: '{engine}'" in config


def test_dependabot_tracks_action_and_node_lock_updates_monthly():
    text = DEPENDABOT.read_text(encoding="utf-8")
    assert text.count("interval: monthly") == 2
    assert "package-ecosystem: github-actions" in text
    assert "package-ecosystem: npm" in text


def test_gate_fingerprints_ci_policy_files():
    inputs = check.input_fingerprints(ROOT)
    assert ".github/workflows/quality.yml" in inputs
    assert ".github/dependabot.yml" in inputs
    assert "playwright.config.mjs" in inputs
    assert "tests/browser/novel_studio.spec.mjs" in inputs
