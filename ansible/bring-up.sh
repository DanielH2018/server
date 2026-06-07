#!/usr/bin/env bash
# Host bring-up — get a freshly-cloned host to the point where Ansible can take over.
#
# Automates §3 (install uv) and §4 (SOPS onboarding via bootstrap.yml) of
# ansible/README.md. It assumes §1 (SSH) and §2 (git clone) are already done — the repo
# has to exist for this script to exist — and stops at the manual, cross-host SOPS
# key-exchange that one host cannot do on its own. After you finish that exchange, run
# the playbooks yourself (the script prints the exact commands):
#     uv run ansible-playbook ansible/initial_setup.yml
#     uv run ansible-playbook ansible/deploy.yml
#
# Idempotent: uv install is guarded by `command -v`, and bootstrap.yml is idempotent
# (age-keygen has a `creates:` guard) — re-running is safe.
#
# Usage:  ansible/bring-up.sh [--host <name>]
#   --host   inventory host to bootstrap (default: this machine's hostname)
set -euo pipefail

HOST="$(hostname)"

usage() {
  cat <<'EOF'
Host bring-up — get a freshly-cloned host ready for Ansible (§3 + §4 of ansible/README.md).
Installs uv, runs bootstrap.yml (SOPS), and prints the manual SOPS key-exchange to finish.

Usage:  ansible/bring-up.sh [--host <name>]
  --host <name>   inventory host to bootstrap (default: this machine's hostname)
  -h, --help      show this help
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --host) HOST="${2:?--host needs a value}"; shift 2 ;;
    --host=*) HOST="${1#*=}"; shift ;;
    -h | --help) usage; exit 0 ;;
    *) echo "error: unknown argument '$1'" >&2; usage >&2; exit 2 ;;
  esac
done

# uv run must execute from the repo root (where pyproject.toml / uv.lock live), not the
# caller's CWD. This script lives in ansible/, so the root is its parent directory.
cd "$(dirname "$(readlink -f "$0")")/.."
[[ -f ansible/bootstrap.yml ]] ||
  { echo "error: ansible/bootstrap.yml not found — run this from a cloned repo" >&2; exit 1; }

# --- §3: install uv (the only manual prerequisite) ----------------------------------
# `uv run` then self-provisions Python 3.13 + ansible-core from uv.lock for the playbook
# below, so no system-wide Ansible is needed.
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

# --- §4: SOPS bootstrap (installs age/sops + collections, generates this host's age ---
# key, prints its public key). No secret dependency — this is what breaks the
# chicken-and-egg before initial_setup.yml's secret-loading pre_tasks.
echo ">> bootstrapping SOPS on host '$HOST' ..."
uv run ansible-playbook ansible/bootstrap.yml --limit "$HOST"

cat <<EOF

============================================================
 SOPS onboarding — finish these MANUAL steps, then deploy
============================================================
The "Your Public Key is: age1..." line above is THIS host's age key.

 1. Add that age1... pubkey to ansible/.sops.yaml under 'age:'.
 2. On a host that can already decrypt (e.g. daniel-server):
        sops updatekeys ansible/vars/secrets.yml
 3. Commit + push the re-encrypted secrets.yml + ansible/.sops.yaml.
 4. Back here:  git pull

 First host ever? bootstrap already seeded ansible/.sops.yaml from
 this host's own key — skip steps 1-4.

 Then hand off to Ansible:
        uv run ansible-playbook ansible/initial_setup.yml
        uv run ansible-playbook ansible/deploy.yml
============================================================
EOF
