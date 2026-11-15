#!/usr/bin/env bash
# One typed confirmation is the only user-supplied operation selector.
# Its exact value is validated before AWS credentials are available.
set -euo pipefail
if [[ "${GITHUB_REF:-}" != "refs/heads/master" ]]; then
  echo "::error::Recipe release must run from master (got ${GITHUB_REF:-unset})." >&2
  exit 1
fi
# A push to the protected master branch is itself deployment approval.
# Do not permit the auto-deploy action through workflow_dispatch or pull requests.
if [[ "${GITHUB_EVENT_NAME:-}" == "push" ]]; then
  [[ "${RELEASE_CONFIRMATION:-}" == "AUTO-DEPLOY-ON-MASTER" ]] || {
    echo "::error::Master push must use the automatic deploy confirmation." >&2
    exit 1
  }
  action=auto-deploy
else
case "${RELEASE_CONFIRMATION:-}" in
  DEPLOY-COOKIDOO-WRITES-DISABLED) action=deploy-disabled ;;
  ENABLE-COOKIDOO-RECIPE-CREATION) action=enable-writes ;;
  DISABLE-COOKIDOO-RECIPE-CREATION) action=disable-writes ;;
  DEPLOY-COOKIDOO-RECIPE-UPDATE-CODE) action=deploy-update-code ;;
  ENABLE-COOKIDOO-RECIPE-UPDATES) action=enable-updates ;;
  DISABLE-COOKIDOO-RECIPE-UPDATES) action=disable-updates ;;
  ENABLE-COOKIDOO-MEAL-PLANNING) action=enable-planning ;;
  DISABLE-COOKIDOO-MEAL-PLANNING) action=disable-planning ;;
  *)
    echo "::error::Invalid confirmation. Enter exactly one of the action codes shown in the workflow form, with no trailing punctuation or comma." >&2
    exit 1
    ;;
esac
fi
# Defence in depth: the privileged release job receives the validated output
# and the original confirmation. Reject any disagreement.
if [[ -n "${RELEASE_ACTION:-}" && "$RELEASE_ACTION" != "$action" ]]; then
  echo "::error::Validated action does not match the deployment action." >&2
  exit 1
fi
printf 'action=%s\n' "$action"
