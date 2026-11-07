"""Fail-closed CloudFormation change-set policy for every recipe release.

All fixtures are synthetic: no AWS credentials or infrastructure mutations.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/guard-recipe-change-set.py"
spec = importlib.util.spec_from_file_location("recipe_change_set_guard", SCRIPT)
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)

BASE = {
    "FunctionName": "cookidoo-mcp",
    "CodeBucket": "aws2022-lambda-code",
    "CodeKey": "cookidoo-mcp/previous/cookidoo-mcp.zip",
    "EmailParameterName": "/cookidoo-mcp/email",
    "PasswordParameterName": "/cookidoo-mcp/password",
    "OriginBearerParameterName": "/cookidoo-mcp/origin-bearer",
    "CloudflareTeamDomain": "https://example.cloudflareaccess.com",
    "CloudflareAudience": "test-audience",
    "EnableFunctionUrl": "true",
    "EnableRecipeCreation": "false",
    "EnableRecipeUpdates": "false",
}
NEW_CODE = "cookidoo-mcp/abc123/cookidoo-mcp.zip"


def params(values):
    return [{"ParameterKey": key, "ParameterValue": val} for key, val in values.items()]


def detail(property_name="Environment", evaluation="Static", recreation="Never"):
    return {
        "Target": {
            "Attribute": "Properties", "Name": property_name,
            "RequiresRecreation": recreation,
        },
        "Evaluation": evaluation, "ChangeSource": "ParameterReference",
    }


def resource(*props, logical_id="CookidooFunction", replacement="False",
             resource_type="AWS::Lambda::Function", action="Modify",
             scope=None):
    return {
        "Type": "Resource",
        "ResourceChange": {
            "Action": action,
            "LogicalResourceId": logical_id,
            "PhysicalResourceId": "cookidoo-mcp" if logical_id == "CookidooFunction" else None,
            "ResourceType": resource_type,
            "Replacement": replacement,
            "Scope": ["Properties"] if scope is None else scope,
            "Details": [detail(prop) for prop in props],
        },
    }


def case(action, missing_update_flag=False):
    old = dict(BASE)
    if action == "disable-writes":
        old["EnableRecipeCreation"] = "true"
    if action == "disable-updates":
        old["EnableRecipeUpdates"] = "true"
    if action == "deploy-update-code":
        old["EnableRecipeCreation"] = "true"
    new = dict(old)
    if action.startswith("deploy-"):
        new["CodeKey"] = NEW_CODE
        changed_props = ("Code", "Environment") if action == "deploy-update-code" else ("Code",)
    else:
        flag, value = guard.FLAG_ACTIONS[action]
        new[flag] = value
        changed_props = ("Environment",)
    current = dict(old)
    if missing_update_flag:
        current.pop("EnableRecipeUpdates")
    stack = {"Stacks": [{"StackName": "cookidoo-mcp", "Parameters": params(current)}]}
    change_set = {
        "StackName": "cookidoo-mcp",
        "Status": "CREATE_COMPLETE", "ExecutionStatus": "AVAILABLE",
        "ChangeSetType": "UPDATE",
        "Parameters": params(new),
        "Changes": [resource(*changed_props)],
    }
    flags = {
        "action": action,
        "code_key": new["CodeKey"],
        "creation": new["EnableRecipeCreation"],
        "updates": new["EnableRecipeUpdates"],
    }
    return stack, change_set, flags


@pytest.mark.parametrize("action", [
    "deploy-disabled", "deploy-update-code", "enable-writes",
    "disable-writes", "enable-updates", "disable-updates",
])
def test_allowed_single_lambda_in_place_change_for_all_release_actions(action):
    stack, change_set, args = case(action)
    approved = guard.validate(change_set, stack, **args)
    assert "Code" in approved if action.startswith("deploy-") else approved == ["Environment"]


def test_first_rollout_with_missing_update_flag_is_safe():
    stack, change_set, args = case("deploy-update-code", missing_update_flag=True)
    assert guard.validate(change_set, stack, **args) == ["Code", "Environment"]


@pytest.mark.parametrize(("kind", "expected"), [
    ("role", "exactly one"),
    ("function_url", "exactly one"),
    ("permission", "exactly one"),
    ("log_group", "exactly one"),
    ("new_function", "unexpected resource"),
    ("replacement", "unexpected resource"),
    ("runtime", "unexpected Lambda property"),
    ("role_property", "unexpected Lambda property"),
    ("timeout", "unexpected Lambda property"),
    ("reserved_concurrency", "unexpected Lambda property"),
    ("dynamic", "Dynamic"),
    ("conditional_replacement", "Dynamic"),
    ("incomplete", "reviewable details"),
    ("unexpected_scope", "unexpected resource"),
    ("wrong_physical_id", "different Lambda"),
    ("wrong_stack", "Wrong target"),
    ("incomplete_page", "paginated"),
    ("unexpected_parameter", "parameters differ"),
    ("creation_flag_on_update", "parameters differ"),
    ("wrong_action", "Unknown"),
])
def test_unsafe_change_set_never_reaches_execution(kind, expected):
    stack, change_set, args = case("deploy-update-code")
    if kind in {"role", "function_url", "permission", "log_group"}:
        ids = {
            "role": ("CookidooRole", "AWS::IAM::Role"),
            "function_url": ("CookidooFunctionUrl", "AWS::Lambda::Url"),
            "permission": ("AllowFunctionUrl", "AWS::Lambda::Permission"),
            "log_group": ("CookidooLogGroup", "AWS::Logs::LogGroup"),
        }
        logical, type_name = ids[kind]
        change_set["Changes"].append(resource("Policies", logical_id=logical, resource_type=type_name))
    elif kind == "new_function":
        change_set["Changes"][0] = resource("Code", action="Add")
    elif kind == "replacement":
        change_set["Changes"][0] = resource("Code", replacement="True")
    elif kind in ("runtime", "role_property", "timeout", "reserved_concurrency"):
        field = {
            "runtime": "Runtime", "role_property": "Role",
            "timeout": "Timeout", "reserved_concurrency": "ReservedConcurrentExecutions",
        }[kind]
        change_set["Changes"][0]["ResourceChange"]["Details"].append(detail(field))
    elif kind == "dynamic":
        change_set["Changes"][0]["ResourceChange"]["Details"][0]["Evaluation"] = "Dynamic"
    elif kind == "conditional_replacement":
        change_set["Changes"][0]["ResourceChange"]["Details"][0]["Target"]["RequiresRecreation"] = "Conditionally"
    elif kind == "incomplete":
        change_set["Changes"][0]["ResourceChange"]["Details"] = []
    elif kind == "unexpected_scope":
        change_set["Changes"][0]["ResourceChange"]["Scope"] = ["Properties", "Metadata"]
    elif kind == "wrong_physical_id":
        change_set["Changes"][0]["ResourceChange"]["PhysicalResourceId"] = "another-lambda"
    elif kind == "wrong_stack":
        change_set["StackName"] = "another-stack"
    elif kind == "incomplete_page":
        change_set["NextToken"] = "unseen-page"
    elif kind == "unexpected_parameter":
        change_set["Parameters"].append({"ParameterKey": "Extra", "ParameterValue": "true"})
    elif kind == "creation_flag_on_update":
        change_set["Parameters"] = params({**BASE, "CodeKey": NEW_CODE, "EnableRecipeCreation": "false"})
    elif kind == "wrong_action":
        args["action"] = "erase-stack"
    with pytest.raises(guard.UnsafeChangeSet, match=expected):
        guard.validate(change_set, stack, **args)


def test_block_unrelated_flag_change_during_update_code():
    stack, change_set, args = case("deploy-update-code")
    change_set["Parameters"] = params({
        **BASE, "CodeKey": NEW_CODE, "EnableRecipeCreation": "false",
    })
    args["creation"] = "false"
    with pytest.raises(guard.UnsafeChangeSet, match="unrelated parameters"):
        guard.validate(change_set, stack, **args)


def test_block_noop_or_unexpected_flag_transition():
    stack, change_set, args = case("enable-updates")
    change_set["Parameters"] = params(BASE)
    args["updates"] = "false"
    with pytest.raises(guard.UnsafeChangeSet, match="unrelated parameters"):
        guard.validate(change_set, stack, **args)


def test_block_malformed_replacement_and_missing_detail():
    stack, change_set, args = case("enable-writes")
    change_set["Changes"][0]["ResourceChange"]["Details"][0]["Target"]["RequiresRecreation"] = None
    with pytest.raises(guard.UnsafeChangeSet, match="Dynamic"):
        guard.validate(change_set, stack, **args)


def test_source_script_executes_exact_arn_only_after_validator():
    script = (SCRIPT.parent / "release-recipe-tools.sh").read_text(encoding="utf-8")
    assert "aws cloudformation create-change-set" in script
    assert "aws cloudformation describe-change-set" in script
    assert "python3 scripts/guard-recipe-change-set.py" in script
    assert "aws cloudformation execute-change-set" in script
    assert "aws cloudformation deploy --" not in script
    assert "aws cloudformation wait stack-update-complete" in script
    assert script.index("aws cloudformation describe-change-set") < script.index("python3 scripts/guard-recipe-change-set.py")
    assert script.index("python3 scripts/guard-recipe-change-set.py") < script.index("aws cloudformation execute-change-set")
    assert '--change-set-name "$change_set_arn"' in script
    assert "--parameters \"file://$change_set_dir/requested-parameters.json\"" in script



def _valid_dynamic_case():
    """Mirror live 7 November preflight: Environment Static + Direct Dynamic."""
    stack, change_set, args = case("deploy-update-code", missing_update_flag=True)
    approved_template = (
        Path(__file__).resolve().parents[1] / "infrastructure/cookidoo.yaml"
    ).read_text(encoding="utf-8")
    previous_template = approved_template.replace(guard._NEW_PARAMETER, "").replace(
        guard._NEW_ENVIRONMENT, ""
    )
    old_params = dict(BASE, EnableRecipeCreation="true")
    old_params.pop("EnableRecipeUpdates")
    before_env = guard._expected_environment(
        {**old_params, "EnableRecipeUpdates": "false"}, old_format=True
    )
    change_set["Changes"][0]["ResourceChange"]["Details"].append({
        "Target": {
            "Attribute": "Properties", "Name": "Environment",
            "RequiresRecreation": "Never",
        },
        "Evaluation": "Dynamic",
        "ChangeSource": "DirectModification",
    })
    return stack, change_set, args, previous_template, approved_template, before_env


def test_preflight_allows_exact_live_template_delta_with_verified_dynamic_environment():
    stack, change_set, args, original, proposed, env = _valid_dynamic_case()
    assert guard._template_matches_existing(original, proposed)
    assert guard.validate(
        change_set, stack, **args,
        existing_template=original, proposed_template=proposed, current_env=env,
    ) == ["Code", "Environment"]


@pytest.mark.parametrize("tamper", [
    "runtime_value", "extra_env_key", "remove_env_key",
    "template_iam", "template_auth", "template_extra",
    "changed_dynamic_source", "dynamic_code", "changed_target",
    "dynamic_replacement", "missing_snapshot",
])
def test_preflight_rejects_unverified_dynamic_environment_changes(tamper):
    stack, change_set, args, original, proposed, env = _valid_dynamic_case()
    if tamper == "runtime_value":
        env["COOKIDOO_RECIPE_CREATION_ENABLED"] = "false"
    elif tamper == "extra_env_key":
        env["UNREVIEWED_SECRET"] = "unexpected"
    elif tamper == "remove_env_key":
        env.pop("ORIGIN_BEARER_PARAM")
    elif tamper == "template_iam":
        proposed = proposed.replace("Action: ssm:GetParameter", "Action: ssm:*")
    elif tamper == "template_auth":
        proposed = proposed.replace("ORIGIN_BEARER_PARAM: !Ref OriginBearerParameterName",
                                    "ORIGIN_BEARER_PARAM: changed")
    elif tamper == "template_extra":
        proposed += "\n# Unreviewed change\n"
    elif tamper == "changed_dynamic_source":
        change_set["Changes"][0]["ResourceChange"]["Details"][-1][
            "ChangeSource"
        ] = "ResourceReference"
    elif tamper == "dynamic_code":
        change_set["Changes"][0]["ResourceChange"]["Details"][-1][
            "Target"
        ]["Name"] = "Code"
    elif tamper == "changed_target":
        change_set["Changes"][0]["ResourceChange"]["Details"][-1][
            "Target"
        ]["Attribute"] = "Metadata"
    elif tamper == "dynamic_replacement":
        change_set["Changes"][0]["ResourceChange"]["Details"][-1][
            "Target"
        ]["RequiresRecreation"] = "Conditionally"
    elif tamper == "missing_snapshot":
        original = None
    with pytest.raises(guard.UnsafeChangeSet):
        guard.validate(
            change_set, stack, **args,
            existing_template=original, proposed_template=proposed,
            current_env=env,
        )


def test_after_initial_rollout_identical_template_dynamic_env_is_verified():
    stack, change_set, args = case("enable-updates")
    new_template = (
        Path(__file__).resolve().parents[1] / "infrastructure/cookidoo.yaml"
    ).read_text(encoding="utf-8")
    before_env = guard._expected_environment(BASE, old_format=False)
    change_set["Changes"][0]["ResourceChange"]["Details"].append({
        "Target": {
            "Attribute": "Properties", "Name": "Environment",
            "RequiresRecreation": "Never",
        },
        "Evaluation": "Dynamic", "ChangeSource": "DirectModification",
    })
    assert guard.validate(
        change_set, stack, **args, existing_template=new_template,
        proposed_template=new_template, current_env=before_env,
    ) == ["Environment"]


def test_guard_script_reads_live_template_and_environment_and_compares_post_state():
    script = (SCRIPT.parent / "release-recipe-tools.sh").read_text(encoding="utf-8")
    assert "aws cloudformation get-template" in script
    assert "aws lambda get-function-configuration" in script
    assert "--existing-template" in script and "--proposed-template" in script
    assert "--current-env" in script
    assert "deployed-env.json" in script
    assert "COOKIDOO_RECIPE_CREATION_ENABLED: $creation" in script
    assert "COOKIDOO_RECIPE_UPDATES_ENABLED: $updates" in script


def test_release_confirmation_error_is_clear():
    source = (SCRIPT.parent / "validate-recipe-release-request.sh").read_text(encoding="utf-8")
    assert "one of one of" not in source
    assert "Enter exactly one of the action codes" in source
