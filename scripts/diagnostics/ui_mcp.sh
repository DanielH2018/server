#!/usr/bin/env bash
# Launch @playwright/mcp against this homelab's LAN routes.
#
# Registered as an MCP server, so Claude gets browser tools (navigate, click, type,
# accessibility snapshot) pointed at `<svc>.local.<domain>`. Three things have to be true
# before that works on this host, and this wrapper supplies all three:
#
#   1. DNS. This host's resolver bypasses the LAN DNS, so `.local.<domain>` does not
#      resolve to the cluster edge from a shell here — the same trap probe_core documents
#      and works around with `curl --resolve`. Chromium's equivalent is
#      --host-resolver-rules, which only reaches it through a config file's launchOptions.
#   2. Auth. Every `*.local.<domain>` route is Authelia one_factor; the browser context
#      loads the session cookie ui_login.py mints.
#   3. The domain is SOPS-encrypted, so neither of the above can be written into
#      ~/.claude.json. The config is generated at launch into a 0600 file instead.
#
# Node comes from fnm's `default` alias rather than $PATH: a Claude session inherits an
# ephemeral /run/user/1000/fnm_multishells/<pid>_<ts> path that dies with the shell that
# made it, and an MCP server outlives that.

set -euo pipefail

NODE_BIN="${NODE_BIN:-/home/ubuntu/.local/share/fnm/aliases/default/bin}"
REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
RUNTIME_DIR="${XDG_RUNTIME_DIR:-/tmp}/claude-ui-mcp"
CONFIG_PATH="$RUNTIME_DIR/playwright-mcp.json"

# `--two-factor` opts this launch into the short-lived two_factor jar, which is the only way
# to reach code-server, n8n and longhorn. It is opt-in per launch and never a fallback: those
# three are a shell as the repo user, arbitrary workflow execution, and volume deletion
# including the B2 backup chain. Picking the jar up automatically when it happened to exist
# would put admin-capable auth behind every ordinary page load.
TIER_ARGS=()
if [[ "${1:-}" == "--two-factor" ]]; then
  TIER_ARGS=(--two-factor)
  CONFIG_PATH="$RUNTIME_DIR/playwright-mcp-2fa.json"
  shift
fi

export PATH="$NODE_BIN:$PATH"

# Check the session, and mint one if it is missing or expired. --check exits non-zero in
# both cases and is one API call, so it is cheap enough to do every launch.
if ! uv --directory "$REPO_ROOT" run python scripts/diagnostics/ui_login.py --check "${TIER_ARGS[@]}" >/dev/null 2>&1; then
  if [[ ${#TIER_ARGS[@]} -gt 0 ]]; then
    # A two_factor session cannot be minted unattended — that is the design, not a gap.
    echo "no live two_factor session. Mint one with:" >&2
    echo "  uv run python scripts/diagnostics/ui_login.py --totp <code>" >&2
    exit 1
  fi
  uv --directory "$REPO_ROOT" run python scripts/diagnostics/ui_login.py >&2
fi

STATE_PATH="$(uv --directory "$REPO_ROOT" run python scripts/diagnostics/ui_login.py --path "${TIER_ARGS[@]}")"
DOMAIN="$(sops -d --extract '["domain"]' "$REPO_ROOT/ansible/vars/secrets.yml")"
VIP="$(awk -F': *' '/^k3s_metallb_ingress_vip:/ {print $2; exit}' \
  "$REPO_ROOT/ansible/inventory/group_vars/all.yml")"

if [[ -z "$DOMAIN" || -z "$VIP" ]]; then
  echo "could not resolve domain or MetalLB ingress VIP" >&2
  exit 1
fi

# Mode set separately from -p: with -p, -m applies only to the deepest directory, so any
# parent this creates would take the default mode (SC2174).
mkdir -p "$RUNTIME_DIR"
chmod 700 "$RUNTIME_DIR"

# 0600 before any content: the file carries the domain, which is a SOPS secret.
umask 077
cat >"$CONFIG_PATH" <<EOF
{
  "browser": {
    "browserName": "chromium",
    "isolated": true,
    "launchOptions": {
      "headless": true,
      "args": [
        "--host-resolver-rules=MAP *.local.$DOMAIN $VIP",
        "--no-sandbox"
      ]
    },
    "contextOptions": {
      "storageState": "$STATE_PATH",
      "ignoreHTTPSErrors": false
    }
  }
}
EOF

# Snapshots, console logs and screenshots default to `.playwright-mcp/` in the CURRENT
# directory, so without this the server litters whatever tree it was launched from — the
# repo root, for a Claude session or the `ui` test suite. `.gitignore`'s catch-all hides it
# from `git status`, which makes the mess easy to miss rather than harmless.
exec playwright-mcp --config "$CONFIG_PATH" --output-dir "$RUNTIME_DIR/output" "$@"
