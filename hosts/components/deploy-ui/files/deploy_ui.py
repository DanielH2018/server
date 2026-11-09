#!/usr/bin/env python3
"""The deploy UI daemon: one page, a JSON API, and the commands the operator types today.

Runs on daniel-box as deploy-ui.service, User=ubuntu, cwd the primary checkout, under
`uv run --no-project` on the host interpreter — so stdlib only, and every repo tool is a
subprocess (`land.sh`, `gh`). The daemon does NO
authentication: ufw admits its port from the cluster only, and Authelia `two_factor` gates
the IngressRoute in front of it. Both are named in this role's CLAUDE.md.

A read that fails answers `{"unavailable": "<reason>"}`, which the page renders in red. It
never answers an empty list for a failed read, because an empty panel reads as "nothing
pending" — the deadman trap this repo has paid for.

Usage: deploy_ui.py   (takes no arguments; -h/--help prints this text)

Configuration comes from the environment, which deploy-ui.service sets:
  DEPLOY_UI_TREE_LOCK  path of the git-tree lock file (required, no default)
  DEPLOY_UI_REPO       primary checkout (default /home/ubuntu/server)
  DEPLOY_UI_STATE      gitops-deploy state dir (default gitops_markers.STATE_DIR)
  DEPLOY_UI_LOGS       log dir (default ~/.local/state/deploy-ui)
  DEPLOY_UI_CLI_LOGS   where `land.sh --detach` logs (default /tmp/homelab-landings-<user>)
  DEPLOY_UI_LOCKS      dir holding the server-deploy-*.lock files (default /var/lock)
  DEPLOY_UI_BIND       listen address (default 127.0.0.1)
  DEPLOY_UI_PORT       listen port (default 8790)
"""

import functools
import getpass
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.parse

from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import ClassVar

from deploy_ui_lib import deploy_ui_reads as reads
from deploy_ui_lib import deploy_ui_writes as writes
from gitops_markers import STATE_DIR
from host_lib import logfmt

# The locks the in-flight panel reads. The tree lock is held for the snapshot only (ADR-0017),
# so for nearly all of a deploy the running thing is a `server-deploy-<tag>.lock` holder --
# or `server-deploy-all.lock`, taken shared by every tagged run and exclusive by a full one.
# The tree lock's path is not named here: the unit passes `server_git_tree_lock` in as
# DEPLOY_UI_TREE_LOCK, so the one definition stays in group_vars (#3276).
SERVICE_LOCK_GLOB = "server-deploy-*.lock"
PAGE = Path(__file__).with_name("deploy_ui.html")


@dataclass(frozen=True)
class Config:
    repo: Path
    state_dir: Path
    log_dir: Path
    bind: str
    port: int
    tree_lock: Path
    lock_dir: Path = Path("/var/lock")
    proc_locks: Path = Path("/proc/locks")
    pr_cache_s: int = 60
    # Where `land.sh --detach` writes when no session dir is set (`land_lib/detach.py`).
    cli_log_dir: Path = Path(f"/tmp/homelab-landings-{getpass.getuser()}")
    landing_window_s: int = 24 * 3600

    @classmethod
    def from_env(cls) -> "Config":
        e = os.environ.get
        return cls(
            # The unit always sets DEPLOY_UI_REPO; the default is the operator's checkout,
            # because this file ships to the host and has no repo to derive one from.
            repo=Path(e("DEPLOY_UI_REPO", "/home/ubuntu/server")),
            state_dir=Path(e("DEPLOY_UI_STATE", STATE_DIR)),
            log_dir=Path(
                e("DEPLOY_UI_LOGS", str(Path.home() / ".local/state/deploy-ui"))
            ),
            bind=e("DEPLOY_UI_BIND", "127.0.0.1"),
            port=int(e("DEPLOY_UI_PORT", "8790")),
            # No default: a guessed path would leave the panel reading `free` over a file
            # nobody locks.
            tree_lock=Path(os.environ["DEPLOY_UI_TREE_LOCK"]),
            lock_dir=Path(e("DEPLOY_UI_LOCKS", "/var/lock")),
            cli_log_dir=Path(
                e("DEPLOY_UI_CLI_LOGS", f"/tmp/homelab-landings-{getpass.getuser()}")
            ),
        )


class Unavailable(Exception):
    pass


class App:
    """Routing plus the subprocess edges. `run` is `subprocess.run` or a test double."""

    def __init__(self, config: Config, run=subprocess.run) -> None:
        self.cfg, self.run = config, run
        # A concurrent cache miss collapses to a single `gh` fetch.
        self._prs_lock = threading.Lock()
        self._prs_cache: tuple[float, list] | None = None
        self._pr_titles: dict[str, str] = {}
        # Landing logs seen on an in-flight row or spawned here, by name; see `_landing_index`.
        self._seen_lock = threading.Lock()
        self._seen: dict[str, Path] = {}

    # ---- subprocess edge ----
    def _out(
        self, argv: list[str], timeout: int, ok_rcs=(0,), merge_stderr: bool = False
    ) -> str:
        """Run argv in the repo and return its stdout.

        `merge_stderr` folds stderr into stdout in write order, for a command whose two
        streams only mean something together (fuser names the file on one and its holders
        on the other).

        Raises:
            Unavailable: the command could not be run, timed out, or exited outside `ok_rcs`.
        """
        try:
            r = self.run(
                argv,
                cwd=self.cfg.repo,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT if merge_stderr else subprocess.PIPE,
                text=True,
                timeout=timeout,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise Unavailable(f"{argv[0]}: {exc}") from exc
        if r.returncode not in ok_rcs:
            raise Unavailable(
                f"{argv[0]} exited {r.returncode}: {(r.stderr or '').strip()[:200]}"
            )
        return r.stdout

    # ---- reads ----
    def _lock_paths(self) -> list[str]:
        """The tree lock plus every service lock file that exists, absolute, sorted."""
        paths = sorted(self.cfg.lock_dir.glob(SERVICE_LOCK_GLOB))
        if self.cfg.tree_lock.exists():
            paths.insert(0, self.cfg.tree_lock)
        return [str(p) for p in paths]

    def inflight(self) -> dict:
        """Every landing, deploy and lock holder as one row each, with its locks.

        Two sources, because neither alone answers "who holds what": fuser names the
        processes with each lock file OPEN (a queued deploy has it open too), and
        `/proc/locks` says which files are granted and which pids are blocked on them.
        `locks_watched` names the files asked about. An empty list is its own state on the
        page: `free` over no files would be the empty-panel trap this module's docstring
        names. A holder this user cannot see in `/proc` is a limit of fuser, not of the
        read, and reads as free.
        """
        ps = self._out(["ps", "-eo", "pid=,ppid=,etimes=,args="], 10)
        paths = self._lock_paths()
        opened = (
            reads.parse_fuser(
                self._out(["fuser", *paths], 5, ok_rcs=(0, 1), merge_stderr=True)
            )
            if paths
            else {}
        )
        try:
            table = reads.parse_proc_locks(self.cfg.proc_locks.read_text())
            keys = {p: reads.lock_key(p) for p in paths}
        except OSError as exc:
            raise Unavailable(f"{self.cfg.proc_locks}: {exc}") from exc
        locks = {p: table[k] for p, k in keys.items() if k in table}
        runs = [
            {
                **r.__dict__,
                "title": self._pr_title(r.pr) if r.pr else "",
                "log": reads.log_path_of(r.pid) if r.kind != "lock" else "",
            }
            for r in reads.runs(reads.parse_ps(ps), opened, locks)
        ]
        for r in runs:
            if r["kind"] == "land" and r["log"]:
                self._remember(Path(r["log"]))
        return {"runs": runs, "locks_watched": [os.path.basename(p) for p in paths]}

    def _pr_title(self, pr: str) -> str:
        """The PR's title, cached for the daemon's life; "" while `gh` cannot answer.

        A landing's PR merges mid-run and leaves `gh pr list`, so the title is read by
        number. A failed lookup is not cached, and never makes the lock panel unavailable.
        """
        if pr not in self._pr_titles:
            try:
                out = self._out(
                    ["gh", "pr", "view", pr, "--json", "title", "--jq", ".title"], 10
                )
            except Unavailable:
                return ""
            if out.strip():
                self._pr_titles[pr] = out.strip()
        return self._pr_titles.get(pr, "")

    def state(self) -> dict:
        try:
            return reads.read_state(self.cfg.state_dir)
        except OSError as exc:
            raise Unavailable(f"state: {exc}") from exc

    def prs(self) -> dict:
        with self._prs_lock:
            if self._prs_cache is not None:
                at, cached = self._prs_cache
                if time.monotonic() - at < self.cfg.pr_cache_s:
                    return {"prs": cached, "cached": True}
            raw = self._out(
                [
                    "gh",
                    "pr",
                    "list",
                    "--json",
                    "number,title,headRefName,isDraft,statusCheckRollup",
                ],
                30,
            )
            rows = reads.parse_prs(raw)
            self._prs_cache = (time.monotonic(), rows)
            return {"prs": rows, "cached": False}

    def _remember(self, log: Path) -> None:
        """Index a landing's log, wherever it lives, if it is a regular landing log file."""
        if reads.landing_log(log, self.cfg.landing_window_s, time.time()):
            with self._seen_lock:
                self._seen[log.name] = log

    def _landing_index(self) -> dict[str, Path]:
        """The landing logs the viewer may read: the two log dirs, plus every log seen.

        The page names a log by file name, never by path, so it reads only a file this index
        listed. A remembered log drops out once it leaves the window.
        """
        with self._seen_lock:
            index = reads.landing_index(
                [self.cfg.log_dir, self.cfg.cli_log_dir],
                list(self._seen.values()),
                self.cfg.landing_window_s,
                time.time(),
            )
            self._seen = {n: p for n, p in self._seen.items() if n in index}
        return index

    def landings(self) -> dict:
        """Every landing log of the window, newest first, with its PR and verdict.

        The title comes from the cache `inflight` fills, never from a `gh` call per row.
        """
        rows = []
        for name, path in self._landing_index().items():
            try:
                mtime = path.stat().st_mtime
                parsed = reads.parse_landing(reads.read_log(path)[0])
            except OSError:
                continue
            rows.append(
                {
                    "name": name,
                    "pr": parsed["pr"],
                    "title": self._pr_titles.get(parsed["pr"], ""),
                    "mtime": int(mtime),
                    "verdict": parsed["verdict"],
                }
            )
        rows.sort(key=lambda r: r["mtime"], reverse=True)
        return {"landings": rows[:30]}

    def log(self, name: str, seen_size: str = "") -> dict:
        """One landing log parsed into stages, or `unchanged` while its size is `seen_size`.

        Raises:
            Unavailable: `name` is not in the landing index, or the file cannot be read.
        """
        path = self._landing_index().get(name)
        if path is None:
            raise Unavailable(f"no landing log named {name!r} in the last day")
        try:
            size = path.stat().st_size
            if seen_size == str(size):
                return {"name": name, "size": size, "unchanged": True}
            text, truncated = reads.read_log(path)
        except OSError as exc:
            raise Unavailable(f"{name}: {exc}") from exc
        parsed = reads.parse_landing(text)
        return {
            **parsed,
            "name": name,
            "size": size,
            "truncated": truncated,
            "title": self._pr_titles.get(parsed["pr"], ""),
            "raw": text,
        }

    # ---- writes ----
    def land(self, body: dict) -> tuple[int, str]:
        pr, since = str(body.get("pr", "")), str(body.get("since", ""))
        if not pr.isdigit():
            return 400, "pr must be digits"
        if not re.fullmatch(r"[0-9a-fA-F]{7,40}", since):
            return 400, "since must be a 7-40 character hex sha"
        refusal = writes.guard_land(
            pr,
            reads.runs(
                reads.parse_ps(
                    self._out(["ps", "-eo", "pid=,ppid=,etimes=,args="], 10)
                ),
                {},
                {},
            ),
            self.state()["hold_sha"],
        )
        if refusal:
            return 409, refusal
        log = writes.spawn_logged(
            ["./scripts/deploy_tools/land.sh", "--pr", pr, "--since", since],
            self.cfg.repo,
            self.cfg.log_dir,
            "land",
        )
        writes.audit(f"action=land pr={pr} since={since} log={log}")
        self._remember(log)
        return 202, json.dumps({"log": log.name})

    def cancel(self, body: dict) -> tuple[int, str]:
        try:
            pid = int(body.get("pid", 0))
        except TypeError, ValueError:
            return 400, "pid must be an integer"
        # Only a landing is cancellable from the page. SIGTERM to a deploy mid-play leaves
        # whatever applied before it live (deploy.sh exit 20), which is not a cancel.
        listed = {r["pid"] for r in self.inflight()["runs"] if r["kind"] == "land"}
        refusal = writes.guard_cancel(pid, listed)
        if refusal:
            return 409, refusal
        writes.terminate(pid)
        writes.audit(f"action=cancel pid={pid}")
        return 200, f"sent SIGTERM to {pid}"

    def hold_clear(self, body: dict) -> tuple[int, str]:
        # Read the entries BEFORE the clear removes them: the operator asked to drop a hold,
        # and what they are dropping with it is one unapplied plane per entry (#2453).
        dropped = self.state()["hold_plane_entries"]
        refusal = writes.clear_hold(
            self.cfg.state_dir, str(body.get("expected_sha", "")), self.cfg.tree_lock
        )
        if refusal:
            return 409, refusal
        # A count, not the entries: an entry is `<playbook> <tags>` and the space would break
        # the logfmt line Alloy ships to Loki. The reply below names them.
        writes.audit(
            f"action=hold-clear sha={body.get('expected_sha')} planes={len(dropped)}"
        )
        return 200, writes.hold_cleared_message(dropped)

    # ---- routing ----
    READS: ClassVar[dict[str, str]] = {
        "/api/inflight": "inflight",
        "/api/state": "state",
        "/api/prs": "prs",
        "/api/landings": "landings",
    }
    WRITES: ClassVar[dict[str, str]] = {
        "/api/land": "land",
        "/api/cancel": "cancel",
        "/api/hold/clear": "hold_clear",
    }

    def get(self, path: str) -> tuple[int, str]:
        url = urllib.parse.urlsplit(path)
        if url.path == "/":
            return 200, PAGE.read_text()
        if url.path == "/api/log":
            q = {k: v[0] for k, v in urllib.parse.parse_qs(url.query).items()}
            read = functools.partial(self.log, q.get("name", ""), q.get("size", ""))
        elif url.path in self.READS:
            read = getattr(self, self.READS[url.path])
        else:
            return 404, "no such route"
        try:
            return 200, json.dumps(read())
        except Unavailable as exc:
            return 200, json.dumps({"unavailable": str(exc)})
        except Exception as exc:
            # A parser meeting output it did not expect (a gh field gone, a JSON shape
            # change) must reach the page as red text, not as a traceback and an empty panel.
            return 200, json.dumps({"unavailable": f"{type(exc).__name__}: {exc}"})

    def post(self, path: str, headers, raw: str) -> tuple[int, str]:
        refusal = writes.write_allowed(dict(headers))
        if refusal:
            return 403, refusal
        name = self.WRITES.get(path)
        if not name:
            return 404, "no such route"
        try:
            body = json.loads(raw or "{}")
        except ValueError:
            return 400, "body is not JSON"
        if not isinstance(body, dict):
            return 400, "body must be a JSON object"
        try:
            return getattr(self, name)(body)
        except Unavailable as exc:
            return 503, str(exc)


def content_length(headers) -> int | None:
    """The request's Content-Length as a byte count, or None when it is not one.

    A missing or empty header is 0 bytes. Anything else that is not a non-negative
    integer is the caller's error, and answering 400 beats raising out of the handler.
    """
    raw = (headers.get("Content-Length") or "").strip()
    if not raw:
        return 0
    try:
        n = int(raw)
    except ValueError:
        return None
    return n if n >= 0 else None


def request_line(
    method: str, path: str, status: int, user: str, event: str = "request"
) -> str:
    """One logfmt line for one request, as journald keeps it for the usage count (#2864).

    Every request gets one, the 10-second panel polls included, so the line carries the
    path: `path=/` is a page load and a POST is a write, and those two are what show a
    person used the page. A poll only shows a tab was open. The query string is dropped,
    and `user` is Authelia's `Remote-User`, `-` for a caller that came around it.
    `event` is the line's first word; the usage count matches only `request`.
    """
    # `user` is narrowed before logfmt sees it: logfmt quotes a newline but does not escape
    # it, so a hostile `Remote-User` would still split the journal line and forge a second one.
    who = re.sub(r"[^\w.@-]", "_", user) or "-"
    url = urllib.parse.urlsplit(path).path or "-"
    fields = {"method": method, "path": url, "status": int(status), "user": who}
    return f"{event} {logfmt(fields)}"


def make_handler(app: App) -> type[BaseHTTPRequestHandler]:
    """The request handler class that serves `app`'s page and API."""

    class Handler(BaseHTTPRequestHandler):
        def _send(self, status: int, text: str, ctype: str) -> None:
            data = text.encode()
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            try:
                self.end_headers()
                self.wfile.write(data)
            except BrokenPipeError, ConnectionResetError:
                # A client that closed mid-response is not a server error; uncaught, it
                # logs a traceback that HostLogErrors counts.
                self.close_connection = True
                self._log(status, "client-disconnected")

        def do_GET(self) -> None:
            status, text = app.get(self.path)
            ctype = (
                "text/html; charset=utf-8" if self.path == "/" else "application/json"
            )
            self._send(status, text, ctype)

        def do_POST(self) -> None:
            n = content_length(self.headers)
            if n is None:
                self._send(
                    400,
                    "Content-Length must be a byte count",
                    "text/plain; charset=utf-8",
                )
                return
            status, text = app.post(
                self.path, self.headers, self.rfile.read(n).decode()
            )
            self._send(
                status,
                text,
                "application/json" if status == 202 else "text/plain; charset=utf-8",
            )

        def log_request(self, code="-", size="-") -> None:
            self._log(code if isinstance(code, int) else 0, "request")

        def _log(self, status: int, event: str) -> None:
            # A request line that failed to parse leaves `command`, `path` and `headers`
            # unset, and send_error still logs it, so each is read with a default.
            headers = getattr(self, "headers", None)
            print(
                request_line(
                    getattr(self, "command", None) or "-",
                    getattr(self, "path", ""),
                    status,
                    headers.get("Remote-User", "") if headers else "",
                    event=event,
                ),
                file=sys.stderr,
                flush=True,
            )

    return Handler


def serve(config: Config) -> None:
    ThreadingHTTPServer(
        (config.bind, config.port), make_handler(App(config))
    ).serve_forever()


if __name__ == "__main__":
    if any(arg in ("-h", "--help") for arg in sys.argv[1:]):
        print(__doc__.strip())
        sys.exit(0)
    serve(Config.from_env())
