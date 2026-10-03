#!/bin/bash
# gen-hooks: library
#   reason: the one shell entry point for every hook here; settings.json switches to it in the second half of #3278, once the primary checkout holds this file
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
#   run-hook.sh bash-pretool --project --ask-on-cd=block-protected-bash,nudge-land-sh
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
# reason names all three guards that did not run. The two silent arms cost a missed approval
# and a missed doc injection, which is a prompt and a re-read, not a bypass.
#
# What #3278 changed: a missing `.py` sibling now asks too, where it used to exit non-zero
# through a failed `exec`. The old note said no `ask` could follow a failed `exec` — true, and
# the fix is to not exec. The file is tested for first, so the decision is still available.
# `.claude/hooks/hooklib/hook_registration_lines.py` exists because that exact failure is
# invisible from the session side: the shim runs, nothing exits 127, and the guard is skipped.
# `uv` itself missing still exits non-zero with its own stderr line.

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

[[ "$ask_guards" == pending ]] && ask_guards=$name
# An argument reaches the reason string, so it is constrained to what a hook name can hold
# rather than escaped. Anything else falls back to the hook's own name, which is already known
# good. A reason is prose for an operator; it is not worth a JSON escaper.
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

if [[ -n "$project" ]]; then
    cd /home/ubuntu/server || {
        did_not_run "guards did not run" "cd to /home/ubuntu/server failed"
        exit 0
    }
fi

if [[ ! -f "$script" ]]; then
    did_not_run "guards did not run" "$script does not exist"
    exit 0
fi

if [[ -n "$project" ]]; then
    exec /home/ubuntu/.local/bin/uv run --no-sync --quiet python "$script"
fi

/home/ubuntu/.local/bin/uv run --no-project --no-python-downloads --python 3.14.6 \
    "$script" 2>/dev/null
exit 0
