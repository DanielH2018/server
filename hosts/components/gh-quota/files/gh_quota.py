#!/usr/bin/python3
"""Count each GitHub call a session makes, and report the count beside GitHub's own.

GitHub meters API calls per identity, per hour, and every session on a host spends the same
identity's quota. `record` adds one to a ledger under /run/gh-quota, keyed by session, for each
`gh` call a wrapper is about to make. About every 30 seconds it also stores what GitHub itself
counted. `gh-quota status` prints the two side by side, so a spike has a session's name on it.
docs/github-quota.md holds the design.

The callers are the wrappers that already stand in front of GitHub, `scripts/lib/gh.py` and
`ghread`. Each passes `record` the arguments it is about to hand `gh`.

Installed as /usr/lib/homelab/gh_quota.py, which the wrappers import, and as
/usr/bin/gh-quota. Without /etc/gh-quota.conf the mode is `off` and `record` returns at
once, so a host without the package behaves as if this module were absent.

Typical usage example:

    import gh_quota
    gh_quota.record(["pr", "list", "--state", "open"])
"""

import argparse
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from contextlib import closing
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

LEDGER_DIR = Path("/run/gh-quota")
CONF = Path("/etc/gh-quota.conf")
BUCKETS = ("graphql", "core")
SAMPLE_EVERY_S = 30
KEEP_S = 8 * 24 * 3600
# `status` lists a session at or above this share of a bucket's limit.
ATTENTION_SHARE = 0.2
_FAILURE_LOG_MAX = 64 * 1024

_FREE = frozenset(
    {"auth", "config", "version", "completion", "help", "alias", "extension"}
)
# gh sends these families through REST. The rest of its porcelain reads through GraphQL.
_REST = frozenset({"run", "workflow", "cache", "secret", "variable", "ruleset"})
_API_VALUE_FLAGS = frozenset(
    {
        "-X", "--method", "-f", "--raw-field", "-F", "--field", "-H", "--header", "-q", "--jq",
        "-t", "--template", "-p", "--preview", "--input", "--cache", "--hostname",
    }
)  # fmt: skip
_API_BODY_FLAGS = ("-f", "-F", "--field", "--raw-field", "--input")
_SUBCOMMAND = re.compile(r"[a-z][a-z-]*\Z")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS spend(
    window INTEGER, session TEXT, bucket TEXT, argv_class TEXT, n INTEGER,
    PRIMARY KEY (window, session, bucket, argv_class));
CREATE TABLE IF NOT EXISTS truth(
    window INTEGER, bucket TEXT, used INTEGER, quota INTEGER, sampled REAL,
    PRIMARY KEY (window, bucket));
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value REAL);
"""


def classify(args: Sequence[str]) -> tuple[str, str] | None:
    """The quota bucket and the call class of a `gh` argv, or None when the call is free.

    The class is the command and its subcommand, such as `pr list`. A `gh api` read is
    `api graphql` or `api rest`, a `gh api` write is `api mutation` or `api write`, and
    `gh api rate_limit` is free because GitHub does not count it. The bucket is an estimate
    for a porcelain command: gh does not say which API it used.
    """
    words = [arg for arg in args if not arg.startswith("-")]
    if not words or words[0] in _FREE:
        return None
    command = words[0]
    if command == "api":
        path = _api_path(args)
        if path.startswith("rate_limit"):
            return None
        if path == "graphql":
            mutates = any("mutation" in arg for arg in args)
            return ("graphql", "api mutation" if mutates else "api graphql")
        return ("core", "api write" if _api_writes(args) else "api rest")
    sub = words[1] if len(words) > 1 and _SUBCOMMAND.match(words[1]) else ""
    return ("core" if command in _REST else "graphql", f"{command} {sub}".strip())


def _api_path(args: Sequence[str]) -> str:
    rest = iter(args[list(args).index("api") + 1 :])
    for arg in rest:
        if arg in _API_VALUE_FLAGS:
            next(rest, None)
        elif not arg.startswith("-"):
            return arg.lstrip("/")
    return ""


def _api_writes(args: Sequence[str]) -> bool:
    # gh sends GET until a field or a body is given, and then POST, unless a method is named.
    method, has_body = "", False
    for flag, value in zip(args, [*args[1:], ""], strict=True):
        if flag in ("-X", "--method"):
            method = value
        elif flag.startswith("--method="):
            method = flag.split("=", 1)[1]
        elif flag.startswith("-X") and len(flag) > 2:
            method = flag[2:]
        elif flag.startswith(_API_BODY_FLAGS):
            has_body = True
    return method.upper() != "GET" if method else has_body


def session_key(
    env: Mapping[str, str], cgroup: Path = Path("/proc/self/cgroup")
) -> str:
    """Who a call is counted against: the Claude session, else the systemd service, else `operator`."""
    session = env.get("CLAUDE_CODE_SESSION_ID")
    if session:
        return session
    try:
        unit = cgroup.read_text().strip().rsplit("/", 1)[-1]
    except OSError:
        return "operator"
    return unit if unit.endswith(".service") else "operator"


def _identity(env: Mapping[str, str]) -> str:
    token = env.get("GH_TOKEN") or env.get("GITHUB_TOKEN")
    if token:
        return "tok-" + hashlib.sha256(token.encode()).hexdigest()[:12]
    return f"uid-{os.getuid()}"


@dataclass(frozen=True)
class Window:
    """One identity's spend in one bucket, for the quota window GitHub has open.

    Attributes:
        identity: `uid-<n>` for a `gh` login, `tok-<fingerprint>` for a `GH_TOKEN`.
        bucket: `graphql` or `core`.
        used: what GitHub counted at the last sample.
        quota: GitHub's limit for the bucket.
        reset: the epoch second the window ends.
        sessions: each session's recorded calls by call class.
    """

    identity: str
    bucket: str
    used: int
    quota: int
    reset: int
    sessions: dict[str, Counter[str]]

    @property
    def recorded(self) -> int:
        """The calls the ledger holds for this window."""
        return sum(sum(calls.values()) for calls in self.sessions.values())

    @property
    def beyond(self) -> int:
        """What GitHub counted beyond the recorded calls.

        For `core` that is spend that went around the wrappers. For `graphql` it also holds
        the points a recorded call cost above one.
        """
        return max(0, self.used - self.recorded)

    def over(self) -> list[str]:
        """The sessions at or above `ATTENTION_SHARE` of the limit, largest first."""
        floor = self.quota * ATTENTION_SHARE
        totals = {
            session: sum(calls.values()) for session, calls in self.sessions.items()
        }
        return sorted(
            (s for s, n in totals.items() if n >= floor), key=lambda s: -totals[s]
        )


@dataclass(frozen=True)
class Ledger:
    """The ledger files under one directory, read and written as one caller.

    Attributes:
        root: the directory that holds one SQLite file per identity and uid.
        conf: the file whose `mode=` line turns recording on.
        env: the environment of the `gh` call being counted.
        gh: the program the caller runs as `gh`, which also takes the sample.
        now: the clock.
        run: runs the `gh api rate_limit` sample.
    """

    root: Path = LEDGER_DIR
    conf: Path = CONF
    env: Mapping[str, str] = field(default_factory=lambda: os.environ)
    gh: str = "gh"
    now: Callable[[], float] = time.time
    run: Callable[..., subprocess.CompletedProcess] = subprocess.run

    def mode(self) -> str:
        """`record` when the host's conf file says so, else `off`.

        `GH_QUOTA_MODE` in the call's environment wins over the file when it is `off` or
        `record`. The test suite sets `off`, so a test's fake `gh` call never lands in the
        host's ledger.
        """
        forced = self.env.get("GH_QUOTA_MODE")
        if forced in ("off", "record"):
            return forced
        try:
            lines = self.conf.read_text().splitlines()
        except OSError:
            return "off"
        return "record" if "mode=record" in (line.strip() for line in lines) else "off"

    def record(self, args: Sequence[str]) -> None:
        """Count one `gh` call with these arguments against the caller's session.

        Never raises and never refuses. A ledger it cannot write is counted in a failure log
        under `root`, and the caller's `gh` call goes ahead.
        """
        if self.mode() == "off":
            return
        call = classify(args)
        if call is None:
            return
        bucket, argv_class = call
        try:
            with closing(self._open()) as db:
                self._sample_if_due(db)
                db.execute(
                    "INSERT INTO spend VALUES (?, ?, ?, ?, 1) "
                    "ON CONFLICT (window, session, bucket, argv_class) DO UPDATE SET n = n + 1",
                    (
                        self._window(db, bucket),
                        session_key(self.env),
                        bucket,
                        argv_class,
                    ),
                )
        except (sqlite3.Error, OSError) as exc:
            self._failed(exc)

    def windows(self) -> list[Window]:
        """Every identity's open window per bucket, after a fresh sample of the caller's own."""
        with closing(self._open()) as db:
            self._sample_if_due(db)
        now = self.now()
        found: dict[tuple[str, str], Window] = {}
        for path in sorted(self.root.glob("*@*.sqlite")):
            identity = path.name.split("@", 1)[0]
            with closing(
                sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=1.0)
            ) as db:
                for bucket in BUCKETS:
                    truth = db.execute(
                        "SELECT window, used, quota FROM truth WHERE bucket = ? AND window > ? "
                        "ORDER BY window DESC LIMIT 1",
                        (bucket, now),
                    ).fetchone()
                    if truth is None:
                        continue
                    reset, used, quota = truth
                    window = found.setdefault(
                        (identity, bucket),
                        Window(identity, bucket, used, quota, reset, {}),
                    )
                    # Every window that has not ended, so a call recorded under the hour's
                    # end while no sample had named GitHub's reset still counts.
                    rows = db.execute(
                        "SELECT session, argv_class, n FROM spend WHERE window > ? AND bucket = ?",
                        (now, bucket),
                    )
                    for session, argv_class, n in rows:
                        window.sessions.setdefault(session, Counter())[argv_class] += n
        return list(found.values())

    def failures(self) -> int:
        """How many times a `record` or a sample failed since boot, across every caller."""
        return sum(
            len(path.read_text().splitlines()) for path in self.root.glob("failures.*")
        )

    def _open(self) -> sqlite3.Connection:
        path = self.root / f"{_identity(self.env)}@{os.getuid()}.sqlite"
        if not path.exists():
            path.touch()
            path.chmod(0o644)
        db = sqlite3.connect(path, timeout=1.0, isolation_level=None)
        try:
            db.executescript(_SCHEMA)
        except sqlite3.Error:
            db.close()
            raise
        return db

    def _window(self, db: sqlite3.Connection, bucket: str) -> int:
        # GitHub's own reset time names the window. Until a sample has one, the hour's end does.
        now = self.now()
        (reset,) = db.execute(
            "SELECT MAX(window) FROM truth WHERE bucket = ? AND window > ?",
            (bucket, now),
        ).fetchone()
        return reset or (int(now) // 3600 + 1) * 3600

    def _sample_if_due(self, db: sqlite3.Connection) -> None:
        now = self.now()
        claimed = db.execute(
            "INSERT INTO meta VALUES ('next_sample', ?) "
            "ON CONFLICT (key) DO UPDATE SET value = excluded.value WHERE meta.value <= ?",
            (now + SAMPLE_EVERY_S, now),
        ).rowcount
        if not claimed:
            return
        try:
            out = self.run(
                [self.gh, "api", "rate_limit"],
                capture_output=True,
                text=True,
                timeout=10,
                check=True,
                env=dict(self.env, GH_PROMPT_DISABLED="1", GH_NO_UPDATE_NOTIFIER="1"),
            ).stdout
            resources = json.loads(out)["resources"]
            for bucket in BUCKETS:
                seen = resources[bucket]
                # An unused bucket reports a reset an hour ahead of every sample, so it has no
                # window to name yet.
                if seen["used"]:
                    db.execute(
                        "INSERT INTO truth VALUES (?, ?, ?, ?, ?) "
                        "ON CONFLICT (window, bucket) DO UPDATE SET "
                        "used = excluded.used, quota = excluded.quota, sampled = excluded.sampled",
                        (seen["reset"], bucket, seen["used"], seen["limit"], now),
                    )
        except (
            OSError,
            subprocess.SubprocessError,
            ValueError,
            KeyError,
            TypeError,
        ) as exc:
            self._failed(exc)
        db.execute("DELETE FROM spend WHERE window < ?", (now - KEEP_S,))
        db.execute("DELETE FROM truth WHERE window < ?", (now - KEEP_S,))

    def _failed(self, exc: Exception) -> None:
        log = self.root / f"failures.{os.getuid()}"
        try:
            if not log.exists() or log.stat().st_size < _FAILURE_LOG_MAX:
                with log.open("a") as out:
                    out.write(f"{int(self.now())} {type(exc).__name__}: {exc}\n")
        except OSError:
            pass


def record(
    args: Sequence[str], env: Mapping[str, str] | None = None, gh: str = "gh"
) -> None:
    """Count one `gh` call against the caller's session, on a host that records.

    Args:
        args: the arguments the caller is about to hand `gh`, without the `gh`.
        env: the environment that `gh` call runs in, when it is not this process's own.
        gh: the program the caller runs as `gh`, when it is not the one on `PATH`. `ghread`
            passes the absolute path it resolved, so no `PATH` chooses the binary.
    """
    _ledger(os.environ if env is None else env, gh).record(args)


def _ledger(env: Mapping[str, str], gh: str = "gh") -> Ledger:
    # GH_QUOTA_DIR moves the ledger for a test that drives a real wrapper.
    return Ledger(root=Path(env.get("GH_QUOTA_DIR") or LEDGER_DIR), env=env, gh=gh)


def _count(n: int, noun: str) -> str:
    return f"{n} {noun}" if n == 1 else f"{n} {noun}s"


def render(windows: Sequence[Window], failures: int, *, every: bool) -> list[str]:
    """The lines `gh-quota status` prints: a count line per window, then the sessions to look at."""
    if not windows:
        return ["no GitHub call recorded in an open quota window"]
    lines = []
    for window in windows:
        over = window.over()
        shown = sorted(window.sessions, key=lambda s: -sum(window.sessions[s].values()))
        shown = shown if every else over
        hidden = len(window.sessions) - len(shown)
        reset = datetime.fromtimestamp(window.reset, UTC).strftime("%H:%M")
        share = round(100 * window.beyond / window.used) if window.used else 0
        line = (
            f"{window.identity} {window.bucket}: {window.used}/{window.quota} used, "
            f"resets {reset} UTC; {_count(len(window.sessions), 'session')} made "
            f"{_count(window.recorded, 'call')}; "
            f"{window.beyond} beyond the ledger ({share}%); "
            f"{len(over)} at or over {round(100 * ATTENTION_SHARE)}%"
        )
        lines.append(line + (f"; --all lists {hidden} more" if hidden else ""))
        for session in shown:
            calls = window.sessions[session]
            top = ", ".join(f"{name} x{n}" for name, n in calls.most_common(3))
            lines.append(
                f"  {session[:36]}  {_count(sum(calls.values()), 'call')}  {top}"
            )
    if failures:
        lines.append(
            f"{failures} ledger failures since boot; see {LEDGER_DIR}/failures.*"
        )
    return lines


def main(argv: Sequence[str] | None = None, ledger: Ledger | None = None) -> int:
    """Run `gh-quota`: `status` reports spend per session, `mode` prints whether the host records."""
    parser = argparse.ArgumentParser(
        prog="gh-quota",
        description="GitHub API spend per session on this host, beside GitHub's own count.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    status = commands.add_parser(
        "status", help="spend per session in the open quota windows"
    )
    status.add_argument("--all", action="store_true", help="list every session")
    status.add_argument(
        "--json", action="store_true", help="print one compact JSON array"
    )
    commands.add_parser("mode", help="print `record` or `off`")
    opts = parser.parse_args(argv)

    ledger = ledger or _ledger(os.environ)
    if opts.command == "mode":
        print(ledger.mode())
        return 0
    if ledger.mode() == "off":
        print(f"gh-quota is off on this host: {CONF} does not say mode=record")
        return 0
    windows = ledger.windows()
    if opts.json:
        rows = [
            {
                "identity": w.identity,
                "bucket": w.bucket,
                "used": w.used,
                "quota": w.quota,
                "reset": w.reset,
                "recorded": w.recorded,
                "beyond": w.beyond,
                "sessions": w.sessions,
            }
            for w in windows
        ]
        print(json.dumps(rows, separators=(",", ":")))
        return 0
    print("\n".join(render(windows, ledger.failures(), every=opts.all)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
