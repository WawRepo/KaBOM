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

# Needs PyYAML. Prefer a python3 that already has it; otherwise fall back
# to uv, which this repo depends on anyway and which fetches it on the fly.
# The ARC runner image ships a python3 with no pip at all, so
# `pip install pyyaml` is not an option there.
if python3 -c "import yaml" 2>/dev/null; then
  PY=(python3)
elif command -v uv >/dev/null 2>&1; then
  PY=(uv run --quiet --with pyyaml python3)
else
  echo "need either python3 with PyYAML, or uv" >&2
  exit 1
fi

status=0

check() {
  local desc="$1"
  shift
  echo "  $desc"
  # No pipefail games: helm failing here would itself be the bug, and the
  # checker exits non-zero on anything unusable.
  if helm template kabom "$CHART_DIR" "$@" | "${PY[@]}" "$CHECKER"; then
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
