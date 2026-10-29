#!/usr/bin/env bash
# Pull the agent user's chezmoi source and apply the operator's dotfiles to it, as the agent.
#
# The dotfiles repo renders an agent variant when its chezmoi data sets `agent = true`
# (.chezmoitemplates/is-agent). That variant leaves the agent's login profile, git identity,
# ssh and own CLAUDE.md to Ansible. A source that predates the variant would hand the agent
# the operator's .gitconfig and push as the operator's identity, so this refuses to apply
# unless the source renders is-agent as true.
#
# Two passes, because a plain apply sets every managed directory to the source's mode, and
# tasks/agent_github.yml sets two of them differently. The source names ~/.claude
# `private_dot_claude`, so an apply chmods it to 0700 and cuts the operator off from the agent's
# memory store and artifacts, which the role opens with mode 0710. It names ~/.config/gh
# `dot_config/gh`, so an apply opens the directory holding the agent's GitHub token to 0755,
# where the role sets 0710 (#6453). The first pass applies every managed directory but those
# two, creating any new one. It passes --recursive=false: chezmoi applies a directory target's
# whole subtree, so applying ~/.config would reach ~/.config/gh anyway. The second pass applies
# everything else and no directory, which leaves an existing directory's mode alone.
#
# Runs as the agent user, with HOME set to its home. The claude_code role runs it on each apply,
# and <name>-dotfiles-sync.timer runs it between applies, since a dotfiles merge applies no role
# here. Prints one line on stdout:
#   skipped: <reason>
#   applied <old>..<new>      the source moved, or `applied at <sha>` when it did not
# Exits non-zero when git or chezmoi fails, or when the source does not render the agent
# variant, with the reason on stderr.
#
# Usage: claude-dotfiles-sync.sh <chezmoi-binary> <seed-config>
set -euo pipefail

for arg in "$@"; do
  case "$arg" in
    -h | --help) awk 'NR > 1 && /^#/ { print; next } NR > 1 { exit }' "$0"; exit 0 ;;
  esac
done

usage="usage: claude-dotfiles-sync.sh <chezmoi-binary> <seed-config>"
chezmoi=${1:?$usage}
seed=${2:?$usage}
export LC_ALL=C
config="$HOME/.config/chezmoi/chezmoi.toml"
# source-path names the .chezmoiroot directory; git works on the repository around it.
repo=$(git -C "$("$chezmoi" source-path)" rev-parse --show-toplevel)

exec 9>"$(git -C "$repo" rev-parse --absolute-git-dir)/claude-dotfiles-sync.lock"
if ! flock -n 9; then
  echo "skipped: another claude-dotfiles-sync run holds the lock"
  exit 0
fi

before=$(git -C "$repo" rev-parse --short HEAD)
git -C "$repo" pull --ff-only --quiet
after=$(git -C "$repo" rev-parse --short HEAD)

# Re-render chezmoi.toml from the pulled template and the seed's answers, every run. A render
# from a template that predates the agent prompt drops the `agent` answer, and rendering from
# the last config would then lose it for good; the seed Ansible writes always carries it.
install -m 0600 "$seed" "$config"
"$chezmoi" init --no-tty

if [[ $("$chezmoi" execute-template '{{ includeTemplate "is-agent" . }}' 2>/dev/null) != true ]]; then
  echo "the source at $after does not render the agent variant (is-agent is not true);" \
    "refusing to apply the operator's dotfiles to $USER" >&2
  exit 1
fi

# The subset the role installed before the dotfiles (tasks/agent_operator_config.yml) is
# root-owned. chezmoi cannot write into it, and the agent cannot delete it, but the agent owns
# ~/.claude and so can rename it in place; the role deletes what this moved on its next apply.
# Only after the check above, so a refused source leaves the subset where it was.
for tree in operator rules output-styles skills; do
  if [[ -d "$HOME/.claude/$tree" && $(stat -c %U "$HOME/.claude/$tree") == root ]]; then
    mv -T "$HOME/.claude/$tree" "$HOME/.claude/.subset-copy-$tree"
  fi
done

mapfile -t dirs < <("$chezmoi" managed --include=dirs --path-style=absolute |
  grep -vxF -e "$HOME/.claude" -e "$HOME/.config/gh")
if ((${#dirs[@]})); then
  "$chezmoi" apply --force --no-tty --include=dirs --recursive=false "${dirs[@]}"
fi
"$chezmoi" apply --force --no-tty --exclude=dirs

if [[ $before == "$after" ]]; then
  echo "applied at $after"
else
  echo "applied $before..$after"
fi
