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
    assert "GITHUB_REF" in load("scripts/validate-recipe-release-request.sh")
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


def test_creation_is_disabled_by_default_and_s3_scope_is_explicit():
    template = load("infrastructure/cookidoo.yaml")
    assert "EnableRecipeCreation:" in template
    assert "Default: 'false'" in template
    assert "COOKIDOO_RECIPE_CREATION_ENABLED: !Ref EnableRecipeCreation" in template
    assert "EnableRecipeUpdates:" in template
    assert "COOKIDOO_RECIPE_UPDATES_ENABLED: !Ref EnableRecipeUpdates" in template
    assert "/cookidoo-mcp/write-guards/*" in template
    assert "s3:PutObject" in template
    deploy = load("scripts/deploy-aws.sh")
    # Normal private deploy must never enable recipe writes unexpectedly.
    assert "EnableRecipeCreation=true" not in deploy


def test_active_function_url_release_is_separately_confirmed():
    workflow = load(".github/workflows/release-recipe-tools.yml")
    release = load("scripts/release-recipe-tools.sh")
    assert "workflow_dispatch:" in workflow
    assert "\n  push:" not in workflow
    assert "\n  pull_request:" not in workflow
    assert "environment: production" in workflow
    assert "id-token: write" in workflow
    assert "github.ref == 'refs/heads/master'" in workflow
    assert "DEPLOY-COOKIDOO-WRITES-DISABLED" in workflow
    assert "ENABLE-COOKIDOO-RECIPE-CREATION" in workflow
    assert "DISABLE-COOKIDOO-RECIPE-CREATION" in workflow
    assert "cookidoo-mcp-release" in workflow
    assert '[[ "$(parameter EnableFunctionUrl)" == "true" ]]' in release
    assert 'ParameterKey:"EnableRecipeCreation"' in release
    assert 'ParameterKey:"EnableRecipeUpdates"' in release
    assert "aws cloudformation create-change-set" in release
    assert "aws cloudformation describe-change-set" in release
    assert "python3 scripts/guard-recipe-change-set.py" in release
    assert "aws cloudformation execute-change-set" in release
    assert "aws cloudformation deploy --" not in release
    assert "CodeKey" in release
    assert "code_key" in release
    assert "deploy-disabled first" in release
    assert "disable-writes" in release
    assert "IfNoneMatch" not in release  # AWS CLI uses --if-none-match
    assert "--if-none-match '*'" in release
    assert "old_url" in release and "new_url" in release


def test_release_workflow_cannot_deploy_writes_by_default():
    release = load("scripts/release-recipe-tools.sh")
    workflow = load(".github/workflows/release-recipe-tools.yml")
    assert 'write_state=true' not in release
    assert 'enabled=true' in release
    assert 'enabled=false' in release
    assert "steps.confirm.outputs.action == 'deploy-disabled'" in workflow
    assert "RELEASE_ACTION: ${{ needs.validate.outputs.action }}" in workflow


def test_origin_bearer_iam_grants_singular_getparameter_on_only_origin_secret():
    """AccessGate calls ssm.get_parameter, not ssm.get_parameters."""
    template = load("infrastructure/cookidoo.yaml")
    origin_stmt = template.split("PolicyName: ReadOriginBearerSingleParameter", 1)[1].split(
        "PolicyName: ReserveOwnCustomRecipeTitles", 1
    )[0]
    assert "Action: ssm:GetParameter" in [line.strip() for line in origin_stmt.splitlines()]
    assert "parameter${OriginBearerParameterName}" in origin_stmt
    assert "EmailParameterName" not in origin_stmt
    assert "PasswordParameterName" not in origin_stmt
    assert "Resource: '*'" not in origin_stmt
    # Preserve existing batch-only access for account credentials.
    batch_stmt = template.split("PolicyName: ReadOnlyCookidooParameters", 1)[1].split(
        "PolicyName: ReadOriginBearerSingleParameter", 1
    )[0]
    assert "Action: ssm:GetParameters" in batch_stmt
    assert "Action: ssm:GetParameter" not in [line.strip() for line in batch_stmt.splitlines()]
