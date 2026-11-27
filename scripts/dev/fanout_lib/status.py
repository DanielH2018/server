"""Read every batch on one host in one call, and stop one."""

import json
import re
import subprocess
import time
from collections.abc import Callable, Iterable, Sequence
import dataclasses
from dataclasses import dataclass
from functools import partial


from lib.exit_codes import (
    FANOUT_PLACE_BATCH_FAILED,
    FANOUT_PLACE_FAILED,
    FANOUT_PLACE_OK,
    FANOUT_PLACE_UNREAD,
)
from deploy_tools.land_lib.detach import result_fields
from dev.fanout_lib import operator_waits
from dev.fanout_lib.branch_pr import PR_URL, BranchPrs, own_pr
from dev.fanout_lib.brief import LANDS, lands
from dev.fanout_lib.land_result import VERDICT_LINE, finished, park_line
from dev.fanout_lib.manifest import Batch, Manifest
from dev.fanout_lib.transport import Tools
from dev.fanout_lib.target import SERVER
from dev.fanout_lib.unit_state import unit_props_command

STATUS_TIMEOUT_S = 30.0
# The brief's *Finishing* section names both prefixes. `.claude/hooks/fanout-stop.py` copies both
# patterns (hooks stay stdlib-only); a test holds the copies equal.
BLOCKER = re.compile(r"(?im)^(?:needs input|failed):")
# A batch on the landing host owes a VERDICT line: a PR URL alone never says it merged and deployed.
# `.claude/hooks/fanout-stop.py` copies this pattern too.
VERDICT = re.compile(r"(?m)^VERDICT:")
RESULT_TYPE = "result"
# The state of a batch whose unit ended cleanly but left no report behind it.
NO_REPORT = "no-report"
# A clean finish whose final text names a blocker line instead of a PR.
NEEDS_INPUT = "needs-input"
NO_PR = "no-pr"
NO_VERDICT = "no-verdict"
# `land_result.park_line` wrote its final line; its sub-coordinator re-lands it, so it needs
# attention and no page.
PARKED = "parked"
PARKED_LINE = re.compile(r"(?im)^parked:")
LANDER_LOGS = "$HOME/.local/state/fanout-server-lander/logs"
AWAITING_LANDER = "awaiting the server lander"
# Three lander passes (`claude_code_fanout_server_lander_interval`); past it the lander is not going
# to take the PR, and the batch reads `needs-input`.
AWAIT_LANDER_S = 30 * 60
# The line `lander_command` prints for a PR whose newest lander log exists.
LANDER_LOG = "LANDER-LOG:"
# `probe_lib/lander.py:UNIT` names the same unit.
LANDER_UNIT = "homelab-claude-fanout-server-lander.service"
RULINGS = "lander-rulings"
# Journal lines read back. A pass prints one per open candidate PR, and two passes are needed.
RULING_LINES = 200
_RULING = re.compile(r"^#(\d+) batch \S+: (.+)$")
INELIGIBLE = "ineligible, "
NEXT_STEP = {
    "lander-surface": "land it with land.sh after probe.py lander",
    "operator-step": "pass its operator step to the operator",
    "findings-remain": "read the findings in its review comment",
    "needs-input": "read the batch's final message",
    "head-moved": "read the commits pushed since the review, then land it with land.sh",
    "ci-gate": "read its PR's checks, then land it with land.sh once they pass",
    "review-incomplete": "review its diff, then land it with land.sh",
}
# The same, for a PR that outlasted `AWAIT_LANDER_S` under no ruling or one the lander clears.
OVERDUE_STEP = "run probe.py lander, then land it with land.sh"


@dataclass(frozen=True)
class BatchStatus:
    """One batch's verdict, read from its unit's properties and its result JSON.

    Attributes:
        batch: the batch id.
        state: running, done, parked, needs-input, no-pr, no-verdict, no-report, or failed.
            `done` means the final text carries a PR URL, and on the landing host a verdict
            with it.
        pr_url: the PR the final text names, or empty.
        final_text: the session's own `result` string, or empty.
        exit_code: the unit's ExecMainStatus, or None when it could not be read.
        stderr_tail: the last of the unit's stderr log.
        is_error: the session's own `is_error` flag — true means the agent failed even
            though the unit may have exited 0.
        permission_denials: how many tool calls the session was denied.
        terminal_reason: why the session ended, when it says.
        unit_result: systemd's `Result` for the unit, such as `oom-kill` when the batch hit
            its MemoryMax or `timeout` when RuntimeMaxSec= expired.
        note: a few words printed after the state, or empty.
        merged: whether GitHub holds `pr_url` as merged.
        awaiting_lander: whether the batch is `done` only until the server lander lands its PR.
        ended_at: the epoch second its report was last written, or None when unread.
        pr_unconfirmed: GitHub could not be asked for the branch's PR, so `pr_url` is the
            first url the final text names and `merged` is unknown.
    """

    batch: str
    state: str
    pr_url: str
    final_text: str
    exit_code: int | None
    stderr_tail: str
    is_error: bool = False
    permission_denials: int = 0
    terminal_reason: str = ""
    unit_result: str = ""
    note: str = ""
    merged: bool = False
    awaiting_lander: bool = False
    ended_at: float | None = None
    pr_unconfirmed: bool = False


def _one(b: Batch) -> str:
    return (
        f"{unit_props_command(b)}; "
        f"echo '--- stderr'; tail -c 2000 {b.worktree}/.fanout/stderr.log 2>/dev/null; "
        f"echo; echo '--- report'; cat {b.worktree}/.fanout/report.json 2>/dev/null; "
        f"echo; echo '--- ended'; stat -c %Y {b.worktree}/.fanout/report.json 2>/dev/null; "
        # A batch can land correctly and not echo the verdict into its final message, so read the
        # landing's own record.
        f"echo; echo '--- verdict'; "
        f"grep -h '^VERDICT:' {b.worktree}/.fanout/land*.log 2>/dev/null | tail -n 1"
    )


def status_command(batches: Sequence[Batch]) -> str:
    return "; ".join(_one(b) for b in batches)


def stop_command(unit: str) -> str:
    return f"systemctl --user stop {unit}"


_SECTION_NAMES = ("stderr", "report", "ended", "verdict")


def _section(block: str, name: str) -> str:
    # Bound the section only at a known marker, never at any line starting "--- ": the section's own
    # content can start that way.
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


def final_state(report: str) -> str:
    """`parked`, `needs-input` or "" for the final text of a `report.json` stream.

    The two states a batch's own final line names, whatever its unit or its PR says.
    """
    final = str(_result(report).get("result") or "")
    if PARKED_LINE.search(final):
        return PARKED
    return NEEDS_INPUT if BLOCKER.search(final) else ""


def parse_status(batches: Sequence[Batch], stdout: str) -> list[BatchStatus]:
    known = {b.batch for b in batches}
    blocks: dict[str, str] = {}
    order: list[str] = []
    # Split only at a genuine block header; content starting with "=== " whose name is not one of
    # ours folds back into the open block.
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
        # Anchor the boundary at the start of a line, like `_section`: a property value carrying
        # "--- " mid-line would truncate the props block.
        props = dict(
            ln.split("=", 1)
            for ln in re.split(r"(?m)^--- ", body, maxsplit=1)[0].splitlines()
            if "=" in ln
        )
        stderr_tail = _section(body, "stderr").strip()
        result = _result(_section(body, "report"))
        final = str(result.get("result") or "")
        # A failed session still exits 0 through systemd, so `Result=success` alone cannot mean
        # done.
        is_error = bool(result.get("is_error"))
        code_text = props.get("ExecMainStatus", "")
        code = int(code_text) if code_text.isdigit() else None
        ended = _section(body, "ended").strip()
        m = PR_URL.search(final)
        # Either the agent echoed the verdict or the land log holds it; the log keeps a
        # tersely-reporting batch out of `no-verdict`.
        landed = bool(
            VERDICT.search(final) or VERDICT.search(_section(body, "verdict"))
        )
        if props.get("ActiveState") in ("active", "activating"):
            state = "running"
        elif props.get("Result") == "success" and final and not is_error:
            # A non-empty final text is not a finish: a turn ending on "Next I will open the PR"
            # exits as cleanly as one that opened it.
            named = final_state(_section(body, "report"))
            if named:
                # Tested first, so a blocker line wins whatever URL follows it: the review hold ends
                # on the full PR URL, which reads as `done` on daniel-server. `m` still fills the PR
                # url below.
                state = named
            elif m and lands(b.host, b.repo) and not landed:
                # Another repo's batch stops at the PR on every host, so its PR URL is the finish;
                # elsewhere a PR URL only means `gh pr create` returned.
                state = NO_VERDICT
            elif m:
                state = "done"
            else:
                state = NO_PR
        elif (
            props
            and props.get("Result") == "success"
            and not is_error
            and not final
            and code in (0, None)
        ):
            # The unit ended cleanly and left no report: an agent that removes its worktree on exit
            # takes .fanout/report.json with it, so a finished batch looks like one that died. Its
            # own state, not `failed`; the caller reconciles it against a merged PR.
            # `props` must be non-empty (a mangled read parses to empty fields and stays `failed`),
            # and `Result` must be `success` (a `RuntimeMaxSec=` stop records `timeout`).
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
                unit_result=props.get("Result", ""),
                ended_at=float(ended) if ended.isdigit() else None,
            )
        )
    return out


def with_own_pr(
    s: BatchStatus,
    branch: str,
    branch_prs: Callable[[str, str], BranchPrs | None],
    repo: str,
    earlier_prs: Callable[[], list[str]] = list,
) -> BatchStatus:
    """`s` with the PR GitHub holds for `branch`, in place of the first URL its text names.

    `merged` is set from the same answer, when that PR has merged.

    `parse_status` reads `pr_url` as the first PR URL in the final text, so a body that links
    another PR before its own reads as that PR. A `done` or `no-verdict` batch whose
    text names a PR the branch does not have becomes `needs-input` naming both, never `done`;
    a `needs-input` or `failed` one loses the foreign url. Such a batch whose text names only
    PRs from `earlier_prs`, which merged before this launch, opened nothing and becomes `no-pr`.
    A batch GitHub could not be asked about keeps its state and its url, marked
    `pr_unconfirmed`: a failed read says nothing about the batch.
    Only a batch with a url asks the forge, and a `landed` one holds the forge's own
    merged url already.
    """
    if not s.pr_url or s.state == "landed":
        return s
    held = branch_prs(branch, repo)
    prs = None if held is None else held.urls
    earlier = earlier_prs() if prs else []
    found = own_pr(
        s.final_text, branch, prs and [u for u in prs if u not in earlier], earlier
    )
    if found.url:
        merged = held is not None and found.url in held.merged
        return dataclasses.replace(s, pr_url=found.url, merged=merged)
    if found.unanswered:
        return dataclasses.replace(s, pr_unconfirmed=True)
    if s.state in ("done", NO_VERDICT):
        if not found.problem:
            return dataclasses.replace(s, state=NO_PR, pr_url="")
        text = f"needs input: {found.problem}. {s.final_text}"
        return dataclasses.replace(s, state=NEEDS_INPUT, pr_url="", final_text=text)
    return dataclasses.replace(s, pr_url="")


def reconciled(
    s: BatchStatus,
    branch: str,
    merged_pr: Callable[[str, str], str],
    repo: str,
    earlier_prs: Callable[[], list[str]] = list,
) -> BatchStatus:
    """`s`, read as `landed` with its merged PR's url when it is a `no-report` GitHub merged.

    Only a `no-report` batch asks the forge, so every other state costs no network call. A PR
    from `earlier_prs`, which merged before this launch, belongs to an earlier launch of the
    same branch name and leaves the batch `no-report`.
    """
    if s.state != NO_REPORT:
        return s
    url = merged_pr(branch, repo)
    if not url or url in earlier_prs():
        return s
    return dataclasses.replace(s, state="landed", pr_url=url)


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
        unreconciled `no-report`, for a clean finish with no PR (`needs-input`, `no-pr`), for
        a PR with no `VERDICT:` line (`no-verdict`) and for a parked landing (`parked`), 5
        for a genuine failure.
    """
    s = reconciled(s, branch, merged_pr, repo)
    state = s.state
    line = f"{s.batch} on {host}: {state}"
    if s.note:
        line += f" ({s.note})"
    if s.pr_url:
        line += f" {s.pr_url}"
    if s.permission_denials:
        line += f" permission_denials={s.permission_denials}"
    if state in ("done", PARKED, NEEDS_INPUT, NO_PR, NO_VERDICT):
        line += f" {one_line(s.final_text)}"
    if state in ("failed", NO_REPORT):
        exit_text = "unknown" if s.exit_code is None else str(s.exit_code)
        line += f" (exit {exit_text})"
        # The page carries only the state and polkit admits a fixed set of names, so an OOM kill or
        # runtime-cap stop is named here.
        if s.unit_result and s.unit_result != "success":
            line += f" result={s.unit_result}"
        if s.terminal_reason:
            line += f" {s.terminal_reason}"
        line += f" {s.stderr_tail[-300:]}"
    if state == NO_REPORT:
        # Tier 1, not 5: nothing here says the work failed, only that neither the unit nor
        # the forge can say what became of it. A 5 would send an operator to a stderr log
        # that a finished batch has already deleted along with its worktree.
        return line + f" no merged PR for {branch}", FANOUT_PLACE_FAILED
    if state in (NEEDS_INPUT, NO_PR, PARKED):
        # Tier 1, not 5: the session ended cleanly and its final text is the thing to read. Not 0:
        # someone has to act.
        return line, FANOUT_PLACE_FAILED
    if state == NO_VERDICT:
        return line + " — land.sh printed no VERDICT", FANOUT_PLACE_FAILED
    return line, FANOUT_PLACE_BATCH_FAILED if state == "failed" else FANOUT_PLACE_OK


# Worst first: numeric `max` would rank a timed-out read (75) above a failed batch (5).
_TIER_RANK = (
    FANOUT_PLACE_BATCH_FAILED,
    FANOUT_PLACE_FAILED,
    FANOUT_PLACE_UNREAD,
    FANOUT_PLACE_OK,
)


def exit_tier(tiers: Iterable[int]) -> int:
    """The worst of `tiers` by `_TIER_RANK`, or 0 when there are none."""
    present = set(tiers)
    return next((t for t in _TIER_RANK if t in present), FANOUT_PLACE_OK)


UNREAD = "unread"


def host_statuses(
    batches: Sequence[Batch], tools: Tools
) -> list[tuple[Batch, BatchStatus | None]]:
    """Each batch with its parsed status, one read per host; None where the read timed out."""
    out: list[tuple[Batch, BatchStatus | None]] = []
    for host in sorted({b.host for b in batches}):
        mine = [b for b in batches if b.host == host]
        try:
            proc = tools.run(host, status_command(mine), STATUS_TIMEOUT_S, None)
        except subprocess.TimeoutExpired:
            out.extend((b, None) for b in mine)
            continue
        out.extend(zip(mine, parse_status(mine, proc.stdout), strict=True))
    return out


def _pr_number(url: str) -> str:
    return url.rstrip("/").rsplit("/", 1)[-1]


def lander_command(prs: Sequence[str]) -> str:
    """Print the `LAND-RESULT:` and `VERDICT:` lines of each PR's newest server-lander log.

    One block per PR, opened by `=== <pr>`: empty with no log, a `LANDER-LOG:` line alone
    while that landing has no result. A last
    block, `=== lander-rulings`, holds the lander unit's state and invocation id, then the
    journal lines the lander printed about these PRs, as JSON. No output at all means this
    user has no lander log directory.
    """
    reads = "; ".join(
        f"echo '=== {pr}'; f=$(ls {LANDER_LOGS}/land{pr}-*.log 2>/dev/null | tail -n 1); "
        f'[ -z "$f" ] || {{ echo "{LANDER_LOG} $f"; grep -h \'^LAND-RESULT:\\|^VERDICT:\' "$f"; }}'
        for pr in prs
    )
    rulings = (
        f"echo '=== {RULINGS}'; "
        f"systemctl show -p ActiveState -p InvocationID {LANDER_UNIT}; "
        f"journalctl -u {LANDER_UNIT} -o json -n {RULING_LINES} --no-pager "
        "--output-fields=MESSAGE,_SYSTEMD_INVOCATION_ID 2>/dev/null "
        f'| grep -E \'"MESSAGE":"#({"|".join(prs)}) \''
    )
    return f"if [ -d {LANDER_LOGS} ]; then {reads}; {rulings}; fi"


def lander_logs(stdout: str) -> dict[str, str]:
    """`lander_command`'s reply as block name -> its lines: each PR number, and `RULINGS`."""
    logs = {}
    for chunk in re.split(r"(?m)^=== ", stdout)[1:]:
        pr, _, lines = chunk.partition("\n")
        logs[pr.strip()] = lines
    return logs


def lander_rulings(block: str) -> dict[str, str]:
    """PR number -> why the lander's newest pass ruled that PR ineligible.

    Args:
        block: the `RULINGS` block of `lander_command`'s reply.

    Returns:
        The reason from the newest line about each PR, for a PR that line rules ineligible.
        A PR the newest pass did not name has left the lander's candidates and has no entry,
        unless that pass still runs and may not have reached it.
    """
    props, newest = {}, {}
    for line in block.splitlines():
        if not line.startswith("{"):
            name, _, value = line.partition("=")
            props[name] = value
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        found = _RULING.match(str(entry.get("MESSAGE") or ""))
        if found:
            newest[found.group(1)] = (
                entry.get("_SYSTEMD_INVOCATION_ID"),
                found.group(2),
            )
    running = props.get("ActiveState") in ("active", "activating")
    return {
        pr: ruling.removeprefix(INELIGIBLE)
        for pr, (invocation, ruling) in newest.items()
        if ruling.startswith(INELIGIBLE)
        and (running or invocation == props.get("InvocationID"))
    }


def _awaiting(s: BatchStatus, log: str, ruling: str, now: float | None) -> BatchStatus:
    """A batch whose PR the lander has no landing result for: its state without one."""
    if s.merged:
        return dataclasses.replace(s, state="landed")
    reason = operator_waits.reason_of(ruling)
    overdue = (
        not log.strip()
        and not s.pr_unconfirmed
        and now is not None
        and s.ended_at is not None
        and now - s.ended_at > AWAIT_LANDER_S
    )
    if not reason and not overdue:
        note = f"{AWAITING_LANDER}: {ruling}" if ruling else AWAITING_LANDER
        return dataclasses.replace(s, note=note, awaiting_lander=True)
    pr = _pr_number(s.pr_url)
    # The next step comes first: a surface ruling lists every file, and the line is cut.
    if reason:
        text = f"the server lander will not land #{pr}; {NEXT_STEP[reason]}"
    else:
        minutes = AWAIT_LANDER_S // 60
        text = f"the server lander has not taken #{pr} in {minutes} minutes; {OVERDUE_STEP}"
    text = f"needs input: {text}. Its ruling: {ruling or 'none'}\n{s.pr_url}"
    return dataclasses.replace(s, state=NEEDS_INPUT, final_text=text)


def with_lander_verdict(
    s: BatchStatus, log: str, ruling: str = "", now: float | None = None
) -> BatchStatus:
    """A `done` batch the server lander lands, read with that landing's result.

    Args:
        s: the status of a batch that stopped at its PR.
        log: the lines `lander_command` printed for that PR: empty when the lander has
            started no landing of it, a `LANDER-LOG:` line alone while that landing runs.
        ruling: why the lander's newest pass ruled the PR ineligible, or "".
        now: the epoch second, or None to leave an old wait unjudged.

    Returns:
        `landed` for a finished landing, `parked` for one `land_result.park_line` parks, and
        `needs-input` naming the verdict for any other that ended. With no result, a PR
        GitHub holds as merged is `landed`: someone landed it by hand. A ruling the lander
        does not clear itself (`operator_waits.reason_of`) is `needs-input` naming the next
        step. So is a batch that ended more than `AWAIT_LANDER_S` before `now` with no
        landing started, unless it is `pr_unconfirmed`: GitHub did not say whether its PR
        merged. Any other stays `done`, noted as awaiting the lander with the
        ruling, and `awaiting_lander` is set.
    """
    result = result_fields(log)
    if result is None:
        return _awaiting(s, log, ruling, now)
    verdict = VERDICT_LINE.search(log)
    line = verdict.group(0) if verdict else ""
    parked = park_line(result, line)
    if parked:
        text = f"{parked}\n{line}\n{s.pr_url}"
        return dataclasses.replace(s, state=PARKED, final_text=text)
    if finished(result):
        return dataclasses.replace(s, state="landed")
    text = f"needs input: the server lander's landing ended short.\n{line}\n{s.pr_url}"
    return dataclasses.replace(s, state=NEEDS_INPUT, final_text=text)


def _lander_lands(b: Batch, s: BatchStatus) -> bool:
    """Whether `s` is a batch that stopped at its PR for the server lander to land.

    This repo's batch on a host that does not land, `done`, with no verdict of its own: a
    batch that landed its own draft ends on its `VERDICT:` line or parks.
    """
    return (
        b.repo == SERVER
        and not lands(b.host, b.repo)
        and s.state == "done"
        and bool(s.pr_url)
        and not VERDICT.search(s.final_text)
    )


def with_lander_verdicts(
    pairs: Sequence[tuple[Batch, BatchStatus | None]],
    tools: Tools,
    now: float | None = None,
) -> list[tuple[Batch, BatchStatus | None]]:
    """`pairs` with each server-lander batch read through `with_lander_verdict`.

    One read on the landing host covers every such batch. A read that fails, times out or
    finds no log directory leaves a batch as it was, unless its PR has merged: that one
    reads `landed`.

    Args:
        pairs: each batch with its status, None where its host was not read.
        tools: the process boundary.
        now: the epoch second, passed only by a caller that asked GitHub whether each PR
            merged. Without that answer an old wait cannot be told from a hand landing.
    """
    mine = [s.pr_url for b, s in pairs if s is not None and _lander_lands(b, s)]
    if not mine:
        return list(pairs)
    try:
        command = lander_command([_pr_number(url) for url in mine])
        logs = lander_logs(tools.run(LANDS, command, STATUS_TIMEOUT_S, None).stdout)
    except subprocess.TimeoutExpired:
        logs = {}
    rulings = lander_rulings(logs.get(RULINGS, ""))
    out = []
    for b, s in pairs:
        pr = _pr_number(s.pr_url) if s is not None and _lander_lands(b, s) else ""
        if s is not None and pr in logs:
            s = with_lander_verdict(s, logs[pr], rulings.get(pr, ""), now)
        elif s is not None and pr and s.merged:
            s = dataclasses.replace(s, state="landed")
        out.append((b, s))
    return out


def landing_holds(
    batches: Sequence[Batch], tools: Tools
) -> dict[tuple[str, str], tuple[str, str]]:
    """`(host, batch id)` -> (state, final text) of each batch whose landing needs a hand.

    That is a `parked` batch, and a batch that stopped at its PR and reads `needs-input`
    from the server lander's log or ruling. It asks the hosts and never the forge, so
    `place runs` can call it over every run. For the same reason it leaves a batch that
    awaited the lander past `AWAIT_LANDER_S` out: only `collect` knows whether its PR merged.
    """
    own = host_statuses(batches, tools)
    held = {}
    for (b, before), (_, s) in zip(own, with_lander_verdicts(own, tools), strict=True):
        if s is None or before is None:
            continue
        if s.state == PARKED or (s.state == NEEDS_INPUT and before.state == "done"):
            held[(b.host, b.batch)] = (s.state, s.final_text)
    return held


def collect(
    run: Manifest,
    tools: Tools,
    one_line: Callable[[str], str],
    clock: Callable[[], float] = time.time,
) -> tuple[list[dict], int]:
    """One row per batch in `run`, and the worst exit tier among them; `status`'s whole read.

    The tier is `exit_tier` over every batch's: 75 for a host whose read timed out, unless a
    batch elsewhere reports 1 or 5.

    Each row carries `batch`, `host`, `branch`, `state`, `pr_url`, `line`, the text line
    `status` prints, `awaiting_lander`, true for a `done` batch whose PR the server lander
    has yet to land, and `pr_unconfirmed`, true when GitHub could not be asked for the
    batch's PR. `status --json` prints the rows themselves, so `fanout.py probe` reads
    those fields instead of a regex over the line. Two states come from here rather
    than from a host: `cleaned` for a batch `clean` removed, and `unread` for a host whose
    read timed out, which still counts as running.

    A cleaned batch is reported from the manifest and never read remotely. `clean` resets the
    failed unit and takes .fanout/report.json with the worktree, so `status_command` finds no
    active state, no result and no report, and `parse_status` reads exactly that as `failed`,
    which would exit 5 for a batch that landed its PR and was tidied up.
    """
    rows, tiers = [], []

    def row(b: Batch, state: str, line: str, st: BatchStatus | None = None) -> dict:
        return {
            "batch": b.batch,
            "host": b.host,
            "branch": b.branch,
            "state": state,
            "pr_url": st.pr_url if st else "",
            "line": line,
            "awaiting_lander": bool(st and st.awaiting_lander),
            "pr_unconfirmed": bool(st and st.pr_unconfirmed),
        }

    for b in run.batches:
        if b.removed_at:
            line = f"{b.batch} on {b.host}: cleaned ({b.removed_at})"
            rows.append(row(b, "cleaned", line))
    live = [b for b in run.batches if not b.removed_at]
    read: list[tuple[Batch, BatchStatus | None]] = []
    for b, st in host_statuses(live, tools):
        if st is not None:
            earlier = partial(tools.earlier_prs, b.branch, b.repo, b.launched_at)
            st = reconciled(st, b.branch, tools.merged_pr, b.repo, earlier)
            st = with_own_pr(st, b.branch, tools.branch_prs, b.repo, earlier)
        read.append((b, st))
    # After `with_own_pr`, so the lander's log is read for the batch's own PR.
    for b, st in with_lander_verdicts(read, tools, clock()):
        if st is None:
            line = f"{b.batch} on {b.host}: status read timed out"
            rows.append(row(b, UNREAD, line))
            tiers.append(FANOUT_PLACE_UNREAD)
            continue
        # Already reconciled, so `status_line` must not ask the forge a second time.
        line, tier = status_line(st, b.host, b.branch, lambda *_: "", one_line, b.repo)
        tiers.append(tier)
        rows.append(row(b, st.state, line, st))
    return rows, exit_tier(tiers)
