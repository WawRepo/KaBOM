#!/usr/bin/env bash
# Runs the Playwright suite against the docker-compose stack (see the repo
# root docker-compose.yml), cycling through all three freshness-banner seed
# scenarios. This is what proves HOME-232's acceptance criteria for real —
# see the README for the manual, one-scenario-at-a-time version of this.
#
# Not wired into .github/workflows/ci.yml yet — see the README's "Testing"
# section for why that is a deliberate, documented scope decision rather
# than an oversight.
#
# Usage: ./tests/e2e/run-e2e.sh
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
E2E_DIR="$REPO_ROOT/tests/e2e"
BASE_URL="${KABOM_BASE_URL:-http://localhost:8090}"

compose() {
  (cd "$REPO_ROOT" && docker compose "$@")
}

wait_for_healthy() {
  echo "Waiting for $BASE_URL/healthz ..."
  for _ in $(seq 1 60); do
    if curl -sf "$BASE_URL/healthz" > /dev/null; then
      echo "KaBOM is up."
      return 0
    fi
    sleep 1
  done
  echo "KaBOM never became healthy at $BASE_URL" >&2
  compose logs
  exit 1
}

run_scenario() {
  local scenario="$1"
  shift
  local spec_files=("$@")

  echo "=== scenario: $scenario ==="
  KABOM_SEED_SCENARIO="$scenario" compose up -d --build
  wait_for_healthy

  local status=0
  (
    cd "$E2E_DIR"
    KABOM_SEED_SCENARIO="$scenario" KABOM_BASE_URL="$BASE_URL" npx playwright test "${spec_files[@]}"
  ) || status=$?

  compose down -v
  return "$status"
}

if [ ! -d "$E2E_DIR/node_modules" ]; then
  echo "Installing Playwright test dependencies..."
  # --with-deps additionally apt-installs OS-level browser dependencies; it
  # is Linux-only (what CI would use) and needs root, so it is opt-in via
  # PLAYWRIGHT_INSTALL_DEPS=1 rather than assumed here.
  (cd "$E2E_DIR" && npm install)
  if [ "${PLAYWRIGHT_INSTALL_DEPS:-}" = "1" ]; then
    (cd "$E2E_DIR" && npx playwright install --with-deps chromium)
  else
    (cd "$E2E_DIR" && npx playwright install chromium)
  fi
fi

overall_status=0

# "mixed" is docker-compose.yml's own default and exercises the most: all
# three screens, keyboard search, the JS-blocked degradation, the RED banner
# (from a real failed sample), and the red result border.
run_scenario mixed tests/screens.spec.ts tests/freshness-banner.spec.ts tests/js-blocked.spec.ts \
  || overall_status=$?

# "fresh" and "amber" only need to prove the other two banner colours —
# everything else about the app is already covered above.
run_scenario fresh tests/freshness-banner.spec.ts || overall_status=$?
run_scenario amber tests/freshness-banner.spec.ts || overall_status=$?

exit $overall_status
