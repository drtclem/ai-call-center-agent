#!/bin/sh
# Make the DB directory on the (root-owned) volume writable by the app user, then drop root.
set -eu
: "${DB_PATH:?DB_PATH must be set (a file on the Railway volume)}"
dir=$(realpath -m "$(dirname "$DB_PATH")")  # absolute, so "data/x" or "//app" can't slip by
case "$dir" in  # the app tree holds the read-only dataset: never hand it to the app user
  /app | /app/*) echo "DB_PATH must be on the volume, not under /app" >&2; exit 1 ;;
esac
mkdir -p "$dir"
chown meridian:meridian "$dir"
exec setpriv --reuid=10001 --regid=10001 --init-groups -- "$@"
