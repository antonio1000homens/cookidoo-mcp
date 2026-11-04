#!/usr/bin/env bash
# One-time *manual* setup. Creates only two IAM roles and GitHub environment variables.
# Does NOT deploy the application, create an S3 bucket or write Cookidoo credentials.
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
region="${AWS_REGION:-eu-west-2}"
profile="${AWS_PROFILE:-windsor}"
stack="${BOOTSTRAP_STACK_NAME:-cookidoo-mcp-bootstrap}"
code_bucket="${CODE_BUCKET:-aws2022-lambda-code}"
repository="antonio1000homens/cookidoo-mcp"
environment="production"

for cmd in aws gh; do
  command -v "$cmd" >/dev/null 2>&1 || { echo "Missing command: $cmd" >&2; exit 1; }
done
gh auth status >/dev/null
aws_cli() { aws --profile "$profile" --region "$region" "$@"; }

account_id="$(aws_cli sts get-caller-identity --query Account --output text)"
provider="arn:aws:iam::${account_id}:oidc-provider/token.actions.githubusercontent.com"
# Must already exist, as used by RSVP/NFC. Do not create a second GitHub provider.
aws_cli iam get-open-id-connect-provider --open-id-connect-provider-arn "$provider" >/dev/null
actual_region="$(aws_cli s3api get-bucket-location --bucket "$code_bucket" --query LocationConstraint --output text)"
if [[ "$actual_region" != "$region" ]]; then
  echo "Code bucket $code_bucket is in $actual_region, expected $region" >&2
  exit 1
fi

aws_cli cloudformation validate-template \
  --template-body "file://$root/infrastructure/bootstrap-github.yaml" >/dev/null
aws_cli cloudformation deploy \
  --stack-name "$stack" \
  --template-file "$root/infrastructure/bootstrap-github.yaml" \
  --capabilities CAPABILITY_NAMED_IAM \
  --no-fail-on-empty-changeset \
  --parameter-overrides \
    "GitHubOidcProviderArn=$provider" "CodeBucket=$code_bucket"

deploy_role="$(aws_cli cloudformation describe-stacks --stack-name "$stack" \
  --query "Stacks[0].Outputs[?OutputKey=='GitHubDeployRoleArn'].OutputValue | [0]" --output text)"
cfn_role="$(aws_cli cloudformation describe-stacks --stack-name "$stack" \
  --query "Stacks[0].Outputs[?OutputKey=='CloudFormationServiceRoleArn'].OutputValue | [0]" --output text)"

if [[ -z "$deploy_role" || "$deploy_role" == None || -z "$cfn_role" || "$cfn_role" == None ]]; then
  echo "Could not retrieve bootstrap role ARNs" >&2
  exit 1
fi

# Restrict the production environment to master. GitHub workflow deployment
# is manually gated; configure additional required reviewers in GitHub UI.
printf '%s\n' '{"deployment_branch_policy":{"protected_branches":false,"custom_branch_policies":true}}' \
  | gh api --method PUT "repos/$repository/environments/$environment" --input - >/dev/null
if ! gh api "repos/$repository/environments/$environment/deployment-branch-policies" \
    --jq '.branch_policies[]? | select(.name == "master") | .name' | grep -qx master; then
  printf '%s\n' '{"name":"master","type":"branch"}' \
    | gh api --method POST \
        "repos/$repository/environments/$environment/deployment-branch-policies" \
        --input - >/dev/null
fi

gh variable set AWS_ROLE_TO_ASSUME --repo "$repository" --env "$environment" --body "$deploy_role"
gh variable set AWS_CLOUDFORMATION_ROLE_ARN --repo "$repository" --env "$environment" --body "$cfn_role"
gh variable set CODE_BUCKET --repo "$repository" --env "$environment" --body "$code_bucket"
gh variable set AWS_REGION --repo "$repository" --env "$environment" --body "$region"

echo "Bootstrap configured $repository / $environment on $region."
echo "Set CF_ACCESS_TEAM_DOMAIN and CF_ACCESS_AUD in the same GitHub environment."
echo "Do not place Cookidoo username/password in GitHub: provision SSM separately."
echo "No application deployed and no Function URL created."
