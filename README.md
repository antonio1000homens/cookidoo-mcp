# Cookidoo MCP — Thermomix TM7 (native AWS Lambda ZIP)

Standalone public infrastructure/application repository for an **unofficial** Cookidoo integration. Python runtime uses upstream [alexandrepa/mcp-cookidoo](https://github.com/alexandrepa/mcp-cookidoo) pinned at commit `6f7c33b1d23f8257814fd790341bc84cd88785e0` (v1.1.0). Do not commit passwords, cookies, tokens or personal recipes.

**Status:** The Lambda-first foundation (PR #2) and native ZIP/Mangum deployment (PR #3) are merged. A follow-up PR adds manual GitHub OIDC deployment. **No AWS deployment has been performed by this repository work.**

## Design and zero-cost target

```text
ChatGPT custom MCP connector
  -> Cloudflare Access Managed OAuth (central configuration: windsor-app)
  -> future authenticated HTTPS forwarding (to be designed/tested)
  -> AWS Lambda Function URL, native Python 3.12 ZIP handler (buffered JSON)
  -> pinned upstream FastMCP tools and cookidoo-api
  -> Cookidoo
```

There are **no containers**, ECR, Docker builds, running VMs, SQS polling, DynamoDB tables, provisioned concurrency, NAT gateways, API Gateway or AWS Secrets Manager charges. The planned release uses an existing regional S3 deployment bucket for the ZIP. The Lambda Function URL is **disabled by default**; a signed `Cf-Access-Jwt-Assertion` is cryptographically verified on *every MCP request*, including those sent directly to its raw AWS URL. `/health` reveals no account information.

**£0/month is a target, not a contractual guarantee:** free allowances are shared with other workloads. Lambda duration/requests, CloudWatch Logs, S3 storage and data transfer can incur usage costs. Choose SSM **standard** SecureString parameters under standard throughput and the AWS-managed KMS key to avoid fixed secret-store charges. Do not enable advanced parameters, provisioned concurrency, a NAT gateway or other resources with fixed charges.

The native Lambda handler in `src/lambda_handler.py` uses Mangum and FastMCP's `stateless_http=True, json_response=True`. It intentionally buffers JSON responses rather than opening long-lived SSE connections. The real ChatGPT client must be tested for protocol compatibility before production activation; if stateless JSON fails, investigate alternative adapters/hosting rather than assuming all FastMCP transports work on Lambda.

### Read-only first

Only `search_recipes`, `get_recipe_details`, `list_custom_recipes`, `get_custom_recipe` and `generate_recipe_structure` are registered. There is **no** `connect_to_cookidoo` (which exposes account email), `upload_custom_recipe`, or `update_custom_recipe` tool. Custom recipe writes require additional explicit, server-enforced safeguards and approval in a later PR.

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
- Two existing Standard SecureString SSM parameters `/cookidoo-mcp/email` and `/cookidoo-mcp/password`; the function's role may read **only** these parameters.
- `EnableFunctionUrl=false` (default): no public HTTP origin is provisioned. Do not turn it on until Cloudflare routing, JWT verification, native MCP JSON-RPC and account logins are demonstrated end-to-end.

Lambda: Python 3.12, 512 MB, 30-second timeout, reserved concurrency 2, seven-day log retention. The deployment uses an **existing S3 code bucket**, rather than an ECR image repository. The separate GitHub OIDC bootstrap and manually gated release workflow are in `infrastructure/bootstrap-github.yaml` and `.github/workflows/deploy.yml`. Merge and review those before running either action.

## Remaining implementation gates

1. Native Lambda Function URL JSON-RPC initialise, tools/list, tools/call compatibility.
2. Real UK TM7 account login after cold start, expiry/retry and concurrency tests.
3. Run the **one-time manual GitHub OIDC bootstrap**, then use the `deploy.yml` workflow's explicit confirmation to deploy the **private** Lambda; Function URL stays **disabled**.
4. Secure Cloudflare Access Managed OAuth routing and raw AWS origin bypass tests.
5. Profile free-tier consumption/shared budgets to avoid unexpected AWS charges.
6. Later explicit write-approval controls and disposable test custom recipe.

Full plan: [issue #1](https://github.com/antonio1000homens/cookidoo-mcp/issues/1).

## GitHub OIDC deployment (manual, private-only)

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

Provision `/cookidoo-mcp/email` and `/cookidoo-mcp/password` in SSM Parameter Store in the same AWS account/region as **Standard SecureString**, encrypted with the AWS-managed default `alias/aws/ssm` key. Use the AWS Console or another secret-safe mechanism; **do not** paste account credentials into repo, GitHub variables, commands saved in shell history, build logs or issues. The bootstrap does not create or read these secrets.

### 2. Choose the manually dispatched workflow

In **Actions → Validate / deploy Cookidoo MCP ZIP Lambda (manual) → Run workflow** on `master`:

- `validate` (default): runs shell/CloudFormation checks, builds the 3.12 ZIP using pinned upstream dependencies and retains a one-day CI artifact. No AWS credentials and no deployment.
- `deploy`: enter exactly `DEPLOY-PRIVATE-LAMBDA`, satisfy the protected `production` environment gate, and assume the repo-specific IAM role via short-lived OIDC. The same validated ZIP is uploaded to `s3://<CODE_BUCKET>/cookidoo-mcp/<commit SHA>/cookidoo-mcp.zip` (checksummed) and the `cookidoo-mcp` CloudFormation stack is deployed.

The deploy role can write **only** under that S3 prefix and manage **only** the Cookidoo stack. The CloudFormation role can create/update the `cookidoo-mcp` Lambda, its `cookidoo-mcp-execution` IAM role and its log group. **Neither role is allowed to expose a Lambda Function URL**; a separate, security-reviewed change will be required for Cloudflare routing. A post-deploy gate fails if a Function URL exists. No Cookidoo credentials are exposed to GitHub Actions.

This workflow does not enable ChatGPT connectivity yet. The future Cloudflare Access/OAuth integration and live cold-start/login testing must follow in separate reviewed work.

### 3. Cost and release considerations

IAM roles and CloudFormation stacks have no separate service fee, but the **£0/month objective is not a guarantee**. Your existing S3 bucket accumulates versioned ZIP objects (~35 MiB each), and Lambda invocation, CloudWatch Logs, network transfer and storage usage count against AWS account billing/free allowances. Periodically remove obsolete S3 keys after keeping an appropriate rollback version. No AWS resources are created by merely merging this pull request.
