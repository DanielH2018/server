#!/usr/bin/env python3
"""SessionStart health banner: surfaces what's already broken before work starts.

When a Claude Code session opens in this repo, this prints anything already broken so
work doesn't start blind:
  * containers that are unhealthy or stuck restarting (fast, local `docker ps`)
  * Prometheus scrape targets that are down (fleet-wide, via scripts/diagnostics/probe.py)
  * this branch sitting behind origin/master's last-fetched ref (local-only, no `git fetch`)
  * a dirty primary checkout, or a GitOps deployer parked behind origin — the two states that
    stop every deploy in the fleet and that a worktree session cannot look at for itself,
    because the isolation guard refuses a git command targeting the shared checkout

Design contract (mirrors the other hooks here):
  - SILENT when all-green: prints nothing, so it adds zero context noise on a
    healthy day. Set SESSION_HEALTH_VERBOSE=1 to force an all-clear line (demo/test).
  - READ-ONLY and NEVER BLOCKS: every external call is timeout-bounded and wrapped;
    any failure degrades to a quiet skip (or, for a wedged dockerd, a one-line
    warning — a dockerd that hangs IS the signal). Always exits 0.
  - A host with no docker binary is not a broken Docker host: daniel-box runs k3s
    and sets has_docker: false. The docker check skips silently there; the Prometheus
    check does NOT depend on docker (it goes through probe.py's cluster route, not
    `docker inspect`) and always runs regardless.
  - The docker check is local + sub-second and always runs. The Prometheus check
    goes through `uv run probe.py` (one subprocess, bounded) — SessionStart fires
    once per session, so the small cost is paid rarely; it's skipped on any error so
    a down monitoring stack can never stall session start. A down target whose
    Deployment is deliberately scaled to 0 replicas (an on-demand game server) is
    filtered out rather than reported — otherwise this would trade a false all-clear
    for the same two names on every session open forever.

Wired via .claude/settings.json -> hooks.SessionStart. Stdout is injected as
session context by Claude Code (same mechanism the remember plugin uses).
"""

import json
import os
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Three sections of the banner live in `.claude/hooks/hooklib/` (its own docstrings cover
# `hooklib`-not-`lib`): the worktree lines, and the docker and scrape-target lines. This file
# sat at its 600-line cap with no headroom for either split. Wrapped like `lib.deployer_park`
# below (issue #1566), in its own try so a failure names the package that broke rather than
# deployer_park's unrelated line.
#
# `SyntaxError` is caught alongside `ImportError` because these modules use PEP 758 syntax
# and this file is run by `session-health.sh`, which sends stderr to /dev/null and exits 0.
# An uncaught SyntaxError at import would take the WHOLE banner out silently, which is the
# #1566 failure class — nothing at module scope may be able to stop the banner. A SyntaxError
# is not an ImportError, so the narrower catch left that hole open.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    from hooklib import service_lines
    from hooklib.worktree_lines import _stale_worktree_lines, remote_fanout_lines

    HOOKLIB_IMPORT_ERROR = ""
except (ImportError, SyntaxError) as exc:
    HOOKLIB_IMPORT_ERROR = f"{type(exc).__name__}: {exc}"
    service_lines = None

    def remote_fanout_lines(*_args, **_kwargs):
        return [f"  ⚠ hooklib is broken: {HOOKLIB_IMPORT_ERROR}"]

    def _stale_worktree_lines(*_args, **_kwargs):
        return []


# The park decision itself lives in `scripts/lib/deployer_park.py`, because `deploy.sh` exit 4
# asks the same question of the same marker and a second derivation would drift (issue #1429).
# The marker directory and the threshold are re-exported under their original names: this
# module's tests read `_mod.BEHIND_PARK_SECONDS`, and `GITOPS_STATE_DIR` is mode 0750 owned by
# `ubuntu`, so a session running as that user reads it and any other host degrades to silence.
#
# Wrapped, because NOTHING at module scope may be able to stop the banner (issue #1566). This
# file is run by `session-health.sh`, which sends stderr to /dev/null and exits 0, so an
# ImportError here would take out the docker, scrape-target, live-session and stale-worktree
# sections as well — silently, and for a reason no session could see. The failure is reported
# in `parked_deployer_problems` instead, the same way its deferred `lib.git` import already is.
sys.path.insert(0, os.path.join(REPO, "scripts"))
try:
    from lib.deployer_park import (
        BEHIND_PARK_SECONDS,
        GITOPS_STATE_DIR,
        park_age,
        read_behind_marker,
    )

    DEPLOYER_PARK_IMPORT_ERROR = ""
except ImportError as exc:
    DEPLOYER_PARK_IMPORT_ERROR = str(exc)

    # Inert placeholders, never read: `parked_deployer_problems` returns its `⚠` line before it
    # asks the park question. They exist so the names stay defined and singly-typed for the type
    # checker. NOT a fallback copy of the real values — a second BEHIND_PARK_SECONDS here would
    # be the second derivation issue #1429 removed, so these are deliberately not the real
    # threshold or the real marker directory, and the raising stubs say so if one is ever called.
    BEHIND_PARK_SECONDS = 0
    GITOPS_STATE_DIR = ""

    def _park_unavailable(*_args, **_kwargs):
        raise ImportError(DEPLOYER_PARK_IMPORT_ERROR)

    park_age = _park_unavailable
    read_behind_marker = _park_unavailable

__all__ = ["BEHIND_PARK_SECONDS", "GITOPS_STATE_DIR"]

# How many dirty paths the banner names before it summarises the rest. Enough to recognise whose
# work it is; short enough to stay one line.
PRIMARY_DIRTY_LIMIT = 8

# How this hook asks the SHARED primary checkout whether it is dirty. `--no-optional-locks` is
# load-bearing, not a nicety: a plain `git status` refreshes and WRITES `.git/index` in the tree
# it reads. Without the flag a SessionStart hook running in a worktree would write the shared
# checkout's index — the one thing the isolation guard exists to prevent — and could collide on
# `index.lock` with the deployer's own `merge --ff-only`, which holds
# /var/lock/server-git-tree.lock and knows nothing about this hook. The flag is global to git, so
# it comes before the subcommand. A constant rather than an inline argv so a test can assert the
# shape without patching `lib.git.git`.
PRIMARY_STATUS_ARGV = ("--no-optional-locks", "status", "--porcelain")


def primary_worktree_path(porcelain):
    """The main checkout's path from `git worktree list --porcelain`, or None.

    git prints the main worktree first and every entry opens with a `worktree <path>` line, so
    the first such line is the primary checkout. Parsed here rather than through
    `prune_worktrees.parse_worktree_list` because this needs one field and must not inherit that
    module's import risk — `other_live_sessions` already carries a `⚠` line for the day that
    import breaks.
    """
    for line in porcelain.splitlines():
        if line.startswith("worktree "):
            return line[len("worktree ") :].strip() or None
    return None


def dirty_primary_lines(porcelain, path):
    """One banner line naming what makes the primary checkout dirty, or [].

    `gitops_deploy` skips a tick outright while `git status --porcelain` on the primary checkout
    is non-empty, so ONE stray file there stops every deploy in the fleet. The session that hits
    it sees `deploy.sh` exit 4 — "this tree is N commit(s) behind origin/master" — which names
    its own worktree and points at `git rebase`, the wrong repair. Worse, no session can look:
    the isolation guard refuses a git command targeting the shared checkout, correctly. This
    banner is the way the cause reaches a worktree session at all (issues #1416, #1418).

    Names the paths with their porcelain status codes, the way the deployer's own journal line
    does. `??` is the code worth seeing: `git status --porcelain` counts untracked files, so the
    tree can be dirty with nothing modified.

    The parse is a deliberate COPY of `dirty_summary` in
    `ansible/roles/setup/gitops_deploy/files/deploy_git.py`, not an import of it — importing
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
    is the number that means something now the tick lands at the newest green ancestor. A
    routine push clears in one tick; a stamp older than `BEHIND_PARK_SECONDS` means several
    ticks in a row moved nothing, which is a park. This catches the parks a dirty tree does not
    explain (a held SHA, an unapplied broad plane) as well as the one it does.

    Malformed or unparsable content reads as "no park": the marker is written atomically, and a
    banner that guessed an age from a torn value would be worse than one that said nothing. The
    decision is `lib.deployer_park.park_age`, shared with `deploy.sh` exit 4 (issue #1429); this
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
    list_worktrees=None, status=None, read_marker=None, now=None
):
    """The primary-checkout and deployer-park banner lines, as one list.

    The four seams are parameters rather than patched attributes so the tests can drive this
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
    if list_worktrees is None:

        def list_worktrees():
            return git(
                "worktree", "list", "--porcelain", cwd=REPO, check=False, timeout=5
            ).stdout

    if status is None:

        def status(path):
            return git(*PRIMARY_STATUS_ARGV, cwd=path, check=False, timeout=5).stdout

    if read_marker is None:

        def read_marker():
            return read_behind_marker(GITOPS_STATE_DIR)

    # Deferred like the `lib.git` import above, and for the same reason the rest of this file
    # defers: nothing at module scope may be able to stop the banner. The one module-scope
    # import this file does keep — `lib.deployer_park` — is wrapped up there and reported here.
    import time

    lines = []
    if DEPLOYER_PARK_IMPORT_ERROR:
        # Added BEFORE the reads below rather than after them, so a `list_worktrees` that raises
        # cannot swallow it: an empty list is indistinguishable from "nothing to report", and
        # that is what hid a whole banner section for a month.
        lines.append(
            f"  ⚠ parked-deployer detection is broken: {DEPLOYER_PARK_IMPORT_ERROR}"
        )
    try:
        primary = primary_worktree_path(list_worktrees())
        if primary:
            lines += dirty_primary_lines(status(primary), primary)
        if DEPLOYER_PARK_IMPORT_ERROR:
            # The dirty-primary half still answered and is kept; only the park half is
            # unanswerable, and `park_age` raises if it is asked.
            return lines
        # An absent marker is the healthy case — the deployer removes it on convergence — and
        # `read_behind_marker` answers None for it, which reads as "no park". An INJECTED
        # read_marker may still raise, and the dirty lines gathered before it are still worth
        # printing.
        lines += behind_park_lines(read_marker(), time.time() if now is None else now)
    except Exception:
        return lines
    return lines


def _run(cmd, timeout):
    """Run cmd in the repo dir, capturing output. Raises on timeout/missing binary."""
    return subprocess.run(
        cmd, capture_output=True, text=True, timeout=timeout, cwd=REPO, check=False
    )


def docker_problems():
    """`hooklib.service_lines.docker_problems`, bound to this file's repo-rooted `_run`.

    A broken hooklib reports no container problems rather than raising; the `⚠` line naming
    the import failure is printed once, by `remote_fanout_lines`.
    """
    if service_lines is None:
        return [], False
    return service_lines.docker_problems(_run)


def target_problems():
    """`hooklib.service_lines.target_problems`, bound to this file's `_run` and REPO."""
    if service_lines is None:
        return []
    return service_lines.target_problems(_run, REPO)


def master_moved_problems():
    """One line when this branch is behind origin/master's remote-tracking ref, else [].

    `git rev-list --count HEAD..origin/master` reads only the local object store -- no
    `git fetch` runs here, so this can only ever be as current as the last fetch this
    checkout happened to do (a login shell's periodic fetch, a manual `git fetch`, an
    earlier `deploy.sh` run). That is the honest answer for a read-only, always-runs
    SessionStart hook: a live check would mean a network call on every session open. The
    line says "last fetched" rather than claiming to be current, and deploy.sh's own
    staleness gate (scripts/deploy_tools/deploy_staleness.py, exit 4) is what actually refuses a stale
    deploy -- this is only the earlier, cheaper warning. [] on any failure of the git READ,
    including a checkout with no origin/master ref at all -- diagnosing that is not this
    hook's job.

    A failure to IMPORT `lib.git` is the exception, and returns a `⚠` line naming it
    (issue #1306). Both halves of this banner import from the same `scripts/` path insert,
    and `other_live_sessions` already fails loudly for the reason recorded there: an empty
    list is indistinguishable from "nothing to report", which is what let a stale insert
    hide that whole section from 2026-08 until 2026-09-01. The two halves now fail the
    same way.

    The read goes through `lib.git.git` rather than `_run`, which strips every `GIT_*`
    variable and so leaves `cwd` alone deciding which tree is counted. Neither `git -C` nor a
    `cwd=` overrides an inherited `GIT_DIR`, and a session opened from inside a git hook
    inherits one. The import is deferred into this function for the reason
    `other_live_sessions` defers its own: an ImportError at module level stops the banner
    instead of degrading it.
    """
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
        "scripts/deploy_tools/deploy_staleness.py)"
    ]


def other_live_sessions(cwd):
    """Lines describing the other Claude sessions working this repo right now.

    Derived from git and /proc rather than from anything a session declares, so it cannot
    go stale when a session forgets to announce itself or dies without cleaning up. Knowing
    another session is already in a role is what stops two of them editing it at once.
    """
    # scripts/dev/, not scripts/ — the module moved when scripts/ was regrouped into
    # subdirectories by what each script acts on (#443), and this insert did not follow it.
    # Nothing failed loudly, because the except below returned an empty list and an empty list
    # is indistinguishable from "no other sessions are running" — so the banner's whole
    # other-sessions section was silently absent from 2026-08 until 2026-09-01.
    sys.path.insert(0, os.path.join(REPO, "scripts", "dev"))
    sys.path.insert(0, os.path.join(REPO, "scripts"))
    try:
        from prune_worktrees import parse_worktree_list, session_is_alive
        from lib.git import git_dirty
    except ImportError as exc:
        # Fail open, because a SessionStart banner must never block a session from starting —
        # but say so. The silence is what let the path bug live for a month.
        return [f"  ⚠ other-session detection is broken: {exc}"]

    lines = []
    for tree in parse_worktree_list(
        _run(["git", "worktree", "list", "--porcelain"], 5).stdout
    )[1:]:
        if os.path.realpath(tree.path) == os.path.realpath(cwd):
            continue
        if not (tree.locked and session_is_alive(tree.lock_reason)):
            continue
        changed = _run(
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
        lines.append(f"  • {tree.branch or os.path.basename(tree.path)} — {shown}")
    return lines


WORKTREE_TIMEOUT_S = 30


def stale_worktree_lines():
    """Shim: `hooklib.worktree_lines` can't import `_run` back from this module."""
    return _stale_worktree_lines(_run, WORKTREE_TIMEOUT_S)


def format_banner(problems):
    """Render the problem list as the session banner (empty string => print nothing)."""
    if not problems:
        return ""
    out = ["\U0001f3e0 Homelab health check — issues detected:"]
    out.extend(problems)
    out.append(
        "  → triage: uv run python scripts/diagnostics/probe.py targets | "
        "probe.py health <svc> | docker ps --filter health=unhealthy"
    )
    return "\n".join(out)


def main(
    *,
    parked_deployer_problems=parked_deployer_problems,
    remote_fanout_lines=remote_fanout_lines,
):
    """Print the SessionStart health banner for a genuine session open, then exit 0.

    Skips a mid-session compaction event. Combines container, Prometheus-target and
    stale-branch problems into one banner, then separately prints other live sessions in
    this repo, worktrees ready to remove, and fan-out worktrees running on another host —
    all three regardless of health status.

    Args:
        parked_deployer_problems: override for the parked-deployer probe. Defaults to the
            module's own `parked_deployer_problems`; a test passes a fake here instead of
            monkeypatching the module attribute, because the monkeypatch ratchet
            (ansible/tests/_ratchet.py) caps this file's patches on a first-party module at
            its current allowlist entry — the same reason `parked_deployer_problems` itself
            takes its four reads as parameters rather than patched globals.
        remote_fanout_lines: override for the same reason (live `.claude/fanout` state).
    """
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        payload = {}
    # Don't re-banner on mid-session compaction — only on a genuine open/resume/clear.
    if payload.get("source") == "compact":
        return 0

    dock, _docker_ok = docker_problems()
    targets = target_problems()
    master_moved = master_moved_problems()
    # Before master_moved deliberately: "this branch is behind origin/master" is the SYMPTOM of a
    # parked deployer, so the cause reads first.
    problems = dock + targets + parked_deployer_problems() + master_moved

    banner = format_banner(problems)
    if banner:
        print(banner)
    elif os.environ.get("SESSION_HEALTH_VERBOSE"):
        print(
            "\U0001f3e0 Homelab health: all containers healthy, all scrape targets up."
        )

    # Printed independently of health: another session's open work is information this
    # session needs even when everything is green.
    try:
        sessions = other_live_sessions(os.getcwd())
    except Exception:
        sessions = []
    if sessions:
        print("\U0001f500 Other Claude sessions in this repo:")
        for line in sessions:
            print(line)

    for line in stale_worktree_lines():
        print(line)
    for line in remote_fanout_lines():
        print(line)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        sys.exit(0)
