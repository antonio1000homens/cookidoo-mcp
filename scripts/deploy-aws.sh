#!/usr/bin/env bash
# Deliberately manual / protected production environment only.
# Never enables an AWS Function URL; does not fetch Cookidoo credentials.
set -euo pipefail

[[ "${GITHUB_ACTIONS:-}" == "true" ]] || { echo "Run only from protected GitHub Actions production deployment." >&2; exit 1; }
[[ "${GITHUB_REF:-}" == "refs/heads/master" ]] || { echo "Must deploy from master" >&2; exit 1; }

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
region="${AWS_REGION:-eu-west-2}"
code_bucket="${CODE_BUCKET:?Set CODE_BUCKET as a GitHub production environment variable}"
cfn_role="${AWS_CLOUDFORMATION_ROLE_ARN:?Set AWS_CLOUDFORMATION_ROLE_ARN in the production environment}"
team_domain="${CF_ACCESS_TEAM_DOMAIN:?Set CF_ACCESS_TEAM_DOMAIN in the production environment}"
audience="${CF_ACCESS_AUD:?Set CF_ACCESS_AUD in the production environment}"
function=cookidoo-mcp
code_key="cookidoo-mcp/${GITHUB_SHA:?}/cookidoo-mcp.zip"
zip_path="$root/dist/cookidoo-mcp.zip"

test -s "$zip_path" || { echo "Missing validated Cookidoo ZIP" >&2; exit 1; }
# Only a same-region existing artifact bucket may be used.
bucket_region="$(aws s3api get-bucket-location \
  --bucket "$code_bucket" --region "$region" \
  --query LocationConstraint --output text)"
[[ "$bucket_region" == "$region" ]] || {
  echo "Code bucket region mismatch" >&2
  exit 1
}

assert_no_function_url() {
  local error
  if error="$(aws lambda get-function-url-config --function-name "$function" \
    --region "$region" 2>&1)"; then
    echo "::error::Deployment halted: this Lambda already has a Function URL."
    exit 1
  elif [[ "$error" != *ResourceNotFoundException* ]]; then
    echo "::error::Could not determine Lambda Function URL state."
    exit 1
  fi
}
assert_no_function_url

local_digest="$(sha256sum "$zip_path" | awk '{print $1}')"
if remote_digest="$(aws s3api head-object \
  --bucket "$code_bucket" --key "$code_key" --region "$region" \
  --query 'Metadata.sha256' --output text 2>&1)"; then
  [[ "$remote_digest" == "$local_digest" ]] || {
    echo "::error::Existing release ZIP differs from current commit."
    exit 1
  }
elif [[ "$remote_digest" == *"(404)"* || "$remote_digest" == *"Not Found"* || "$remote_digest" == *"NoSuchKey"* ]]; then
  # Race-safe upload: never overwrite an artifact previously stored under a commit SHA.
  aws s3api put-object \
    --region "$region" --bucket "$code_bucket" --key "$code_key" \
    --body "$zip_path" --server-side-encryption AES256 \
    --metadata "sha256=$local_digest" --if-none-match '*' >/dev/null
else
  echo "::error::Unable to inspect existing S3 release; refusing to overwrite."
  exit 1
fi

aws cloudformation validate-template --region "$region" \
  --template-body "file://$root/infrastructure/cookidoo.yaml" >/dev/null
aws cloudformation deploy \
  --region "$region" \
  --stack-name "$function" \
  --template-file "$root/infrastructure/cookidoo.yaml" \
  --role-arn "$cfn_role" \
  --capabilities CAPABILITY_NAMED_IAM \
  --no-fail-on-empty-changeset \
  --parameter-overrides \
    "FunctionName=$function" \
    "CodeBucket=$code_bucket" \
    "CodeKey=$code_key" \
    "CloudflareTeamDomain=$team_domain" \
    "CloudflareAudience=$audience" \
    "EnableFunctionUrl=false"

aws lambda wait function-active-v2 --region "$region" --function-name "$function"
assert_no_function_url
aws lambda get-function-configuration --region "$region" \
  --function-name "$function" \
  --query '[FunctionName,Runtime,State,LastUpdateStatus]' --output table
{
  echo '### Private Cookidoo MCP Lambda deployed'
  echo "- Git commit: ${GITHUB_SHA}"
  echo "- Code key: $code_key"
  echo '- Public Function URL: **disabled**'
  echo '- Next gate: test signed Cloudflare Access/OAuth routing and live Cookidoo authentication.'
} >> "${GITHUB_STEP_SUMMARY:-/dev/null}"
