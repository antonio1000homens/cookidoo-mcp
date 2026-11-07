#!/usr/bin/env bash
# Manual production release: preserves the already active Lambda URL.
set -euo pipefail

# Repeat exact confirmation validation in the privileged deployment job.
bash scripts/validate-recipe-release-request.sh

[[ "$GITHUB_ACTIONS" == "true" && "$GITHUB_REF" == "refs/heads/master" ]] || exit 1
[[ "$AWS_REGION" == "eu-west-2" && "$CODE_BUCKET" == "aws2022-lambda-code" ]] || exit 1
[[ "$RELEASE_ACTION" =~ ^(deploy-disabled|deploy-update-code|enable-writes|disable-writes|enable-updates|disable-updates)$ ]] || exit 1

stack="$(aws cloudformation describe-stacks --region "$AWS_REGION" --stack-name cookidoo-mcp --output json)"
parameter() {
  jq -er --arg key "$1" '.Stacks[0].Parameters[] | select(.ParameterKey == $key) | .ParameterValue' <<< "$stack"
}
[[ "$(parameter FunctionName)" == "cookidoo-mcp" ]] || exit 1
[[ "$(parameter CodeBucket)" == "$CODE_BUCKET" ]] || exit 1
[[ "$(parameter EnableFunctionUrl)" == "true" ]] || {
  echo 'Function URL must be enabled via its separate review gate.' >&2; exit 1;
}
old_url="$(aws lambda get-function-url-config --region "$AWS_REGION" \
  --function-name cookidoo-mcp --query FunctionUrl --output text)"
[[ "$old_url" == https://*.lambda-url.eu-west-2.on.aws/ ]] || exit 1
[[ "$(aws lambda get-function-url-config --region "$AWS_REGION" \
  --function-name cookidoo-mcp --query AuthType --output text)" == "NONE" ]] || exit 1
[[ "$(aws lambda get-function-configuration --region "$AWS_REGION" \
  --function-name cookidoo-mcp --query LastUpdateStatus --output text)" == "Successful" ]] || exit 1

code_key="$(parameter CodeKey)"
enabled="$(parameter EnableRecipeCreation 2>/dev/null || printf 'false')"
[[ "$enabled" == "false" || "$enabled" == "true" ]] || exit 1
updates="$(parameter EnableRecipeUpdates 2>/dev/null || printf 'false')"
[[ "$updates" == "false" || "$updates" == "true" ]] || exit 1

case "$RELEASE_ACTION" in
  deploy-disabled|deploy-update-code)
    if [[ "$RELEASE_ACTION" == 'deploy-disabled' ]]; then
      [[ "$enabled" == "false" && "$updates" == "false" ]] || {
        echo 'Disable all recipe writes before releasing in disabled mode.' >&2; exit 1;
      }
    else
      [[ "$updates" == "false" ]] || {
        echo 'Disable recipe updates before deploying updated code.' >&2; exit 1;
      }
      # Preserve the existing creation/image upload flag.
    fi
    zip_path='dist/cookidoo-mcp.zip'
    [[ -s "$zip_path" ]] || exit 1
    code_key="cookidoo-mcp/$GITHUB_SHA/cookidoo-mcp.zip"
    digest="$(sha256sum "$zip_path" | awk '{print $1}')"
    if ! aws s3api put-object --region "$AWS_REGION" --bucket "$CODE_BUCKET" \
      --key "$code_key" --body "$zip_path" --server-side-encryption AES256 \
      --metadata "sha256=$digest" --if-none-match '*' >/dev/null; then
      remote="$(aws s3api head-object --region "$AWS_REGION" --bucket "$CODE_BUCKET" \
        --key "$code_key" --query Metadata.sha256 --output text)"
      [[ "$remote" == "$digest" ]] || { echo 'Immutable release mismatch' >&2; exit 1; }
    fi
    if [[ "$RELEASE_ACTION" == 'deploy-disabled' ]]; then enabled=false; fi
    ;;
  enable-writes)
    [[ "$enabled" == "false" ]] || { echo 'Writes already enabled' >&2; exit 1; }
    [[ "$code_key" == "cookidoo-mcp/$GITHUB_SHA/cookidoo-mcp.zip" ]] || {
      echo 'Live release differs from this reviewed commit; deploy-disabled first.' >&2; exit 1;
    }
    enabled=true
    ;;
  disable-writes)
    enabled=false
    ;;
  enable-updates)
    [[ "$updates" == "false" ]] || { echo 'Recipe updates already enabled' >&2; exit 1; }
    [[ "$code_key" == "cookidoo-mcp/$GITHUB_SHA/cookidoo-mcp.zip" ]] || {
      echo 'Live release differs from this reviewed commit; deploy-update-code first.' >&2; exit 1;
    }
    updates=true
    ;;
  disable-updates)
    updates=false
    ;;
esac

# Do not call 'aws cloudformation deploy' here. In particular, existing
# CloudFormation drift in issue #16 MUST NOT be silently reconciled by a
# recipe release. Inspect and execute the *same* immutable change-set ARN.
change_set_dir="$(mktemp -d)"
chmod 700 "$change_set_dir"
change_set_arn=''
cleanup() {
  if [[ -n "$change_set_arn" ]]; then
    # Safe to attempt deletion after execution; ignore already-executed sets.
    aws cloudformation delete-change-set --region "$AWS_REGION" \
      --stack-name cookidoo-mcp --change-set-name "$change_set_arn" >/dev/null 2>&1 || true
  fi
  rm -rf "$change_set_dir"
}
trap cleanup EXIT

printf '%s' "$stack" > "$change_set_dir/current-stack.json"
# Use the *live* original stack template and current Lambda environment to
# prove that the first update rollout changes only its reviewed flag and that
# no hidden environment drift will be overwritten. Private files are removed
# by the EXIT trap and must never appear in runner logs/artifacts.
aws cloudformation get-template --region "$AWS_REGION" \
  --stack-name cookidoo-mcp --template-stage Original \
  --query TemplateBody --output text > "$change_set_dir/current-template.yaml"
aws lambda get-function-configuration --region "$AWS_REGION" \
  --function-name cookidoo-mcp \
  --query Environment.Variables --output json > "$change_set_dir/current-env.json"

# Retain the current approved configuration. Only the deliberately requested
# CodeKey and feature flags may differ from the existing stack parameters.
jq --arg code "$code_key" --arg creation "$enabled" --arg updates "$updates" '
  [.Stacks[0].Parameters[]
   | select(.ParameterKey != "CodeKey" and
            .ParameterKey != "EnableRecipeCreation" and
            .ParameterKey != "EnableRecipeUpdates")
   | {ParameterKey, ParameterValue}]
  + [
      {ParameterKey:"CodeKey", ParameterValue:$code},
      {ParameterKey:"EnableRecipeCreation", ParameterValue:$creation},
      {ParameterKey:"EnableRecipeUpdates", ParameterValue:$updates}
    ]
' "$change_set_dir/current-stack.json" > "$change_set_dir/requested-parameters.json"

[[ -n "${GITHUB_RUN_ID:-}" && -n "${GITHUB_RUN_ATTEMPT:-}" ]] || {
  echo 'Missing GitHub Actions run identity; change set not created' >&2; exit 1;
}
change_set_name="cookidoo-${GITHUB_RUN_ID}-${GITHUB_RUN_ATTEMPT}-${RELEASE_ACTION}"
change_set_arn="$(aws cloudformation create-change-set --region "$AWS_REGION" \
  --stack-name cookidoo-mcp --change-set-name "$change_set_name" \
  --change-set-type UPDATE --template-body file://infrastructure/cookidoo.yaml \
  --role-arn "$AWS_CLOUDFORMATION_ROLE_ARN" --capabilities CAPABILITY_NAMED_IAM \
  --parameters "file://$change_set_dir/requested-parameters.json" \
  --query Id --output text)"
[[ "$change_set_arn" == arn:aws:cloudformation:eu-west-2:*:changeSet/* ]] || {
  echo 'Could not obtain a valid CloudFormation change set ARN' >&2; exit 1;
}
if ! aws cloudformation wait change-set-create-complete --region "$AWS_REGION" \
  --stack-name cookidoo-mcp --change-set-name "$change_set_arn"; then
  echo '::error::Change set creation failed or contains no executable changes; no deployment attempted.' >&2
  exit 1
fi

aws cloudformation describe-change-set --region "$AWS_REGION" \
  --stack-name cookidoo-mcp --change-set-name "$change_set_arn" \
  --output json > "$change_set_dir/change-set.json"
python3 scripts/guard-recipe-change-set.py \
  --change-set "$change_set_dir/change-set.json" \
  --stack "$change_set_dir/current-stack.json" \
  --action "$RELEASE_ACTION" --code-key "$code_key" \
  --creation "$enabled" --updates "$updates" \
  --existing-template "$change_set_dir/current-template.yaml" \
  --proposed-template infrastructure/cookidoo.yaml \
  --current-env "$change_set_dir/current-env.json"

# Only execute the ARN verified above; never regenerate a change set between
# inspection and execution. One repository-wide release concurrency group
# serialises normal releases; out-of-band changes fail the resource allowlist.
aws cloudformation execute-change-set --region "$AWS_REGION" \
  --stack-name cookidoo-mcp --change-set-name "$change_set_arn"
aws cloudformation wait stack-update-complete --region "$AWS_REGION" \
  --stack-name cookidoo-mcp

aws lambda wait function-updated-v2 --region "$AWS_REGION" --function-name cookidoo-mcp
new_url="$(aws lambda get-function-url-config --region "$AWS_REGION" \
  --function-name cookidoo-mcp --query FunctionUrl --output text)"
[[ "$new_url" == "$old_url" ]] || { echo 'Origin URL unexpectedly changed' >&2; exit 1; }
flag="$(aws lambda get-function-configuration --region "$AWS_REGION" \
  --function-name cookidoo-mcp \
  --query 'Environment.Variables.COOKIDOO_RECIPE_CREATION_ENABLED' --output text)"
[[ "$flag" == "$enabled" ]] || { echo 'Write state mismatch' >&2; exit 1; }
update_flag="$(aws lambda get-function-configuration --region "$AWS_REGION" \
  --function-name cookidoo-mcp \
  --query 'Environment.Variables.COOKIDOO_RECIPE_UPDATES_ENABLED' --output text)"
[[ "$update_flag" == "$updates" ]] || { echo 'Recipe update state mismatch' >&2; exit 1; }
# Validate the *entire* post-release environment, not merely feature flags.
# Only the two explicitly approved switches may differ from the preflight.
aws lambda get-function-configuration --region "$AWS_REGION" \
  --function-name cookidoo-mcp \
  --query Environment.Variables --output json > "$change_set_dir/deployed-env.json"
jq -e --slurpfile before "$change_set_dir/current-env.json" \
  --arg creation "$enabled" --arg updates "$updates" \
  '. == ($before[0] + {
    COOKIDOO_RECIPE_CREATION_ENABLED: $creation,
    COOKIDOO_RECIPE_UPDATES_ENABLED: $updates
  })' "$change_set_dir/deployed-env.json" >/dev/null || {
    echo '::error::Unexpected Lambda environment change after deployment' >&2
    exit 1
  }
{
  echo '### Cookidoo MCP production release'
  echo "- Operation: $RELEASE_ACTION"
  echo "- Recipe creation enabled: $enabled"
  echo "- In-place recipe updates enabled: $updates"
  echo '- Existing Lambda Function URL was preserved'
  echo '- No recipe was uploaded'
} >> "$GITHUB_STEP_SUMMARY"
