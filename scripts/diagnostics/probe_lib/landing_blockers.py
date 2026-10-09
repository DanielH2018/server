"""`probe.py landing` — what would stop a landing now, and who else is working the repo.

CLAUDE.md's *When to wait* list is checked by hand, and the SessionStart banner shows the
state behind it once. This subcommand reads that state on demand, as one JSON document with
`--json`, so the `deck` mod in `.claude/plugins/deck/` can redraw it every minute (#3676).

Every fact comes from a reader that already exists; this module only joins them:

- **Hold.** Where this user can read the deployer's state directory, `hold_sha` and the
  `owed` ledger are read directly, through `gitops_markers` and `gitops_ledger`. Anywhere else,
  including a user on the deployer's host the directory's mode shuts out, deploy-ui's
  `/api/state` serves the same two through those same modules.
- **Manual planes.** Read from the ledger through `gitops_ledger.manual_plane_entries`, and
  worded by `deployer_park.manual_plane_lines`, the banner's renderer. deploy-ui serves that
  class's raw ledger lines as `manual_plane_owed`, so both paths word them the same way. A
  deploy-ui that predates the key leaves the field `null` (unknown), never `[]`: an empty list
  would read as "nothing owed" when the truth is "not readable from here".
- **Master CI.** The newest `ci.yml` run on master, through `gh`. A conclusion in
  `deploy_git._CI_NO_VERDICT_CONCLUSIONS` is no verdict and a run in progress is pending.
  Neither is red, per `docs/landing.md`.
- **Runs in flight.** deploy-ui's `/api/inflight`: every `land.sh` and `deploy.sh` family on
  the deployer's host. A run's `VERDICT:` line comes from `land_lib.detach.verdict_in` where
  its log is readable on this host, and from deploy-ui's `/api/log` tail elsewhere. That
  endpoint serves only the logs of runs deploy-ui started, so a terminal run's verdict shows
  only on the deployer's host.
- **Last verdict.** The newest `land*.log` in `land_lib.detach.default_log_dir()`, as this
  process sees it: `$CLAUDE_JOB_DIR/tmp` when that variable is set, otherwise
  `/tmp/homelab-landings-<user>`.
- **Worktrees and claims.** `git worktree list` and `findings.py claims --json`. A claim
  joins a worktree on its branch, or on the issue numbers a fan-out batch worktree's name
  carries (`fanout_lib.launch.worktree_path`: `fanout-<n>-<n>`), because a fan-out batch's
  issues are claimed under the orchestrator's branch, not the batch's.

`blockers` is the list a landing would stop on: a non-empty hold, a red master CI, or an owed
manual plane. It is computed here rather than in the mod so that the band, the model's
system-prompt section and this command's text view cannot disagree. A blocker line carries no
age or other value that moves each minute: the mod puts the list in the system prompt, and a
line that changed every read would rewrite the prompt on every turn. A source that fails is
named in `errors` and leaves its own field `null`; it never fails the whole read.

Exit code: 0 when nothing blocks a landing, 1 when something does.
"""

# `probe_lib` is a namespace package under `scripts/`, so reaching a sibling by package name
# needs `scripts/` on sys.path — a module gets only its importer's path otherwise, and
# pyproject's `pythonpath` is a pytest setting. This has to sit ABOVE the imports below.
import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))
_sys.path.insert(0, str(_Path(__file__).resolve().parents[2] / "deploy_tools"))

import json
import os
import re
import socket
import subprocess
import time
import urllib.parse
import urllib.request
from collections.abc import Callable
from pathlib import Path

from land_lib.detach import _VERDICT_LINE, default_log_dir, verdict_in
from lib import deployer_park
from lib.repo_paths import GITOPS_DEPLOY_FILES, REPO

# The deployer's own modules, so the CI and ledger rules are its rules and not a copy.
_sys.path.insert(0, str(GITOPS_DEPLOY_FILES))
from deploy_git import _CI_NO_VERDICT_CONCLUSIONS
from gitops_ledger import held_planes, manual_plane_entries
from gitops_markers import MARKERS

# deploy-ui's daemon on the deployer's host: `deploy_ui_port` in
# `ansible/roles/setup/deploy_ui/defaults/main.yml`. Its ufw rule admits every node IP, which
# is what lets a session on daniel-server read it.
DEPLOY_UI_URL = "http://daniel-box:8790"
HTTP_TIMEOUT_S = 5
SUBPROCESS_TIMEOUT_S = 30


def ci_state(run: dict | None) -> str:
    """Reduce the newest master `ci.yml` run to `green`, `red`, `pending` or `no-verdict`.

    `None` (no run listed) is `pending`, because an empty list is never green
    (`docs/gitops-pipeline.md`). Only a completed run with a real failing conclusion is `red`.
    """
    if not run or run.get("status") != "completed":
        return "pending"
    conclusion = run.get("conclusion") or None
    if conclusion == "success":
        return "green"
    if conclusion in _CI_NO_VERDICT_CONCLUSIONS:
        return "no-verdict"
    return "red"


_BATCH_DIR = re.compile(r"^fanout-(\d+(?:-\d+)*)$")


def batch_issues(path: str) -> set[int]:
    """The issue numbers a fan-out batch worktree's directory name carries, or an empty set."""
    match = _BATCH_DIR.match(Path(path).name)
    return {int(n) for n in match.group(1).split("-")} if match else set()


def worktree_claims(tree: dict, claims: list[dict]) -> list[int]:
    """The claimed issues one worktree is working, ascending.

    A claim counts when it names the worktree's branch, or when its issue is one the
    worktree's batch name carries.
    """
    batch = batch_issues(tree["path"])
    return sorted(
        c["number"]
        for c in claims
        if (tree["branch"] and c["worktree"] == tree["branch"]) or c["number"] in batch
    )


def parse_worktrees(porcelain: str) -> list[dict]:
    """`git worktree list --porcelain` -> `[{path, branch}]`, the primary checkout included.

    A detached worktree's branch is `""`.
    """
    trees, cur = [], None
    for line in porcelain.splitlines():
        if line.startswith("worktree "):
            cur = {"path": line[len("worktree ") :], "branch": ""}
            trees.append(cur)
        elif line.startswith("branch ") and cur is not None:
            cur["branch"] = line[len("branch ") :].removeprefix("refs/heads/")
    return trees


_TOKEN = re.compile(r"[\w:./-]{1,200}", re.ASCII)
UNSAFE_TOKEN = "<entry not shown>"


def safe_token(value) -> str:
    """`value` where it is one short `[\\w:./-]` word, else `UNSAFE_TOKEN`.

    Every token a blocker line interpolates comes from deploy-ui's `/api/state` (plain HTTP,
    no authentication) or from `gh`, and the deck mod puts the line in every session's system
    prompt (#3793). Replacing the whole token, rather than its bad characters, keeps a prose
    payload from surviving as words joined by placeholders.
    """
    return value if isinstance(value, str) and _TOKEN.fullmatch(value) else UNSAFE_TOKEN


def blockers(snap: dict) -> list[str]:
    """One line per condition in CLAUDE.md's *When to wait* that this snapshot shows.

    A field that is `null` (unread) adds nothing: the mod gates nothing, and a band that cried
    "blocked" every time a source timed out would be read past. Each value from a source passes
    through `safe_token`, so a line carries only this function's own words and allowlisted
    tokens.
    """
    out = []
    hold = snap.get("hold")
    if hold and hold.get("sha"):
        planes = [safe_token(p) for p in hold.get("planes") or []]
        out.append(
            f"hold_sha is set ({safe_token(hold['sha'][:8])})"
            + (f", waiting on {', '.join(planes)}" if planes else "")
        )
    if (snap.get("ci") or {}).get("state") == "red":
        url = snap["ci"].get("url", "")
        out.append(f"master CI is red: {safe_token(url) if url else ''}".rstrip(": "))
    for owed in snap.get("manual_planes") or []:
        out.append(
            f"the `{safe_token(owed['role'])}` setup role is merged and unapplied; "
            "`probe.py landing` names the apply"
        )
    return out


def manual_plane_rows(owed: str | None, now: float) -> list[dict]:
    """Each owed `manual_plane` role with the banner's line for it, oldest first.

    The line comes from `deployer_park.manual_plane_lines`, which walks the same entries in the
    same order, so the two zip. The line carries the role's age and its way out.
    """
    entries, _ = manual_plane_entries(owed)
    lines = deployer_park.manual_plane_lines(owed, now)
    return [
        {"role": e.role, "line": line.strip().removeprefix("✗ ").strip()}
        for e, line in zip(entries, lines, strict=True)
    ]


def _owed_locally(
    state_dir: str = deployer_park.GITOPS_STATE_DIR,
) -> tuple[str | None, str | None] | None:
    """`(hold_sha, owed ledger text)` from the deployer's state dir, or None if unreadable.

    The directory is 0750 and owned by the deploy user, so another user on the deployer's host
    (the `claude` agent user) sees it exist and still cannot read a marker. `deployer_park`'s
    readers turn that EACCES into "absent", which would read as no hold and nothing owed. None
    sends the caller to deploy-ui instead.
    """
    if not os.path.isdir(state_dir) or not os.access(state_dir, os.R_OK | os.X_OK):
        return None
    markers = [Path(state_dir) / MARKERS[name] for name in ("hold", "owed")]
    if any(m.exists() and not os.access(m, os.R_OK) for m in markers):
        return None
    return (
        deployer_park._read(state_dir, MARKERS["hold"]),
        deployer_park.read_manual_plane_marker(state_dir),
    )


def _get_text(path: str) -> str:
    with urllib.request.urlopen(DEPLOY_UI_URL + path, timeout=HTTP_TIMEOUT_S) as resp:
        return resp.read().decode("utf-8", errors="replace")


def _get_json(path: str) -> dict:
    return json.loads(_get_text(path))


def _run(argv: list[str]) -> str:
    return subprocess.run(
        argv,
        capture_output=True,
        text=True,
        check=True,
        timeout=SUBPROCESS_TIMEOUT_S,
        cwd=REPO,
    ).stdout


def _last_verdict() -> dict | None:
    """The newest `land*.log` in this session's landing-log directory, with its verdict."""
    logs = sorted(default_log_dir().glob("land*.log"), key=lambda p: p.stat().st_mtime)
    if not logs:
        return None
    return {"log": str(logs[-1]), "verdict": verdict_in(logs[-1])}


def collect(
    owed_locally: Callable[[], tuple[str | None, str | None] | None] = _owed_locally,
    get_json: Callable[[str], dict] = _get_json,
    run: Callable[[list[str]], str] = _run,
    get_text: Callable[[str], str] = _get_text,
) -> dict:
    """Read every source once. A source that fails is named in `errors`, its field `null`.

    Args:
        owed_locally: reads this host's deployer markers, or answers None off that host.
        get_json: GETs one deploy-ui API path and parses it.
        run: runs an argv in the repo and answers its stdout.
        get_text: GETs one deploy-ui API path as text, for `/api/log`.
    """
    snap: dict = {
        "host": socket.gethostname(),
        "read_at": int(time.time()),
        "hold": None,
        "manual_planes": None,
        "ci": None,
        "runs": None,
        "last_verdict": None,
        "worktrees": None,
        "claims": None,
        "errors": [],
    }

    def attempt(name, fn):
        # One failed source must not hide the others.
        try:
            fn()
        except Exception as exc:
            snap["errors"].append(f"{name}: {type(exc).__name__}: {exc}")

    def read_hold():
        local = owed_locally()
        if local is not None:
            sha, owed = local
            snap["hold"] = {"sha": sha or "", "planes": held_planes(owed)}
            snap["manual_planes"] = manual_plane_rows(owed, time.time())
            return
        state = get_json("/api/state")
        snap["hold"] = {
            "sha": state.get("hold_sha", ""),
            "planes": state.get("hold_plane_entries", []),
        }
        if "manual_plane_owed" in state:
            snap["manual_planes"] = manual_plane_rows(
                state["manual_plane_owed"], time.time()
            )

    def read_ci():
        runs = json.loads(
            run(
                [
                    "gh",
                    "run",
                    "list",
                    "--branch",
                    "master",
                    "--workflow",
                    "ci.yml",
                    "--limit",
                    "1",
                    "--json",
                    "status,conclusion,headSha,url",
                ]
            )
        )
        newest = runs[0] if runs else None
        snap["ci"] = {
            "state": ci_state(newest),
            "sha": (newest or {}).get("headSha", "")[:8],
            "url": (newest or {}).get("url", ""),
        }

    def verdict_of(log: str) -> str | None:
        if not log:
            return None
        if Path(log).exists():
            return verdict_in(Path(log))
        try:
            tail = get_text("/api/log?path=" + urllib.parse.quote(log))
        except OSError:
            return None
        match = _VERDICT_LINE.search(tail)
        return match.group(0) if match else None

    def read_runs():
        rows = get_json("/api/inflight").get("runs", [])
        snap["runs"] = [
            {
                "kind": r.get("kind", ""),
                "pr": r.get("pr", ""),
                "tag": r.get("tag", ""),
                "elapsed_s": r.get("elapsed_s", 0),
                "verdict": verdict_of(r.get("log", "")),
            }
            for r in rows
        ]

    def read_last_verdict():
        snap["last_verdict"] = _last_verdict()

    def read_sessions():
        trees = parse_worktrees(run(["git", "worktree", "list", "--porcelain"]))
        claims = json.loads(
            run([_sys.executable, "scripts/dev/findings.py", "claims", "--json"])
        )
        snap["worktrees"] = [
            {**t, "claims": worktree_claims(t, claims)}
            for t in trees
            if "/.claude/worktrees/" in t["path"]
        ]
        snap["claims"] = claims

    attempt("hold", read_hold)
    attempt("ci", read_ci)
    attempt("runs", read_runs)
    attempt("last_verdict", read_last_verdict)
    attempt("sessions", read_sessions)
    snap["blockers"] = blockers(snap)
    return snap


def unmatched_claims(snap: dict) -> list[dict]:
    """The claims no listed worktree works, each still carrying findings.py's own reason."""
    held = {n for t in snap["worktrees"] or [] for n in t["claims"]}
    return [c for c in snap["claims"] or [] if c["number"] not in held]


def format_text(snap: dict) -> str:
    """The snapshot as lines for a terminal."""

    def known(value, render):
        return "unknown" if value is None else render(value)

    lines = ["Landing blockers:"]
    lines += [f"  ✗ {b}" for b in snap["blockers"]] or ["  none"]
    lines.append(
        "hold: "
        + known(snap["hold"], lambda h: h["sha"][:8] or "none")
        + "   master CI: "
        + known(snap["ci"], lambda c: f"{c['state']} {c['sha']}".strip())
        + "   manual planes: "
        + known(snap["manual_planes"], lambda m: str(len(m)))
    )
    if snap["runs"]:
        lines.append("In flight:")
        for r in snap["runs"]:
            what = f"PR {r['pr']}" if r["pr"] else r["tag"][:60]
            lines.append(
                f"  {r['kind']} {what} {r['elapsed_s']}s  {r['verdict'] or ''}".rstrip()
            )
    for owed in snap["manual_planes"] or []:
        lines.append(f"  owed: {owed['line']}")
    if snap["last_verdict"]:
        lines.append(
            f"Last landing: {snap['last_verdict']['verdict'] or 'no VERDICT line'}"
        )
    if snap["worktrees"]:
        lines.append("Worktrees:")
        for t in snap["worktrees"]:
            nums = " ".join(f"#{n}" for n in t["claims"])
            lines.append(f"  {t['branch'] or t['path']}  {nums}".rstrip())
    for c in unmatched_claims(snap):
        lines.append(
            f"Claim no worktree here works: #{c['number']} {c['reason']}".rstrip()
        )
    lines += [f"error: {e}" for e in snap["errors"]]
    return "\n".join(lines)


def run_landing(ns) -> int:
    snap = collect()
    print(json.dumps(snap) if ns.json else format_text(snap))
    return 1 if snap["blockers"] else 0
