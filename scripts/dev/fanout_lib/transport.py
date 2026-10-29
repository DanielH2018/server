"""The transport seam: every process boundary the dispatcher crosses, as one injectable object.

The land_lib/tools.py shape: a test replaces one field and never patches a module attribute
(the monkeypatch ratchet caps new first-party targets at zero).
"""

import json
import os
import socket
import subprocess
import dataclasses
from collections.abc import Callable, Sequence
from dataclasses import dataclass

# Reach the sibling package: a directly-invoked script gets only its own directory on
# sys.path, and pyproject's `pythonpath` is a pytest setting.
import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))
# `scripts/lib` sits two levels up from this package, one above the `scripts/dev` insert
# above; `lib.git` / `lib.gh` are the one way this tree runs git and gh.
_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))

from fanout_lib import signing
from fanout_lib.branch_pr import branch_pr_argv, parse_branch_prs
from fanout_lib.brief import WORKED_BY, Comment, Issue
from fanout_lib.placement import READ_COMMAND, HostReading, parse_reading
from fanout_lib.manifest import Batch
from fanout_lib.target import SERVER, SERVER_CHECKOUT
from fanout_lib.unit_state import appended_command, split_reply
from findings_lib.issue_model import (
    _CLAIM_RE,
    _RELEASE_RE,
    _REOBSERVED,
    is_operator_comment,
    ordered_comments,
)
from lib.gh import gh

HOSTS = ("daniel-box", "daniel-server")
REPO = SERVER_CHECKOUT
# This checkout's own `findings.py`, which `launch` runs to claim a batch in another repo's
# register. Resolved from this file rather than from REPO: the orchestrator runs the dispatcher
# from its own worktree, and that worktree's copy is the one whose `--repo` it just used.
FINDINGS = str(_Path(__file__).resolve().parents[1] / "findings.py")
SSH_OPTS = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=8"]
# Every ssh call to one host rides one connection (#5667's follow-up). `ufw limit ssh`
# rejects a sixth NEW connection within 30s and counts nothing that reuses one, so with
# a master open a run's reads and launches cost one connection however many batches it
# places. Measured to daniel-server on 2026-10-18: eight calls in a row through one
# socket all answered, the first in 0.22s and each later one in 0.01s, and no call
# waited on the backgrounded master's pipes. ServerAlive bounds a master whose network
# went away, which every call riding it would otherwise wait on. ssh falls back to a
# fresh connection when the socket is dead, as after a reboot.
MUX_PERSIST_S = 60
CONTROL_DIR_NAME = "fanout-ssh"
READ_TIMEOUT_S = 20.0
GH_TIMEOUT_S = 30.0
# The one read every host gets: READ_COMMAND's four memory lines and live-agent count, plus
# the signing-key line the launch gate needs, folded into the same connection because the ssh
# budget (2 reads plus MAX_BATCHES_PER_REMOTE_HOST launches = the 5 `ufw limit ssh` allows
# per 30s) has none spare. The key goes last; parse_reading documents the full line order.
HOST_READ_COMMAND = f"{READ_COMMAND}; {signing.signing_key_read_command(REPO)}"
# `labels` is what the launch gate reads; dropping it from this list would refuse every
# issue rather than fail loudly, which is why issue_from_view indexes it. `comments` is
# indexed the same way: a fetch that stopped asking for it would silently drop every
# operator decision posted as a comment, which is the defect #3498 fixed.
ISSUE_FIELDS = "number,title,body,labels,comments"

# The openings of the records `findings.py` and the fan-out itself post as the operator's
# account (findings_lib/plans.py and brief.WORKED_BY). They say who held or deferred an
# issue, which the brief already states or the agent has no use for. Claim and release
# records are matched by their trailer instead, through the claim protocol's own regexes.
_BOOKKEEPING_PREFIXES = (
    WORKED_BY,
    _REOBSERVED,
    "Deferred until ",
    "Deferral cleared.",
    "Marked manual:",
    "Manual cleared:",
)


def this_host() -> str:
    return socket.gethostname()


def mux_opts(control: str) -> list[str]:
    """The ssh options that share one connection per host through sockets in `control`."""
    return [
        "-o", "ControlMaster=auto", "-o", f"ControlPath={control}/%C",
        "-o", f"ControlPersist={MUX_PERSIST_S}",
        "-o", "ServerAliveInterval=10", "-o", "ServerAliveCountMax=3",
    ]  # fmt: skip


def control_dir(environ: dict[str, str], uid: int) -> str | None:
    """The private directory for this user's ssh control sockets, creating it, or None.

    It sits in the user's runtime directory, which systemd keeps mode 0700 and empties at
    logout. A socket there is a live shell on the remote host, so a directory this user does
    not own, or one other users can enter, is refused rather than used. None, for a refused
    or missing directory, means ssh opens a connection per call, as before.
    """
    runtime = environ.get("XDG_RUNTIME_DIR") or f"/run/user/{uid}"
    if not os.path.isdir(runtime):
        return None
    path = os.path.join(runtime, CONTROL_DIR_NAME)
    try:
        os.makedirs(path, mode=0o700, exist_ok=True)
        info = os.lstat(path)
    except OSError:
        return None
    if info.st_uid != uid or info.st_mode & 0o077 or not os.path.isdir(path):
        return None
    return path


def ssh_multiplexed() -> bool:
    """Whether this user's ssh calls share one connection per host."""
    return control_dir(dict(os.environ), os.getuid()) is not None


def _argv(
    host: str, command: str, local_host: str, control: str | None = None
) -> list[str]:
    """The argv to run `command` on `host`: local `bash -c` when `host` is `local_host`, else ssh.

    Args:
        host: the target host.
        command: the shell command to run.
        local_host: the name `host` is compared against to decide local vs. remote.
        control: the control-socket directory, or None for a connection per call.

    Returns:
        `["bash", "-c", command]` when `host == local_host`, else
        `["ssh", *SSH_OPTS, *mux_opts(control), host, command]`, without the mux
        options when `control` is None.
    """
    if host == local_host:
        return ["bash", "-c", command]
    mux = mux_opts(control) if control else []
    return ["ssh", *SSH_OPTS, *mux, host, command]


def local_env(environ: dict[str, str], uid: int) -> dict[str, str]:
    """The environment a LOCAL leg runs under: `environ`, plus the user bus when it is unset.

    `systemd-run --user` and `systemctl --user` find the user manager through
    `DBUS_SESSION_BUS_ADDRESS`, or through `XDG_RUNTIME_DIR` (`$XDG_RUNTIME_DIR/bus`). An
    interactive Claude session's shell carries neither, so the local leg of a launch would
    fail with "Failed to connect to bus: No medium found" after its worktree was already
    created and locked, and `status` would read the unit it could not reach as `failed (exit
    unknown)` while `systemctl` under a pinned env shows it active. The ssh leg gets a login
    environment and never hit this, which is why every remote batch in the same call launched
    cleanly.

    Both are derived from the uid rather than hard-coded: `/run/user/<uid>` is where
    systemd-logind puts the runtime directory, and the bus socket has a fixed name inside
    it. A value already present is kept — a shell that set its own is not second-guessed.

    Args:
        environ: the caller's environment, typically `os.environ`.
        uid: the real uid the user manager belongs to.

    Returns:
        A new dict; `environ` is not modified.
    """
    env = dict(environ)
    runtime_dir = env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{uid}")
    env.setdefault("DBUS_SESSION_BUS_ADDRESS", f"unix:path={runtime_dir}/bus")
    return env


def run_command(
    host: str,
    command: str,
    timeout: float,
    stdin: str | None = None,
    local_host: str | None = None,
) -> subprocess.CompletedProcess:
    """Run `command` on `host` — locally when it is this host, else over ssh.

    The local leg runs under `local_env`, so its `systemd-run --user` / `systemctl --user`
    reach the user bus from a shell that never exported it. The ssh leg's environment is
    the remote login's own and is left alone.

    Args:
        host: the target host.
        command: the shell command to run.
        timeout: seconds to wait before raising `subprocess.TimeoutExpired`.
        stdin: text piped to the command's stdin, or None.
        local_host: this host's own name; defaults to `socket.gethostname()`.

    Returns:
        The finished `subprocess.CompletedProcess` (never raises on a non-zero exit).
    """
    local = local_host or this_host()
    control = None if host == local else control_dir(dict(os.environ), os.getuid())
    argv = _argv(host, command, local, control)
    env = local_env(dict(os.environ), os.getuid()) if argv[0] == "bash" else None
    return subprocess.run(
        argv,
        input=stdin,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        env=env,
    )


def _is_bookkeeping(body: str) -> bool:
    return (
        body.startswith(_BOOKKEEPING_PREFIXES)
        or bool(_CLAIM_RE.search(body))
        or bool(_RELEASE_RE.search(body))
    )


def operator_comments(issue: dict) -> tuple[Comment, ...]:
    """The comments of ``issue`` the brief carries, oldest first.

    Only the operator's comments count, judged by the claim protocol's own author check:
    the repo is public, and a drive-by account's comment is not a decision. That check
    drops almost nothing in practice, because `gh` posts every claim, release and
    "Worked by" record as the operator. The bookkeeping filter does the real narrowing.

    Raises:
        KeyError: ``issue`` carries no `comments` key — see ISSUE_FIELDS.
    """
    if "comments" not in issue:
        raise KeyError("comments")
    return tuple(
        Comment(str(c.get("createdAt") or "unknown time"), c.get("body") or "")
        for c in ordered_comments(issue)
        if is_operator_comment(c) and not _is_bookkeeping(c.get("body") or "")
    )


def issue_from_view(data: dict) -> Issue:
    """Map one `gh issue view --json ISSUE_FIELDS` object onto an `Issue`.

    Args:
        data: the decoded JSON object for one issue.

    Returns:
        The issue, carrying its label names.

    Raises:
        KeyError: a field ISSUE_FIELDS asks for is missing. `labels` is read with `[]`
            rather than `.get()` on purpose: gh returns an empty list for an unlabelled
            issue, so an absent key means the fetch stopped asking for it — and the launch
            gate would then refuse every issue instead of the unlabelled ones. `comments`
            is strict for the same reason.
    """
    return Issue(
        data["number"],
        data["title"],
        data["body"],
        tuple(str(label["name"]) for label in data["labels"]),
        operator_comments(data),
    )


def gh_issue(number: int, repo: str = SERVER) -> Issue:
    """Fetch one GitHub issue by number via `gh issue view`.

    `--repo` is always passed. Issue numbers collide across repos, so a fetch that fell back
    to the cwd's repo would put another repo's issue body into the brief.

    Raises:
        subprocess.CalledProcessError: `gh` exited non-zero.
        subprocess.TimeoutExpired: `gh` did not answer within GH_TIMEOUT_S. Unbounded, one
            hung fetch mid-loop would strand every batch already launched.
    """
    out = gh(
        "issue",
        "view",
        str(number),
        "--repo",
        repo,
        "--json",
        ISSUE_FIELDS,
        timeout=GH_TIMEOUT_S,
    ).stdout
    return issue_from_view(json.loads(out))


def merged_pr_url(branch: str, repo: str = SERVER) -> str:
    """The URL of a merged PR opened from `branch` in `repo`, or "" when GitHub knows of none.

    The forge is the only oracle that can settle a batch whose worktree is gone. The tree,
    its report and its unit all go with it, but the PR it opened does not. This runs
    locally rather than over ssh: GitHub answers the same from any checkout, and the ssh
    budget on the batch's host is already spent on the status read.

    Returns "" on any failure — a `gh` that exits non-zero, times out, or prints something
    unparseable. A reconciliation that could not be taken must read the same as "no merged
    PR", so the caller keeps the batch unresolved rather than calling it landed.
    """
    try:
        out = gh(
            "pr",
            "list",
            "--state",
            "merged",
            "--repo",
            repo,
            "--head",
            branch,
            "--json",
            "url",
            "--limit",
            "1",
            check=False,
            timeout=GH_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        return ""
    if out.returncode != 0:
        return ""
    try:
        data = json.loads(out.stdout)
    except ValueError:
        return ""
    return str(data[0]["url"]) if data else ""


def branch_pr_urls(branch: str, repo: str = SERVER) -> list[str] | None:
    """Every PR url GitHub holds for `branch` in `repo`, newest first; None when unreadable.

    An empty list means GitHub answered and knows of none. None means it could not be asked
    (a `gh` that failed, timed out or printed something unparseable), which a caller must not
    read as "no PR": `branch_pr.own_pr` words the two differently.
    """
    try:
        out = gh(*branch_pr_argv(branch, repo)[1:], check=False, timeout=GH_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        return None
    return parse_branch_prs(out.stdout) if out.returncode == 0 else None


def comment_issue(number: int, body: str, repo: str = SERVER) -> bool:
    """Post `body` as a comment on issue `number` in `repo`; False when `gh` failed."""
    try:
        out = gh(
            "issue", "comment", str(number), "--repo", repo, "--body", body,
            check=False, timeout=GH_TIMEOUT_S,
        )  # fmt: skip
    except subprocess.TimeoutExpired:
        return False
    return out.returncode == 0


def run_findings(argv: list[str]) -> subprocess.CompletedProcess:
    """Run this checkout's `findings.py` with `argv`, never raising on a non-zero exit."""
    return subprocess.run(
        ["uv", "run", "python", FINDINGS, *argv],
        capture_output=True,
        text=True,
        timeout=GH_TIMEOUT_S * 4,
        check=False,
        cwd=_Path(FINDINGS).parents[2],
    )


def default_ref(checkout: str) -> str | None:
    """`checkout`'s remote default branch, such as `origin/main`, or None when it has none."""
    from lib.worktrees import default_ref as read

    return read(checkout)


def head_branch() -> str:
    """The branch checked out in the process's cwd, or `HEAD` when it is detached.

    `launch` and `claim` take the orchestrator's branch from here rather than from a flag:
    the orchestrator runs the dispatcher from its own worktree, and pasting the name by hand
    was the step the issue-fanout skill spent a page on (#3695).
    """
    from lib.git import git_stdout

    return git_stdout("rev-parse", "--abbrev-ref", "HEAD", timeout=10.0)


@dataclass(frozen=True)
class Tools:
    """Every process boundary the dispatcher crosses.

    Attributes:
        run: run a command on a host.
        gh_issue: fetch one issue by number from a repo.
        signing_keys: the signing keys GitHub verifies for the account.
        merged_pr: the URL of a merged PR for a branch in a repo, or "".
        branch_prs: every PR url for a branch in a repo, newest first, or None when unreadable.
        findings: run `findings.py` with an argv, for the claim `launch` takes itself.
        default_ref: read a checkout's remote default branch, for `target.resolve`.
        head_branch: read the orchestrator's own branch, which holds this repo's claims.
        comment: post a comment on an issue in a repo, for the `Worked by` record.
        multiplexed: whether ssh calls share one connection per host, which lifts the
            per-run ssh budget `launch_gates.over_ssh_budget` enforces.
    """

    run: Callable[..., subprocess.CompletedProcess] = run_command
    gh_issue: Callable[[int, str], Issue] = gh_issue
    signing_keys: Callable[[], frozenset[str]] = signing.registered_signing_keys
    merged_pr: Callable[[str, str], str] = merged_pr_url
    branch_prs: Callable[[str, str], list[str] | None] = branch_pr_urls
    findings: Callable[[list[str]], subprocess.CompletedProcess] = run_findings
    default_ref: Callable[[str], str | None] = default_ref
    head_branch: Callable[[], str] = head_branch
    comment: Callable[[int, str, str], bool] = comment_issue
    multiplexed: Callable[[], bool] = ssh_multiplexed


def read_host(
    tools: Tools, host: str, standing: Sequence[Batch] = ()
) -> HostReading | str:
    """Take the headroom reading for `host`, or return why it could not be taken.

    Never guesses: an unreachable host, a timeout, or an unparseable reply all come back
    as a one-line string rather than a fabricated `HostReading`.

    Args:
        tools: the process boundary.
        host: the host to read.
        standing: the host's uncleaned batches. Their unit states ride this same connection,
            behind `unit_state.MARKER`, because the ssh budget has no connection to spare for
            a read of their own (#5506). They come back on `HostReading.unit_states`.
    """
    try:
        proc = tools.run(
            host, HOST_READ_COMMAND + appended_command(standing), READ_TIMEOUT_S, None
        )
    except subprocess.TimeoutExpired:
        return "%s: headroom read timed out" % host
    # The signing-key read runs last and always exits 0, so a non-zero status is the
    # transport's own: ssh's 255 for a host this user cannot reach. A missing or unreadable
    # key prints an empty line, which the parse refuses rather than this check.
    if proc.returncode not in (0, 1):
        return "%s: headroom read failed (%d): %s" % (
            host,
            proc.returncode,
            " ".join(proc.stderr.split()),
        )
    reply, units = split_reply(proc.stdout)
    try:
        reading = parse_reading(host, reply)
    except ValueError as exc:
        return str(exc)
    return dataclasses.replace(reading, unit_states=units)


def error_text(exc: BaseException) -> str:
    """The readable text of a subprocess failure on one line, from either exception shape."""
    # CalledProcessError.stderr is text under `text=True`; TimeoutExpired.stderr is bytes
    # or None. Printing either straight would raise inside the handler that exists to make
    # the failure clean.
    err = getattr(exc, "stderr", None)
    if isinstance(err, bytes):
        err = err.decode("utf-8", "replace")
    return " ".join((err or str(exc)).split())


def registered_keys(tools: Tools) -> frozenset[str] | str:
    """The account's registered signing keys, or a one-line reason they could not be read."""
    try:
        return tools.signing_keys()
    except (
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
        ValueError,
    ) as exc:
        return (
            "could not read the GitHub account's registered signing keys "
            f"({error_text(exc)})"
        )


def verified_hosts(
    readings: list[HostReading], registered: frozenset[str]
) -> tuple[list[HostReading], list[str]]:
    """Split readings into the hosts GitHub verifies and the reasons the rest were dropped.

    A dropped host is not a failure to retry: its commits would read `verified=false
    reason=unknown_key`, and the PR the agent opens there cannot merge past the
    verified-signatures rule until someone re-signs the branch by hand.

    Args:
        readings: the candidate hosts' readings.
        registered: the account's registered signing keys.

    Returns:
        The readings to place on, and one reason per host dropped — never the key itself.
    """
    keep: list[HostReading] = []
    reasons: list[str] = []
    for r in readings:
        reason = signing.unverified_reason(r.host, r.signing_key, registered)
        if reason is None:
            keep.append(r)
        else:
            reasons.append(reason)
    return keep, reasons
