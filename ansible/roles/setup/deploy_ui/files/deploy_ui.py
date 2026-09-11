#!/usr/bin/env python3
"""The deploy UI daemon: one page, a JSON API, and the commands the operator types today.

Runs on daniel-box as deploy-ui.service, User=ubuntu, cwd the primary checkout, under
`uv run --no-project` on the host interpreter — so stdlib only, and every repo tool is a
subprocess (`probe.py releases`, `land.sh`, `deploy.sh`, `gh`). The daemon does NO
authentication: ufw admits its port from the cluster only, and Authelia `two_factor` gates
the IngressRoute in front of it. Both are named in this role's CLAUDE.md.

A read that fails answers `{"unavailable": "<reason>"}`, which the page renders in red. It
never answers an empty list for a failed read, because an empty panel reads as "nothing
pending" — the deadman trap this repo has paid for.
"""

import json
import os
import re
import subprocess
import threading
import time
import urllib.parse

from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import ClassVar

import deploy_ui_reads as reads
import deploy_ui_writes as writes

LOCK = "/var/lock/server-git-tree.lock"
PAGE = Path(__file__).with_name("deploy_ui.html")
STALE_CACHE_S = 60


@dataclass(frozen=True)
class Config:
    repo: Path
    state_dir: Path
    log_dir: Path
    bind: str
    port: int
    pr_cache_s: int = 60

    @classmethod
    def from_env(cls) -> "Config":
        e = os.environ.get
        return cls(
            repo=Path(e("DEPLOY_UI_REPO", "/home/ubuntu/server")),
            state_dir=Path(e("DEPLOY_UI_STATE", "/var/lib/gitops-deploy")),
            log_dir=Path(
                e("DEPLOY_UI_LOGS", str(Path.home() / ".local/state/deploy-ui"))
            ),
            bind=e("DEPLOY_UI_BIND", "127.0.0.1"),
            port=int(e("DEPLOY_UI_PORT", "8790")),
        )


class Unavailable(Exception):
    pass


class App:
    """Routing plus the subprocess edges. `run` is `subprocess.run` or a test double."""

    def __init__(self, config: Config, run=subprocess.run) -> None:
        self.cfg, self.run = config, run
        # One lock per cache, so a concurrent miss on one collapses to a single fetch
        # without the 120s `probe.py releases` blocking a /api/prs request behind it.
        self._stale_lock = threading.Lock()
        self._prs_lock = threading.Lock()
        self._prs_cache: tuple[float, list] | None = None
        self._stale_cache: tuple[float, list] | None = None

    # ---- subprocess edge ----
    def _capture(self, argv: list[str], timeout: int):
        """Run argv in the repo and return the CompletedProcess, whatever its exit code.

        Raises:
            Unavailable: the command could not be run or timed out.
        """
        try:
            return self.run(
                argv,
                cwd=self.cfg.repo,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise Unavailable(f"{argv[0]}: {exc}") from exc

    def _out(self, argv: list[str], timeout: int, ok_rcs=(0,)) -> str:
        r = self._capture(argv, timeout)
        if r.returncode not in ok_rcs:
            raise Unavailable(
                f"{argv[0]} exited {r.returncode}: {r.stderr.strip()[:200]}"
            )
        return r.stdout

    # ---- reads ----
    def inflight(self) -> dict:
        ps = self._out(["ps", "-eo", "pid=,etimes=,args="], 10)
        landings = [
            {**l.__dict__, "log": reads.log_path_of(l.pid)} for l in reads.parse_ps(ps)
        ]
        holder = None
        pid = reads.parse_fuser_pid(self._out(["fuser", LOCK], 5, ok_rcs=(0, 1)))
        if pid is not None:
            line = self._out(
                ["ps", "-o", "etimes=,args=", "-p", str(pid)], 5, ok_rcs=(0, 1)
            ).strip()
            et, _, args = line.partition(" ")
            holder = {"pid": pid, "elapsed_s": int(et or 0), "args": args.strip()[:200]}
        return {"landings": landings, "lock_holder": holder}

    def stale(self) -> dict:
        with self._stale_lock:
            if self._stale_cache is not None:
                at, cached = self._stale_cache
                if time.monotonic() - at < STALE_CACHE_S:
                    return {"stale": cached, "cached": True}
            r = self._capture(
                [
                    "uv",
                    "run",
                    "python",
                    "scripts/diagnostics/probe.py",
                    "releases",
                    "--stale-only",
                ],
                120,
            )
            rows = reads.parse_stale(r.stdout)
            # probe.py exits 1 both when it lists stale services and when it fails with
            # nothing on stdout. Exit 0 is the only state that means "read it, none stale";
            # rc 1 with no rows parsed is a failed read, and must not cache as an empty list.
            if r.returncode != 0 and not rows:
                raise Unavailable(
                    f"probe.py releases exited {r.returncode}: {r.stderr.strip()[:200]}"
                )
            self._stale_cache = (time.monotonic(), rows)
            return {"stale": rows, "cached": False}

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

    def log_tail(self, path: str) -> str:
        """Tail the last 20 KB of one log in `log_dir`, named by the 202 that created it.

        Only the basename of the caller's path is used, so nothing the page sends can name
        a file outside `log_dir`. The parent it did send is still compared, lexically, so a
        path from somewhere else is refused rather than silently redirected into `log_dir`.
        """
        name = Path(path).name
        if not name or Path(path).parent != self.cfg.log_dir:
            return "refused: not a deploy-ui log"
        try:
            resolved = (self.cfg.log_dir / name).resolve()
        except OSError as exc:
            return f"unavailable: {exc}"
        if self.cfg.log_dir.resolve() not in resolved.parents:
            return "refused: not a deploy-ui log"
        try:
            with resolved.open("rb") as f:
                f.seek(0, os.SEEK_END)
                size = f.tell()
                f.seek(max(0, size - 20000))
                data = f.read()
        except OSError as exc:
            return f"unavailable: {exc}"
        return data.decode("utf-8", errors="replace")

    # ---- writes ----
    def _deployable_tags(self) -> set[str]:
        """The service tags `/api/deploy` accepts — the listed tags minus the block tags.

        `--list-services` prints `config`, `deploy`, `cron` and `always` too; see
        `writes.NON_SERVICE_TAGS` for why none of them may reach this API.
        """
        return writes.service_tags(
            set(self._out(["./scripts/deploy.sh", "--list-services"], 60).split())
        )

    def land(self, body: dict) -> tuple[int, str]:
        pr, since = str(body.get("pr", "")), str(body.get("since", ""))
        if not pr.isdigit():
            return 400, "pr must be digits"
        if not re.fullmatch(r"[0-9a-fA-F]{7,40}", since):
            return 400, "since must be a 7-40 character hex sha"
        refusal = writes.guard_land(
            pr,
            reads.parse_ps(self._out(["ps", "-eo", "pid=,etimes=,args="], 10)),
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
        return 202, json.dumps({"log": str(log)})

    def deploy(self, body: dict) -> tuple[int, str]:
        tag = str(body.get("tag", ""))
        refusal = writes.guard_deploy(
            tag, self._deployable_tags(), self.state()["hold_sha"]
        )
        if refusal:
            return 409, refusal
        log = writes.spawn_logged(
            ["./scripts/deploy.sh", "--tags", tag],
            self.cfg.repo,
            self.cfg.log_dir,
            "deploy",
        )
        writes.audit(f"action=deploy tag={tag} log={log}")
        return 202, json.dumps({"log": str(log)})

    def cancel(self, body: dict) -> tuple[int, str]:
        try:
            pid = int(body.get("pid", 0))
        except TypeError, ValueError:
            return 400, "pid must be an integer"
        listed = {l["pid"] for l in self.inflight()["landings"]}
        refusal = writes.guard_cancel(pid, listed)
        if refusal:
            return 409, refusal
        writes.terminate(pid)
        writes.audit(f"action=cancel pid={pid}")
        return 200, f"sent SIGTERM to {pid}"

    def hold_clear(self, body: dict) -> tuple[int, str]:
        refusal = writes.clear_hold(
            self.cfg.state_dir, str(body.get("expected_sha", ""))
        )
        if refusal:
            return 409, refusal
        writes.audit(f"action=hold-clear sha={body.get('expected_sha')}")
        return 200, "hold cleared (hold_sha and hold_plane)"

    def staging_override(self, body: dict) -> tuple[int, str]:
        action = str(body.get("action", ""))
        refusal = writes.set_override(self.cfg.state_dir, action)
        if refusal:
            return 400, refusal
        writes.audit(f"action=staging-override value={action}")
        return 200, f"staging override {action}"

    # ---- routing ----
    READS: ClassVar[dict[str, str]] = {
        "/api/inflight": "inflight",
        "/api/stale": "stale",
        "/api/state": "state",
        "/api/prs": "prs",
    }
    WRITES: ClassVar[dict[str, str]] = {
        "/api/land": "land",
        "/api/deploy": "deploy",
        "/api/cancel": "cancel",
        "/api/hold/clear": "hold_clear",
        "/api/staging-override": "staging_override",
    }

    def get(self, path: str) -> tuple[int, str]:
        url = urllib.parse.urlsplit(path)
        if url.path == "/":
            return 200, PAGE.read_text()
        if url.path == "/api/log":
            q = urllib.parse.parse_qs(url.query).get("path", [""])[0]
            return 200, self.log_tail(q)
        name = self.READS.get(url.path)
        if not name:
            return 404, "no such route"
        try:
            return 200, json.dumps(getattr(self, name)())
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


def serve(config: Config) -> None:
    app = App(config)

    class Handler(BaseHTTPRequestHandler):
        def _send(self, status: int, text: str, ctype: str) -> None:
            data = text.encode()
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:
            status, text = app.get(self.path)
            ctype = (
                "text/html; charset=utf-8"
                if self.path == "/"
                else (
                    "text/plain; charset=utf-8"
                    if self.path.startswith("/api/log")
                    else "application/json"
                )
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

        def log_message(
            self, format, *args
        ) -> None:  # journald gets one line per write only
            if self.command == "POST":
                super().log_message(format, *args)

    ThreadingHTTPServer((config.bind, config.port), Handler).serve_forever()


if __name__ == "__main__":
    serve(Config.from_env())
