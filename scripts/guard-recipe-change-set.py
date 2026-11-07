#!/usr/bin/env python3
"""Reject CloudFormation change sets outside the Cookidoo recipe-release scope.

Only a single in-place Lambda Function change is permitted. This intentionally
fails closed for existing IAM drift (#16), dynamic/ambiguous changes, nested
stacks, resource additions/removals, and any unexpected parameter transition.

The change set being inspected MUST be the exact ARN later executed.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

KNOWN_PARAMETERS = frozenset({
    "FunctionName", "CodeBucket", "CodeKey",
    "EmailParameterName", "PasswordParameterName",
    "OriginBearerParameterName", "CloudflareTeamDomain",
    "CloudflareAudience", "EnableFunctionUrl",
    "EnableRecipeCreation", "EnableRecipeUpdates",
})
FLAG_ACTIONS = {
    "enable-writes": ("EnableRecipeCreation", "true"),
    "disable-writes": ("EnableRecipeCreation", "false"),
    "enable-updates": ("EnableRecipeUpdates", "true"),
    "disable-updates": ("EnableRecipeUpdates", "false"),
}
CODE_ACTIONS = {"deploy-disabled", "deploy-update-code"}


class UnsafeChangeSet(ValueError):
    """The requested deployment contains an unreviewed or ambiguous change."""


def _parameters(entries: object) -> dict[str, str]:
    if not isinstance(entries, list):
        raise UnsafeChangeSet("Missing CloudFormation parameter list")
    result: dict[str, str] = {}
    for item in entries:
        if not isinstance(item, dict):
            raise UnsafeChangeSet("Malformed CloudFormation parameter")
        key, val = item.get("ParameterKey"), item.get("ParameterValue")
        if not isinstance(key, str) or not isinstance(val, str) or key in result:
            raise UnsafeChangeSet("Duplicate, missing, or unresolved parameter")
        result[key] = val
    return result


def validate(
    change_set: dict, stack: dict, *,
    action: str, code_key: str, creation: str, updates: str,
) -> list[str]:
    """Validate a complete DescribeChangeSet response against current stack."""
    if action not in CODE_ACTIONS | FLAG_ACTIONS.keys():
        raise UnsafeChangeSet("Unknown release action")
    if creation not in ("true", "false") or updates not in ("true", "false"):
        raise UnsafeChangeSet("Invalid requested feature switches")
    if change_set.get("Status") != "CREATE_COMPLETE" or (
        change_set.get("ExecutionStatus") != "AVAILABLE"
    ):
        raise UnsafeChangeSet("Change set is not executable")
    if change_set.get("NextToken"):
        raise UnsafeChangeSet("Change set is paginated or incomplete")
    if change_set.get("StackName") != "cookidoo-mcp":
        raise UnsafeChangeSet("Wrong target CloudFormation stack")
    if change_set.get("ChangeSetType") not in (None, "UPDATE"):
        raise UnsafeChangeSet("Change set is not an update")
    if not isinstance(stack.get("Stacks"), list) or len(stack["Stacks"]) != 1:
        raise UnsafeChangeSet("Missing current stack state")
    existing_stack = stack["Stacks"][0]
    if existing_stack.get("StackName") != "cookidoo-mcp":
        raise UnsafeChangeSet("Current stack name mismatch")

    old = _parameters(existing_stack.get("Parameters"))
    # On the first deployment of this template, EnableRecipeUpdates is absent
    # from the old stack. Treat that absent flag as the default disabled state.
    if "EnableRecipeUpdates" not in old:
        old["EnableRecipeUpdates"] = "false"
    if set(old) != KNOWN_PARAMETERS:
        raise UnsafeChangeSet("Unexpected parameters in current stack")
    if old["EnableFunctionUrl"] != "true":
        raise UnsafeChangeSet("Existing Function URL is not enabled")

    expected = dict(old)
    expected["CodeKey"] = code_key
    expected["EnableRecipeCreation"] = creation
    expected["EnableRecipeUpdates"] = updates
    if _parameters(change_set.get("Parameters")) != expected:
        raise UnsafeChangeSet("Change set parameters differ from approved release")

    changes = {key for key in expected if old[key] != expected[key]}
    if action in CODE_ACTIONS:
        if changes != {"CodeKey"}:
            raise UnsafeChangeSet("Code release also changes unrelated parameters")
        if action == "deploy-disabled" and (
            creation != "false" or updates != "false"
        ):
            raise UnsafeChangeSet("Disabled release cannot enable recipe writes")
        if action == "deploy-update-code" and updates != "false":
            raise UnsafeChangeSet("Updated code must be deployed with edits disabled")
        allowed_props = {"Code", "Environment"}
        required_props = {"Code"}
    else:
        flag, desired = FLAG_ACTIONS[action]
        if changes != {flag} or expected[flag] != desired:
            raise UnsafeChangeSet("Feature toggle also changes unrelated parameters")
        allowed_props = {"Environment"}
        required_props = {"Environment"}

    change_items = change_set.get("Changes")
    if not isinstance(change_items, list) or len(change_items) != 1:
        raise UnsafeChangeSet("Expected exactly one modified Lambda resource")
    change = change_items[0]
    if not isinstance(change, dict) or change.get("Type") != "Resource":
        raise UnsafeChangeSet("Unexpected change-set entry")
    resource = change.get("ResourceChange")
    if not isinstance(resource, dict):
        raise UnsafeChangeSet("Missing resource change")
    if (
        resource.get("LogicalResourceId") != "CookidooFunction"
        or resource.get("ResourceType") != "AWS::Lambda::Function"
        or resource.get("Action") != "Modify"
        or str(resource.get("Replacement")).lower() != "false"
        or resource.get("Scope") != ["Properties"]
    ):
        raise UnsafeChangeSet("Change set affects an unexpected resource or replacement")
    if resource.get("PhysicalResourceId") not in (None, "cookidoo-mcp"):
        raise UnsafeChangeSet("Change set targets a different Lambda")

    details = resource.get("Details")
    if not isinstance(details, list) or not details:
        raise UnsafeChangeSet("Lambda property changes lack reviewable details")
    found: set[str] = set()
    for detail in details:
        if not isinstance(detail, dict):
            raise UnsafeChangeSet("Malformed Lambda property details")
        target = detail.get("Target")
        if not isinstance(target, dict):
            raise UnsafeChangeSet("Missing Lambda property target")
        name = target.get("Name")
        if (
            target.get("Attribute") != "Properties"
            or name not in allowed_props
            or target.get("RequiresRecreation") != "Never"
            or detail.get("Evaluation") != "Static"
        ):
            raise UnsafeChangeSet(
                "Dynamic, replacement, or unexpected Lambda property modification"
            )
        found.add(name)
    if not required_props.issubset(found):
        raise UnsafeChangeSet("Expected Lambda Code/Environment property change missing")
    return sorted(found)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--change-set", type=Path, required=True)
    parser.add_argument("--stack", type=Path, required=True)
    parser.add_argument("--action", required=True)
    parser.add_argument("--code-key", required=True)
    parser.add_argument("--creation", required=True)
    parser.add_argument("--updates", required=True)
    args = parser.parse_args()
    try:
        approved = validate(
            json.loads(args.change_set.read_text(encoding="utf-8")),
            json.loads(args.stack.read_text(encoding="utf-8")),
            action=args.action,
            code_key=args.code_key,
            creation=args.creation,
            updates=args.updates,
        )
    except (UnsafeChangeSet, ValueError, OSError, TypeError, KeyError) as exc:
        print(
            f"::error::CloudFormation release rejected (no change set executed): {exc}",
            file=sys.stderr,
        )
        return 1
    print("Approved change set: CookidooFunction Modify; properties: " + ", ".join(approved))
    return 0


if __name__ == "__main__":
    sys.exit(main())
