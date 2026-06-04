#!/usr/bin/env bash
# Deploy the plugin to ~/.hermes/plugins/mempalace-mcp/ on the hermes host.
#
# Hermes loads plugins from $HERMES_HOME/plugins/<name>/ — the directory
# must contain ``__init__.py`` and ``plugin.yaml`` at its root. We rsync
# the contents of ``plugin/`` (not the dir itself) into the target.
#
# Usage:
#   ./deploy.sh                  # default host=hermes, default target=~/.hermes/plugins/mempalace-mcp/
#   HOST=other ./deploy.sh
#   HERMES_HOME=/srv/hermes ./deploy.sh

set -euo pipefail

HOST="${HOST:-hermes}"
HERMES_HOME="${HERMES_HOME:-~/.hermes}"
TARGET_DIR="${HERMES_HOME}/plugins/mempalace-mcp"

cd "$(dirname "$0")"

if [[ ! -f plugin/__init__.py || ! -f plugin/plugin.yaml ]]; then
    echo "error: plugin/__init__.py or plugin/plugin.yaml not found in $(pwd)" >&2
    exit 1
fi

# Ensure the target dir exists on the remote and is empty (so removed
# files don't linger across deploys).
ssh "${HOST}" "rm -rf ${TARGET_DIR} && mkdir -p ${TARGET_DIR}"

# Sync via tar-over-ssh — rsync isn't always present on slim Linux installs
# and scp -r doesn't honor exclude patterns. tar handles both.
tar --exclude='__pycache__' --exclude='*.pyc' -C plugin -czf - . \
    | ssh "${HOST}" "tar -xzf - -C ${TARGET_DIR}"

# Sanity: list what landed.
echo ""
echo "Deployed to ${HOST}:${TARGET_DIR}/"
ssh "${HOST}" "ls -la ${TARGET_DIR}/"
