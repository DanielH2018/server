"""Read every batch on one host in one call, and stop one."""

import json
import re
import subprocess
from collections.abc import Callable, Sequence
import dataclasses
from dataclasses import dataclass

# Reach the sibling package: a directly-invoked script gets only its own directory on
# sys.path, and pyproject's `pythonpath` is a pytest setting.
import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))

from fanout_lib.brief import lands
from fanout_lib.manifest import Batch, Manifest
from fanout_lib.transport import Tools
from fanout_lib.target import SERVER

STATUS_TIMEOUT_S = 30.0
PR_URL = re.compile(r"https://github\.com/[\w.-]+/[\w.-]+/pull/\d+")
# The line a session ends with when it cannot finish: the brief's *Finishing* section names
# both prefixes. `.claude/hooks/fanout-stop.py` lets a session stop on either this or a PR URL,
# and copies both patterns because the hooks stay stdlib-only; a test holds the copies equal.
BLOCKER = re.compile(r"(?im)^(?:needs input|failed):")
# The line `land.sh` prints when the landing reaches a verdict. A batch on the landing host
# owes one: a PR URL alone says the PR was opened, never that it merged and deployed.
# `.claude/hooks/fanout-stop.py` copies this pattern too.
VERDICT = re.compile(r"(?m)^VERDICT:")
RESULT_TYPE = "result"
# The state of a batch whose unit ended cleanly but left no report behind it.
NO_REPORT = "no-report"
# A clean finish whose final text names a blocker line instead of a PR.
NEEDS_INPUT = "needs-input"
# A clean finish whose final text names neither a PR nor a blocker: a progress report that
# ended the turn, left after the Stop hook's continuations ran out.
NO_PR = "no-pr"
# A batch on the landing host that opened its PR and ended its turn before `land.sh` printed a
# verdict. The PR exists; the landing it owes did not finish.
NO_VERDICT = "no-verdict"


@dataclass(frozen=True)
class BatchStatus:
    """One batch's verdict, read from its unit's properties and its result JSON.

    Attributes:
        batch: the batch id.
        state: running, done, needs-input, no-pr, no-verdict, no-report, or failed. `done`
            means the final text carries a PR URL, and on the landing host a verdict with
            it.
        pr_url: the PR the final text names, or empty.
        final_text: the session's own `result` string, or empty.
        exit_code: the unit's ExecMainStatus, or None when it could not be read.
        stderr_tail: the last of the unit's stderr log.
        is_error: the session's own `is_error` flag — true means the agent failed even
            though the unit may have exited 0.
        permission_denials: how many tool calls the session was denied.
        terminal_reason: why the session ended, when it says.
    """

    batch: str
    state: str  # running | done | needs-input | no-pr | no-verdict | no-report | failed
    pr_url: str
    final_text: str
    exit_code: int | None
    stderr_tail: str
    is_error: bool = False
    permission_denials: int = 0
    terminal_reason: str = ""


def _one(b: Batch) -> str:
    return (
        f"echo '=== {b.batch}'; "
        f"systemctl --user show {b.unit} -p ActiveState -p Result -p ExecMainStatus; "
        f"echo '--- stderr'; tail -c 2000 {b.worktree}/.fanout/stderr.log 2>/dev/null; "
        f"echo; echo '--- report'; cat {b.worktree}/.fanout/report.json 2>/dev/null; "
        # The landing's own record, read because a batch can land correctly and still not
        # echo the verdict into its final message. Only the last line is needed, and
        # `land.sh` prints one verdict per run.
        f"echo; echo '--- verdict'; "
        f"grep -h '^VERDICT:' {b.worktree}/.fanout/land*.log 2>/dev/null | tail -n 1"
    )


def status_command(batches: Sequence[Batch]) -> str:
    return "; ".join(_one(b) for b in batches)


def stop_command(unit: str) -> str:
    return f"systemctl --user stop {unit}"


_SECTION_NAMES = ("stderr", "report", "verdict")


def _section(block: str, name: str) -> str:
    # Bound the section only at a KNOWN marker ("--- stderr" / "--- report"), never at
    # any line starting "--- " — the section's own content (a stderr line, report prose)
    # can start that way without ending the section early.
    marker = f"--- {name}\n"
    if marker not in block:
        return ""
    rest = block.split(marker, 1)[1]
    ends = [
        idx
        for other in _SECTION_NAMES
        if other != name
        for idx in [rest.find(f"\n--- {other}\n")]
        if idx != -1
    ]
    return rest[: min(ends)] if ends else rest


def _result(report: str) -> dict:
    """Return the last `{"type": "result"}` object on the report stream, or {}.

    The fields read here are the ones renovate_agent's `agent_logic._outcome` reads from the
    same `claude -p --output-format json` stream: `result`, `is_error`, `terminal_reason`
    and `permission_denials`.
    """
    for line in reversed(report.strip().splitlines()):
        stripped = line.strip()
        if not stripped.startswith("{"):
            continue
        try:
            data = json.loads(stripped)
        except ValueError:
            continue
        if isinstance(data, dict) and data.get("type") == RESULT_TYPE:
            return data
    return {}


def parse_status(batches: Sequence[Batch], stdout: str) -> list[BatchStatus]:
    known = {b.batch for b in batches}
    blocks: dict[str, str] = {}
    order: list[str] = []
    # Split only at a genuine block header (start of line). A block's own content — a
    # report's result prose, a stderr line — can still start with "=== "; when the header
    # name that produces isn't one of ours, it's not a new block, so fold it back into the
    # block already open.
    for chunk in re.split(r"(?m)^=== ", stdout)[1:]:
        name, _, body = chunk.partition("\n")
        name = name.strip()
        if name in known:
            blocks[name] = body
            order.append(name)
        elif order:
            blocks[order[-1]] += "=== " + chunk
    out = []
    for b in batches:
        body = blocks.get(b.batch, "")
        # Anchor the props/sections boundary at the start of a line, like _section does: a
        # property VALUE carrying "--- " mid-line (e.g. Description=fanout --- stderr sink)
        # would otherwise truncate the props block at that "--- ", not at the real marker.
        props = dict(
            ln.split("=", 1)
            for ln in re.split(r"(?m)^--- ", body, maxsplit=1)[0].splitlines()
            if "=" in ln
        )
        stderr_tail = _section(body, "stderr").strip()
        result = _result(_section(body, "report"))
        final = str(result.get("result") or "")
        # A session that failed still exits 0 through systemd — the failure is in the result
        # object, not the unit, which is why `Result=success` alone cannot mean done.
        is_error = bool(result.get("is_error"))
        code_text = props.get("ExecMainStatus", "")
        code = int(code_text) if code_text.isdigit() else None
        m = PR_URL.search(final)
        # Either the agent echoed the verdict or the land log holds it; the log is what keeps
        # a batch that landed correctly but reported tersely out of `no-verdict`.
        landed = bool(
            VERDICT.search(final) or VERDICT.search(_section(body, "verdict"))
        )
        if props.get("ActiveState") in ("active", "activating"):
            state = "running"
        elif props.get("Result") == "success" and final and not is_error:
            # A non-empty final text is not a finish: a turn that ends on "Next I will open
            # the PR" exits the process just as cleanly as one that opened it.
            if m and lands(b.host, b.repo) and not landed:
                # The brief tells a landing-host batch to wait for `land.sh`'s VERDICT line.
                # Without that, a PR URL only means `gh pr create` returned. Another repo's
                # batch stops at the PR on every host, so its PR URL is the finish.
                state = NO_VERDICT
            elif m:
                state = "done"
            elif BLOCKER.search(final):
                state = NEEDS_INPUT
            else:
                state = NO_PR
        elif (
            props
            and props.get("Result") == "success"
            and not is_error
            and not final
            and code in (0, None)
        ):
            # The unit ended cleanly and left no report. An agent that removes its own
            # worktree on exit takes .fanout/report.json with it, so this is what a
            # FINISHED batch looks like once it has tidied up — indistinguishable here
            # from one that died before writing anything. Only the forge can tell them
            # apart, which is why this is its own state rather than `failed`: the caller
            # reconciles it against a merged PR for the batch's branch.
            #
            # `props` must be non-empty: a batch whose block never appeared in the reply at
            # all — a mangled read, a host that answered nothing for it — parses to the same
            # empty fields, and there the absence is the defect rather than a tidied-up
            # worktree. That case stays `failed`.
            #
            # `Result` must be `success` for the same reason: a unit that `RuntimeMaxSec=`
            # stopped records `Result=timeout`, and its claude process wrote no report either.
            state = NO_REPORT
        else:
            state = (
                "failed"  # the unit exited non-zero, or the session reported is_error
            )
        out.append(
            BatchStatus(
                b.batch,
                state,
                m.group(0) if m else "",
                final,
                code,
                stderr_tail,
                is_error=is_error,
                permission_denials=len(result.get("permission_denials") or []),
                terminal_reason=str(result.get("terminal_reason") or ""),
            )
        )
    return out


def reconciled(
    s: BatchStatus, branch: str, merged_pr: Callable[[str, str], str], repo: str
) -> BatchStatus:
    """`s`, read as `landed` with its merged PR's url when it is a `no-report` GitHub merged.

    Only a `no-report` batch asks the forge, so every other state costs no network call.
    """
    if s.state != NO_REPORT:
        return s
    url = merged_pr(branch, repo)
    return dataclasses.replace(s, state="landed", pr_url=url) if url else s


def status_line(
    s: BatchStatus,
    host: str,
    branch: str,
    merged_pr: Callable[[str, str], str],
    one_line: Callable[[str], str],
    repo: str = SERVER,
) -> tuple[str, int]:
    """Render one batch's status line, and the exit tier it contributes.

    A batch on the landing host reads `no-verdict` rather than `done` when nothing — not its
    final text, not its `land<n>.log` — carries a `VERDICT:` line: the PR was opened and the
    landing it owes did not finish. A daniel-server batch stops at `gh pr create`, so a PR url
    alone is `done` there.

    A `no-report` batch is reconciled here rather than left as it was parsed: `merged_pr`
    is the only oracle that can tell a batch which finished and removed its own worktree
    from one that died before writing a report, since both leave a unit that exited 0 and
    no report to read.

    Args:
        s: the parsed status.
        host: the host the batch ran on, for the line's prefix.
        branch: the batch's branch, the key `merged_pr` is asked about.
        merged_pr: (branch, repo) -> merged PR url, or "" when GitHub knows of none.
        one_line: collapse the agent's final text to one line.
        repo: the repo the batch's branch lives in, which `merged_pr` asks about.

    Returns:
        The line to print, and the exit tier: 0 for running/done/landed, 1 for an
        unreconciled `no-report` and for a clean finish with no PR (`needs-input`, `no-pr`),
        5 for a genuine failure.
    """
    s = reconciled(s, branch, merged_pr, repo)
    state = s.state
    line = f"{s.batch} on {host}: {state}"
    if s.pr_url:
        line += f" {s.pr_url}"
    if s.permission_denials:
        line += f" permission_denials={s.permission_denials}"
    if state in ("done", NEEDS_INPUT, NO_PR, NO_VERDICT):
        line += f" {one_line(s.final_text)}"
    if state in ("failed", NO_REPORT):
        exit_text = "unknown" if s.exit_code is None else str(s.exit_code)
        line += f" (exit {exit_text})"
        if s.terminal_reason:
            line += f" {s.terminal_reason}"
        line += f" {s.stderr_tail[-300:]}"
    if state == NO_REPORT:
        # Tier 1, not 5: nothing here says the work failed, only that neither the unit nor
        # the forge can say what became of it. A 5 would send an operator to a stderr log
        # that a finished batch has already deleted along with its worktree.
        return line + f" no merged PR for {branch}", 1
    if state in (NEEDS_INPUT, NO_PR):
        # Tier 1, not 5: the session ended cleanly and its final text is the thing to read,
        # not a stderr log. Not 0 either: no PR exists to land, so an operator has to act.
        return line, 1
    if state == NO_VERDICT:
        # Tier 1 for the same reason, and the PR url stays on the line: the work to do is to
        # land that PR, which nothing here says has merged or deployed.
        return line + " — land.sh printed no VERDICT", 1
    return line, 5 if state == "failed" else 0


UNREAD = "unread"


def collect(
    run: Manifest, tools: Tools, one_line: Callable[[str], str]
) -> tuple[list[dict], int]:
    """One row per batch in `run`, and the worst exit tier among them; `status`'s whole read.

    Each row carries `batch`, `host`, `branch`, `state`, `pr_url` and `line`, the text line
    `status` prints. `status --json` prints the rows themselves, so `fanout_probe.py` reads the
    state field instead of a regex over the line (#3926). Two states come from here rather
    than from a host: `cleaned` for a batch `clean` removed, and `unread` for a host whose
    read timed out, which still counts as running.

    A cleaned batch is reported from the manifest and never read remotely. `clean` resets the
    failed unit and takes .fanout/report.json with the worktree, so `status_command` finds no
    active state, no result and no report, and `parse_status` reads exactly that as `failed`,
    which would exit 5 for a batch that landed its PR and was tidied up.
    """
    rows, worst = [], 0

    def row(b: Batch, host: str, state: str, pr_url: str, line: str) -> dict:
        return {
            "batch": b.batch,
            "host": host,
            "branch": b.branch,
            "state": state,
            "pr_url": pr_url,
            "line": line,
        }

    for b in run.batches:
        if b.removed_at:
            line = f"{b.batch} on {b.host}: cleaned ({b.removed_at})"
            rows.append(row(b, b.host, "cleaned", "", line))
    live = [b for b in run.batches if not b.removed_at]
    for host in sorted({b.host for b in live}):
        mine = [b for b in live if b.host == host]
        try:
            proc = tools.run(host, status_command(mine), STATUS_TIMEOUT_S, None)
        except subprocess.TimeoutExpired:
            for b in mine:
                line = f"{b.batch} on {host}: status read timed out"
                rows.append(row(b, host, UNREAD, "", line))
            worst = max(worst, 1)
            continue
        by_id = {b.batch: b for b in mine}
        for st in parse_status(mine, proc.stdout):
            b = by_id[st.batch]
            st = reconciled(st, b.branch, tools.merged_pr, b.repo)
            # Already reconciled, so `status_line` must not ask the forge a second time.
            line, tier = status_line(
                st, host, b.branch, lambda *_: "", one_line, b.repo
            )
            worst = max(worst, tier)
            rows.append(row(b, host, st.state, st.pr_url, line))
    return rows, worst
