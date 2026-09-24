#!/usr/bin/env python3
# gen-hooks: library
#   reason: the four PreToolUse:Bash arms, run in one process by bash-pretool.sh
"""One PreToolUse:Bash hook that runs the four Bash arms in a single process.

WHY. Each Bash tool call used to start five `uv run --no-sync --quiet python <arm>.py`
processes — `auto-approve-readonly`, `block-protected-bash`, `nudge-land-sh`,
`block-footguns` and `inject-nested-docs`. Loki counted 18,421 PreToolUse:Bash calls in the
7 days to 2026-09-24 at a mean of about 92 ms per call, and four of those five interpreter
starts buy nothing: all five arms import `_hook_common` and `claude_guard.segment`, so one
process loads that once and calls each arm's own function. Issue #2394.

The fifth arm, `auto-approve-readonly`, moved into the dotfiles `claude_guard` package
(`claude_guard/readonly.py`, dotfiles #628), whose user-level PreToolUse hook allows a
provably read-only command in every trusted checkout. No arm here returns `allow` since.

`uv-python.sh` stays a separate hook. It rewrites the command rather than judging it, so it
has no verdict to merge, and a rewrite arm inside a verdict dispatcher would have to decide
whether the arms after it judge the old text or the new one.

THE MERGE is the one Claude Code performs across separate hooks, read from the 2.1.267
bundle: `deny` is sticky, `defer` outranks `ask`, `ask` outranks `allow`, and `allow` only
stands when nothing else was returned. The surfaced reason belongs to the FIRST hook whose
decision equals the winner, so the arms run here in the order their registrations used to
give them (block-protected-bash 30, nudge-land-sh 40, block-footguns 50,
inject-nested-docs 70) and the first arm at the winning level keeps the reason. No arm
returns `defer` or `allow`, so in practice this carries `deny > ask`.

`permissionDecision` and `additionalContext` ride in ONE `hookSpecificOutput` object. The
bundle's PreToolUse branch reads the two keys independently off the same object
(`O.additionalContext=e.hookSpecificOutput.additionalContext` runs after the
`permissionDecision` switch, unconditionally), and a hook that denies still yields its
context. So a denied command keeps the nested-docs injection it had when the injector was
its own hook.

DECIDED: every arm runs under its own `try/except`, and an arm that raises loses its verdict
while the others keep theirs. That matches what separate processes did — an
unhandled raise in one `.py` exited non-zero with no stdout, which the harness reads as "no
decision" for that hook alone. What it must never become is silent: the traceback goes to
stderr named by arm, because an arm that has stopped judging while the dispatcher still
reports four clean verdicts is the failure this file is most able to hide (#2394).

Run standalone: `uv run --no-sync python .claude/hooks/bash-pretool.py < payload.json`.
"""

import importlib.util
import json
import os
import sys
import traceback

_HERE = os.path.dirname(os.path.abspath(__file__))

# The arms, in the order their separate registrations ran. That order decides which reason is
# surfaced when two arms return the same decision, so it is data here rather than an accident
# of import order.
_DECISION_ARMS = (
    ("block-protected-bash", "block_protected_bash"),
    ("nudge-land-sh", "nudge_land_sh"),
    ("block-footguns", "block_footguns"),
)
_CONTEXT_ARM = ("inject-nested-docs", "inject_nested_docs")

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
    """Name the arm that stopped judging, on stderr, with its traceback."""
    print(
        f"bash-pretool.py: arm {arm_name} raised, so it returned no verdict:",
        file=sys.stderr,
    )
    traceback.print_exception(type(exc), exc, exc.__traceback__, file=sys.stderr)


def collect(payload, arms=_DECISION_ARMS, context_arm=_CONTEXT_ARM, load=load_arm):
    """Every arm's verdict for `payload`: a list of (decision, reason), plus the context text.

    An arm that raises — including one that cannot be imported — contributes nothing and is
    reported on stderr. An arm with no opinion contributes nothing and is silent.

    Args:
        payload: the hook JSON, already parsed.
        arms: the (filename, module name) pairs to ask for a decision, in precedence order.
        context_arm: the one pair asked for injectable context instead.
        load: how to turn a pair into a module. The tests hand one that raises, or one that
            returns a real arm with its temp paths redirected, because each arm is loaded
            fresh here and so cannot be monkeypatched from outside.
    """
    verdicts = []
    for filename, module_name in arms:
        try:
            verdict = load(filename, module_name).decision(payload)
        except Exception as exc:  # Blind on purpose: one arm must not silence the rest.
            _report(filename, exc)
            continue
        if verdict:
            verdicts.append(verdict)
    context = None
    try:
        context = load(*context_arm).context(payload)
    except Exception as exc:
        _report(context_arm[0], exc)
    return verdicts, context


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


def emit(decision, reason, context):
    """Print the single `hookSpecificOutput` carrying whatever the arms produced."""
    output = {"hookEventName": "PreToolUse"}
    if decision:
        output["permissionDecision"] = decision
        output["permissionDecisionReason"] = reason
    if context:
        output["additionalContext"] = context
    if len(output) == 1:
        return
    print(json.dumps({"hookSpecificOutput": output}))


def main(load=load_arm):
    """Read the hook payload from stdin, run every arm, emit the merged decision.

    `load` is `collect`'s loader, threaded through so the tests can drive a stub arm from
    the real entry point rather than from a re-implementation of it.
    """
    try:
        payload = json.load(sys.stdin)
    except Exception:  # An unreadable payload is one no arm could have judged.
        return 0
    verdicts, context = collect(payload, load=load)
    decision, reason = merge(verdicts)
    emit(decision, reason, context)
    return 0


if __name__ == "__main__":
    sys.exit(main())
