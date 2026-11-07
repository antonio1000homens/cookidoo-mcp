"""The manual release has one exact confirmation input, never two mismatched fields."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path
import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/validate-recipe-release-request.sh"
WORKFLOW = ROOT / ".github/workflows/release-recipe-tools.yml"


def invoke(confirmation, action=None, branch="refs/heads/master"):
    env = {**os.environ, "GITHUB_REF": branch, "RELEASE_CONFIRMATION": confirmation}
    env.pop("RELEASE_ACTION", None)
    if action is not None:
        env["RELEASE_ACTION"] = action
    return subprocess.run(["bash", str(SCRIPT)], cwd=ROOT, env=env,
                          capture_output=True, text=True, check=False)


@pytest.mark.parametrize(("confirmation", "operation"), [
    ("DEPLOY-COOKIDOO-WRITES-DISABLED", "deploy-disabled"),
    ("ENABLE-COOKIDOO-RECIPE-CREATION", "enable-writes"),
    ("DISABLE-COOKIDOO-RECIPE-CREATION", "disable-writes"),
    ("DEPLOY-COOKIDOO-RECIPE-UPDATE-CODE", "deploy-update-code"),
    ("ENABLE-COOKIDOO-RECIPE-UPDATES", "enable-updates"),
    ("DISABLE-COOKIDOO-RECIPE-UPDATES", "disable-updates"),
])
def test_confirmation_determines_action(confirmation, operation):
    result = invoke(confirmation)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == f"action={operation}"
    assert invoke(confirmation, operation).returncode == 0


@pytest.mark.parametrize("confirmation", [
    "", "DEPLOY-COOKIDOO-WRITES-DISABLED,",
    "DEPLOY-COOKIDOO-WRITES-DISABLE",
    "ENABLE-COOKIDOO-RECIPE-CREATION ",
    "enable-writes",
    "disable-writes",
    "ENABLE-COOKIDOO-RECIPE-UPDATES,",
])
def test_invalid_confirmation_fails_visibly(confirmation):
    result = invoke(confirmation)
    assert result.returncode != 0
    assert "::error::" in result.stderr


def test_mismatched_privileged_action_fails():
    result = invoke("DEPLOY-COOKIDOO-WRITES-DISABLED", "enable-writes")
    assert result.returncode != 0
    assert "::error::" in result.stderr


def test_non_master_branch_fails_visibly():
    result = invoke("DEPLOY-COOKIDOO-WRITES-DISABLED", branch="refs/heads/feature")
    assert result.returncode != 0
    assert "master" in result.stderr
    assert "::error::" in result.stderr


def test_workflow_uses_only_confirmation_and_defers_write_approval():
    workflow = WORKFLOW.read_text(encoding="utf-8")
    validate_job = workflow.split("  validate:", 1)[1].split("  release:", 1)[0]
    form = workflow.split("    inputs:", 1)[1].split("permissions:", 1)[0]
    assert "      operation:" not in form
    assert form.count("      confirmation:") == 1
    assert "type: string" in form
    assert "type: choice" not in form
    assert "inputs.operation" not in workflow
    assert "\\n    if:" not in validate_job
    assert "id: confirm" in validate_job
    assert "bash scripts/validate-recipe-release-request.sh" in validate_job
    assert 'action: ${{ steps.confirm.outputs.action }}' in validate_job
    assert "needs.validate.outputs.action" in workflow
    assert "environment: production" in workflow
    script = (ROOT / "scripts/release-recipe-tools.sh").read_text(encoding="utf-8")
    assert "bash scripts/validate-recipe-release-request.sh" in script
