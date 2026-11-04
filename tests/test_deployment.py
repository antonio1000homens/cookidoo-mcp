"""Deployment controls must be reviewed and offline-testable without AWS accounts."""
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]


def load(path):
    return (ROOT / path).read_text(encoding="utf-8")


def test_public_endpoint_unavailable_by_default():
    app = load("infrastructure/cookidoo.yaml")
    assert "Default: 'false'" in app
    assert "PackageType: Zip" in app
    assert "Runtime: python3.12" in app
    workflow = load(".github/workflows/deploy.yml")
    assert "DEPLOY-PRIVATE-LAMBDA" in workflow
    assert "EnableFunctionUrl=false" in load("scripts/deploy-aws.sh")
    assert "OriginBearerParameterName" in app


def test_oidc_restricted_to_exact_repository_and_production_environment():
    bootstrap = load("infrastructure/bootstrap-github.yaml")
    assert (
        "repo:antonio1000homens@36929120/"
        "cookidoo-mcp@1446019078:environment:production"
    ) in bootstrap
    assert "token.actions.githubusercontent.com:aud" in bootstrap
    assert "sts.amazonaws.com" in bootstrap
    assert "repo:antonio1000homens/*" not in bootstrap


def test_function_url_activation_is_separately_confirmed_and_scoped():
    bootstrap = load("infrastructure/bootstrap-github.yaml")
    app = load("infrastructure/cookidoo.yaml")
    activation = load(".github/workflows/activate-function-url.yml")
    assert "ACTIVATE-COOKIDOO-FUNCTION-URL" in activation
    assert "lambda:CreateFunctionUrlConfig" in bootstrap
    assert "/cookidoo-mcp/origin-bearer" in app
    assert "stack-name cookidoo-mcp" in activation
    assert "Function URL already exists" in activation
    assert "ssm:DescribeParameters" not in activation
    assert "configured SSM path" in activation
    assert "EnableFunctionUrl=true" in activation
    assert "assert_no_function_url" in load("scripts/deploy-aws.sh")
    assert "arn:__D__{AWS::Partition}:s3:::__D__{CodeBucket}/cookidoo-mcp/*".replace("__D__", "$") in bootstrap


def test_deployment_is_manual_and_reuses_tested_build():
    workflow = load(".github/workflows/deploy.yml")
    assert "workflow_dispatch:" in workflow
    assert "\n  push:" not in workflow
    assert "\n  pull_request:" not in workflow
    assert "github.ref == 'refs/heads/master'" in workflow
    assert "environment: production" in workflow
    assert "id-token: write" in workflow
    assert "actions/upload-artifact@" in workflow
    assert "actions/download-artifact@" in workflow
    assert "inputs.confirmation == 'DEPLOY-PRIVATE-LAMBDA'" in workflow
    assert "ECR" not in workflow
    assert "docker build" not in workflow
    assert "--if-none-match '*'" in load("scripts/deploy-aws.sh")


def test_bootstrap_does_not_create_per_request_billed_services():
    bootstrap = load("infrastructure/bootstrap-github.yaml")
    assert bootstrap.count("Type: AWS::IAM::Role") == 2
    assert "Type: AWS::S3::Bucket" not in bootstrap
    assert "Type: AWS::ECR::Repository" not in bootstrap
    assert "Type: AWS::Lambda::Function" not in bootstrap
    assert "Type: AWS::DynamoDB::Table" not in bootstrap


def test_action_commit_pins_use_full_40_character_sha():
    """GitHub Actions rejects abbreviated commit SHA refs before jobs start."""
    for workflow_path in (".github/workflows/deploy.yml", ".github/workflows/ci.yml"):
        workflow = load(workflow_path)
        for action, ref in re.findall(r"^\\s*(?:-\\s*)?uses:\\s+([^@\\s]+)@([^\\s#]+)", workflow, re.MULTILINE):
            if re.fullmatch(r"[0-9a-f]+", ref):
                assert len(ref) == 40, f"{workflow_path}: {action}@{ref} is not a full SHA"
