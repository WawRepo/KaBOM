#!/usr/bin/env bash
# Regenerate kabom/static/css/style.css using the Tailwind *standalone* CLI —
# a single downloaded binary, no npm, no node_modules, no JS build tooling.
# The runtime image must never contain any of
# that; this script is a dev-time/build-time step only.
#
# Usage:
#   ./scripts/build-css.sh            # build once
#   ./scripts/build-css.sh --watch    # rebuild on template/CSS changes
set -euo pipefail

TAILWIND_VERSION="v4.3.3"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BIN_DIR="$REPO_ROOT/.tailwindcss-cli"
INPUT="$REPO_ROOT/kabom/static/css/input.css"
OUTPUT="$REPO_ROOT/kabom/static/css/style.css"

os="$(uname -s)"
arch="$(uname -m)"

case "$os" in
  Darwin) platform="macos" ;;
  Linux) platform="linux" ;;
  *)
    echo "Unsupported OS for the standalone Tailwind CLI: $os" >&2
    exit 1
    ;;
esac

case "$arch" in
  arm64|aarch64) tw_arch="arm64" ;;
  x86_64|amd64) tw_arch="x64" ;;
  *)
    echo "Unsupported architecture for the standalone Tailwind CLI: $arch" >&2
    exit 1
    ;;
esac

asset="tailwindcss-${platform}-${tw_arch}"
binary="$BIN_DIR/${asset}-${TAILWIND_VERSION}"

mkdir -p "$BIN_DIR"
if [ ! -x "$binary" ]; then
  echo "Downloading Tailwind standalone CLI ${TAILWIND_VERSION} (${asset})..."
  url="https://github.com/tailwindlabs/tailwindcss/releases/download/${TAILWIND_VERSION}/${asset}"
  curl -sL -o "$binary" "$url"
  chmod +x "$binary"
fi

extra_args=()
if [ "${1:-}" = "--watch" ]; then
  extra_args+=(--watch)
fi

"$binary" --cwd "$REPO_ROOT" -i "$INPUT" -o "$OUTPUT" --minify "${extra_args[@]}"
echo "Wrote $OUTPUT"
