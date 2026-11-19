#!/usr/bin/env python3
# gen-hooks: register
#   event: PreToolUse
#   matcher: Bash
#   timeout: 15
#   order: 10
#   args: --ask-on-cd=block-protected-bash,block-footguns
"""One PreToolUse:Bash hook that runs the Bash arms in a single process.

Usage: Claude Code runs this hook on PreToolUse:Bash, passing
`--ask-on-cd=block-protected-bash,block-footguns`. It reads the hook JSON payload on stdin. It
prints one merged `hookSpecificOutput` JSON object (a `deny` or `ask` decision, an
`additionalContext` injection, an `updatedInput` rewrite) or nothing, and it exits 0. `-h` or
`--help` prints this text and exits 0 without reading stdin.

WHY. Each Bash tool call used to start five `uv run --no-sync --quiet python <arm>.py`
processes — `auto-approve-readonly`, `block-protected-bash`, `nudge-land-sh`,
`block-footguns` and `inject-nested-docs`. Loki counted 18,421 PreToolUse:Bash calls in the
7 days to 2026-09-24 at a mean of about 92 ms per call, and four of those five interpreter
starts buy nothing: all five arms import `_hook_common` and `claude_guard.segment`, so one
process loads that once and calls each arm's own function. Issue #2394. `nudge-land-sh` was
deleted in #3395.

The fifth arm, `auto-approve-readonly`, moved into the dotfiles `claude_guard` package
(`claude_guard/readonly.py`, dotfiles #628), whose user-level PreToolUse hook allows a
provably read-only command in every trusted checkout. No arm here returns `allow` since.

Two arms rewrite rather than judge, and they run LAST, so every decision arm reads the command
the session typed. `uv-python` (#3286) routes bare python/pytest through `uv run`, which
is what the two separate hooks did, `bash-pretool.sh` at order 10 and `uv-python.sh` at order 20.
Its own docstring owns the rewrite rules and what the harness does with an `updatedInput` that
arrives beside a `deny` or an `ask`. `strip-cd-cwd` (#3957) runs before it and drops a leading
`cd <dir> &&` when `<dir>` is the session's cwd. Each rewrite arm reads the text the one before
it produced.

AUTO MODE TAKES NO REWRITE. The auto-mode classifier reviews a call before this hook runs, and
it refuses any call whose input a hook then changed. So in auto mode `strip-cd-cwd` does not
run, and whatever `uv-python` produced is emitted as a `deny` whose reason names the rewritten
command. The session's one retry sends that command, which the classifier reviews as it runs
and which every rewrite arm leaves as it is.

THE MERGE is the one Claude Code performs across separate hooks, read from the 2.1.267
bundle: `deny` is sticky, `defer` outranks `ask`, `ask` outranks `allow`, and `allow` only
stands when nothing else was returned. The surfaced reason belongs to the FIRST hook whose
decision equals the winner, so the arms run here in the order their registrations used to
give them (block-protected-bash 30, block-footguns 50, inject-nested-docs 70) and the first arm at the winning level keeps the reason. No arm
returns `defer` or `allow`, so in practice this carries `deny > ask`.

`permissionDecision`, `additionalContext` and `updatedInput` ride in ONE `hookSpecificOutput`
object. The bundle's PreToolUse branch reads `additionalContext` independently off the same
object (`O.additionalContext=e.hookSpecificOutput.additionalContext` runs after the
`permissionDecision` switch, unconditionally), and a hook that denies still yields its
context. So a denied command keeps the nested-docs injection it had when the injector was
its own hook. `updatedInput` is the one key the decision does govern: a `deny` drops it, an
`ask` carries it into the prompt. `uv-python.py`'s docstring has the evidence and why that
reproduces what two separate hooks did.

DECIDED: every arm runs under its own `try/except`, and an arm that raises loses its verdict
while the others keep theirs. That matches what separate processes did — an
unhandled raise in one `.py` exited non-zero with no stdout, which the harness reads as "no
decision" for that hook alone. What it must never become is silent: the traceback goes to
stderr named by arm, because an arm that has stopped judging while the dispatcher still
reports clean verdicts is the failure this file is most able to hide (#2394).

Run standalone: `uv run --no-sync python .claude/hooks/bash-pretool.py < payload.json`.
"""

import dataclasses
import importlib.util
import json
import os
import sys
import traceback

from _hook_common import BashCall, read_payload, tagged

_HERE = os.path.dirname(os.path.abspath(__file__))

# The arms, in the order their separate registrations ran. That order decides which reason is
# surfaced when two arms return the same decision, so it is data here rather than an accident
# of import order.
_DECISION_ARMS = (
    ("block-protected-bash", "block_protected_bash"),
    ("block-footguns", "block_footguns"),
    ("block-batch-full-suite", "block_batch_full_suite"),
)
_CONTEXT_ARM = ("inject-nested-docs", "inject_nested_docs")

# The arms that rewrite rather than judge, in the order they apply. Asked last, so every
# decision above read the command as typed. `strip-cd-cwd` goes first so that `cd <cwd> &&
# pytest` reaches `uv-python` as `pytest` and leaves as `uv run pytest`.
_REWRITE_ARMS = (("strip-cd-cwd", "strip_cd_cwd"), ("uv-python", "uv_python"))

# Rewrite arms not asked in auto mode: the classifier has already reviewed the typed command, so
# stripping a `cd` there buys nothing and draws the input-changed refusal.
_SKIPPED_IN_AUTO_MODE = frozenset({"strip-cd-cwd"})

_AUTO_MODE_DENY = (
    "Auto mode refuses a call whose input a hook changed after its review, so this hook names "
    "the rewrite instead of applying it. Send this command, alone: {command}"
)

# Highest first. `defer` is in the table because the harness ranks it between deny and ask; no
# arm returns one today, and leaving it out would silently demote an arm that grew one.
_PRECEDENCE = ("deny", "defer", "ask", "allow")


def load_arm(filename, module_name):
    """The arm module at `filename`, loaded by path.

    Every arm's filename is hyphenated, so none is importable by name — and renaming them
    would change the paths the tests and the standalone invocations use.
    """
    path = os.path.join(_HERE, filename + ".py")
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec and spec.loader, f"spec_from_file_location found no loader for {path}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _report(arm_name, exc):
    """Name the arm that stopped judging: its traceback on stderr, one line for the model.

    The harness drops a hook's stderr when it exits 0, so the traceback reaches only a person
    running this by hand. The returned line rides in `additionalContext`, which is how the agent
    learns a guard skipped this call.

    Returns:
        `[bash-pretool] arm <arm> raised <ExceptionType>: <first line of its message>`.
    """
    line = tagged("bash-pretool", f"arm {arm_name} raised {type(exc).__name__}")
    print(f"{line}, so it returned no verdict:", file=sys.stderr)
    traceback.print_exception(type(exc), exc, exc.__traceback__, file=sys.stderr)
    message = str(exc).strip().splitlines()
    return f"{line}: {message[0]}" if message else line


def collect(
    call: BashCall,
    arms=_DECISION_ARMS,
    context_arm=_CONTEXT_ARM,
    load=load_arm,
    rewrite_arms=_REWRITE_ARMS,
):
    """Every arm's output for `call`: the verdicts, the context text and the rewrite.

    Each reason carries its arm's name as a `[<arm>] ` prefix, so the merged verdict still
    says which arm decided: the OTEL `tool_decision` stream names `run-hook.sh bash-pretool`
    for every Bash verdict. An arm that raises, including one that cannot be imported,
    contributes no verdict; `_report` names it on stderr and in one line that opens `context`.
    An arm with no opinion contributes nothing and is silent.

    Args:
        call: the parsed Bash call.
        arms: the (filename, module name) pairs to ask for a decision, in precedence order.
        context_arm: the one pair asked for injectable context instead.
        load: how to turn a pair into a module. The tests hand one that raises, or one that
            returns a real arm with its temp paths redirected, because each arm is loaded
            fresh here and so cannot be monkeypatched from outside.
        rewrite_arms: the pairs asked for a rewritten command, in order. Each reads the text
            the previous one produced, through `dataclasses.replace`, and an arm that raises or returns None leaves that text
            as it was. Asked after the decision arms so they judge the command as typed. In
            auto mode an arm in `_SKIPPED_IN_AUTO_MODE` is not asked, and the text the others
            produced becomes a `deny` verdict naming it, attributed to the last arm that
            changed it.

    Returns:
        `(verdicts, context, command)`, where `context` is None when no arm raised and the
        context arm had nothing, and `command` is None when no rewrite arm changed the text,
        and always None in auto mode.
    """
    verdicts = []
    failures = []
    for filename, module_name in arms:
        try:
            verdict = load(filename, module_name).decision(call)
        except Exception as exc:  # Blind on purpose: one arm must not silence the rest.
            failures.append(_report(filename, exc))
            continue
        if verdict:
            decision, reason = verdict
            verdicts.append((decision, tagged(filename, reason)))
    context = None
    try:
        context = load(*context_arm).context(call)
    except Exception as exc:
        failures.append(_report(context_arm[0], exc))
    auto_mode = call.permission_mode == "auto"
    command = None
    rewriter = ""
    for filename, module_name in rewrite_arms:
        if auto_mode and filename in _SKIPPED_IN_AUTO_MODE:
            continue
        current = (
            call if command is None else dataclasses.replace(call, command=command)
        )
        try:
            rewritten = load(filename, module_name).rewrite(current)
        except Exception as exc:
            failures.append(_report(filename, exc))
            continue
        if rewritten is not None:
            command, rewriter = rewritten, filename
    if auto_mode and command is not None:
        reason = _AUTO_MODE_DENY.format(command=command)
        verdicts.append(("deny", tagged(rewriter, reason)))
        command = None
    if failures:
        context = "\n\n".join([*failures, *([context] if context else [])])
    return verdicts, context, command


def merge(verdicts):
    """The winning (decision, reason), or (None, None) when no arm decided anything.

    Highest precedence wins; within a level the earliest arm keeps the reason, which is what
    the harness does across separate hooks. A decision this table does not know is ignored
    rather than guessed at — ranking one wrongly is how a deny becomes an allow.
    """
    for level in _PRECEDENCE:
        for decision, reason in verdicts:
            if decision == level:
                return decision, reason
    return None, None


def emit(decision, reason, context, command=None, tool_input=None):
    """Print the single `hookSpecificOutput` carrying whatever the arms produced.

    A rewrite is emitted whatever the decision is, rather than suppressed under a `deny`. The
    harness already drops it there, and suppressing it here would put a second rule in a second
    place for the same outcome — the rewrite arm would then be the only arm whose output another
    arm's verdict can erase before it is printed.

    The rewrite carries every key of `tool_input` with only `command` replaced. The harness
    REPLACES the tool input with `updatedInput` rather than merging it, so a rewrite that sent
    `command` alone dropped `run_in_background`, `timeout` and `description`. A backgrounded bare
    `pytest --version` ran inline under the 120s default timeout, while an unrewritten
    backgrounded `printf` got a task ID (#3501, 2026-10-04).
    """
    output = {"hookEventName": "PreToolUse"}
    if decision:
        output["permissionDecision"] = decision
        output["permissionDecisionReason"] = reason
    if context:
        output["additionalContext"] = context
    if command is not None:
        output["updatedInput"] = {**(tool_input or {}), "command": command}
    if len(output) == 1:
        return
    print(json.dumps({"hookSpecificOutput": output}))


def main(load=load_arm):
    """Read the hook payload from stdin, run every arm, emit the merged decision.

    `load` is `collect`'s loader, threaded through so the tests can drive a stub arm from
    the real entry point rather than from a re-implementation of it.
    """
    event = read_payload()
    call = event.bash() if event else None
    if call is None:  # A call with no command is one no arm could have judged.
        return 0
    verdicts, context, command = collect(call, load=load)
    decision, reason = merge(verdicts)
    emit(decision, reason, context, command, call.tool_input)
    return 0


if __name__ == "__main__":
    if any(arg in ("-h", "--help") for arg in sys.argv[1:]):
        print(__doc__.strip())
        sys.exit(0)
    sys.exit(main())
