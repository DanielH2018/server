#!/usr/bin/env bash
# Launch @playwright/mcp against this homelab's LAN routes.
#
# Registered as an MCP server, so Claude gets browser tools (navigate, click, type,
# accessibility snapshot) pointed at `<svc>.local.<domain>`. Three things have to be true
# before that works on this host, and this wrapper supplies all three:
#
#   1. DNS. This host's resolver bypasses the LAN DNS, so `.local.<domain>` does not
#      resolve to the cluster edge from a shell here — the same trap probe_lib/core.py documents
#      and works around with `curl --resolve`. Chromium's equivalent is
#      --host-resolver-rules, which only reaches it through a config file's launchOptions.
#   2. Auth. Every `*.local.<domain>` route is Authelia one_factor; the browser context
#      loads the session cookie ui_login.py mints. The operator logs in with SOPS values. The
#      agent user, which has no age key, logs in as its own Authelia account from
#      ~/.config/homelab-ui/credentials.json (see ui_login.py).
#   3. The domain, the ingress VIP and the session-state path are looked up at launch, so
#      neither of the above can be written into ~/.claude.json. The config is generated at
#      launch into a 0600 file instead.
#
# Node comes from fnm's `default` alias rather than $PATH: a Claude session inherits an
# ephemeral /run/user/1000/fnm_multishells/<pid>_<ts> path that dies with the shell that
# made it, and an MCP server outlives that.

set -euo pipefail

if [ "${1:-}" = "-h" ] || [ "${1:-}" = "--help" ]; then
  awk 'NR>1 && /^#/ {sub(/^# ?/, ""); print; next} NR>1 {exit}' "$0"
  exit 0
fi

NODE_BIN="${NODE_BIN:-$HOME/.local/share/fnm/aliases/default/bin}"
REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
RUNTIME_DIR="${XDG_RUNTIME_DIR:-/tmp}/claude-ui-mcp"
CONFIG_PATH="$RUNTIME_DIR/playwright-mcp.json"

# `--two-factor` is opt-in per launch, never a fallback: code-server, n8n and longhorn are a shell,
# arbitrary workflows and volume deletion.
TIER_ARGS=()
if [[ "${1:-}" == "--two-factor" ]]; then
  TIER_ARGS=(--two-factor)
  CONFIG_PATH="$RUNTIME_DIR/playwright-mcp-2fa.json"
  shift
fi

export PATH="$NODE_BIN:$PATH"

# `UI_MCP_STATE_PATH` uses a caller-owned state file and skips the mint: test_ui_state_reload.py
# swaps it under a running server. Its config gets its own name.
if [[ -n "${UI_MCP_STATE_PATH:-}" ]]; then
  STATE_PATH="$UI_MCP_STATE_PATH"
  CONFIG_PATH="$RUNTIME_DIR/playwright-mcp-private-$(basename "$STATE_PATH").json"
else
  if ! uv --directory "$REPO_ROOT" run python scripts/diagnostics/ui_login.py --check "${TIER_ARGS[@]}" >/dev/null 2>&1; then
    # Both tiers mint unattended; the two_factor session lasts about an hour, so this re-mints on
    # every launch.
    uv --directory "$REPO_ROOT" run python scripts/diagnostics/ui_login.py "${TIER_ARGS[@]}" >&2
  fi

  STATE_PATH="$(uv --directory "$REPO_ROOT" run python scripts/diagnostics/ui_login.py --path "${TIER_ARGS[@]}")"
fi
DOMAIN="$(uv --directory "$REPO_ROOT" run python scripts/diagnostics/ui_login.py --domain)"
VIP="$(awk -F': *' '/^k3s_metallb_ingress_vip:/ {print $2; exit}' \
  "$REPO_ROOT/inventory/group_vars/all.yml")"

if [[ -z "$DOMAIN" || -z "$VIP" ]]; then
  echo "could not resolve domain or MetalLB ingress VIP" >&2
  exit 1
fi

# Mode set separately from -p, which applies -m only to the deepest directory (SC2174).
mkdir -p "$RUNTIME_DIR"
chmod 700 "$RUNTIME_DIR"

# 0600 before any content: the file names the session-state file that holds the login cookie.
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
        "--no-sandbox",
        "--disable-http2"
      ]
    },
    "contextOptions": {
      "storageState": "$STATE_PATH",
      "ignoreHTTPSErrors": false
    }
  }
}
EOF

# `--disable-http2` is load-bearing: every `*.local.<domain>` shares one address and certificate, so
# Chromium coalesces connections and Traefik answers a page's own XHRs with 421.

# Output defaults to `.playwright-mcp/` in the CURRENT directory, which would litter the launch
# tree; `.gitignore` hides it from `git status`.
exec playwright-mcp --config "$CONFIG_PATH" --output-dir "$RUNTIME_DIR/output" "$@"
