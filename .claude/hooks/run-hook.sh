#!/bin/bash
# gen-hooks: library
#   reason: the one shell entry point for every hook here; a `.py` register block renders as `run-hook.sh <stem> <args>`
#
# Usage: run-hook.sh <name> [--project] [--ask-on-cd[=<guards>]]
#
# Runs `.claude/hooks/<name>.py` beside this script, under the one interpreter pin these hosts
# have. The flags select one of three postures:
#
#   run-hook.sh session-health
#       uv run --no-project, always exit 0. The posture for an observability hook that must
#       never block: SessionStart, InstructionsLoaded, Stop. stdout passes through. stderr is
#       dropped on success; on a non-zero exit its last line replaces stdout as one
#       `{"systemMessage":"[<name>] failed: <line>"}`, so a crash is never silent.
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
# stays fail-open, so a broken guard never bricks every tool call. An `ask` is a prompt, not a
# brick, while a bare `exit 0` from a DENY guard disarms it in silence. One process serves
# several PreToolUse:Bash guards, so it asks and the reason names every guard that did not run.
# The script is tested for before the `exec`, because no `ask` can follow a failed `exec`.
#
# `$HOOKS_DIR` is the session checkout's hooks directory, the one its `settings.json` was
# rendered from. The `cd` names the primary checkout instead: it supplies the `.venv`, which a
# fresh worktree has not built. When that venv cannot run, the session checkout's venv stands
# in; with neither, the guards ask. A missing `uv` exits non-zero with its own stderr line.
#
# This file cannot report its own absence: when `$CLAUDE_PROJECT_DIR` is deleted, the
# `--ask-on-cd` registrations deny through `gen_hook_settings.py`'s `GUARD_SUFFIX`.

set -u

for arg in "$@"; do
  case "$arg" in
    -h | --help) awk 'NR > 1 && /^#/ { print; next } NR > 1 { exit }' "$0"; exit 0 ;;
  esac
done

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
            echo "[run-hook.sh] unknown flag '$1' — the hook did not run" >&2
            exit 0
            ;;
        *)
            if [[ -n "$name" ]]; then
                echo "[run-hook.sh] more than one hook name ('$name', '$1') — neither ran" >&2
                exit 0
            fi
            name=$1
            ;;
    esac
    shift
done

if [[ -z "$name" ]]; then
    echo "[run-hook.sh] no hook name given — nothing ran" >&2
    exit 0
fi

# Unparsable output from a DENY guard reads as a silent allow.
if [[ ! "$name" =~ ^[A-Za-z0-9_.-]+$ ]]; then
    echo "[run-hook.sh] '$name' is not a hook name — nothing ran" >&2
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
    echo "[$name] $what ($why)${ask_guards:+ — asking instead of allowing}" >&2
    [[ -n "$ask_guards" ]] || return 0
    printf '%s\n' "{\"hookSpecificOutput\":{\"hookEventName\":\"PreToolUse\",\"permissionDecision\":\"ask\",\"permissionDecisionReason\":\"[$name] $what ($why), so this call was checked by none of: $ask_guards. Review it yourself.\"}}"
}

# The checkout whose `.venv` `uv run --no-sync` uses, and the uv that runs it, default to the
# running user's own. The Renovate agent's user cannot read the operator's home, so its unit
# overrides both. A value outside the path charset is dropped, since it reaches the JSON `ask`
# reason; a dropped `$HOME` leaves `/server`, so the `cd` fails and the guard asks.
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

# Walk for `.fanout/brief.md` here so an ordinary session's Stop starts no interpreter.
if [[ "$name" == fanout-stop ]]; then
    payload=$(cat)
    cwd_re='"cwd"[[:space:]]*:[[:space:]]*"([^"\]*)"'
    if [[ "$payload" =~ $cwd_re ]]; then
        dir=${BASH_REMATCH[1]}
        [[ "$dir" == /* ]] || dir=$PWD/$dir
        while [[ ! -f "$dir/.fanout/brief.md" ]]; do
            [[ -n "$dir" ]] || exit 0
            dir=${dir%/*}
        done
    fi
    exec <<<"$payload"
fi

# `--no-sync` keeps a venv whose interpreter mismatches the pin, but uv deletes and recreates one
# whose `bin/python` cannot run, and so creates a missing one. A sandboxed session cannot write
# the primary checkout, so that rebuild fails with EACCES; the session's own venv stands in.
# With neither, the guards ask: the project-free interpreter lacks PyYAML, which
# block-protected-bash's secret-path arm needs. A cd target with no project runs as is.
# DECIDED: the hook's own scripts/ goes ahead of the venv's .pth, which names the venv's own
# checkout, and a module this checkout lacks falls through to that checkout. (ADR-0039)
if [[ -n "$project" ]]; then
    export PYTHONPATH="${HOOKS_DIR%/.claude/hooks}/scripts"
    if [[ -x .venv/bin/python || ! -f pyproject.toml ]]; then
        exec "$uv" run --no-sync --quiet python "$script"
    fi
    own_venv=${HOOKS_DIR%/.claude/hooks}/.venv
    if [[ -x "$own_venv/bin/python" && "$own_venv" =~ ^/[A-Za-z0-9_./-]+$ ]]; then
        echo "[$name] $project_dir/.venv/bin/python cannot run, so $own_venv runs this hook" >&2
        UV_PROJECT_ENVIRONMENT=$own_venv exec "$uv" run --no-sync --quiet python "$script"
    fi
    did_not_run "guards did not run" "neither $project_dir/.venv nor $own_venv has a Python that runs"
    exit 0
fi

# A no-venv run gets no `.pth`, so `PYTHONPATH` supplies the import root: this checkout's
# `scripts/`.
exec 3>&1
err=$(PYTHONPATH="${HOOKS_DIR%/.claude/hooks}/scripts" "$uv" run --quiet --no-project --no-python-downloads --python 3.15.2 \
    "$script" 2>&1 >&3 3>&-)
rc=$?
exec 3>&-
if ((rc != 0)); then
    # Strip control characters and escape JSON specials: the traceback line can carry any bytes.
    cause=$(printf '%s\n' "$err" | awk 'NF { last = $0 } END { print last }' | tr -d '\000-\037')
    cause=${cause:0:300}
    cause=${cause:-exit $rc with no stderr}
    cause=${cause//\\/\\\\}
    cause=${cause//\"/\\\"}
    printf '%s\n' "{\"systemMessage\":\"[$name] failed: $cause\"}"
fi
exit 0
