#!/usr/bin/env bash
# Manual production release: preserves the already active Lambda URL.
set -euo pipefail

[[ "$GITHUB_ACTIONS" == "true" && "$GITHUB_REF" == "refs/heads/master" ]] || exit 1
[[ "$AWS_REGION" == "eu-west-2" && "$CODE_BUCKET" == "aws2022-lambda-code" ]] || exit 1
[[ "$RELEASE_ACTION" =~ ^(deploy-disabled|enable-writes|disable-writes)$ ]] || exit 1

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

case "$RELEASE_ACTION" in
  deploy-disabled)
    [[ "$enabled" == "false" ]] || {
      echo 'Disable existing recipe writes before releasing new code.' >&2; exit 1;
    }
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
    enabled=false
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
esac

aws cloudformation deploy --region "$AWS_REGION" --stack-name cookidoo-mcp \
  --template-file infrastructure/cookidoo.yaml \
  --role-arn "$AWS_CLOUDFORMATION_ROLE_ARN" --capabilities CAPABILITY_NAMED_IAM \
  --no-fail-on-empty-changeset --parameter-overrides \
  "FunctionName=$(parameter FunctionName)" \
  "CodeBucket=$(parameter CodeBucket)" "CodeKey=$code_key" \
  "EmailParameterName=$(parameter EmailParameterName)" \
  "PasswordParameterName=$(parameter PasswordParameterName)" \
  "OriginBearerParameterName=$(parameter OriginBearerParameterName)" \
  "CloudflareTeamDomain=$(parameter CloudflareTeamDomain)" \
  "CloudflareAudience=$(parameter CloudflareAudience)" \
  "EnableFunctionUrl=true" "EnableRecipeCreation=$enabled"

aws lambda wait function-updated-v2 --region "$AWS_REGION" --function-name cookidoo-mcp
new_url="$(aws lambda get-function-url-config --region "$AWS_REGION" \
  --function-name cookidoo-mcp --query FunctionUrl --output text)"
[[ "$new_url" == "$old_url" ]] || { echo 'Origin URL unexpectedly changed' >&2; exit 1; }
flag="$(aws lambda get-function-configuration --region "$AWS_REGION" \
  --function-name cookidoo-mcp \
  --query 'Environment.Variables.COOKIDOO_RECIPE_CREATION_ENABLED' --output text)"
[[ "$flag" == "$enabled" ]] || { echo 'Write state mismatch' >&2; exit 1; }
{
  echo '### Cookidoo MCP production release'
  echo "- Operation: $RELEASE_ACTION"
  echo "- Recipe writes enabled: $enabled"
  echo '- Existing Lambda Function URL was preserved'
  echo '- No recipe was uploaded'
} >> "$GITHUB_STEP_SUMMARY"
