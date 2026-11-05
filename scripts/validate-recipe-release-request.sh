#!/usr/bin/env bash
# Invalid manual release input must fail visibly, not skip all jobs.
set -euo pipefail
if [[ "${GITHUB_REF:-}" != "refs/heads/master" ]]; then
  echo "::error::Recipe release must run from master (got ${GITHUB_REF:-unset})." >&2
  exit 1
fi
case "${RELEASE_ACTION:-}" in
  deploy-disabled) expected='DEPLOY-COOKIDOO-WRITES-DISABLED' ;;
  enable-writes) expected='ENABLE-COOKIDOO-RECIPE-CREATION' ;;
  disable-writes) expected='DISABLE-COOKIDOO-RECIPE-CREATION' ;;
  *)
    echo "::error::Invalid operation. Choose deploy-disabled, enable-writes or disable-writes." >&2
    exit 1
    ;;
esac
if [[ "${RELEASE_CONFIRMATION:-}" != "$expected" ]]; then
  echo "::error::Incorrect confirmation for '${RELEASE_ACTION}'. Enter exactly: $expected" >&2
  exit 1
fi
echo "Manual release request verified: ${RELEASE_ACTION} on master."
