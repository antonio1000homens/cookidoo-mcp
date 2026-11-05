"""Guard regression: invalid workflow_dispatch inputs must FAIL, not skip."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/validate-recipe-release-request.sh"
WORKFLOW = ROOT / ".github/workflows/release-recipe-tools.yml"


def invoke(operation, confirmation, branch="refs/heads/master"):
    return subprocess.run(
        ["bash", str(SCRIPT)],
        cwd=ROOT,
        env={
            **os.environ,
            "RELEASE_ACTION": operation,
            "RELEASE_CONFIRMATION": confirmation,
            "GITHUB_REF": branch,
        },
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.parametrize(("operation", "confirmation"), [
    ("deploy-disabled", "DEPLOY-COOKIDOO-WRITES-DISABLED"),
    ("enable-writes", "ENABLE-COOKIDOO-RECIPE-CREATION"),
    ("disable-writes", "DISABLE-COOKIDOO-RECIPE-CREATION"),
])
def test_matching_confirmation_accepted(operation, confirmation):
    result = invoke(operation, confirmation)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(("operation", "confirmation"), [
    ("deploy-disabled", ""),
    ("deploy-disabled", "DEPLOY-COOKIDOO-WRITES-DISABLE"),
    ("enable-writes", "DEPLOY-COOKIDOO-WRITES-DISABLED"),
    ("disable-writes", "ENABLE-COOKIDOO-RECIPE-CREATION"),
    ("unknown-operation", "DEPLOY-COOKIDOO-WRITES-DISABLED"),
    ("", ""),
])
def test_invalid_confirmation_fails_with_actionable_error(operation, confirmation):
    result = invoke(operation, confirmation)
    assert result.returncode != 0
    assert "::error::" in result.stderr
    assert "skipped" not in result.stderr.lower()


def test_non_master_branch_fails_with_clear_message():
    result = invoke("deploy-disabled", "DEPLOY-COOKIDOO-WRITES-DISABLED", "refs/heads/test")
    assert result.returncode != 0
    assert "::error::" in result.stderr
    assert "master" in result.stderr


def test_job_does_not_skip_on_missing_confirmation():
    workflow = WORKFLOW.read_text(encoding="utf-8")
    validate_job = workflow.split("  validate:", 1)[1].split("  release:", 1)[0]
    assert "\n    if:" not in validate_job
    assert "Validate master branch, operation and confirmation" in validate_job
    assert "RELEASE_CONFIRMATION: ${{ inputs.confirmation }}" in workflow
    assert "run: bash scripts/validate-recipe-release-request.sh" in validate_job
    assert "needs.validate.result == 'success'" in workflow
    script = (ROOT / "scripts/release-recipe-tools.sh").read_text(encoding="utf-8")
    assert "bash scripts/validate-recipe-release-request.sh" in script
