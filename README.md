# Cookidoo MCP — Thermomix TM7 (native AWS Lambda ZIP)

Standalone public infrastructure/application repository for an **unofficial** Cookidoo integration. Python runtime uses upstream [alexandrepa/mcp-cookidoo](https://github.com/alexandrepa/mcp-cookidoo) pinned at commit `6f7c33b1d23f8257814fd790341bc84cd88785e0` (v1.1.0). Do not commit passwords, cookies, tokens or personal recipes.

**Status:** The Lambda-first foundation and manual OIDC ZIP deployment are merged. The origin requires both a dedicated Worker bearer and a cryptographically verified Cloudflare Access JWT. Public AWS and Cloudflare activation remain separate, manually gated operations; this repository change does not run them.

## Design and zero-cost target

```text
ChatGPT custom MCP connector
  -> Cloudflare Access Managed OAuth (central configuration: windsor-app)
  -> Cloudflare Worker adds a dedicated origin bearer and forwards the signed Access JWT
  -> AWS Lambda Function URL, native Python 3.12 ZIP handler (buffered JSON)
  -> pinned upstream FastMCP tools and cookidoo-api
  -> Cookidoo
```

There are **no containers**, ECR, Docker builds, running VMs, SQS polling, DynamoDB tables, provisioned concurrency, NAT gateways, API Gateway or AWS Secrets Manager charges. The planned release uses an existing regional S3 deployment bucket for the ZIP. The Lambda Function URL is **disabled by default**; once activated, every request requires the SSM-backed origin bearer and a cryptographically verified `Cf-Access-Jwt-Assertion`, including direct calls to the raw AWS URL. `/health` is also protected and reveals no account information.

**£0/month is a target, not a contractual guarantee:** free allowances are shared with other workloads. Lambda duration/requests, CloudWatch Logs, S3 storage and data transfer can incur usage costs. Choose SSM **standard** SecureString parameters under standard throughput and the AWS-managed KMS key to avoid fixed secret-store charges. Do not enable advanced parameters, provisioned concurrency, a NAT gateway or other resources with fixed charges.

The Lambda checks the Worker bearer against AWS SSM Standard SecureString using a constant-time comparison, then independently validates the Access JWT. Missing/unavailable secrets, missing bearer, and invalid JWT all fail closed. The secret is cached per warm Lambda environment; rotate it by provisioning the new value and deploying/recycling the Lambda before updating the Worker secret. The Worker secret and SSM value must be identical and must never be placed in tracked configuration. The native handler uses Mangum and FastMCP's `stateless_http=True, json_response=True`.

### Guarded recipe creation (disabled until separately approved)

The default remote tool catalog includes the five original read/validation tools plus \`preview_custom_recipe_upload\`. This preview validates upstream \`CustomRecipe\` data (including TM7 settings, reverse speeds and ingredient links), applies strict payload limits, and returns a JSON preview plus a SHA-256 confirmation string. It does not write anything.

\`upload_custom_recipe\` is registered **only if** the separate CloudFormation parameter \`EnableRecipeCreation=true\` is explicitly set. It requires the exact previewed recipe JSON and matching \`CREATE-COOKIDOO-SHA256:<digest>\` confirmation **after a user approves that exact preview**. This is a data-integrity gate, not proof of user consent; clients must obtain explicit user approval before invoking the write tool. Existing-recipe names are checked before submission. A create-only S3 marker in the pre-existing code bucket under \`cookidoo-mcp/write-guards/\` prevents multiple writes using the same normalized title across concurrent or repeated invocations. A timeout or ambiguous remote failure retains its marker to prevent unsafe automatic retries. Inspect the account and marker manually before clearing and retrying. Store no recipe text or credentials in this marker; only SHA-256 hashes.

The existing \`update_custom_recipe\`, deletion and \`connect_to_cookidoo\` remain unavailable. Normal deployment remains private/read-only: no new AWS service, public URL or write activation is performed by merging this change. **Do not switch on writes until a disposable recipe test, account-scoped access and an explicit production approval have been reviewed.**

Defaults are `COOKIDOO_COUNTRY=gb`, `COOKIDOO_LANGUAGE=en-GB` and `COOKIDOO_DEVICE=TM7`.

## ZIP build / offline tests

Prerequisites: Git, [uv](https://docs.astral.sh/uv/), ZIP and Python 3.12. Use Linux x86_64 for native dependency wheel compatibility with AWS Lambda.

```bash
bash scripts/build-zip.sh
unzip -l dist/cookidoo-mcp.zip | head
```

The script fetches the **pinned** upstream commit, exports its locked dependencies, installs Mangum plus the existing SSM/JWT libraries, and packages the normal `src.lambda_handler.handler` Lambda entry point. It does not log in to Cookidoo or contact AWS. The GitHub CI builds a ZIP and runs offline unit tests. Existing live Cookidoo credentials must never appear in GitHub tests or actions logs.

## CloudFormation deployment

`infrastructure/cookidoo.yaml` is a ZIP-based Lambda template. It requires:

- `CodeBucket` and `CodeKey`: a same-region, existing S3 object containing the Lambda ZIP.
- `CloudflareTeamDomain` and `CloudflareAudience`: the Access issuer and application audience (not a secret).
- Three Standard SecureString SSM parameters `/cookidoo-mcp/email`, `/cookidoo-mcp/password`, and `/cookidoo-mcp/origin-bearer`; the function role may read only these exact parameters.
- `EnableFunctionUrl=false` (default): no public HTTP origin is provisioned. Do not turn it on until Cloudflare routing, JWT verification, native MCP JSON-RPC and account logins are demonstrated end-to-end.

Lambda: Python 3.12, 512 MB, 30-second timeout, reserved concurrency 2, seven-day log retention. The deployment uses an **existing S3 code bucket**, rather than an ECR image repository. The separate GitHub OIDC bootstrap and manually gated release workflow are in `infrastructure/bootstrap-github.yaml` and `.github/workflows/deploy.yml`. Merge and review those before running either action.

## Remaining implementation gates

1. Native Lambda Function URL JSON-RPC initialise, tools/list, tools/call compatibility.
2. Real UK TM7 account login after cold start, expiry/retry and concurrency tests.
3. After merging the Windsor PR, stage the disabled Worker.
4. Run `scripts/cookidoo-origin-bootstrap.sh` from the Windsor app checkout to generate one bearer and install it in SSM and the Worker secret. It does not activate either endpoint.
5. Run the manual GitHub OIDC bootstrap if needed, then deploy the **private** Lambda with `deploy.yml`.
6. After auth tests pass, separately activate the AWS Function URL. Verify requests without credentials and with either credential alone return 403 while Worker forwarding stays disabled.
7. Separately activate Worker forwarding after direct-origin checks and real ChatGPT OAuth/MCP tests are reviewed.
8. Profile free-tier consumption/shared budgets to avoid unexpected AWS charges.
9. Run offline recipe-preview/idempotency tests; separately approve and enable guarded creation only after a disposable test recipe has been reviewed.

Full plan: [issue #1](https://github.com/antonio1000homens/cookidoo-mcp/issues/1).

## GitHub OIDC deployment (manual, separately gated activation)

The deployment pipeline mirrors the per-application ownership in [rsvp](https://github.com/antonio1000homens/rsvp), but **never automatically deploys on push/merge**. It uses the existing regional S3 bucket, creates **no** Docker/ECR repository, paid secret store, database, queue, NAT gateway, or provisioned concurrency.

### 1. One-time AWS / GitHub bootstrap

From your local machine with a valid AWS profile for the intended account, an authenticated `gh` CLI, and permission to create IAM roles:

```bash
AWS_PROFILE=windsor AWS_REGION=eu-west-2 CODE_BUCKET=aws2022-lambda-code \
  bash scripts/bootstrap-aws.sh
```

This **explicit manual command** deploys `infrastructure/bootstrap-github.yaml`: two application-scoped IAM roles (GitHub OIDC deploy and CloudFormation service), **no billable infrastructure**. It reuses your existing `token.actions.githubusercontent.com` IAM OIDC provider rather than creating a duplicate. The GitHub trust policy uses the exact immutable GitHub owner/repo IDs for `antonio1000homens/cookidoo-mcp` and **only** the `production` environment (as used in `nfc`); it does not grant role access to generic pull requests.

The bootstrap also creates/updates the `production` GitHub environment, restricts deployments to `master`, and sets these **non-secret environment variables** automatically: `AWS_ROLE_TO_ASSUME`, `AWS_CLOUDFORMATION_ROLE_ARN`, `AWS_REGION`, `CODE_BUCKET`. Consider additionally configuring an environment reviewer and protecting the `master` branch in GitHub Settings.

You must separately supply `CF_ACCESS_TEAM_DOMAIN` (e.g. `https://myteam.cloudflareaccess.com`) and `CF_ACCESS_AUD` (the Cookidoo Access application audience) as `production` environment variables. Neither is a password. **No Cloudflare application or route is created by this workflow.**

Provision `/cookidoo-mcp/email` and `/cookidoo-mcp/password` in SSM Parameter Store in the same AWS account/region as **Standard SecureString**, encrypted with `alias/aws/ssm`. The origin bearer is provisioned by the Windsor terminal bootstrap script; do not put credentials in tracked files, GitHub variables, command arguments, build logs or issues.

### 2. Choose the manually dispatched workflow

In **Actions → Validate / deploy Cookidoo MCP ZIP Lambda (manual) → Run workflow** on `master`:

- `validate` (default): runs shell/CloudFormation checks, builds the 3.12 ZIP using pinned upstream dependencies and retains a one-day CI artifact. No AWS credentials and no deployment.
- `deploy`: enter exactly `DEPLOY-PRIVATE-LAMBDA`, satisfy the protected `production` environment gate, and assume the repo-specific IAM role via short-lived OIDC. The same validated ZIP is uploaded to `s3://<CODE_BUCKET>/cookidoo-mcp/<commit SHA>/cookidoo-mcp.zip` (checksummed) and the `cookidoo-mcp` CloudFormation stack is deployed.

The deploy role can write only under that S3 prefix and manage only the Cookidoo stack. Private deployment keeps the URL disabled and fails if one already exists. The CloudFormation role can manage the Cookidoo Function URL, but activation is available only through a separate confirmation-gated workflow. Cookidoo account credentials are never exposed to GitHub Actions.

This workflow does not enable ChatGPT connectivity yet. The future Cloudflare Access/OAuth integration and live cold-start/login testing must follow in separate reviewed work.

### 3. Cost and release considerations

IAM roles and CloudFormation stacks have no separate service fee, but the **£0/month objective is not a guarantee**. Your existing S3 bucket accumulates versioned ZIP objects (~35 MiB each), and Lambda invocation, CloudWatch Logs, network transfer and storage usage count against AWS account billing/free allowances. Periodically remove obsolete S3 keys after keeping an appropriate rollback version. No AWS resources are created by merely merging this pull request.
