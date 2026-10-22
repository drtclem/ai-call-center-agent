#!/usr/bin/env bash
# Local mirror of .gitlab-ci.yml. Docker Desktop must be running for gitleaks.
set -euo pipefail

cd "$(dirname "$0")/.."

uv sync --frozen
uv run ruff check
uv run ruff format --check
uv run pyright
uv run pytest

docker run --rm --entrypoint "" \
  -v "$PWD:/repo" \
  -w /repo \
  ghcr.io/gitleaks/gitleaks:v8.30.1 \
  gitleaks detect --source . --redact --verbose
