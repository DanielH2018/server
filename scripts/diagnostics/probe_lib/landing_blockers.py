"""`probe.py landing` — what would stop a landing now, and who else is working the repo.

CLAUDE.md's *When to wait* list is checked by hand, and the SessionStart banner shows the
state behind it once. This subcommand reads that state on demand, as text or, with `--json`,
as one JSON document.

Every fact comes from a reader that already exists; this module only joins them:

- **Master CI.** The newest `ci.yml` run on master, through `gh`. A run that
  `ci_verdict.WorkflowRun.no_verdict` admits is no verdict, and a run in progress is pending.
  Neither is red, per `docs/landing.md`.
- **Runs in flight.** deploy-ui's `/api/inflight`: every `land.sh` family on
  daniel-box. A run's `VERDICT:` line comes from `land_lib.detach.verdict_in` where
  its log is readable on this host, and from deploy-ui's `/api/log` tail elsewhere. That
  endpoint serves only the logs of runs deploy-ui started, so a terminal run's verdict shows
  only on daniel-box.
- **The landing queue.** The same read's `landings` and `turn`, as deploy-ui answers them:
  each landing's state from its state record, on either host, in queue order, and the turn,
  serial and slot locks. The text view prints one line per landing.
- **Last verdict.** The newest `land*.log` in `land_lib.detach.default_log_dir()`, as this
  process sees it: `$CLAUDE_JOB_DIR/tmp` when that variable is set, otherwise
  `/tmp/homelab-landings-<user>`. A log with no `VERDICT:` line reports its last `land:`
  line, which is where a landing-policy refusal writes its reason.
- **Promotion (Flux-owned tags).** `DEPLOY_HOLD` from one `gh variable get`; `prod` from the
  OCIRepository `homelab`'s `status.artifact`, through the read-only `kubectl`; `last-good` from
  one GHCR manifest fetch, which costs a token request and one GET; and every suspended
  Kustomization and OCIRepository with the field manager of its `spec.suspend`, from a second
  read-only `kubectl` read (D5.1b). That is one `api.github.com` call, two `ghcr.io` requests
  and two cluster reads per run (docs/archive/deploy/d2-first-leaf.md, *What
  `probe.py landing` reads*). A failed read leaves its own field `null`, and the text view then
  never reports `prod` equal to `last-good`. `run_landing` passes the live reads; `collect`
  called without them reads none.
- **Worktrees and claims.** `git worktree list` and `findings.py claims --json`. A claim
  joins a worktree on its branch, or on the issue numbers a fan-out batch worktree's name
  carries (`fanout_lib.launch.worktree_path`: `fanout-<n>-<n>`), because a fan-out batch's
  issues are claimed under the orchestrator's branch, not the batch's.

- **The serial landing.** `land_lib.serial_lock.serial_landing`: whether a landing holds the
  serial lock on daniel-box, and which PR's. On daniel-box it reads `/proc/locks`; on
  daniel-server it is one ssh call. The text view names it and marks its run in flight, but
  it is no blocker: `land.sh` waits in line behind it (`land_lib/slots.py:_bar`), as it
  waits on a pending master CI.

`blockers` is the list a landing would stop on: a `DEPLOY_HOLD` other than `none` or a red
master CI. It is computed here rather than in the mod so that the band, the model's
system-prompt section and this command's text view cannot disagree. A blocker line carries no
age or other value that moves each minute: the mod puts the list in the system prompt, and a
line that changed every read would rewrite the prompt on every turn. Because that prompt is
every session's, a line interpolates only a value with a closed shape, such as a hex SHA or a
setup role this checkout has. The held planes are free text from deploy-ui, so the line counts
them and the text view lists them. A source that fails is
named in `errors` and leaves its own field `null`; it never fails the whole read.

Exit code: 0 when nothing blocks a landing, 1 when something does.
"""

import functools
import json
import re
import socket
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from collections.abc import Callable
from pathlib import Path

from deploy_tools.land_lib.detach import (
    _VERDICT_LINE,
    default_log_dir,
    error_in,
    verdict_in,
)
from deploy_tools.land_lib.serial_lock import serial_landing
from lib.inventory_lists import LANDING_HOST, host_vars_file
from lib.repo_paths import REPO

from diagnostics.probe_lib.promote_state import (
    LIVE_PROMOTE_READS,
    PromoteReads,
    hold_lines,
)

# The host-side verdict logic, so the CI rule is the builder's and not a copy.
from lib import host_modules  # noqa: F401
from ci_verdict import WorkflowRun

# Its ufw rules admit every node IP, so a session on daniel-server can read it.
DEPLOY_UI_PORT = 8790
HTTP_TIMEOUT_S = 5
SUBPROCESS_TIMEOUT_S = 30


def ci_state(run: WorkflowRun | None) -> str:
    """Reduce the newest master `ci.yml` run to `green`, `red`, `pending` or `no-verdict`.

    `None` (no run listed) is `pending`, because an empty list is never green
    (`docs/landing.md`). Only a completed run with a real failing conclusion is `red`.
    """
    if run is None or run.status != "completed":
        return "pending"
    if run.conclusion == "success":
        return "green"
    if run.no_verdict:
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


# An agent reads blocker lines as instructions, so a line interpolates only a value checked against
# a closed shape.
_RUN_URL = re.compile(
    r"https://github\.com/[\w.-]{1,100}/[\w.-]{1,100}/actions/runs/\d{1,20}"
    r"(/attempts/\d{1,5})?",
    re.ASCII,
)
_PR_NUMBER = re.compile(r"\d{1,9}", re.ASCII)
UNSAFE_TOKEN = "<entry not shown>"


def _checked(value, shape: Callable[[str], object]) -> str:
    """`value` where it is a string `shape` returns a truthy result for, else `UNSAFE_TOKEN`."""
    return value if isinstance(value, str) and shape(value) else UNSAFE_TOKEN


def blockers(snap: dict) -> list[str]:
    """One line per condition in CLAUDE.md's *When to wait* that this snapshot shows.

    A field that is `null` (unread) adds nothing: the mod gates nothing, and a band that cried
    "blocked" every time a source timed out would be read past. `DEPLOY_HOLD` is free text
    from GitHub, so the line says it is set and `probe.py landing` names the value.

    Args:
        snap: the snapshot `collect` builds.
    """
    out = []
    if (snap.get("promote") or {}).get("deploy_hold") not in (None, "", "none"):
        out.append("DEPLOY_HOLD is set; `probe.py landing` names it")
    if (snap.get("ci") or {}).get("state") == "red":
        url = snap["ci"].get("url", "")
        out.append(
            f"master CI is red: {_checked(url, _RUN_URL.fullmatch) if url else ''}".rstrip(
                ": "
            )
        )
    return out


def serial_state(pr: str | None) -> dict:
    """`serial_lock.serial_landing`'s answer as the snapshot's `serial` field."""
    return {"live": pr is not None, "pr": pr or ""}


@functools.cache
def deploy_ui_url(host_vars: Path | None = None) -> str:
    """The base URL of deploy-ui, addressed by the GitOps host's `server_ip`.

    The address is the IP, not the hostname. The unit binds `DEPLOY_UI_BIND={{ server_ip }}`
    only, and on daniel-box itself `/etc/hosts` resolves `daniel-box` to 127.0.1.1, where
    nothing listens. The IP is read rather than typed because host_vars is its one source
    (`tests/repo/docs/test_lan_facts_have_one_source.py`).

    Args:
        host_vars: the host_vars file to read; the GitOps host's when omitted.
    """
    # Call-time: the `landing-clear` cc-wait source imports this module outside the venv.
    from lib import yaml_fast

    data = yaml_fast.safe_load((host_vars or host_vars_file(LANDING_HOST)).read_text())
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


def _last_verdict(log_dir: Path | None = None) -> dict | None:
    """The newest `land*.log` in this session's landing-log directory, with its verdict.

    A stop that names no verdict (a landing-policy refusal) reports its `land:` line instead.
    """
    base = log_dir or default_log_dir()
    logs = sorted(base.glob("land*.log"), key=lambda p: p.stat().st_mtime)
    if not logs:
        return None
    return {"log": str(logs[-1]), "verdict": verdict_in(logs[-1]) or error_in(logs[-1])}


def collect(
    get_json: Callable[[str], dict] = _get_json,
    run: Callable[[list[str]], str] = _run,
    get_text: Callable[[str], str] = _get_text,
    promote: PromoteReads | None = None,
    serial: Callable[[], str | None] | None = None,
) -> dict:
    """Read every source once. A source that fails is named in `errors`, its field `null`.

    Args:
        get_json: GETs one deploy-ui API path and parses it.
        run: runs an argv in the repo and answers its stdout.
        get_text: GETs one deploy-ui API path as text, for `/api/log`.
        promote: the promotion reads. None reads none of them and leaves each field `null`.
        serial: reads the PR whose landing holds the serial lock, as
            `serial_lock.serial_landing` answers. None reads nothing and leaves `serial`
            `null`.
    """
    snap: dict = {
        "host": socket.gethostname(),
        "read_at": int(time.time()),
        "ci": None,
        "runs": None,
        "last_verdict": None,
        "serial": None,
        "worktrees": None,
        "claims": None,
        "promote": {
            "deploy_hold": None,
            "prod": None,
            "last_good": None,
            "suspended": None,
            "reconcile_disabled": None,
        },
        "errors": [],
        "landings": None,
        "turn": None,
    }

    def attempt(name, fn):
        # One failed source must not hide the others.
        try:
            fn()
        except Exception as exc:
            snap["errors"].append(f"{name}: {type(exc).__name__}: {exc}")

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
        newest = WorkflowRun.parse(runs[0]) if runs else None
        snap["ci"] = {
            "state": ci_state(newest),
            "sha": newest.head_sha[:8] if newest else "",
            "url": newest.url if newest else "",
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
        inflight = get_json("/api/inflight")
        rows = inflight.get("runs", [])
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
        snap["landings"] = inflight.get("landings", [])
        snap["turn"] = inflight.get("turn")

    def read_last_verdict():
        snap["last_verdict"] = _last_verdict()

    def read_sessions():
        trees = parse_worktrees(run(["git", "worktree", "list", "--porcelain"]))
        claims = json.loads(
            run([sys.executable, "scripts/dev/findings.py", "claims", "--json"])
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
        read_objects = promote.suspended
        if read_objects is not None:

            def read_suspended():
                snap["promote"]["suspended"] = read_objects()

            attempt("suspended", read_suspended)
        read_annotated = promote.reconcile_disabled
        if read_annotated is not None:

            def read_reconcile_disabled():
                snap["promote"]["reconcile_disabled"] = read_annotated()

            attempt("reconcile_disabled", read_reconcile_disabled)
    if serial is not None:

        def read_serial():
            snap["serial"] = serial_state(serial())

        attempt("serial", read_serial)
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


def worktree_lines(worktrees: list[dict], blocked: bool) -> list[str]:
    """The worktrees holding a claim while something blocks, then a count of the rest.

    A blocker raises the question of whose change it is, which the claims answer. No blocker
    names a worktree, so a claim is the one tie the snapshot carries.
    """
    claimed = [t for t in worktrees if t["claims"]] if blocked else []
    lines = ["Worktrees holding a claim:"] if claimed else []
    for t in claimed:
        nums = " ".join(f"#{n}" for n in t["claims"])
        lines.append(f"  {t['branch'] or t['path']}  {nums}")
    others = len(worktrees) - len(claimed)
    if others:
        lead = f"{others} other" if claimed else str(others)
        lines.append(f"{lead} worktree(s); `probe.py landing --json` lists them.")
    return lines


_QUEUE_WORDS = {"turn": "holds the turn", "stepped-out": "stepped out"}


def queue_line(row: dict) -> str:
    """One landing of deploy-ui's queue: its PR, state, place in the queue and progress."""
    if row["alive"] is False:
        where = "died"
    elif row["alive"] is None:
        where = "liveness unknown"
    elif row["queue"] == "waiting":
        where = f"waiting #{row['position']}"
    else:
        where = _QUEUE_WORDS.get(row["queue"], "")
    progress = row["progress"]
    parts = [
        f"PR {row['pr']}",
        row["label"],
        f"({where})" if where else "",
        f"{row['in_state_s']}s",
        f"{progress['done']}/{progress['total']} {progress['what']}"
        if progress
        else "",
    ]
    return " ".join(p for p in parts if p)


def format_text(snap: dict) -> str:
    """The snapshot as lines for a terminal, one blank line between sections."""

    def known(value, render):
        return "unknown" if value is None else render(value)

    sections = [
        [f"{len(snap['blockers'])} landing blocker(s)"]
        + [f"  ✗ {b}" for b in snap["blockers"]]
    ]
    state = [
        line
        for line in hold_lines(snap)
        if line.startswith("prod:") or not line.endswith(": none")
    ]
    if snap["ci"] is None or snap["ci"]["state"] != "green":
        state.append(
            "master CI: "
            + known(snap["ci"], lambda c: f"{c['state']} {c['sha']}".strip())
        )
    serial = snap.get("serial")
    if serial and serial["live"]:
        who = f"PR #{serial['pr']}'s" if _PR_NUMBER.fullmatch(serial["pr"]) else "a"
        state.append(
            f"{who} serial landing is live; `land.sh` waits in line until it ends"
        )
    sections.append(state)
    if snap["runs"]:
        in_flight = ["In flight:"]
        serial_pr = (snap.get("serial") or {}).get("pr")
        for r in snap["runs"]:
            what = f"PR {r['pr']}" if r["pr"] else r["tag"][:60]
            if r["pr"] and str(r["pr"]) == serial_pr:
                what += " (serial)"
            in_flight.append(
                f"  {r['kind']} {what} {r['elapsed_s']}s  {r['verdict'] or ''}".rstrip()
            )
        sections.append(in_flight)
    if snap["landings"]:
        sections.append(
            ["Landing queue:"] + [f"  {queue_line(r)}" for r in snap["landings"]]
        )
    if snap["last_verdict"]:
        sections.append(
            [f"Last landing: {snap['last_verdict']['verdict'] or 'no VERDICT line'}"]
        )
    if snap["worktrees"]:
        sections.append(worktree_lines(snap["worktrees"], bool(snap["blockers"])))
    tail = [
        f"Claim no worktree here works: #{c['number']} {c['reason']}".rstrip()
        for c in unmatched_claims(snap)
    ]
    tail += [f"error: {e}" for e in snap["errors"]]
    if tail:
        sections.append(tail)
    return "\n\n".join("\n".join(section) for section in sections if section)


def run_landing(ns, collect=collect) -> int:
    snap = collect(
        promote=LIVE_PROMOTE_READS,
        serial=lambda: serial_landing(socket.gethostname()),
    )
    print(json.dumps(snap) if ns.json else format_text(snap))
    return 1 if snap["blockers"] else 0
