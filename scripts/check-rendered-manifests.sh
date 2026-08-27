#!/usr/bin/env bash
# Render the chart across several value combinations and assert every
# document is usable Kubernetes YAML. See scripts/check_manifests.py for
# why `helm lint` and `helm template` passing is not enough.
#
# Usage: ./scripts/check-rendered-manifests.sh
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CHART_DIR="$REPO_ROOT/charts/kabom"
CHECKER="$REPO_ROOT/scripts/check_manifests.py"

status=0

check() {
  local desc="$1"
  shift
  echo "  $desc"
  # No pipefail games: helm failing here would itself be the bug, and the
  # checker exits non-zero on anything unusable.
  if helm template kabom "$CHART_DIR" "$@" | python3 "$CHECKER"; then
    return 0
  fi
  echo "::error::rendered manifests are unusable: $desc" >&2
  status=1
}

echo "Checking rendered manifests parse as valid Kubernetes objects."

check "default values"

check "google auth + TLS" \
  --set env.authMode=google \
  --set-string env.allowedEmails="a@example.com" \
  --set ingress.tls.enabled=true \
  --set ingress.tls.secretName=kabom-tls

check "insecure cookies + a different storage class" \
  --set insecureCookies=true \
  --set persistence.storageClass=some-other-class

if [ "$status" -ne 0 ]; then
  echo "FAILED" >&2
  exit 1
fi
echo "All rendered manifests are valid."
