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
  worded by `deployer_park.manual_plane_lines`, the banner's renderer. The text view prints
  each role's apply and clear commands on lines of their own, from
  `deployer_park.manual_plane_remedies`, so each pastes as it stands. deploy-ui serves that
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
  `/tmp/homelab-landings-<user>`. A log with no `VERDICT:` line reports its last `land:`
  line, which is where a landing-policy refusal writes its reason.
- **Promotion (Flux-owned tags).** `DEPLOY_HOLD` from one `gh variable get`; `prod` from the
  OCIRepository `homelab`'s `status.artifact`, through the read-only `kubectl`; `last-good` from
  one GHCR manifest fetch, which costs a token request and one GET. That is one
  `api.github.com` call, two `ghcr.io` requests and one cluster read per run
  (docs/target-architecture/deploy/d2-first-leaf.md, *What `probe.py landing` reads*). A
  failed read leaves its own field `null`, and the text view then never reports `prod` equal
  to `last-good`. `run_landing` passes the live reads; `collect` called without them reads none.
- **Worktrees and claims.** `git worktree list` and `findings.py claims --json`. A claim
  joins a worktree on its branch, or on the issue numbers a fan-out batch worktree's name
  carries (`fanout_lib.launch.worktree_path`: `fanout-<n>-<n>`), because a fan-out batch's
  issues are claimed under the orchestrator's branch, not the batch's.

`blockers` is the list a landing would stop on: a non-empty hold, a red master CI, or an owed
manual plane. It is computed here rather than in the mod so that the band, the model's
system-prompt section and this command's text view cannot disagree. A blocker line carries no
age or other value that moves each minute: the mod puts the list in the system prompt, and a
line that changed every read would rewrite the prompt on every turn. Because that prompt is
every session's, a line interpolates only a value with a closed shape, such as a hex SHA or a
setup role this checkout has. The held planes are free text from deploy-ui, so the line counts
them and the text view lists them. A source that fails is
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

import functools
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

from land_lib.detach import _VERDICT_LINE, default_log_dir, error_in, verdict_in
from lib import deployer_park, yaml_fast
from lib.ansible_inventory import GITOPS_HOST, host_vars_file
from lib.repo_paths import GITOPS_DEPLOY_FILES, REPO, ROLES

from diagnostics.probe_lib.promote_state import (
    LIVE_PROMOTE_READS,
    PromoteReads,
    hold_lines,
)

# The deployer's own modules, so the CI and ledger rules are its rules and not a copy.
_sys.path.insert(0, str(GITOPS_DEPLOY_FILES))
from deployer_lib.deploy_git import _CI_NO_VERDICT_CONCLUSIONS
from gitops_hold import DeployerSnapshot
from gitops_ledger import held_planes

# deploy-ui's daemon on the deployer's host: `deploy_ui_port` in
# `ansible/roles/setup/deploy_ui/defaults/main.yml`. Its ufw rule admits every node IP, which
# is what lets a session on daniel-server read it.
DEPLOY_UI_PORT = 8790
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


# The blocker lines go into every deck session's system prompt (#3793), and off the deployer's
# host the hold and the owed roles come from deploy-ui's `/api/state`. So a line interpolates
# only a value checked against a closed shape: a hex SHA, a setup role this checkout has, a
# GitHub Actions run URL. A free-text field, such as a held plane's tags, is counted instead.
_HEX_SHA = re.compile(r"[0-9a-f]{1,8}")
_RUN_URL = re.compile(
    r"https://github\.com/[\w.-]{1,100}/[\w.-]{1,100}/actions/runs/\d{1,20}"
    r"(/attempts/\d{1,5})?",
    re.ASCII,
)
UNSAFE_TOKEN = "<entry not shown>"


def setup_roles() -> frozenset[str]:
    """The setup roles this checkout carries: every directory under `ansible/roles/setup/`."""
    return frozenset(p.name for p in (ROLES / "setup").iterdir() if p.is_dir())


def _checked(value, shape: Callable[[str], object]) -> str:
    """`value` where it is a string `shape` returns a truthy result for, else `UNSAFE_TOKEN`."""
    return value if isinstance(value, str) and shape(value) else UNSAFE_TOKEN


def blockers(snap: dict, roles: frozenset[str] | None = None) -> list[str]:
    """One line per condition in CLAUDE.md's *When to wait* that this snapshot shows.

    A field that is `null` (unread) adds nothing: the mod gates nothing, and a band that cried
    "blocked" every time a source timed out would be read past.

    Args:
        snap: the snapshot `collect` builds.
        roles: the setup role names a manual-plane role must be one of. Defaults to
          `setup_roles()`.
    """
    roles = setup_roles() if roles is None else roles
    out = []
    hold = snap.get("hold")
    if hold and hold.get("sha"):
        count = len(hold.get("planes") or [])
        out.append(
            f"hold_sha is set ({_checked(hold['sha'][:8], _HEX_SHA.fullmatch)})"
            + (
                f", waiting on {count} plane{'s' if count != 1 else ''}; "
                "`probe.py landing` names them"
                if count
                else ""
            )
        )
    if (snap.get("ci") or {}).get("state") == "red":
        url = snap["ci"].get("url", "")
        out.append(
            f"master CI is red: {_checked(url, _RUN_URL.fullmatch) if url else ''}".rstrip(
                ": "
            )
        )
    for owed in snap.get("manual_planes") or []:
        out.append(
            f"the `{_checked(owed['role'], roles.__contains__)}` setup role is merged and "
            "unapplied; `probe.py landing` names the apply"
        )
    return out


def manual_plane_rows(owed: str | None, now: float) -> list[dict]:
    """Each owed `manual_plane` role with the banner's line for it and its parts, oldest first.

    `line` comes from `deployer_park.manual_plane_lines` and carries the role's age and its way
    out; the deck mod renders it. The other fields are that line's parts, from
    `deployer_park.manual_plane_remedies`. Both walk the same entries in the same order, so
    the two zip.
    """
    remedies = deployer_park.manual_plane_remedies(owed, now)
    lines = deployer_park.manual_plane_lines(owed, now)
    return [
        {
            "role": r.role,
            "line": line.strip().removeprefix("✗ ").strip(),
            "age": r.age,
            "apply": r.apply,
            "by_hand": r.by_hand,
            "warning": r.warning,
            "clear": r.clear,
        }
        for r, line in zip(remedies, lines, strict=True)
    ]


def _owed_locally(
    state_dir: str = deployer_park.GITOPS_STATE_DIR,
) -> tuple[str | None, str | None] | None:
    """`(hold_sha, owed ledger text)` from the deployer's state dir, or None if unreadable.

    The directory is 0750 and owned by the deploy user, so another user on the deployer's host
    (the `claude` agent user) sees it exist and still cannot read a marker. `DeployerSnapshot`
    raises on that EACCES rather than reading it as no hold and nothing owed, and None sends
    the caller to deploy-ui instead.
    """
    if not os.path.isdir(state_dir):
        return None
    try:
        snap = DeployerSnapshot.load(state_dir)
    except OSError, UnicodeDecodeError:
        return None
    return snap.hold, snap.owed


@functools.cache
def deploy_ui_url(host_vars: Path | None = None) -> str:
    """The base URL of deploy-ui, addressed by the GitOps host's `server_ip`.

    The address is the IP, not the hostname. The unit binds `DEPLOY_UI_BIND={{ server_ip }}`
    only, and on daniel-box itself `/etc/hosts` resolves `daniel-box` to 127.0.1.1, where
    nothing listens. The IP is read rather than typed because host_vars is its one source
    (`ansible/tests/repo/test_lan_facts_have_one_source.py`).

    Args:
        host_vars: the host_vars file to read; the GitOps host's when omitted.
    """
    data = yaml_fast.safe_load((host_vars or host_vars_file(GITOPS_HOST)).read_text())
    return f"http://{data['server_ip']}:{DEPLOY_UI_PORT}"


def _get_text(path: str) -> str:
    with urllib.request.urlopen(deploy_ui_url() + path, timeout=HTTP_TIMEOUT_S) as resp:
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


def _last_verdict(log_dir: _Path | None = None) -> dict | None:
    """The newest `land*.log` in this session's landing-log directory, with its verdict.

    A stop that names no verdict (a landing-policy refusal) reports its `land:` line instead.
    """
    base = log_dir or default_log_dir()
    logs = sorted(base.glob("land*.log"), key=lambda p: p.stat().st_mtime)
    if not logs:
        return None
    return {"log": str(logs[-1]), "verdict": verdict_in(logs[-1]) or error_in(logs[-1])}


def collect(
    owed_locally: Callable[[], tuple[str | None, str | None] | None] = _owed_locally,
    get_json: Callable[[str], dict] = _get_json,
    run: Callable[[list[str]], str] = _run,
    get_text: Callable[[str], str] = _get_text,
    promote: PromoteReads | None = None,
) -> dict:
    """Read every source once. A source that fails is named in `errors`, its field `null`.

    Args:
        owed_locally: reads this host's deployer markers, or answers None off that host.
        get_json: GETs one deploy-ui API path and parses it.
        run: runs an argv in the repo and answers its stdout.
        get_text: GETs one deploy-ui API path as text, for `/api/log`.
        promote: the promotion reads. None reads none of them and leaves each field `null`.
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
        "promote": {"deploy_hold": None, "prod": None, "last_good": None},
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

    if promote is not None:
        for name in ("deploy_hold", "prod", "last_good"):

            def read(name=name):
                snap["promote"][name] = getattr(promote, name)()
                if name != "last_good" or snap["promote"][name] is not None:
                    return
                # Absent, as opposed to unread: no verified promotion has seeded it yet.
                snap["promote"][name] = {"commit": "", "digest": ""}

            attempt(name, read)
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


def owed_lines(row: dict) -> list[str]:
    """One owed manual plane as lines for a terminal, each command alone on its line.

    A command line holds nothing but the command, so a triple-click copies a line that runs
    as pasted. A warning goes above the apply it is about, so it is read before the paste.
    """
    lines = [
        f"Owed: the `{row['role']}` setup role, merged {row['age']} ago. "
        "The GitOps deployer cannot apply it itself."
    ]
    if row["apply"]:
        if row["warning"]:
            lines.append(f"  WARNING: {row['warning']}")
        lines += ["  Apply it by hand:", f"    {row['apply']}"]
    else:
        lines.append(f"  {row['by_hand'][:1].upper()}{row['by_hand'][1:]}.")
    lines += ["  Then clear its ledger line:", f"    {row['clear']}"]
    return lines


def format_text(snap: dict) -> str:
    """The snapshot as lines for a terminal, one blank line between sections.

    The owed manual planes follow the status line that counts them, ahead of the runs in
    flight, so no owed line reads as a run.
    """

    def known(value, render):
        return "unknown" if value is None else render(value)

    lines = ["Landing blockers:"]
    lines += [f"  ✗ {b}" for b in snap["blockers"]] or ["  none"]
    lines += ["", *hold_lines(snap)]
    lines += [
        "master CI: "
        + known(snap["ci"], lambda c: f"{c['state']} {c['sha']}".strip())
        + "   manual planes: "
        + known(snap["manual_planes"], lambda m: str(len(m))),
    ]
    if snap["hold"] and snap["hold"]["planes"]:
        lines.append("held planes: " + "; ".join(map(str, snap["hold"]["planes"])))
    for owed in snap["manual_planes"] or []:
        lines += ["", *owed_lines(owed)]
    if snap["runs"]:
        lines += ["", "In flight:"]
        for r in snap["runs"]:
            what = f"PR {r['pr']}" if r["pr"] else r["tag"][:60]
            lines.append(
                f"  {r['kind']} {what} {r['elapsed_s']}s  {r['verdict'] or ''}".rstrip()
            )
    if snap["last_verdict"]:
        lines += [
            "",
            f"Last landing: {snap['last_verdict']['verdict'] or 'no VERDICT line'}",
        ]
    if snap["worktrees"]:
        lines += ["", "Worktrees:"]
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
    snap = collect(promote=LIVE_PROMOTE_READS)
    print(json.dumps(snap) if ns.json else format_text(snap))
    return 1 if snap["blockers"] else 0
