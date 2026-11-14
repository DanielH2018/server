"""Carry out the GitHub writes a session asked for, treating its request files as untrusted.

Guardrails plan B1, #967's I3. The Renovate session reads GitHub with a read-only token and
cannot close, comment on or re-run a PR itself. It writes one JSON file per request into
`REQUESTS_DIR` under the state directory instead, and the runner carries the requests out
with its own token once the session has ended:

    {"action": "close", "pr": 71, "body": "Superseded by #80."}
    {"action": "comment", "pr": 71, "body": "..."}
    {"action": "rerun", "pr": 71}
    {"action": "land", "pr": 71}

A `land` request nominates a PR; the session never lands one (#967's I9). After the other
requests, the runner starts `renovate-agent-land@<n>` for each nomination in file order, one at
a time, and grades how each ended. It goes on after `settled`, `nothing-to-deploy` or the
lander's own pre-merge `REFUSED:`, and starts no further landing after anything else. It drops a
nomination past the run's landing cap, one for a PR the run closed, and any once the run's
deadline has passed.

A request is read only when it is a regular file under `MAX_BYTES`, opened without following
a link and without blocking, so a symlink, a FIFO or a device node is refused rather than
read. A request for a PR outside the run's census of open Renovate PRs is dropped. Every
outcome, done or refused, is one line of `REQUEST_LOG`. Stdlib only, like the rest of the
agent.
"""

from __future__ import annotations

import errno
import json
import os
import shutil
import stat
import time
from collections.abc import Callable
from dataclasses import dataclass

from agent_toolbox import AgentTools

REQUESTS_DIR = "requests"
REQUEST_LOG = "requests.jsonl"
MAX_BYTES = 16 * 1024
MAX_REQUESTS = 50
MAX_BODY = 4000
ACTIONS = ("close", "comment", "rerun", "land")
VERDICT_DIR = "/var/lib/renovate-agent-land"
# land.sh's VERDICT: states after which the next nomination may land.
GOES_ON = ("settled", "nothing-to-deploy")


@dataclass(frozen=True)
class Outcome:
    """What became of one request file."""

    name: str
    done: bool
    detail: str
    action: str = ""
    pr: int = 0


def fresh_dir(state_dir: str) -> str:
    """An empty requests directory for the next session; a leftover one is set aside, not read."""
    path = os.path.join(state_dir, REQUESTS_DIR)
    os.makedirs(state_dir, exist_ok=True)
    if os.path.lexists(path):
        stale = f"{path}.stale-{int(time.time())}"
        os.rename(path, stale)
        shutil.rmtree(stale, ignore_errors=True)
    os.mkdir(path, 0o700)
    return path


def _read(dir_fd: int, name: str) -> bytes:
    """The bytes of regular file `name` in `dir_fd`, or raise ValueError naming why not."""
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dir_fd)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise ValueError("a symlink") from exc
        raise ValueError(f"cannot open it: {exc.strerror}") from exc
    try:
        mode = os.fstat(fd).st_mode
        if not stat.S_ISREG(mode):
            raise ValueError("not a regular file")
        data = os.read(fd, MAX_BYTES + 1)
    finally:
        os.close(fd)
    if len(data) > MAX_BYTES:
        raise ValueError(f"over {MAX_BYTES} bytes")
    return data


def parse(data: bytes, census: set[int]) -> dict:
    """The request in `data`, or raise ValueError naming what is wrong with it."""
    try:
        request = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("not JSON") from exc
    if not isinstance(request, dict):
        raise ValueError("not a JSON object")
    action, pr = request.get("action"), request.get("pr")
    if action not in ACTIONS:
        raise ValueError(f"action {action!r} is not one of {', '.join(ACTIONS)}")
    if not isinstance(pr, int) or isinstance(pr, bool) or pr not in census:
        raise ValueError(f"PR {pr!r} is not an open Renovate PR this run counted")
    body = request.get("body", "")
    if action in ("close", "comment"):
        if not isinstance(body, str) or not body.strip() and action == "comment":
            raise ValueError("a comment needs a non-empty string body")
        if len(body) > MAX_BODY:
            raise ValueError(f"its body is over {MAX_BODY} characters")
    return {"action": action, "pr": pr, "body": body}


def _carry_out(request: dict, repo: str, tools: AgentTools) -> tuple[bool, str]:
    pr, action = str(request["pr"]), request["action"]
    if action == "comment":
        argv = ["gh", "pr", "comment", pr, "--repo", repo, "--body", request["body"]]
    elif action == "close":
        argv = ["gh", "pr", "close", pr, "--repo", repo]
        if request["body"]:
            argv += ["--comment", request["body"]]
    else:
        return _rerun(pr, repo, tools)
    rc, out = tools.run(argv)
    return rc == 0, "done" if rc == 0 else f"gh exited {rc}: {out.strip()[-200:]}"


def _rerun(pr: str, repo: str, tools: AgentTools) -> tuple[bool, str]:
    """Re-run the failed workflow runs at the PR's head."""
    rc, out = tools.run(
        ["gh", "pr", "view", pr, "--repo", repo, "--json", "headRefOid"]
    )
    try:
        head = json.loads(out)["headRefOid"] if rc == 0 else ""
    except json.JSONDecodeError, KeyError, TypeError:
        head = ""
    if not head:
        return False, "cannot read the PR's head"
    rc, out = tools.run(
        [
            "gh", "run", "list", "--repo", repo, "--commit", head,
            "--status", "failure", "--json", "databaseId",
        ]
    )  # fmt: skip
    try:
        runs = [r["databaseId"] for r in json.loads(out)] if rc == 0 else None
    except json.JSONDecodeError, KeyError, TypeError:
        runs = None
    if runs is None:
        return False, "cannot list the head's runs"
    for run_id in runs:
        rc, out = tools.run(
            ["gh", "run", "rerun", str(run_id), "--failed", "--repo", repo]
        )
        if rc != 0:
            return False, f"gh run rerun {run_id} exited {rc}"
    return True, f"re-ran {len(runs)} failed run(s)"


def grade(result: str, verdict: str) -> tuple[bool, bool, str]:
    """(landed, go_on, detail) for a lander unit that ended with `result` and wrote `verdict`.

    `success` and `exit-code` mean the lander finished and wrote its file; any other systemd
    `Result`, or a file still reading `PENDING`, means it was stopped before it wrote an outcome.
    """
    line = verdict.strip().splitlines()[-1] if verdict.strip() else ""
    if result not in ("success", "exit-code"):
        return (
            False,
            False,
            f"systemd stopped the lander (Result={result or 'unknown'})",
        )
    if line.startswith("REFUSED:"):
        return False, True, line
    state = (
        line.removeprefix("VERDICT: ").split(" ", 1)[0]
        if line.startswith("VERDICT: ")
        else ""
    )
    if state in GOES_ON:
        return True, True, line
    return False, False, line or "the lander left no verdict"


def _land(
    pr: int,
    tools: AgentTools,
    timeout: int,
) -> tuple[bool, bool, str]:
    """Start the lander for `pr`, wait for it, and grade how it ended."""
    unit = f"renovate-agent-land@{pr}.service"
    rc, _ = tools.run(["systemctl", "start", unit], timeout=timeout)
    if rc == 124:
        # The lander keeps running in its own unit; only this wait ended.
        return False, False, "still running at the run's deadline"
    _, result = tools.run(["systemctl", "show", "-p", "Result", "--value", unit])
    verdict = tools.read_file(os.path.join(VERDICT_DIR, f"{pr}.verdict"))
    return grade(result.strip(), verdict[:4096])


def _land_all(
    nominated: list[tuple[str, int]],
    closed: set[int],
    tools: AgentTools,
    max_lands: int,
    deadline: float,
    clock: Callable[[], float],
) -> list[Outcome]:
    outcomes: list[Outcome] = []
    started, stopped = 0, ""
    for name, pr in nominated:
        left = int(deadline - clock())
        if pr in closed:
            detail = "not landed: the run closed it"
        elif stopped:
            detail = f"not landed: {stopped}"
        elif started >= max_lands:
            detail = f"not landed: over the run's {max_lands} landings"
        elif left <= 0:
            stopped = "the run's deadline passed"
            detail = f"not landed: {stopped}"
        else:
            started += 1
            landed, go_on, detail = _land(pr, tools, left)
            if not go_on:
                stopped = f"#{pr} stopped the run: {detail}"
            outcomes.append(Outcome(name, landed, detail, "land", pr))
            continue
        outcomes.append(Outcome(name, False, detail, "land", pr))
    return outcomes


def carry_out(
    requests_dir: str,
    census: set[int],
    repo: str,
    tools: AgentTools,
    state_dir: str,
    max_lands: int,
    deadline: float,
    clock: Callable[[], float] = time.time,
) -> list[Outcome]:
    """Read every request in `requests_dir`, carry out the valid ones and log each outcome.

    Lands go last, after every close, comment and rerun, under `max_lands` and `deadline`.
    """
    outcomes: list[Outcome] = []
    nominated: list[tuple[str, int]] = []
    try:
        dir_fd = os.open(requests_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError as exc:
        outcomes.append(
            Outcome("", False, f"cannot open {requests_dir}: {exc.strerror}")
        )
    else:
        try:
            names = sorted(os.listdir(dir_fd))
            for index, name in enumerate(names):
                if index >= MAX_REQUESTS:
                    outcomes.append(
                        Outcome(name, False, f"over {MAX_REQUESTS} requests")
                    )
                    continue
                try:
                    request = parse(_read(dir_fd, name), census)
                except ValueError as exc:
                    outcomes.append(Outcome(name, False, f"refused: {exc}"))
                    continue
                if request["action"] == "land":
                    if request["pr"] not in [pr for _, pr in nominated]:
                        nominated.append((name, request["pr"]))
                    continue
                done, detail = _carry_out(request, repo, tools)
                outcomes.append(
                    Outcome(name, done, detail, request["action"], request["pr"])
                )
        finally:
            os.close(dir_fd)
    closed = {o.pr for o in outcomes if o.action == "close" and o.done}
    outcomes += _land_all(nominated, closed, tools, max_lands, deadline, clock)
    stamp = int(time.time())
    with open(os.path.join(state_dir, REQUEST_LOG), "a", encoding="utf-8") as log_file:
        for o in outcomes:
            line = {"at": stamp, "file": o.name, "action": o.action, "pr": o.pr}
            log_file.write(
                json.dumps({**line, "done": o.done, "detail": o.detail}) + "\n"
            )
    return outcomes
