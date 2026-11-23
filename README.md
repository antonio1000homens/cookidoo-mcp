# Cookidoo MCP — Thermomix TM7 (native AWS Lambda ZIP)

Standalone public infrastructure/application repository for an **unofficial** Cookidoo integration. Python runtime uses upstream [alexandrepa/mcp-cookidoo](https://github.com/alexandrepa/mcp-cookidoo) pinned at commit `6f7c33b1d23f8257814fd790341bc84cd88785e0` (v1.1.0). Do not commit passwords, cookies, tokens or personal recipes.

**Status:** The Lambda-first foundation, OIDC deployment, and automated guarded release on master are merged. The origin requires both a dedicated Worker bearer and a cryptographically verified Cloudflare Access JWT. Public AWS and Cloudflare activation remain separate, manually gated operations; this repository change does not run them.

## Production deployment policy (automatic after merge)

Every successful push/merge to **master** triggers
[`release-recipe-tools.yml`](.github/workflows/release-recipe-tools.yml).
The workflow runs offline tests, builds one immutable Python 3.12 Lambda ZIP,
then uses short-lived production-environment OIDC credentials to deploy via
a single inspected CloudFormation change set. **One release enables recipe
creation, in-place recipe updates, and My Week scheduling; it does not create
or edit any recipes or calendar entries itself.** No second operator
"enable" command is required. These recipe mutations still require explicit
user approval at the MCP tool level where defined.

The production deployment validates the already-active Function URL, preserves
its address/authentication, and permits only an in-place Lambda update.
CloudFormation reports valid `Code` changes as `Dynamic / DirectModification`.
The validator permits them only during non-replacing code releases. A dynamic
`Environment` change is accepted only after the complete live Lambda
environment matches the current stack parameters and the live template
matches the proposed template, or differs only by the exact reviewed
recipe-update or meal-planning flag additions. IAM, URL, role, unexpected
resources, and unrelated parameter transitions remain blocked. This addresses
the failed 15 November 2026 release (workflow run #7) without broadening the
dynamic environment allowance. Existing OIDC/production
environment protection remains in force.

Manual `workflow_dispatch` remains available for emergency feature shutdown
and specialised recovery. Any subsequent approved code push re-enables the
supported recipe tools. CI or CloudFormation failures **stop deployment**;
they never fall back to an unvalidated direct Lambda update.

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

#### Individual ingredient links in guided steps

Agents creating or updating recipes should annotate **each added or weighed ingredient individually** in structured \`steps[].ingredients\`, with \`text\` matching a literal phrase in the step and \`ingredient\` matching an exact ingredient-list entry. For example:

\`\`\`json
{
  "ingredients": ["40 g light soy sauce", "70 g water"],
  "steps": [{
    "text": "Add light soy sauce and water. Mix {settings}.",
    "settings": [{"time": 120, "temperature": "50", "speed": "2"}],
    "ingredients": [
      {"text": "light soy sauce", "ingredient": "40 g light soy sauce"},
      {"text": "water", "ingredient": "70 g water"}
    ]
  }]
}
\`\`\`

The MCP server instructions and \`generate_recipe_structure\` tool description recommend these separate links by default. Validation rejects unknown ingredient references, link text missing from the step, and repeated links to the same ingredient within one step. Both create/update previews expose \`linked_ingredient_count\` and advisory \`ingredient_link_warnings\` when a guided step uses addition verbs but has no links. Steps with no additions do not need annotations; warnings do not reject legacy unlinked recipes or bypass user approval.

Ingredient-link annotations are not necessarily separate clickable Cookidoo/TM7 tickboxes; confirm that behaviour in the actual UI. Tests are offline and do not create any Cookidoo recipes.

The default remote tool catalog includes the five original read/validation tools plus \`preview_custom_recipe_upload\`. This preview validates upstream \`CustomRecipe\` data (including TM7 settings, reverse speeds and ingredient links), applies strict payload limits, and returns a JSON preview plus a SHA-256 confirmation string. It does not write anything.

\`upload_custom_recipe\` is registered **only if** the separate CloudFormation parameter \`EnableRecipeCreation=true\` is explicitly set. It requires the exact previewed recipe JSON and matching \`CREATE-COOKIDOO-SHA256:<digest>\` confirmation **after a user approves that exact preview**. This is a data-integrity gate, not proof of user consent; clients must obtain explicit user approval before invoking the write tool. Existing-recipe names are checked before submission. A create-only S3 marker in the pre-existing code bucket under \`cookidoo-mcp/write-guards/\` prevents multiple writes using the same normalized title across concurrent or repeated invocations. A timeout or ambiguous remote failure retains its marker to prevent unsafe automatic retries. Inspect the account and marker manually before clearing and retrying. Store no recipe text or credentials in this marker; only SHA-256 hashes.

Custom recipe updates are available behind a **separate, default-off** `EnableRecipeUpdates` switch. `preview_custom_recipe_update(recipe_id, recipe_json)` is read-only and binds the proposed replacement recipe to the *current* recipe ID, its existing image and its complete raw guided instructions using SHA-256 fingerprints. After showing the complete proposal to the user and obtaining explicit approval, `update_custom_recipe(recipe_id, recipe_json, confirmation)` checks the same current fingerprint before updating, and verifies the persisted recipe ID, ingredients, steps, timings and image. Concurrent modifications between the last read and remote PATCH cannot be made fully atomic by the upstream API; never retry ambiguous failures automatically. Deletion and `connect_to_cookidoo` remain unavailable. Normal deployment remains private/read-only: no new AWS service, public URL or write activation is performed by merging this change. **Do not switch on writes until a disposable recipe test, account-scoped access and an explicit production approval have been reviewed.**

Defaults are `COOKIDOO_COUNTRY=gb`, `COOKIDOO_LANGUAGE=en-GB` and `COOKIDOO_DEVICE=TM7`.

## Cookidoo My Week (meal planning)

The Cookidoo app and the pinned `cookidoo-api==0.19.0` already support weekly
planning for **both catalogue and user-created custom recipes**. The upstream
`alexandrepa/mcp-cookidoo` server does not expose those operations; this
repository provides an independently controlled adapter:

- `get_calendar_week(day)` takes an ISO date (`YYYY-MM-DD`), is always
  read-only, and returns the matching week with resolved custom-recipe names,
  IDs, sources and recipe links.
- `schedule_recipe(day, recipe_id, is_custom=false)` schedules **one**
  Cookidoo recipe on that date. Obtain the ID using `search_recipes` or
  `list_custom_recipes`. Set `is_custom=true` for a user-created recipe
  (including `Chicken Madras TM7 – Browning Edition`).
- Scheduling first reads the existing day, returns `already_scheduled` if the
  recipe is present, and verifies a new entry after the write. A failed or
  ambiguous network response requires a fresh `get_calendar_week` read
  before attempting another write. Neither tool creates a Google Calendar
  event or modifies the recipe.

**Release safety:** The CloudFormation default is
`EnableMealPlanningWrites=false` for an unactivated stack, but the
automatic `master` release sets it to `true` **together with** the reviewed
code (and the other recipe tool switches). No separate enablement is
needed. A single `DISABLE-COOKIDOO-MEAL-PLANNING` manual release can turn
it off in an emergency; a later code push turns it back on.
The release itself does not schedule any meals or add any test recipes.

## In-place editing existing custom recipes

The upstream Cookidoo service already provides `update_custom_recipe` and uses Cookidoo's own update API; this wrapper does **not** delete and recreate a recipe. To edit your existing recipe without losing its ID or picture:

1. Call `get_custom_recipe` and prepare the proposed full guided recipe JSON.
2. Call `preview_custom_recipe_update` with the exact existing recipe ID and proposed JSON. It returns both a current-recipe fingerprint and the full replacement recipe, plus an `UPDATE-COOKIDOO-SHA256:...` confirmation. **This does not write anything.**
3. Show the original ID, existing name, all new ingredients/steps/settings, replacement scope and image-preservation status to the user. Obtain **explicit approval** for the exact preview before calling any mutation.
4. With the separate `EnableRecipeUpdates=true` release gate enabled, call `update_custom_recipe` using the *identical* ID, JSON and confirmation. It will fail if the source or proposal differs from the preview.
5. Read back the recipe and verify the same ID, recipe content, guided machine settings and existing picture. On a timeout or mismatch, inspect manually; do **not** retry automatically.

The update tool is intentionally separate from creation/image-editing enablement. Production updates are disabled by default and merging the feature changes neither the Lambda nor a Cookidoo recipe. The live Cookidoo API has no compare-and-swap update; the pre-PATCH re-read substantially reduces, but does not eliminate, last-moment concurrency conflicts.

**Release:** Every merge to `master` automatically builds, validates,
deploys, and enables reviewed recipe creation, update and planning tools in
one release. Previous two-step manual enablement is no longer required.
Explicit preview-and-approval gates for mutation calls still apply. The
release's CloudFormation guard fails closed if unexpected resources or unsafe
property changes are detected.


## Deploying to the existing active Function URL

The original `deploy.yml` still handles **private-only** bootstrap and
deliberately refuses to modify the active Function URL. Normal ongoing
deployments use [`release-recipe-tools.yml`](.github/workflows/release-recipe-tools.yml)
on every merge to `master`. Its build/tests and CloudFormation change-set
guard complete before the update is applied, with no extra manual "enable"
stage. The deployed ZIP must match the merged revision.

For exceptional recovery, the same workflow retains manually dispatched
`DEPLOY-COOKIDOO-RECIPE-UPDATE-CODE`,
`DISABLE-COOKIDOO-RECIPE-CREATION`,
`DISABLE-COOKIDOO-RECIPE-UPDATES` and
`DISABLE-COOKIDOO-MEAL-PLANNING` actions. These are **not required** for
normal operation. A later master release deliberately restores the enabled
feature flags.


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

## Custom recipe pictures (review-first)

### Verified image status and post-creation workflow

`get_custom_recipe_image_status(recipe_id)` is **always read-only**. It
reads the authenticated recipe's *actual stored image reference* and, if that
reference is a public HTTPS URL, attempts a bounded, SSRF-guarded image
download and decode. The result distinguishes:

- `missing`: no image reference is stored.
- `present_and_verified`: a stored HTTPS reference was actually fetched and
  decoded (not merely preserved as a string).
- `unreachable`: a stored image URL failed retrieval/decoding.
- `reference_not_fetchable`: a stored asset identifier/relative path cannot
  be fetched without inventing a CDN URL. This is **unverified**, not missing.
- `not_checked`: the recipe could not be read by ID. Neither image presence
  nor absence can be inferred.

`recipe_list_status` is separately `present`, `absent` or `unavailable`.
A created recipe may be directly accessible by ID but not yet visible in
`list_custom_recipes`. That does **not** establish list-confirmed ownership
for writes, and no upload is permitted until the list contains the recipe.

**After creating a custom recipe with an intended photograph:**

1. Call `upload_custom_recipe` only after the complete recipe preview and
   explicit creation approval. The create result reports whether the new ID
   is visible in a subsequent list read; the operation **does not** upload
   a photo and must not be retried just because the list is delayed.
2. Call `get_custom_recipe_image_status` with the newly created ID. If the
   recipe is absent from the list or its status is uncertain, resolve that
   before attempting image mutation.
3. With an approved public HTTPS JPEG/PNG photo URL, call
   `preview_custom_recipe_image(recipe_id, image_url)`. Show the intended
   image, its content hash, and whether an existing image would be replaced.
4. Obtain a **new, explicit image-upload approval**. Then call
   `set_custom_recipe_image` with the exact preview confirmation and
   `replace_existing=true` only when replacement was expressly approved.
5. Call `get_custom_recipe_image_status` again. Only report the photograph
   as **present and verified** if `image_present_and_verified=true`. The
   upload response itself confirms only that the expected image reference
   survived Cookidoo read-back; it does not prove the CDN photo is retrievable.

Recipe-update responses now distinguish `image_reference_unchanged` from
`image_present_and_verified` (unknown unless a separate status check is
performed). The legacy `image_preserved` and `verified` fields remain for
backwards compatibility but refer to recipe/image-reference persistence, not
verified image download. Missing/broken photos and list-delay scenarios are
covered by offline regression tests. No fixture creates a live recipe or
uploads a photograph.


The MCP now supports `preview_custom_recipe_image(recipe_id, image_url)` and
`set_custom_recipe_image(recipe_id, image_url, confirmation, replace_existing=false)`.
It uses Cookidoo's **unofficial**, reverse-engineered signed Cloudinary upload
flow (the original creator's Cookidoo account is required).

- The preview is read-only: confirm the recipe is owned, download from a **public
  HTTPS image URL**, validate JPEG/PNG and image dimensions (80–6000 px), and
  strip embedded EXIF/GPS metadata. It returns an image SHA-256 and a
  confirmation bound to the *current* existing recipe photo; image URLs are
  never echoed into logs or preview results.
- Upload requires the **same URL and exact confirmation**, plus explicit user
  approval. If a picture already exists, `replace_existing=true` is required;
  a changed photo or changed image bytes invalidates the confirmation.
- The server signs the request with Cookidoo, uploads to Vorwerk's fixed
  Cloudinary tenant, patches **only** the image field, and reads the recipe back.
  A conditional S3 write-guard prevents silent retries on uncertain results.
- The write tool is visible only when `COOKIDOO_RECIPE_CREATION_ENABLED=true`
  (the existing recipe-writes operator gate). No image is automatically uploaded
  as part of recipe creation. Other recipe edits/deletions remain unavailable.
- A public HTTPS URL is required, not a local file path, data URI or private
  signed-in web page. To avoid SSRF, private DNS/IPs and redirects are rejected,
  the downloaded source is capped at 10 MB, and cookies/credentials never go
  to the image host or Cloudinary. Only an image file is transmitted to Cloudinary.
- **Image uploads are not yet kitchen- or live-API-tested**. Use a separate
  temporary recipe for the first live acceptance test; don't overwrite manually
  curated pictures without approval.


### Reuse a photo from another of your own custom recipes

Call `preview_copy_custom_recipe_image(source_recipe_id, destination_recipe_id)`.
After displaying the source/destination names, image SHA-256 and replacement
warning, obtain explicit user approval and call
`copy_custom_recipe_image(source_recipe_id, destination_recipe_id, confirmation,
replace_existing=false)`. The source recipe picture must be a publicly
fetchable HTTPS Cookidoo CDN URL. The image is re-downloaded, validated,
metadata-stripped and re-uploaded as a **new** signed Cloudinary asset for
the destination, rather than reusing a picture asset ID (which Cookidoo
may reject). No recipe ingredients, titles or cooking settings are changed.

The source picture remains untouched. Copying is never automatic, and an
already existing picture on the destination requires explicit replacement
approval. The existing write-guard prevents automatic retries on uncertain
results. If the source image is not available as a HTTPS URL, ask for a new
photo URL instead; never attempt to read a private or internal source.
