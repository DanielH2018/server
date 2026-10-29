#!/usr/bin/env python3
# gen-hooks: register
#   event: SessionStart
#   timeout: 15
#   order: 10
"""SessionStart health banner: surfaces what's already broken before work starts.

Usage: Claude Code runs this hook on the SessionStart event. It takes no stdin payload. It
prints a health banner to stdout, or nothing when everything is green, and it always exits 0. `-h`
or `--help` prints this text and exits 0.

When a Claude Code session opens in this repo, this prints anything already broken so
work doesn't start blind:
  * Prometheus scrape targets that are down (fleet-wide, via scripts/diagnostics/probe.py)
  * services the release-staleness cron last found running manifests behind origin/master
  * this branch sitting behind origin/master's last-fetched ref (local-only, no `git fetch`)
  * a dirty primary checkout, or a GitOps deployer parked behind origin — the two states that
    stop every deploy in the fleet and that a worktree session cannot look at for itself,
    because the isolation guard refuses a git command targeting the shared checkout

It also appends one `session_start` row to the instructions log for the auto-memory
`MEMORY.md` and one for the active output style. Claude Code fires no InstructionsLoaded
event for either, so without these rows `fact_status.py report` under-counts each session's
at-start bytes (#4656).

Design contract (mirrors the other hooks here):
  - SILENT when all-green: prints nothing, so it adds zero context noise on a
    healthy day. Set SESSION_HEALTH_VERBOSE=1 to force an all-clear line (demo/test).
  - READ-ONLY and NEVER BLOCKS: every external call is timeout-bounded and wrapped;
    any failure degrades to a quiet skip. Always exits 0.
  - The Prometheus check
    goes through `uv run probe.py` (one subprocess, bounded) — SessionStart fires
    once per session, so the small cost is paid rarely; it's skipped on any error so
    a down monitoring stack can never stall session start. A down target whose
    Deployment is deliberately scaled to 0 replicas (an on-demand game server) is
    filtered out rather than reported — otherwise this would trade a false all-clear
    for the same two names on every session open forever.

Stdout is injected as session context by Claude Code (same mechanism the remember plugin uses).
"""

# DECIDED: return [] on any error, so a broken health read never blocks a session start;
# SESSION_HEALTH_VERBOSE=1 forces output.
# DECIDED: the gen-hooks block above carries no `matcher`, so the hook also fires on `compact`.
# A compaction is not a no-op here: `main` rows `MEMORY.md` under `compact` before it returns,
# because Claude Code reloads the file with no InstructionsLoaded event (#4668). A
# `startup|resume|clear` matcher would drop that row (#6153). A second, `compact`-only entry
# pointing at a row-only script saves nothing: `run-hook.sh` starts the same `uv run`
# interpreter for every registration, and that start is the whole cost of a compaction here.

import functools
import os
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Three sections of the banner live in `.claude/hooks/hooklib/` (its own docstrings cover
# `hooklib`-not-`lib`): the worktree, scrape-target and stale-release lines. Wrapped like
# `lib.deployer_park` below, in its own try so a failure names the package that broke rather
# than deployer_park's unrelated line. `SyntaxError` is caught alongside `ImportError` because
# these modules use PEP 758 syntax and `run-hook.sh` sends this hook's stderr to /dev/null, exits 0.
# An uncaught SyntaxError at import would take the WHOLE banner out silently: nothing at
# module scope may be able to stop the banner.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    from _hook_common import primary_checkout, read_payload
except ImportError, SyntaxError:
    # Without the payload the banner cannot tell a compaction from an open, so it prints on
    # both. One repeat banner is the cost; a hook that printed none would hide this failure.
    def read_payload():
        return None

    # Without the primary checkout the dirty-primary line has no tree to read, so it is
    # skipped; the park lines that follow it do not need one.
    def primary_checkout():
        return None


try:
    from hooklib import service_lines
    from hooklib.sections import run_sections
    from hooklib.worktree_lines import _stale_worktree_lines, remote_fanout_lines

    HOOKLIB_IMPORT_ERROR = ""
except (ImportError, SyntaxError) as exc:
    HOOKLIB_IMPORT_ERROR = f"{type(exc).__name__}: {exc}"
    service_lines = None

    def remote_fanout_lines(*_args, **_kwargs):
        return [f"  ⚠ hooklib is broken: {HOOKLIB_IMPORT_ERROR}"]

    def _stale_worktree_lines(*_args, **_kwargs):
        return []

    # One after another, with no deadline: the banner is what this file must keep, and a
    # serial run that the harness may kill still prints whatever finished first.
    def run_sections(probes, deadline):
        out = []
        for _name, probe in probes:
            try:
                out.append(probe())
            except Exception:
                out.append([])
        return out


# The park decision lives in `scripts/lib/deployer_park.py`, because `deploy.sh` exit 4 asks
# the same question of the same marker and a second derivation would drift; the
# marker parsers and clear commands come from `gitops_markers`, the deployer's own module,
# which `deployer_park` imports from the gitops_deploy role's files/. `GITOPS_STATE_DIR` is mode 0750 owned by `ubuntu`: any other host reads silence.
#
# Wrapped, because NOTHING at module scope may be able to stop the banner. This
# file is run by `run-hook.sh`, which sends its stderr to /dev/null and exits 0, so an
# ImportError here would take out the scrape-target, live-session and stale-worktree
# sections as well — silently, and for a reason no session could see. The failure is reported
# in `parked_deployer_problems` instead, the same way its deferred `lib.git` import already is.
sys.path.insert(0, os.path.join(REPO, "scripts"))
try:
    from lib.deployer_park import (
        BEHIND_PARK_SECONDS,
        CONTENTION_PARK_SECONDS,
        GITOPS_STATE_DIR,
        park_age,
        read_behind_marker,
        read_contention_marker,
        contention_lines,
        k8s_deferred_lines,
        k8s_unapplied_lines,
        manual_plane_lines,
        read_k8s_deferred_marker,
        read_k8s_unapplied_marker,
        read_manual_plane_marker,
    )

    DEPLOYER_PARK_IMPORT_ERROR = ""
except ImportError as exc:
    DEPLOYER_PARK_IMPORT_ERROR = str(exc)

    # Inert placeholders, never read: `parked_deployer_problems` returns its `⚠` line before it
    # asks the park question. They keep the names defined and singly-typed for the type checker.
    # NOT a fallback copy of the real values — a second BEHIND_PARK_SECONDS here would be a
    # second derivation — and the raising stubs say so if one is ever called.
    BEHIND_PARK_SECONDS = 0
    CONTENTION_PARK_SECONDS = 0
    GITOPS_STATE_DIR = ""

    def _park_unavailable(*_args, **_kwargs):
        raise ImportError(DEPLOYER_PARK_IMPORT_ERROR)

    park_age = _park_unavailable
    read_behind_marker = _park_unavailable
    manual_plane_lines = _park_unavailable
    read_manual_plane_marker = _park_unavailable
    contention_lines = _park_unavailable
    read_contention_marker = _park_unavailable
    k8s_deferred_lines = _park_unavailable
    read_k8s_deferred_marker = _park_unavailable
    k8s_unapplied_lines = _park_unavailable
    read_k8s_unapplied_marker = _park_unavailable

__all__ = ["BEHIND_PARK_SECONDS", "GITOPS_STATE_DIR"]

# How many dirty paths the banner names before it summarises the rest. Enough to recognise whose
# work it is; short enough to stay one line.
PRIMARY_DIRTY_LIMIT = 8

# How this hook asks the SHARED primary checkout whether it is dirty. `--no-optional-locks` is
# load-bearing, not a nicety: a plain `git status` refreshes and WRITES `.git/index` in the tree
# it reads. Without the flag a SessionStart hook running in a worktree would write the shared
# checkout's index — the one thing the isolation guard exists to prevent — and could collide on
# `index.lock` with the deployer's own `merge --ff-only`, which holds
# the git-tree lock and knows nothing about this hook. The flag is global to git, so
# it comes before the subcommand. A constant rather than an inline argv so a test can assert the
# shape without patching `lib.git.git`.
PRIMARY_STATUS_ARGV = ("--no-optional-locks", "status", "--porcelain")


def dirty_primary_lines(porcelain, path):
    """One banner line naming what makes the primary checkout dirty, or [].

    `gitops_deploy` skips a tick outright while `git status --porcelain` on the primary checkout
    is non-empty, so ONE stray file there stops every deploy in the fleet. The session that hits
    it sees `deploy.sh` exit 4 — "this tree is N commit(s) behind origin/master" — which names
    its own worktree and points at `git rebase`, the wrong repair. Worse, no session can look:
    the isolation guard refuses a git command targeting the shared checkout, correctly. This
    banner is the way the cause reaches a worktree session at all.

    Names the paths with their porcelain status codes, the way the deployer's own journal line
    does. `??` is the code worth seeing: `git status --porcelain` counts untracked files, so the
    tree can be dirty with nothing modified.

    The parse is a deliberate COPY of `dirty_summary` in
    `ansible/roles/setup/gitops_deploy/files/deployer_lib/deploy_git.py`, not an import of it — importing
    would drag the whole `deploy_git` module into a hook that must never fail to load. The cost
    is that the two can drift apart silently if the deployer ever changes what counts as dirty,
    so change them together.
    """
    entries = []
    for line in porcelain.splitlines():
        if not line.strip():
            continue
        code, entry = line[:2].strip() or "??", line[3:].strip()
        entries.append(f"{code} {entry}" if entry else code)
    if not entries:
        return []
    shown = ", ".join(entries[:PRIMARY_DIRTY_LIMIT])
    if len(entries) > PRIMARY_DIRTY_LIMIT:
        shown += f", +{len(entries) - PRIMARY_DIRTY_LIMIT} more"
    return [
        f"  ✗ primary checkout {path} is dirty — the GitOps tick skips every run while it is, "
        f"so nothing deploys and land.sh fails with exit 4 naming YOUR tree: {shown}"
    ]


def behind_park_lines(marker, now):
    """One banner line when the deployer has gone too long without a fast-forward, or [].

    `behind_since` holds `"<origin_sha> <unix_ts_first_seen>"` while the host is behind
    `origin/master`. Any tick that fast-forwarded renews the stamp, so its age is how long the
    deployer has moved the tree nowhere rather than how long it has been behind the tip — which
    is the number that means something because the tick lands at the newest green ancestor. A
    routine push clears in one tick; a stamp older than `BEHIND_PARK_SECONDS` means several
    ticks in a row moved nothing, which is a park. This catches the parks a dirty tree does not
    explain (a held SHA, an unapplied broad plane) as well as the one it does.

    Malformed or unparsable content reads as "no park": the marker is written atomically, and a
    banner that guessed an age from a torn value would be worse than one that said nothing. The
    decision is `lib.deployer_park.park_age`, shared with `deploy.sh` exit 4; this
    function owns only the banner line it becomes.
    """
    age = park_age(marker, now)
    if age is None:
        return []
    return [
        f"  ✗ the GitOps deployer has not fast-forwarded for {int(age // 60)} min and is behind "
        "origin/master — that is a park, not a queue; check `journalctl -t gitops-deploy` for "
        "the skip reason"
    ]


def parked_deployer_problems(
    primary=None,
    status=None,
    read_marker=None,
    now=None,
    read_manual=None,
    read_contention=None,
    read_k8s_deferred=None,
    read_k8s_unapplied=None,
):
    """The primary-checkout and deployer-park banner lines, as one list.

    The seams are parameters rather than patched attributes so the tests can drive this
    without pinning a module name (the repo's monkeypatch ratchet caps a new test module at
    zero patches on a first-party module). Every default reaches the real thing.

    Best-effort, like every other check here: any failure of a read returns [], because a
    SessionStart banner must never block a session from starting. The exception matches
    `master_moved_problems` — an ImportError is loud, since an empty list is indistinguishable
    from "nothing to report" and that is what hid a whole banner section for a month.
    """
    sys.path.insert(0, os.path.join(REPO, "scripts"))
    try:
        from lib.git import git
    except ImportError as exc:
        return [f"  ⚠ parked-deployer detection is broken: {exc}"]

    # `lib.git.git` strips every `GIT_*` variable, so `cwd` alone decides which tree is read —
    # neither `git -C` nor a `cwd=` overrides an inherited `GIT_DIR`.
    # The primary checkout is `_hook_common`'s one answer, shared with the instructions log.
    if primary is None:
        primary = primary_checkout

    if status is None:

        def status(path):
            return git(*PRIMARY_STATUS_ARGV, cwd=path, check=False, timeout=5).stdout

    if read_marker is None:

        def read_marker():
            return read_behind_marker(GITOPS_STATE_DIR)

    if read_manual is None:

        def read_manual():
            return read_manual_plane_marker(GITOPS_STATE_DIR)

    if read_contention is None:

        def read_contention():
            return read_contention_marker(GITOPS_STATE_DIR)

    if read_k8s_deferred is None:

        def read_k8s_deferred():
            return read_k8s_deferred_marker(GITOPS_STATE_DIR)

    if read_k8s_unapplied is None:

        def read_k8s_unapplied():
            return read_k8s_unapplied_marker(GITOPS_STATE_DIR)

    # Deferred like the `lib.git` import above, and for the same reason the rest of this file
    # defers: nothing at module scope may be able to stop the banner. The one module-scope
    # import this file does keep — `lib.deployer_park` — is wrapped up there and reported here.
    import time

    lines = []
    if DEPLOYER_PARK_IMPORT_ERROR:
        # Added BEFORE the reads below rather than after them, so a `primary` that raises
        # cannot swallow it: an empty list is indistinguishable from "nothing to report", and
        # that is what hid a whole banner section for a month.
        lines.append(
            f"  ⚠ parked-deployer detection is broken: {DEPLOYER_PARK_IMPORT_ERROR}"
        )
    try:
        path = primary()
        if path:
            lines += dirty_primary_lines(status(path), path)
        if DEPLOYER_PARK_IMPORT_ERROR:
            # The dirty-primary half still answered and is kept; only the park half is
            # unanswerable, and `park_age` raises if it is asked.
            return lines
        # An absent marker is the healthy case — the deployer removes it on convergence — and
        # `read_behind_marker` answers None for it, which reads as "no park". An INJECTED
        # read_marker may still raise, and the dirty lines gathered before it are still worth
        # printing.
        clock = time.time() if now is None else now
        lines += behind_park_lines(read_marker(), clock)
        # After the park line and independent of it: the two are different deferrals of the
        # same tick, and a host can be in both. A read that raises keeps the lines gathered
        # before it, the same way the park read does.
        lines += manual_plane_lines(read_manual(), clock)
        lines += contention_lines(read_contention(), clock)
        # Last, after every other deferral: a deferred image bump is the cheapest of them to
        # clear and the least urgent. One deploy of the named service does it, and this banner
        # reaches the session that can run it — `land.sh` printed the command to whoever merged
        # the bump, and nothing printed it again.
        lines += k8s_deferred_lines(read_k8s_deferred(), clock)
        # Last of all, because it is the only one nothing pages on: a k8s role this deployer
        # never applies is owed to a hand indefinitely, and the banner is where that is said.
        lines += k8s_unapplied_lines(read_k8s_unapplied(), clock)
    except Exception:
        return lines
    return lines


def _run(cmd, timeout):
    """Run cmd in the repo dir, capturing output. Raises on timeout/missing binary."""
    return subprocess.run(
        cmd, capture_output=True, text=True, timeout=timeout, cwd=REPO, check=False
    )


def target_problems():
    """hooklib's scrape-target lines, bound to this file's `_run` and REPO."""
    if service_lines is None:
        return []
    return service_lines.target_problems(_run, REPO)


def stale_release_problems():
    """hooklib's stale-release line, read from the cron's verdict file.

    A probe of its own rather than the tail of `target_problems`, so a slow read past the
    deadline cannot cost the down-target lines that already finished.
    """
    if service_lines is None:
        return []
    return service_lines.stale_release_problems()


def master_moved_problems(git=None):
    """One line when this branch is behind origin/master's remote-tracking ref, else [].

    `git rev-list --count HEAD..origin/master` reads only the local object store -- no
    `git fetch` runs here, so this can only ever be as current as the last fetch this
    checkout happened to do (a login shell's periodic fetch, a manual `git fetch`, an
    earlier `deploy.sh` run). That is the honest answer for a read-only, always-runs
    SessionStart hook: a live check would mean a network call on every session open. The
    line says "last fetched" rather than claiming to be current, and deploy.sh's own
    staleness gate (scripts/deploy_tools/deploy_lib/staleness.py, exit 4) is what actually refuses a stale
    deploy -- this is only the earlier, cheaper warning. [] on any failure of the git READ,
    including a checkout with no origin/master ref at all -- diagnosing that is not this
    hook's job.

    A failure to IMPORT `lib.git` is the exception, and returns a `⚠` line naming it. Both halves of
    this banner import from the same `scripts/` path insert, and `other_live_sessions` already fails
    loudly for the reason recorded there: an empty list is indistinguishable from "nothing to report".
    The two halves fail the same way.

    The read goes through `lib.git.git` rather than `_run`, which strips every `GIT_*`
    variable and so leaves `cwd` alone deciding which tree is counted. Neither `git -C` nor a
    `cwd=` overrides an inherited `GIT_DIR`, and a session opened from inside a git hook
    inherits one. The import is deferred into this function for the reason
    `other_live_sessions` defers its own: an ImportError at module level stops the banner
    instead of degrading it. `git` is that function, and a test passes a fake instead.
    """
    if git is None:
        sys.path.insert(0, os.path.join(REPO, "scripts"))
        try:
            from lib.git import git
        except ImportError as exc:
            return [f"  ⚠ behind-master detection is broken: {exc}"]
    try:
        res = git(
            "rev-list",
            "--count",
            "HEAD..origin/master",
            cwd=REPO,
            check=False,
            timeout=5,
        )
        if res.returncode != 0:
            return []
        count = int(res.stdout.strip())
    except Exception:
        return []
    if count <= 0:
        return []
    plural = "s" if count != 1 else ""
    return [
        f"  ⚠ this branch is {count} commit{plural} behind origin/master (as of the last "
        "fetch -- `git fetch` to refresh; a deploy from here would be refused, see "
        "scripts/deploy_tools/deploy_lib/staleness.py)"
    ]


# How long the per-worktree reads in `other_live_sessions` may take together, after
# `git worktree list`. Under TOTAL_DEADLINE_S, so a slow tree is named on its own line before
# the outer deadline gives up on the whole session list.
SESSION_SCAN_DEADLINE_S = 6


def other_live_sessions(
    cwd, *, run=None, git_dirty=None, deadline=SESSION_SCAN_DEADLINE_S
):
    """Lines describing the other Claude sessions working this repo right now.

    Derived from git and /proc rather than from anything a session declares, so it cannot
    go stale when a session forgets to announce itself or dies without cleaning up. Knowing
    another session is already in a role is what stops two of them editing it at once.

    `run` and `git_dirty` default to `_run` and `lib.git.git_dirty`; a test passes fakes.
    `deadline` bounds the per-worktree reads together; a test passes a short one.
    """
    run = run or _run
    # scripts/, which holds `lib/`. A stale insert would fail silently: the except below
    # returns an empty list, and an empty list is indistinguishable from "no other sessions
    # are running".
    sys.path.insert(0, os.path.join(REPO, "scripts"))
    try:
        from lib.worktree_label import with_label
        from lib.worktrees import parse_worktree_list, session_is_alive

        if git_dirty is None:
            from lib.git import git_dirty
    except ImportError as exc:
        # Fail open, because a SessionStart banner must never block a session from starting —
        # but say so. The silence is what let the path bug live for a month.
        return [f"  ⚠ other-session detection is broken: {exc}"]

    def describe(tree):
        changed = run(
            ["git", "-C", tree.path, "diff", "--name-only", "origin/master...HEAD"], 5
        ).stdout
        # Untracked counted: an unlanded scratch file in another live session's worktree is
        # exactly the "+ uncommitted" this banner exists to surface. check=False here (not
        # git_dirty's own check=True default) because a tree that vanished mid-scan must read
        # as "not dirty", the same silent-degrade `_run(check=False)` gave before this routed
        # through lib.git.
        try:
            dirty = git_dirty(tree.path, include_untracked=True)
        except subprocess.CalledProcessError:
            dirty = False
        paths = sorted({p for p in changed.splitlines() if p})
        shown = ", ".join(paths[:4]) or "no commits yet"
        if len(paths) > 4:
            shown += f", +{len(paths) - 4} more"
        if dirty:
            shown += " (+ uncommitted)"
        return [f"  • {name_of(tree)} — {shown}"]

    def name_of(tree):
        return with_label(tree.branch or os.path.basename(tree.path), tree.path)

    live = [
        tree
        for tree in parse_worktree_list(
            run(["git", "worktree", "list", "--porcelain"], 5).stdout
        )[1:]
        if os.path.realpath(tree.path) != os.path.realpath(cwd)
        and tree.locked
        and session_is_alive(tree.lock_reason)
    ]
    # One thread per live worktree, each with its own diff and dirty read: run in turn, a
    # handful of sessions on a loaded box passed the banner's deadline and lost the whole
    # list. A tree that does not finish costs its own line, never its siblings' (#6153).
    sections = run_sections(
        [
            (f"the scan of {name_of(tree)}", functools.partial(describe, tree))
            for tree in live
        ],
        deadline,
    )
    return [line for lines in sections for line in lines]


def unlabelled_worktree_lines(cwd, *, run=None):
    """A prompt to label this session's worktree when Claude Code named it and nothing has.

    A Remote Control session starts in `bridge-<sessionId>` before it knows its task, so the
    banner asks it to say what it works on once it does. Empty for any other tree.

    `run` defaults to `_run`; a test passes a fake.
    """
    run = run or _run
    sys.path.insert(0, os.path.join(REPO, "scripts"))
    try:
        from lib.worktree_label import has_generated_name, read_label
    except ImportError as exc:
        return [f"  ⚠ worktree-label check is broken: {exc}"]
    top = run(["git", "-C", cwd, "rev-parse", "--show-toplevel"], 5).stdout.strip()
    if not top or not has_generated_name(top) or read_label(top):
        return []
    return [
        f"WARN: this worktree's name ({os.path.basename(top)}) was generated. Once you "
        "know the task, label it: `uv run python scripts/dev/label_worktree.py <issue>...` "
        'or `--text "<what you are doing>"`.'
    ]


# Well inside the 15s budget settings.json kills this hook at (this file's gen-hooks
# header). prune_worktrees.py --brief takes 1.3s warm.
WORKTREE_TIMEOUT_S = 5

# How long every probe together may take, from when they start. The 15s hook budget less
# the `uv run` interpreter start that comes before `main`, with room left to print. One
# probe can still chain past it: the scrape-target probe runs `probe.py targets` (6s) and then
# one 5s `kubectl` read per down target. That probe then prints the `did not finish` line,
# and every other probe's lines still print.
TOTAL_DEADLINE_S = 10


def stale_worktree_lines(run=None):
    """Shim: `hooklib.worktree_lines` can't import `_run` back from this module."""
    return _stale_worktree_lines(run or _run, WORKTREE_TIMEOUT_S)


PROBE = "uv run python scripts/diagnostics/probe.py"


def format_banner(problems, landing=False):
    """Render the problem list as the session banner (empty string => print nothing).

    The triage line names whole commands joined by `or`, so a line copied into a shell is
    one runnable command rather than a pipe. `landing` adds `probe.py landing`, the command
    that reads the deployer and primary-checkout state; `main` sets it when the
    parked-deployer section produced a line, because `targets` and `health` cannot see that
    state.
    """
    if not problems:
        return ""
    out = ["WARN: homelab health check found issues:"]
    out.extend(problems)
    commands = [f"{PROBE} targets", f"{PROBE} health <svc>"]
    if landing:
        commands.insert(0, f"{PROBE} landing")
    out.append("  → triage: " + " or ".join(f"`{c}`" for c in commands))
    return "\n".join(out)


def main(
    *,
    read_payload=read_payload,
    target_problems=target_problems,
    stale_release_problems=stale_release_problems,
    master_moved_problems=master_moved_problems,
    parked_deployer_problems=parked_deployer_problems,
    other_live_sessions=other_live_sessions,
    unlabelled_worktree_lines=unlabelled_worktree_lines,
    stale_worktree_lines=stale_worktree_lines,
    remote_fanout_lines=remote_fanout_lines,
    deadline=TOTAL_DEADLINE_S,
):
    """Print the SessionStart health banner for a genuine session open, then exit 0.

    Prints nothing for a mid-session compaction. Combines container, Prometheus-target and
    stale-branch problems into one banner, then separately prints other live sessions in
    this repo, worktrees ready to remove, and fan-out worktrees running on another host —
    all three regardless of health status.

    Every keyword argument is one of this module's own probes, and its default is that
    probe. A test passes a fake instead of monkeypatching the module attribute: the
    monkeypatch ratchet (ansible/tests/_ratchet.py) caps a test module at zero patches on a
    first-party module, and each probe reads live state (stdin, this checkout's git, the
    machine's worktrees, `.claude/fanout`) that would make an assertion depend on the host.
    `deadline` is the seconds all of them together may take; a test passes a short one.
    """
    payload = read_payload() or {}
    compact = payload.get("source") == "compact"
    try:
        # Imported here so a broken module costs these rows, never the banner. A compaction
        # reloads MEMORY.md with no InstructionsLoaded event, so it rows that too (#4668).
        from hooklib.instruction_rows import log_unevented_loads

        log_unevented_loads(payload, "compact" if compact else "session_start")
    except Exception:  # noqa: S110 -- a failed row must never cost the banner
        pass
    # Don't re-banner on mid-session compaction — only on a genuine open/resume/clear.
    if compact:
        return 0

    # Every probe at once, under one deadline (#6153): run in turn, their caps summed to more
    # than the hook's budget, and the harness's kill took the whole banner with it.
    cwd = os.getcwd()
    (
        targets,
        stale_release,
        master_moved,
        parked,
        sessions,
        unlabelled,
        worktrees,
        fanout,
    ) = run_sections(
        [
            ("the scrape-target check", target_problems),
            ("the stale-release check", stale_release_problems),
            ("the behind-master check", master_moved_problems),
            ("the parked-deployer check", parked_deployer_problems),
            ("the other-session scan", lambda: other_live_sessions(cwd)),
            ("the worktree-label check", lambda: unlabelled_worktree_lines(cwd)),
            ("the removable-worktree survey", stale_worktree_lines),
            ("the remote fan-out read", remote_fanout_lines),
        ],
        deadline,
    )
    # master_moved last deliberately: "this branch is behind origin/master" is the SYMPTOM of a
    # parked deployer, so the cause reads first.
    problems = targets + stale_release + parked + master_moved

    banner = format_banner(problems, landing=bool(parked))
    if banner:
        print(banner)
    elif os.environ.get("SESSION_HEALTH_VERBOSE"):
        print("Homelab health: all scrape targets up.")

    # Printed independently of health: another session's open work is information this
    # session needs even when everything is green.
    if sessions:
        print("Other Claude sessions in this repo:")
        for line in sessions:
            print(line)
    for line in unlabelled:
        print(line)

    # What keeps the banner inside the hook's budget is `deadline`, not this flush: every
    # probe has finished or been given up on by now. The flush only puts the banner on the
    # pipe ahead of the worktree lines.
    sys.stdout.flush()
    for line in worktrees + fanout:
        print(line)
    return 0


if __name__ == "__main__":
    if any(arg in ("-h", "--help") for arg in sys.argv[1:]):
        print(__doc__.strip())
        sys.exit(0)
    try:
        sys.exit(main())
    except Exception:
        sys.exit(0)
