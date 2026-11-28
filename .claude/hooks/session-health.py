#!/usr/bin/env python3
# gen-hooks: register
#   event: SessionStart
#   timeout: 15
#   order: 10
"""SessionStart health banner: surfaces what's already broken before work starts.

Usage: Claude Code runs this hook on the SessionStart event. It takes no stdin payload. It
prints one SessionStart JSON object to stdout, or nothing when everything is green, and it
exits 0. A bug that escapes the per-probe guards raises and exits 1, which `run-hook.sh` turns
into one `[session-health] failed: <cause>` line while still exiting 0. `-h` or `--help` prints this text and exits 0. `--sessions` prints every other live
session in this repo with the paths it changed, which the banner lists only where they overlap
this branch, and then each coordinator project that no live session holds.

When a Claude Code session opens in this repo, this prints anything already broken so
work doesn't start blind:
  * Prometheus scrape targets that are down (fleet-wide, via scripts/diagnostics/probe.py)
  * this branch sitting behind origin/master's last-fetched ref (local-only, no `git fetch`)
  * a dirty primary checkout, which a worktree session cannot look at for itself, because the
    isolation guard refuses a git command targeting the shared checkout

It also appends one `session_start` row to the instructions log for the auto-memory
`MEMORY.md` and one for the active output style. Claude Code fires no InstructionsLoaded
event for either, so without these rows `fact_status.py report` under-counts each session's
at-start bytes (#4656).

Design contract (mirrors the other hooks here):
  - SILENT when all-green: prints nothing, so it adds zero context noise on a
    healthy day. Set SESSION_HEALTH_VERBOSE=1 to force an all-clear line (demo/test).
  - READ-ONLY and NEVER BLOCKS: every external call is timeout-bounded and wrapped;
    a probe's failure degrades to a quiet skip, and a probe that timed out is named.
    `run-hook.sh` exits 0 whatever this does. The one write is the worktree survey's
    cache under `~/.cache`, which a detached refresh fills.
  - The Prometheus check
    goes through `uv run probe.py` (one subprocess, bounded) — SessionStart fires
    once per session, so the small cost is paid rarely; it's skipped on any error so
    a down monitoring stack can never stall session start. A down target whose
    Deployment is deliberately scaled to 0 replicas (an on-demand game server) is
    filtered out rather than reported — otherwise this would trade a false all-clear
    for the same two names on every session open forever.

The JSON splits the output by reader. `hookSpecificOutput.additionalContext` carries what the
agent acts on: the health banner, other live sessions and the unlabelled-worktree prompt.
`systemMessage` carries the operator's housekeeping, which the model never sees: worktrees ready
to remove, entries under the primary checkout's `.claude/worktrees/` that are not trees, and
fan-out batches running on another host.
"""

# DECIDED: return [] on any error, so a broken health read never blocks a session start;
# SESSION_HEALTH_VERBOSE=1 forces output.
# DECIDED: the gen-hooks block above carries no `matcher`, so the hook also fires on `compact`,
# where `main` rows `MEMORY.md`: Claude Code reloads it with no InstructionsLoaded event. A
# separate `compact`-only entry saves nothing, since the interpreter start is the whole cost.

import functools
import json
import os
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# `SyntaxError` is caught too: these modules use PEP 758 syntax.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    from _hook_common import primary_checkout, read_payload, tagged
except ImportError, SyntaxError:

    def tagged(hook, text):
        return f"[{hook}] {text}"

    # Without the payload the banner prints on both open and compaction.
    def read_payload():
        return None

    def primary_checkout():
        return None


try:
    from hooklib import service_lines
    from hooklib.sections import keep_where, run_sections
    from hooklib.worktree_lines import remote_fanout_lines, stale_worktree_lines

    HOOKLIB_IMPORT_ERROR = ""
except (ImportError, SyntaxError) as exc:
    HOOKLIB_IMPORT_ERROR = f"{type(exc).__name__}: {exc}"
    service_lines = None

    def remote_fanout_lines(*_args, **_kwargs):
        return [f"  ⚠ hooklib is broken: {HOOKLIB_IMPORT_ERROR}"]

    def stale_worktree_lines(*_args, **_kwargs):
        return []

    # One after another, with no deadline: the banner is what this file must keep, and a
    # serial run that the harness may kill still prints whatever finished first.
    def run_sections(probes, _deadline):
        out = []
        for _name, probe in probes:
            try:
                out.append(probe())
            except Exception:
                out.append([])
        return out

    def keep_where(items, check, _deadline):
        return [item for item in items if check(item)]


PRIMARY_DIRTY_LIMIT = 8

# `--no-optional-locks` is load-bearing: a plain `git status` writes `.git/index` and races
# homelab-checkout-sync on `index.lock`. It is a global flag, so it precedes the subcommand.
PRIMARY_STATUS_ARGV = ("--no-optional-locks", "status", "--porcelain")


def dirty_primary_lines(porcelain, path):
    """One banner line naming what makes the primary checkout dirty, or [].

    `homelab-checkout-sync` refuses to fast-forward while `git status --porcelain` on the
    primary checkout is non-empty, so ONE stray file there parks the checkout every landing and
    probe reads. The session that hits it sees a stale-tree refusal, which names its own
    worktree and points at `git rebase`, the wrong repair. Worse, no session can look:
    the isolation guard refuses a git command targeting the shared checkout, correctly. This
    banner is the way the cause reaches a worktree session at all.

    Names the paths with their porcelain status codes. `??` is the code worth seeing: `git status --porcelain` counts untracked files, so the
    tree can be dirty with nothing modified.

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
        f"  ✗ primary checkout {path} is dirty — homelab-checkout-sync refuses to fast-forward it "
        f"while it is, so the checkout lags origin/master: {shown}"
    ]


def dirty_primary_problems(primary=None, status=None):
    """The primary-checkout banner lines, as one list.

    The seams are parameters rather than patched attributes so the tests can drive this
    without pinning a module name (the repo's monkeypatch ratchet caps a new test module at
    zero patches on a first-party module). Every default reaches the real thing.

    Best-effort, like every other check here: any failure of a read returns [], because a
    SessionStart banner must never block a session from starting. A timeout propagates, so
    `run_sections` names it. The exception matches
    `master_moved_problems` — an ImportError is loud, since an empty list is indistinguishable
    from "nothing to report" and that is what hid a whole banner section for a month.
    """
    sys.path.insert(0, os.path.join(REPO, "scripts"))
    try:
        from lib.git import git
    except ImportError as exc:
        return [f"  ⚠ dirty-primary detection is broken: {exc}"]

    # `lib.git.git` strips every `GIT_*` variable, so `cwd` alone decides which tree is read.
    if primary is None:
        primary = primary_checkout

    if status is None:

        def status(path):
            return git(*PRIMARY_STATUS_ARGV, cwd=path, check=False, timeout=5).stdout

    try:
        path = primary()
        return dirty_primary_lines(status(path), path) if path else []
    except subprocess.TimeoutExpired:
        raise
    except Exception:
        return []


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


def master_moved_problems(git=None):
    """One line when origin/master moved on a file this branch also changes, else [].

    Being behind alone prints nothing: every fresh worktree falls behind within minutes, and
    a warning in most sessions taught the reader to skip it. The line fires when the commits
    this branch lacks touch a path the branch changes since the merge base, and names those
    paths, because that is when a merge is due before trusting a test or a render.

    `git rev-list --count HEAD..origin/master` reads only the local object store -- no
    `git fetch` runs here, so this can only ever be as current as the last fetch this
    checkout happened to do (a login shell's periodic fetch, a manual `git fetch`, an
    earlier `git pull`). That is the honest answer for a read-only, always-runs
    SessionStart hook: a live check would mean a network call on every session open. The
    line says "last fetched" rather than claiming to be current, and the stale-probe rule
    (`.claude/hooks/_stale_probe_rule.py`) is what actually refuses a probe gate from a stale
    tree -- this is only the earlier, cheaper warning. [] on any failure of the git READ,
    including a checkout with no origin/master ref at all -- diagnosing that is not this
    hook's job. A timeout propagates, so `run_sections` names it.

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
        if count <= 0:
            return []
        shared = sorted(
            _changed_files(git, "origin/master...HEAD")
            & _changed_files(git, "HEAD...origin/master")
        )
    except subprocess.TimeoutExpired:
        raise
    except Exception:
        return []
    if not shared:
        return []
    plural = "s" if count != 1 else ""
    named = ", ".join(shared[:SHARED_FILES_NAMED])
    if len(shared) > SHARED_FILES_NAMED:
        named += f" and {len(shared) - SHARED_FILES_NAMED} more"
    return [
        f"  ⚠ this branch is {count} commit{plural} behind origin/master, which changed "
        f"{len(shared)} file{'s' if len(shared) != 1 else ''} this branch also changes: "
        f"{named} (as of the last fetch; `git fetch` and merge origin/master before "
        "relying on them)"
    ]


# How many shared paths the behind-master line names before it counts the rest.
SHARED_FILES_NAMED = 3


def _changed_files(git, revisions):
    """The paths `git diff --name-only <revisions>` names, as a set; raises on a failed read."""
    res = git("diff", "--name-only", revisions, cwd=REPO, check=False, timeout=5)
    if res.returncode != 0:
        raise RuntimeError(f"git diff {revisions} exited {res.returncode}")
    return {line for line in res.stdout.splitlines() if line}


# How long the per-worktree reads in `other_live_sessions` may take together, after
# `git worktree list`. Under TOTAL_DEADLINE_S, so a slow tree is named on its own line before
# the outer deadline gives up on the whole session list.
SESSION_SCAN_DEADLINE_S = 6
# A lock not judged in time counts as live.
LOCK_CHECK_DEADLINE_S = 2


SESSIONS_COMMAND = "uv run python .claude/hooks/session-health.py --sessions"


def changed_paths(cwd, run):
    """The paths a worktree's branch changed against origin/master, plus its dirty paths."""
    committed = run(
        ["git", "-C", cwd, "diff", "--name-only", "origin/master...HEAD"], 5
    ).stdout
    status = run(
        [
            "git",
            "-C",
            cwd,
            "--no-optional-locks",
            "status",
            "--porcelain",
            "--untracked-files=all",
        ],
        5,
    ).stdout
    dirty = {line[3:].split(" -> ")[-1] for line in status.splitlines() if line[3:]}
    return {p for p in committed.splitlines() if p} | dirty


def other_live_sessions(
    cwd, *, run=None, git_dirty=None, deadline=SESSION_SCAN_DEADLINE_S, show_all=False
):
    """Lines describing the other Claude sessions working this repo right now.

    Derived from git and /proc rather than from anything a session declares, so it cannot
    go stale when a session forgets to announce itself or dies without cleaning up. Knowing
    another session is already in a role is what stops two of them editing it at once.

    Only a session whose branch diff or dirty set shares a path with this session's gets its
    own line, naming the shared paths. The rest fold into one count line that
    names `SESSIONS_COMMAND`, because a full list on a busy day pushed the banner past the
    size at which Claude Code shows only a preview (#7898). `show_all` lists every session
    with its own changed paths instead, which is what `SESSIONS_COMMAND` prints.

    `run` and `git_dirty` default to `_run` and `lib.git.git_dirty`; a test passes fakes.
    `deadline` bounds the per-worktree reads together; a test passes a short one.
    """
    run = run or _run
    # A stale insert fails silently: the except below returns an empty list.
    sys.path.insert(0, os.path.join(REPO, "scripts"))
    try:
        from lib.worktree_label import with_label
        from lib.fanout_lock import process_unit_state
        from lib.worktrees import has_live_session, parse_worktree_list

        if git_dirty is None:
            from lib.git import git_dirty
    except ImportError as exc:
        # Fail open: a SessionStart banner must never block a session from starting.
        return [f"  ⚠ other-session detection is broken: {exc}"]

    def describe(tree):
        paths = sorted(changed_paths(tree.path, run))
        # `check=False` so a tree that vanished mid-scan reads as not dirty; untracked files count.
        try:
            dirty = git_dirty(tree.path, include_untracked=True)
        except subprocess.CalledProcessError:
            dirty = False
        if not show_all:
            paths = sorted(set(paths) & mine)
            if not paths:
                return []
        shown = ", ".join(paths[:4]) or "no commits yet"
        if len(paths) > 4:
            shown += f", +{len(paths) - 4} more"
        if dirty:
            shown += " (+ uncommitted)"
        return [f"  • {name_of(tree)} — {shown}"]

    def name_of(tree):
        return with_label(tree.branch or os.path.basename(tree.path), tree.path)

    locked = [
        tree
        for tree in parse_worktree_list(
            run(["git", "worktree", "list", "--porcelain"], 5).stdout
        )[1:]
        if os.path.realpath(tree.path) != os.path.realpath(cwd) and tree.locked
    ]
    # A tree another user owns is judged from /proc, because that user's manager answers only its
    # owner.
    live = keep_where(
        locked,
        functools.partial(has_live_session, foreign=process_unit_state()),
        LOCK_CHECK_DEADLINE_S,
    )
    try:
        mine = set() if show_all else changed_paths(cwd, run)
    except Exception:
        mine = set()
    # One thread per live worktree: a tree that does not finish costs its own line.
    sections = run_sections(
        [
            (f"the scan of {name_of(tree)}", functools.partial(describe, tree))
            for tree in live
        ],
        deadline,
    )
    lines = [line for lines in sections for line in lines]
    quiet = sum(1 for tree_lines in sections if not tree_lines)
    if quiet and not show_all:
        lines.append(
            f"  {quiet} other live session{'s' if quiet != 1 else ''}, no overlap with "
            f"this branch — `{SESSIONS_COMMAND}` lists them"
        )
    return lines


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
        f"This worktree's name ({os.path.basename(top)}) was generated. Once you "
        "know the task, label it: `uv run python scripts/dev/label_worktree.py <issue>...` "
        'or `--text "<what you are doing>"`.'
    ]


# The 15s hook budget less the `uv run` start; a probe that chains past it prints `did not finish`
# and the others still print.
TOTAL_DEADLINE_S = 10


PROBE = "uv run python scripts/diagnostics/probe.py"


def format_banner(targets=(), parked=(), master_moved=()):
    """Render the problem sections as the banner's lines ([] => print nothing).

    The parked-checkout lines come before the branch line, because "this branch is behind
    origin/master" is the SYMPTOM of a parked `homelab-checkout-sync`, so the cause reads first.

    The triage line names whole commands joined by `or`, so a line copied into a shell is
    one runnable command rather than a pipe. It names only the commands that can see a
    problem the banner printed: `landing` for a parked-checkout line, `targets` and `health` for a
    down scrape target. A banner holding only the branch line gets no triage line, because
    neither command says anything about it.
    """
    problems = [*targets, *parked, *master_moved]
    if not problems:
        return []
    commands = []
    if parked:
        commands.append(f"{PROBE} landing")
    if targets:
        commands += [f"{PROBE} targets", f"{PROBE} health <svc>"]
    out = ["Homelab health check found issues:", *problems]
    if commands:
        out.append("  → triage: " + " or ".join(f"`{c}`" for c in commands))
    return out


# Claude Code shows the session only a 2 KB preview of a longer SessionStart output.
BANNER_BUDGET_BYTES = 2000


def _tag_first(lines):
    """`lines` with the `[session-health] ` tag on the first, so each message names its hook."""
    return [tagged("session-health", lines[0]), *lines[1:]] if lines else []


def fit_to_budget(lines, budget=BANNER_BUDGET_BYTES):
    """The leading `lines` that fit in `budget` bytes, then one line counting the rest."""
    if sum(len(line.encode()) + 1 for line in lines) <= budget:
        return lines
    tail = f"  … {{}} more line(s) cut at the banner's {budget}-byte budget"
    room = budget - len(tail.format(len(lines)).encode()) - 1
    kept = []
    for line in lines:
        size = len(line.encode()) + 1
        if size > room:
            break
        kept.append(line)
        room -= size
    return [*kept, tail.format(len(lines) - len(kept))]


def main(
    *,
    read_payload=read_payload,
    target_problems=target_problems,
    master_moved_problems=master_moved_problems,
    dirty_primary_problems=dirty_primary_problems,
    other_live_sessions=other_live_sessions,
    unlabelled_worktree_lines=unlabelled_worktree_lines,
    stale_worktree_lines=stale_worktree_lines,
    remote_fanout_lines=remote_fanout_lines,
    deadline=TOTAL_DEADLINE_S,
):
    """Print the SessionStart health banner for a genuine session open, then exit 0.

    Prints nothing for a mid-session compaction. Combines Prometheus-target, parked-checkout and
    stale-branch problems into one banner, then adds the unlabelled-worktree prompt and the
    other live sessions in this repo to the agent's context, cut to `BANNER_BUDGET_BYTES` so
    what asks for an action comes before what only informs. Worktrees ready to remove and
    fan-out worktrees running on another host go to the operator's `systemMessage`. Each
    section prints regardless of health status.

    Every keyword argument is one of this module's own probes, and its default is that
    probe. A test passes a fake instead of monkeypatching the module attribute: the
    monkeypatch ratchet (tests/_ratchet.py) caps a test module at zero patches on a
    first-party module, and each probe reads live state (stdin, this checkout's git, the
    machine's worktrees, `.claude/fanout`) that would make an assertion depend on the host.
    `deadline` is the seconds all of them together may take; a test passes a short one.
    """
    event = read_payload()
    compact = event is not None and event.raw.get("source") == "compact"
    try:
        # Imported here so a broken module costs these rows, never the banner.
        from hooklib.instruction_rows import log_unevented_loads

        if event is not None:
            log_unevented_loads(event, "compact" if compact else "session_start")
    except Exception:  # noqa: S110 -- a failed row must never cost the banner
        pass
    # Don't re-banner on mid-session compaction — only on a genuine open/resume/clear.
    if compact:
        return 0

    # Every probe at once under one deadline: run in turn, their caps summed past the hook's budget
    # and the harness's kill took the whole banner.
    cwd = os.getcwd()
    (
        targets,
        master_moved,
        parked,
        sessions,
        unlabelled,
        worktrees,
        fanout,
    ) = run_sections(
        [
            ("the scrape-target check", target_problems),
            ("the behind-master check", master_moved_problems),
            ("the dirty-primary check", dirty_primary_problems),
            ("the other-session scan", lambda: other_live_sessions(cwd)),
            ("the worktree-label check", lambda: unlabelled_worktree_lines(cwd)),
            ("the removable-worktree survey", lambda: stale_worktree_lines(REPO)),
            ("the remote fan-out read", remote_fanout_lines),
        ],
        deadline,
    )
    # What the agent acts on: health, the unlabelled-worktree prompt, then other sessions.
    context = format_banner(targets, parked, master_moved)
    if not context and os.environ.get("SESSION_HEALTH_VERBOSE"):
        context = ["Homelab health: all scrape targets up."]
    # Lines asking for an action first, information last, so the budget cuts information.
    context += unlabelled
    if sessions:
        context += ["Other Claude sessions in this repo:", *sessions]

    # Operator housekeeping goes to `systemMessage`, which the model never reads.
    print_hook_output(
        fit_to_budget(_tag_first(context)), _tag_first(worktrees + fanout)
    )
    return 0


def print_hook_output(context, operator):
    """Print SessionStart's JSON form, or nothing when both line lists are empty."""
    if not context and not operator:
        return
    out = {"hookSpecificOutput": {"hookEventName": "SessionStart"}}
    if context:
        out["hookSpecificOutput"]["additionalContext"] = "\n".join(context)
    if operator:
        out["systemMessage"] = "\n".join(operator)
    print(json.dumps(out, ensure_ascii=False))
    sys.stdout.flush()


def dead_project_lines(projects=None):
    """A line per coordinator project with no live session, naming the batches that wait on it.

    `scripts/dev/fanout_lib/coordinators.py:projects` is the read, and the server lander
    pages from the same one. `projects` is that function; a test passes a fake.
    """
    sys.path.insert(0, os.path.join(REPO, "scripts"))
    try:
        from dev.fanout_lib import coordinators

        return coordinators.dead_lines((projects or coordinators.projects)())
    except Exception as exc:
        return [f"  ⚠ coordinator-project detection is broken: {exc}"]


def print_all_sessions(dead_project_lines=dead_project_lines):
    """Print every other live session with its changed paths, for `--sessions`.

    Then the coordinator projects no session holds, which the list above leaves out.
    """
    for line in other_live_sessions(os.getcwd(), show_all=True):
        print(line)
    for line in dead_project_lines():
        print(line)
    return 0


if __name__ == "__main__":
    if any(arg in ("-h", "--help") for arg in sys.argv[1:]):
        print(__doc__.strip())
        sys.exit(0)
    sys.exit(print_all_sessions() if "--sessions" in sys.argv[1:] else main())
