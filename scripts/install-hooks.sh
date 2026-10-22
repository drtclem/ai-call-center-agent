#!/usr/bin/env bash
# Copy the repo hooks into .git/hooks. Does not change git config.
set -euo pipefail

root=$(git rev-parse --show-toplevel)
hooks="$root/.git/hooks"
mkdir -p "$hooks"

for name in pre-commit commit-msg pre-push; do
  cp "$root/scripts/hooks/$name" "$hooks/$name"
  chmod +x "$hooks/$name"
done

echo "Installed pre-commit, commit-msg, and pre-push into .git/hooks"
