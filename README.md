# Cookidoo MCP — Thermomix TM7 (native AWS Lambda ZIP)

Standalone public infrastructure/application repository for an **unofficial** Cookidoo integration. Python runtime uses upstream [alexandrepa/mcp-cookidoo](https://github.com/alexandrepa/mcp-cookidoo) pinned at commit `6f7c33b1d23f8257814fd790341bc84cd88785e0` (v1.1.0). Do not commit passwords, cookies, tokens or personal recipes.

**Status:** Lambda-first feasibility spike merged in [PR #2](https://github.com/antonio1000homens/cookidoo-mcp/pull/2). The next PR converts its container packaging to an ordinary Python ZIP Lambda using [Mangum](https://pypi.org/project/mangum/). **Nothing has been deployed to AWS.**

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

Lambda: Python 3.12, 512 MB, 30-second timeout, reserved concurrency 2, seven-day log retention. The deployment uses an **existing S3 code bucket**, rather than an ECR image repository. Role/bootstrap and release automation will be added through a separate, manually gated GitHub OIDC workflow analogous to `rsvp`.

## Remaining implementation gates

1. Native Lambda Function URL JSON-RPC initialise, tools/list, tools/call compatibility.
2. Real UK TM7 account login after cold start, expiry/retry and concurrency tests.
3. GitHub OIDC role + restricted CloudFormation service role and manual deployment with **Function URL disabled**.
4. Secure Cloudflare Access Managed OAuth routing and raw AWS origin bypass tests.
5. Profile free-tier consumption/shared budgets to avoid unexpected AWS charges.
6. Later explicit write-approval controls and disposable test custom recipe.

Full plan: [issue #1](https://github.com/antonio1000homens/cookidoo-mcp/issues/1).
