#!/usr/bin/env bash
# One typed confirmation is the only user-supplied operation selector.
# Its exact value is validated before AWS credentials are available.
set -euo pipefail
if [[ "${GITHUB_REF:-}" != "refs/heads/master" ]]; then
  echo "::error::Recipe release must run from master (got ${GITHUB_REF:-unset})." >&2
  exit 1
fi
case "${RELEASE_CONFIRMATION:-}" in
  DEPLOY-COOKIDOO-WRITES-DISABLED) action=deploy-disabled ;;
  ENABLE-COOKIDOO-RECIPE-CREATION) action=enable-writes ;;
  DISABLE-COOKIDOO-RECIPE-CREATION) action=disable-writes ;;
  DEPLOY-COOKIDOO-RECIPE-UPDATE-CODE) action=deploy-update-code ;;
  ENABLE-COOKIDOO-RECIPE-UPDATES) action=enable-updates ;;
  DISABLE-COOKIDOO-RECIPE-UPDATES) action=disable-updates ;;
  *)
    echo "::error::Invalid confirmation. Enter exactly one of one of the exact action codes shown in the workflow form, with no trailing punctuation or comma." >&2
    exit 1
    ;;
esac
# Defence in depth: the privileged release job receives the validated output
# and the original confirmation. Reject any disagreement.
if [[ -n "${RELEASE_ACTION:-}" && "$RELEASE_ACTION" != "$action" ]]; then
  echo "::error::Validated action does not match the deployment action." >&2
  exit 1
fi
printf 'action=%s\n' "$action"
