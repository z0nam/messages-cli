#!/usr/bin/env bash
# Launch the messages-cli HTTP surface, loading secrets from a file OUTSIDE the
# (public) repo. This script carries NO secrets and is safe to commit.
#
# Secrets live in ~/.config/msg/http.env (chmod 600). See SETUP-REMOTE.md.
set -euo pipefail

ENV_FILE="${MSG_HTTP_ENV:-$HOME/.config/msg/http.env}"
if [[ -f "$ENV_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
else
  echo "config not found: $ENV_FILE  (see SETUP-REMOTE.md)" >&2
  exit 1
fi

cd "$(dirname "$0")"
# MSG_PYTHON lets a host pin a specific interpreter (e.g. /usr/bin/python3 on
# macOS, whose path is stable so a Full Disk Access grant survives upgrades).
exec "${MSG_PYTHON:-python3}" http_server.py
