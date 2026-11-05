#!/usr/bin/env bash
# One-time, CloudFormation-managed repair: allow only ssm:GetParameter for
# the existing origin bearer SSM SecureString. Never read or log the secret.
set -euo pipefail

[[ "${GITHUB_ACTIONS:-}" == "true" && "${GITHUB_REF:-}" == "refs/heads/master" ]] || {
  echo '::error::Production repair requires GitHub Actions on master' >&2; exit 1;
}
[[ "${AWS_REGION:-}" == "eu-west-2" && "${CODE_BUCKET:-}" == "aws2022-lambda-code" ]] || {
  echo '::error::Unexpected AWS region or artifact bucket' >&2; exit 1;
}
: "${AWS_CLOUDFORMATION_ROLE_ARN:?Missing scoped CloudFormation role}"

stack="$(aws cloudformation describe-stacks --region "$AWS_REGION" --stack-name cookidoo-mcp --output json)"
jq -e --arg bucket "$CODE_BUCKET" '
  .Stacks | length == 1
  and .[0].StackStatus == "UPDATE_COMPLETE"
  and any(.[0].Parameters[]?; .ParameterKey == "FunctionName" and .ParameterValue == "cookidoo-mcp")
  and any(.[0].Parameters[]?; .ParameterKey == "CodeBucket" and .ParameterValue == $bucket)
  and any(.[0].Parameters[]?; .ParameterKey == "EnableFunctionUrl" and .ParameterValue == "true")
' <<< "$stack" >/dev/null || {
  echo '::error::Unexpected stack state, function, bucket or Function URL setting' >&2; exit 1;
}
old_key="$(jq -er '.Stacks[0].Parameters[] | select(.ParameterKey == "CodeKey") | .ParameterValue' <<< "$stack")"
old_creation="$(jq -er '.Stacks[0].Parameters[] | select(.ParameterKey == "EnableRecipeCreation") | .ParameterValue' <<< "$stack")"
[[ "$old_creation" == "true" || "$old_creation" == "false" ]] || exit 1
old_url="$(aws lambda get-function-url-config --region "$AWS_REGION" --function-name cookidoo-mcp --query FunctionUrl --output text)"
[[ "$old_url" == https://*.lambda-url.eu-west-2.on.aws/ ]] || exit 1

# Preserve every live stack parameter, including the deployed ZIP key and
# write-enablement setting. Do not overwrite or rotate any credential.
parameters="$(jq -c '[.Stacks[0].Parameters[] | {ParameterKey: .ParameterKey, UsePreviousValue: true}]' <<< "$stack")"
change_name="cookidoo-origin-ssm-getparameter-${GITHUB_RUN_ID:?}"
aws cloudformation create-change-set --region "$AWS_REGION" \
  --stack-name cookidoo-mcp \
  --change-set-name "$change_name" --change-set-type UPDATE \
  --template-body file://infrastructure/cookidoo.yaml \
  --parameters "$parameters" \
  --capabilities CAPABILITY_NAMED_IAM \
  --role-arn "$AWS_CLOUDFORMATION_ROLE_ARN" >/dev/null
aws cloudformation wait change-set-create-complete \
  --region "$AWS_REGION" --stack-name cookidoo-mcp --change-set-name "$change_name"

changes="$(aws cloudformation describe-change-set --region "$AWS_REGION" \
  --stack-name cookidoo-mcp --change-set-name "$change_name" --output json)"
jq -e '
  [.Changes[]?.ResourceChange] as $items
  | ($items | length) == 1
    and all($items[];
      .LogicalResourceId == "CookidooRole"
      and .ResourceType == "AWS::IAM::Role"
      and .Action == "Modify"
      and (.Replacement // "False") != "True"
    )
' <<< "$changes" >/dev/null || {
  echo '::error::Refusing unexpected CloudFormation changes outside Cookidoo IAM role' >&2
  aws cloudformation delete-change-set --region "$AWS_REGION" \
    --stack-name cookidoo-mcp --change-set-name "$change_name" >/dev/null || true
  exit 1
}

aws cloudformation execute-change-set --region "$AWS_REGION" \
  --stack-name cookidoo-mcp --change-set-name "$change_name"
aws cloudformation wait stack-update-complete --region "$AWS_REGION" --stack-name cookidoo-mcp

# Confirm no change to code, public endpoint or recipe-creation toggle.
updated="$(aws cloudformation describe-stacks --region "$AWS_REGION" \
  --stack-name cookidoo-mcp --output json)"
jq -e --arg code "$old_key" --arg creation "$old_creation" '
  .Stacks[0].StackStatus == "UPDATE_COMPLETE"
  and any(.Stacks[0].Parameters[]?;
    .ParameterKey == "CodeKey" and .ParameterValue == $code)
  and any(.Stacks[0].Parameters[]?;
    .ParameterKey == "EnableRecipeCreation" and .ParameterValue == $creation)
' <<< "$updated" >/dev/null || {
  echo '::error::Post-deploy CloudFormation parameter checks failed' >&2; exit 1;
}
new_url="$(aws lambda get-function-url-config --region "$AWS_REGION" \
  --function-name cookidoo-mcp --query FunctionUrl --output text)"
[[ "$new_url" == "$old_url" ]] || {
  echo '::error::The Cookidoo Lambda Function URL changed unexpectedly' >&2; exit 1;
}
echo 'Origin bearer IAM GetParameter repair applied; Lambda code, URL, and write flag preserved.'
{
 echo '### Cookidoo origin bearer IAM repair'
 echo '- CloudFormation changes restricted to existing Cookidoo execution role'
 echo '- Scoped GetParameter on origin-bearer only'
 echo '- Original Lambda ZIP, Function URL and recipe-creation state preserved'
 echo '- SSM secret value not read, logged or changed'
} >> "$GITHUB_STEP_SUMMARY"
