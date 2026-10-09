#!/bin/bash
# gen-hooks: library
#   reason: the one shell entry point for every hook here; a `.py` register block renders as `run-hook.sh <stem> <args>` (#3278)
#
# Usage: run-hook.sh <name> [--project] [--ask-on-cd[=<guards>]]
#
# Runs `.claude/hooks/<name>.py` beside this script, under the one interpreter pin these hosts
# have. Six per-hook shims in three variants were 134 lines of near-duplicate bash, and the
# interpreter pin was written out three times (#3278). The variants are now flags:
#
#   run-hook.sh session-health
#       uv run --no-project, stderr quieted, always exit 0. The posture for an observability
#       hook that must never block: SessionStart, InstructionsLoaded, Stop.
#
#   run-hook.sh auto-mode-bridge --project
#       cd into the repo, then `exec` so the hook's stdin JSON survives. No output means the
#       event passes through unchanged. A failed `cd` is silent on stdout.
#
#   run-hook.sh bash-pretool --ask-on-cd=block-protected-bash,block-footguns
#       the same, plus an `ask` decision when the hook could not run at all. `<guards>` names
#       what did not run; it defaults to <name>.
#
# `--ask-on-cd` implies `--project`: there is no `cd` to fail without one.
#
# Routed through uv so the project-pinned interpreter runs (not the system python3, which
# cannot parse this repo). `--no-sync` skips the env reconcile to stay fast on the per-call hot
# path, where `--no-project` is for a hook whose cwd is arbitrary and would otherwise make uv
# resolve some other project.
#
# DECIDED: the `cd` arm and a missing `.py` both ask under `--ask-on-cd`; every other failure
# stays fail-open. Issue #1014 made all three failure paths fail-open so that a broken guard
# does not brick every tool call, and gave the `cd` arm the stderr line the other two already
# had. An `ask` is a prompt, not a brick: the operator reads why a DENY guard could not run and
# decides once, where a bare `exit 0` disarmed it in silence (#2171). #2394 made one shim carry
# a posture that used to be split: three of the five PreToolUse:Bash arms asked on a failed
# `cd` and two stayed silent, and a single process can only do one thing, so it asks and the
# reason names every guard that did not run. The two silent arms cost a missed approval
# and a missed doc injection, which is a prompt and a re-read, not a bypass.
#
# What #3278 changed: a missing `.py` sibling now asks too, where it used to exit non-zero
# through a failed `exec`. The old note said no `ask` could follow a failed `exec` — true, and
# the fix is to not exec. The file is tested for first, so the decision is still available.
#
# Each session registers this file from its own checkout, as `$CLAUDE_PROJECT_DIR/.claude/
# hooks/run-hook.sh` (#3394), so `$HOOKS_DIR` is that checkout's hooks directory and the `.py`
# beside it is the one the session's `settings.json` was rendered from. The `cd` below still
# names the primary checkout: it supplies the `.venv` `uv run` uses, which a fresh worktree has
# not built yet.
# `uv` itself missing still exits non-zero with its own stderr line.
#
# This file cannot report its own absence. When the session's `$CLAUDE_PROJECT_DIR` is deleted,
# the `--ask-on-cd` registrations deny through `gen_hook_settings.py`'s `GUARD_SUFFIX` (#3887).

set -u

HOOKS_DIR="$(dirname "$(readlink -f "$0")")"

name=
project=
ask_guards=

while (($# > 0)); do
    case "$1" in
        --project) project=1 ;;
        --ask-on-cd)
            project=1
            ask_guards=pending
            ;;
        --ask-on-cd=*)
            project=1
            ask_guards=${1#--ask-on-cd=}
            ;;
        -*)
            echo "run-hook.sh: unknown flag '$1' — the hook did not run" >&2
            exit 0
            ;;
        *)
            if [[ -n "$name" ]]; then
                echo "run-hook.sh: more than one hook name ('$name', '$1') — neither ran" >&2
                exit 0
            fi
            name=$1
            ;;
    esac
    shift
done

if [[ -z "$name" ]]; then
    echo "run-hook.sh: no hook name given — nothing ran" >&2
    exit 0
fi

# Both arguments reach the `ask` reason, which is a JSON string, so each is constrained to what
# a hook name can hold rather than escaped. A reason is prose for an operator; it is not worth
# a JSON escaper, and unparsable output from a DENY guard reads to the harness as no decision —
# a silent allow, which is what #2171's `ask` exists to prevent.
#
# The name bails rather than falling back, because there is nothing to fall back TO: a name
# carrying a quote or a slash is no `.py` basename this directory could hold, so running on is
# a guess about which hook the operator meant.
if [[ ! "$name" =~ ^[A-Za-z0-9_.-]+$ ]]; then
    echo "run-hook.sh: '$name' is not a hook name — nothing ran" >&2
    exit 0
fi

[[ "$ask_guards" == pending ]] && ask_guards=$name
# The guard list does fall back, to the name this point has already validated.
if [[ -n "$ask_guards" && ! "$ask_guards" =~ ^[A-Za-z0-9_,.-]+$ ]]; then
    ask_guards=$name
fi

script="$HOOKS_DIR/$name.py"

# Both arguments are prose for the one stderr line and the one `ask` reason.
did_not_run() {
    local what=$1 why=$2
    echo "run-hook.sh $name: $what ($why)${ask_guards:+ — asking instead of allowing}" >&2
    [[ -n "$ask_guards" ]] || return 0
    printf '%s\n' "{\"hookSpecificOutput\":{\"hookEventName\":\"PreToolUse\",\"permissionDecision\":\"ask\",\"permissionDecisionReason\":\"run-hook.sh $name: $what ($why), so this call was checked by none of: $ask_guards. Review it yourself.\"}}"
}

# The checkout whose `.venv` `uv run --no-sync` uses, and the uv that runs it. Both default to
# the running user's own: `$HOME/server` and `$HOME/.local/bin/uv`, which are the operator's
# checkout and uv for `ubuntu`. The unattended Renovate agent runs as its own user, which
# cannot read the operator's home, so its unit points both at its own run worktree and its own
# uv. An override outside the path charset is ignored rather than trusted, because the
# directory reaches the `ask` reason, which is a JSON string. A `$HOME` outside it is dropped
# for the same reason, which leaves `/server`: the `cd` fails and the guard asks.
home=${HOME:-}
[[ "$home" =~ ^/[A-Za-z0-9_./-]+$ ]] || home=
project_dir=${RUN_HOOK_PROJECT_DIR:-$home/server}
uv=${RUN_HOOK_UV:-$home/.local/bin/uv}
[[ "$project_dir" =~ ^/[A-Za-z0-9_./-]+$ ]] || project_dir=$home/server
[[ "$uv" =~ ^/[A-Za-z0-9_./-]+$ ]] || uv=$home/.local/bin/uv

if [[ -n "$project" ]]; then
    cd "$project_dir" || {
        did_not_run "guards did not run" "cd to $project_dir failed"
        exit 0
    }
fi

if [[ ! -f "$script" ]]; then
    did_not_run "guards did not run" "$script does not exist"
    exit 0
fi

if [[ -n "$project" ]]; then
    exec "$uv" run --no-sync --quiet python "$script"
fi

"$uv" run --no-project --no-python-downloads --python 3.14.6 \
    "$script" 2>/dev/null
exit 0
