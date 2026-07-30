#!/usr/bin/env bash
# Host bring-up — get a freshly-cloned host to the point where Ansible can take over.
#
# Automates §3 (install uv), §4 (inventory scaffolding, with --scaffold) and §5 (SOPS
# onboarding via bootstrap.yml) of ansible/README.md. It assumes §1 (SSH) and §2 (git clone)
# are already done, and stops at the manual, cross-host SOPS key-exchange that one host
# cannot do on its own. Finish that exchange, then re-run with --continue to hand off to
# Ansible (§8):
#     ansible/bring-up.sh --continue
#
# Idempotent throughout: uv install is guarded by `command -v`, --scaffold skips anything
# that already exists, and bootstrap.yml has a `creates:` guard on age-keygen.
#
# Usage:  ansible/bring-up.sh [--host <name>] [--scaffold] [--continue]
set -euo pipefail

HOST="$(hostname)"
SCAFFOLD=false
CONTINUE=false

usage() {
  cat <<'EOF'
Host bring-up — get a freshly-cloned host ready for Ansible (§3-§5 of ansible/README.md).
Installs uv, scaffolds inventory, runs bootstrap.yml (SOPS), and prints the manual SOPS
key-exchange to finish. Re-run with --continue afterwards to run the playbooks (§8).

Usage:  ansible/bring-up.sh [--host <name>] [--scaffold] [--continue]
  --host <name>   inventory host to bring up (default: this machine's hostname)
  --scaffold      create the §4 inventory entries (hosts.ini line + host_vars file) if
                  missing, then stop so you can edit them. Never overwrites.
  --continue      skip §3-§5 and run §8: preflight.yml, initial_setup.yml, deploy.yml
  -h, --help      show this help
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --host) HOST="${2:?--host needs a value}"; shift 2 ;;
    --host=*) HOST="${1#*=}"; shift ;;
    --scaffold) SCAFFOLD=true; shift ;;
    --continue) CONTINUE=true; shift ;;
    -h | --help) usage; exit 0 ;;
    *) echo "error: unknown argument '$1'" >&2; usage >&2; exit 2 ;;
  esac
done

# uv run must execute from the repo root (where pyproject.toml / uv.lock live), not the
# caller's CWD. This script lives in ansible/, so the root is its parent directory.
cd "$(dirname "$(readlink -f "$0")")/.."
[[ -f ansible/bootstrap.yml ]] ||
  { echo "error: ansible/bootstrap.yml not found — run this from a cloned repo" >&2; exit 1; }

HOSTS_INI=ansible/inventory/hosts.ini
HOST_VARS="ansible/inventory/host_vars/${HOST}.yml"

# --- §3: install uv (the only manual prerequisite) ----------------------------------
# `uv run` then self-provisions Python 3.14 + ansible-core from uv.lock for every playbook
# below, so no system-wide Ansible is needed. Done first because the inventory check and
# every later step run through it.
if ! command -v uv >/dev/null 2>&1; then
  echo ">> uv not found — installing per-user into ~/.local/bin ..."
  curl -LsSf https://astral.sh/uv/install.sh | sh
  # The installer edits ~/.bashrc for FUTURE shells; put uv on PATH for THIS one too.
  export PATH="$HOME/.local/bin:$PATH"
  hash -r
fi
command -v uv >/dev/null 2>&1 ||
  { echo "error: uv still not on PATH after install — add ~/.local/bin to PATH" >&2; exit 1; }
echo ">> uv $(uv --version)"

# --- §4: inventory scaffolding (opt-in) ---------------------------------------------
# Both files are git-tracked, so a wrong guess here is reviewable rather than destructive.
# Never overwrite: an existing entry is the operator's, not ours.
if [[ "$SCAFFOLD" == true ]]; then
  if grep -qE "^[[:space:]]*${HOST}[[:space:]]" "$HOSTS_INI"; then
    echo ">> $HOSTS_INI already lists '$HOST' — leaving it alone"
  elif [[ "$HOST" == "$(hostname)" ]]; then
    echo ">> adding '$HOST' to $HOSTS_INI (ansible_connection=local — playbooks run ON it)"
    printf '%s  ansible_connection=local\n' "$HOST" >>"$HOSTS_INI"
  else
    # We know the name but not its IP or SSH user, so this line is a stub the operator must
    # finish. Comment it out rather than leave a half-valid entry that would resolve and
    # then fail mid-connection.
    echo ">> adding a COMMENTED stub for '$HOST' to $HOSTS_INI — fill in the IP + user"
    printf '# %s  ansible_host=<lan-ip> ansible_user=<user> ansible_connection=ssh\n' \
      "$HOST" >>"$HOSTS_INI"
  fi

  if [[ -f "$HOST_VARS" ]]; then
    echo ">> $HOST_VARS already exists — leaving it alone"
  else
    echo ">> creating $HOST_VARS from _example.yml"
    cp ansible/inventory/host_vars/_example.yml "$HOST_VARS"
  fi

  cat <<EOF

Scaffolding written. Edit both before continuing (README §4):
    $HOSTS_INI  — the connection fields are NOT optional
    $HOST_VARS  — server_ip, ssh_config_path, containers_list, has_gitops: false

Then re-run without --scaffold.
EOF
  exit 0
fi

# --- §4: the host must resolve to exactly one host ----------------------------------
# `--limit <host>` alone is NOT enough: bootstrap.yml's `hosts:` is
# `{{ target | default(lookup('pipe','hostname')) }}`, so without `-e target=` the play
# resolves to the CONTROLLER and --limit intersects that to zero hosts — exit 0, no output,
# a silent no-op that looks like success until initial_setup.yml fails on a missing age key
# much later. Pass both, and confirm against --list-hosts rather than grepping hosts.ini
# (a regex there also matches group names and comments).
if uv run ansible-playbook ansible/bootstrap.yml -e target="$HOST" --limit "$HOST" \
  --list-hosts 2>/dev/null | grep -q 'hosts (0)'; then
  echo "error: '$HOST' resolves to no host in $HOSTS_INI — see README §4." >&2
  echo "       Re-run with --scaffold to create the inventory entries." >&2
  exit 1
fi

# --- §8: hand off to Ansible (opt-in, after the SOPS exchange) ------------------------
if [[ "$CONTINUE" == true ]]; then
  echo ">> preflight (read-only) ..."
  uv run ansible-playbook ansible/preflight.yml -e target="$HOST"
  echo ">> initial_setup (OS hardening) ..."
  uv run ansible-playbook ansible/initial_setup.yml -e target="$HOST"
  echo ">> deploy (all containers, dependency-ordered) ..."
  uv run ansible-playbook ansible/deploy.yml -e target="$HOST"
  cat <<'EOF'

============================================================
 Deployed. Now finish README §9 — the app-database setup
 Ansible cannot do (Uptime-Kuma admin, *arr API keys, HA
 tokens, ...). Most of it fails SILENTLY, so verify with:
        uv run python scripts/postflight.py
============================================================
EOF
  exit 0
fi

# --- §5: SOPS bootstrap (installs age/sops + collections, generates this host's age ---
# key, prints its public key). No secret dependency — this is what breaks the
# chicken-and-egg before initial_setup.yml's secret-loading pre_tasks.
echo ">> bootstrapping SOPS on host '$HOST' ..."
uv run ansible-playbook ansible/bootstrap.yml -e target="$HOST" --limit "$HOST"

cat <<EOF

============================================================
 SOPS onboarding — finish these MANUAL steps, then continue
============================================================
The "Your Public Key is: age1..." line above is THIS host's age key.

 1. Add that age1... pubkey to ansible/.sops.yaml under 'age:'.
 2. On a host that can already decrypt (e.g. daniel-server):
        sops updatekeys ansible/vars/secrets.yml
 3. Commit + push the re-encrypted secrets.yml + ansible/.sops.yaml.
 4. Back here:  git pull

 First host ever? bootstrap already seeded ansible/.sops.yaml from
 this host's own key — skip steps 1-4.

 Then hand off to Ansible (preflight + initial_setup + deploy):
        ansible/bring-up.sh --continue --host $HOST
============================================================
EOF
