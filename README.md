# Cookidoo MCP — TM7 (Lambda-first)

Standalone public infrastructure/application repository for an **unofficial** Cookidoo integration. The server is a thin deployment/security layer over [alexandrepa/mcp-cookidoo](https://github.com/alexandrepa/mcp-cookidoo) pinned to upstream commit `6f7c33b1d23f8257814fd790341bc84cd88785e0` (release v1.1.0). No passwords, sessions or personal recipes belong in this repository.

**Current status: implementation spike, not yet deployed.** This branch introduces read-only Streamable HTTP transport, a signed Cloudflare Access JWT gate, Lambda Web Adapter image and a CloudFormation template. See [issue #1](https://github.com/antonio1000homens/cookidoo-mcp/issues/1).

## Architecture

```text
ChatGPT custom MCP client
  -> Cloudflare Access Managed OAuth (windsor-app owns Access application/zone)
  -> secure origin mapping (NOT YET CONFIGURED)
  -> Lambda Function URL / streaming Web Adapter
  -> Python FastMCP, stateless HTTP at /mcp
  -> Cookidoo HTTPS via pinned upstream Python library
```

The application requires a **cryptographically validated** `Cf-Access-Jwt-Assertion` on all MCP methods, including requests sent directly to AWS. JWT signature (Cloudflare JWKS), issuer, audience and expiry are checked. `GET /health` is deliberately minimal and public solely for Lambda Web Adapter readiness; other paths return 404. **A Cloudflare Access login alone does not protect a public AWS Function URL.** Direct-origin bypass tests must pass before enabling it.

### Read-only first

The adapter explicitly registers only:

- `search_recipes`, `get_recipe_details`
- `list_custom_recipes`, `get_custom_recipe`
- `generate_recipe_structure` (validation/preview, no upload)

It does **not** register `connect_to_cookidoo` (which reports the account email), `upload_custom_recipe`, or `update_custom_recipe`. Upstream prompt guidance about seeking user approval is not a server-side security control. Mutation tools will need a separate deliberate approval/authorization design and a later PR.

Defaults are `COOKIDOO_COUNTRY=gb`, `COOKIDOO_LANGUAGE=en-GB`, and `COOKIDOO_DEVICE=TM7`.

## Local / CI

The Dockerfile copies the Lambda Web Adapter from AWS's published image, but the same Python ASGI process can run locally without Lambda. Build:

```bash
docker build -t cookidoo-mcp .
```

The server requires `CF_ACCESS_TEAM_DOMAIN=https://YOUR-TEAM.cloudflareaccess.com` and `CF_ACCESS_AUD=YOUR_APPLICATION_AUD`. It **fails at startup** if these are missing or invalid. For local development only, `COOKIDOO_ALLOW_LOCAL_ENV=1` permits credentials from environment variables; production never enables this override. Do not commit or print credential values.

```bash
docker run --rm -p 8080:8080 \
  -e CF_ACCESS_TEAM_DOMAIN=https://YOUR-TEAM.cloudflareaccess.com \
  -e CF_ACCESS_AUD=YOUR_APPLICATION_AUD \
  -e COOKIDOO_ALLOW_LOCAL_ENV=1 \
  -e COOKIDOO_EMAIL -e COOKIDOO_PASSWORD cookidoo-mcp
curl -i http://localhost:8080/health   # 200 ok
curl -i -X POST http://localhost:8080/mcp   # 403 without a valid JWT
```

CI fetches the pinned upstream commit, runs offline/mocked Python tests, CloudFormation lint and builds a Docker container to test the anonymous-origin denial. It does **not** access your Cookidoo account or AWS.

## Lambda configuration

`infrastructure/cookidoo.yaml` defines:

- One Python-container Lambda (x86_64, memory 512 MB, 30s timeout, reserved concurrency 2).
- Response-streaming Lambda Web Adapter / matching Function URL mode.
- Least-privilege `ssm:GetParameters` for **two** pre-existing encrypted SSM SecureString parameters, default `/cookidoo-mcp/email` and `/cookidoo-mcp/password`.
- Cloudflare Access team issuer and Application AUD configuration (**not** the Cloudflare API token).
- Seven-day CloudWatch log retention.
- **`EnableFunctionUrl=false` by default**. An AWS origin is not publicly reachable until explicitly activated.

For now, provision secret values separately via an appropriate secure channel (do not store their plaintext in CloudFormation, GitHub Actions variables/logs, README or CLI command history). The runtime retrieves and caches them in the warm process. Rotating a secret requires recycling warm Lambda execution environments; a supported rotation procedure is future work.

A first manual deployment would build the image, push it to ECR in the Lambda's AWS region, and deploy the CloudFormation template with `ImageUri`, `CloudflareTeamDomain`, and `CloudflareAudience` supplied as **configuration values**. It must be reviewed and gated; there is intentionally **no automatic deployment** yet, no OIDC deployment role and no public Function URL until end-to-end validation.

## Outstanding gates before production

1. Verify upstream license distribution terms (upstream `pyproject.toml` declares MIT; verify a separate LICENSE file/author confirmation before copying code).
2. Confirm a real Cloudflare Access Managed OAuth app, OAuth metadata routes and secure forwarding to a Lambda origin; `windsor-app` owns shared Access/Terraform configuration.
3. Run **real** authenticated MCP initialize, tools/list and tools/call against a Lambda Function URL (CI tests the local transport/auth gate, not Lambda).
4. Measure Cookidoo cold starts, repeated logins, MFA, API limits and response streaming with UK account/TM7.
5. Investigate production session lifecycle/secret rotation, full dependency lock/digest pinning, audit and concurrency guardrails.
6. Test guided cooking on a disposable custom recipe with explicit approval before exposing write tools.

If Lambda proves unsuitable, retain this application/repo and replace the AWS deployment with a one-replica Kubernetes FastMCP service. See the original context in [windsor-app #1997](https://github.com/antonio1000homens/windsor-app/issues/1997).

## References

- [Upstream MCP](https://github.com/alexandrepa/mcp-cookidoo)
- [Lambda Web Adapter](https://github.com/awslabs/aws-lambda-web-adapter)
- [Cloudflare signed Access JWT validation](https://developers.cloudflare.com/cloudflare-one/access-controls/applications/http-apps/authorization-cookie/validating-json/)
- [Lambda Function URL response streaming](https://docs.aws.amazon.com/lambda/latest/dg/config-rs-invoke-furls.html)
- Existing deployment conventions: [NFC](https://github.com/antonio1000homens/nfc) / [RSVP](https://github.com/antonio1000homens/rsvp)
