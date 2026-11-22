#!/usr/bin/python3
"""Count each GitHub call a session makes, and report the count beside GitHub's own.

GitHub meters API calls per identity, per hour, and every session on a host spends the same
identity's quota. `record` adds one to a ledger under /var/lib/gh-quota, keyed by session, for each
`gh` call a wrapper is about to make. About every 30 seconds it also stores what GitHub itself
counted. `gh-quota status`, in gh_quota_cli.py, prints the two side by side, so a spike has a
session's name on it. docs/github-quota.md holds the design.

The callers are the wrappers that already stand in front of GitHub, `scripts/lib/gh.py` and
`ghread`. Each passes `record` the arguments it is about to hand `gh`.

Installed as /usr/lib/homelab/gh_quota.py, which the wrappers import. It imports nothing from
beside it, because `ghread` loads it by its file path. Without /etc/gh-quota.conf the mode is `off` and `record` returns at
once, so a host without the package behaves as if this module were absent.

Typical usage example:

    import gh_quota
    gh_quota.record(["pr", "list", "--state", "open"])
"""

import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import time
from collections import Counter
from collections.abc import Callable, Generator, Iterable, Mapping, Sequence
from contextlib import closing, contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path

LEDGER_DIR = Path("/var/lib/gh-quota")
CONF = Path("/etc/gh-quota.conf")
BUCKETS = ("graphql", "core")
SAMPLE_EVERY_S = 30
KEEP_S = 8 * 24 * 3600
# GitHub's reported reset for one core window moves by up to 45 s between samples, and its
# two graphql counters for one token reset about 21 minutes apart. Two tokens of one user
# report resets within this of each other too, because they spend one quota.
RESET_DRIFT_S = 120
# How long a ledger write waits for another writer before the call goes unrecorded. Every
# counted `gh` call can wait this long, so it stays a few seconds.
LOCK_WAIT_S = 5.0
# `status` lists a session at or above this share of a bucket's limit.
ATTENTION_SHARE = 0.2
_FAILURE_LOG_MAX = 64 * 1024
_ANCESTRY_MAX = 12

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
# Porcelain subcommands that send a POST, PATCH, PUT or DELETE.
_WRITE_SUBCOMMANDS = frozenset(
    {
        "add", "archive", "cancel", "close", "comment", "create", "delete", "disable", "edit",
        "enable", "fork", "lock", "merge", "pin", "ready", "remove", "rename", "reopen",
        "rerun", "review", "revert", "run", "set", "sync", "transfer", "unarchive", "unlock",
        "unpin", "update-branch", "upload",
    }
)  # fmt: skip

_SCHEMA = """
CREATE TABLE IF NOT EXISTS spend(
    window INTEGER, session TEXT, bucket TEXT, argv_class TEXT, n INTEGER,
    PRIMARY KEY (window, session, bucket, argv_class));
CREATE TABLE IF NOT EXISTS truth(
    window INTEGER, bucket TEXT, used INTEGER, quota INTEGER, sampled REAL,
    PRIMARY KEY (window, bucket));
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value REAL);
CREATE TABLE IF NOT EXISTS writes(at REAL, session TEXT);
CREATE TABLE IF NOT EXISTS sampler(
    window INTEGER, bucket TEXT, session TEXT, gh TEXT, env_names TEXT,
    PRIMARY KEY (window, bucket));
CREATE TABLE IF NOT EXISTS sampler_process(
    window INTEGER, bucket TEXT, cgroup TEXT, ancestry TEXT,
    PRIMARY KEY (window, bucket));
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


def is_write(argv_class: str) -> bool:
    """Whether GitHub counts a call of this class against its content-creating limits."""
    if argv_class in ("api mutation", "api write"):
        return True
    command, _, sub = argv_class.partition(" ")
    return command != "api" and sub in _WRITE_SUBCOMMANDS


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


@dataclass(frozen=True)
class Window:
    """The spend in one bucket of the quota window GitHub has open, across the tokens sharing it.

    Every token one GitHub user owns spends that user's one quota, so the ledger joins the
    identities whose windows share a reset and a limit.

    Attributes:
        identities: each `tok-<fingerprint>` of a token `gh` used, or `uid-<n>` when it had
            none, sorted.
        bucket: `graphql` or `core`.
        used: what GitHub counted at the freshest sample of any of the identities.
        quota: GitHub's limit for the bucket.
        reset: the epoch second the window ends.
        sessions: each session's recorded calls by call class, under every identity.
    """

    identities: tuple[str, ...]
    bucket: str
    used: int
    quota: int
    reset: int
    sessions: dict[str, Counter[str]]

    @property
    def identity(self) -> str:
        """The identities joined by `+`, the name `status` prints."""
        return "+".join(self.identities)

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
class Writes:
    """One identity's recorded writes in the last minute and the last hour.

    A lower bound on what GitHub counted: a `gh` call that goes around the wrappers is not
    recorded. It can also exceed it, because a write is recorded before GitHub answers, so a
    refused write is counted too.

    Attributes:
        identity: `tok-<fingerprint>` or `uid-<n>`, as in `Window`.
        minute: the writes recorded in the last 60 seconds.
        hour: the writes recorded in the last 3600 seconds.
    """

    identity: str
    minute: int
    hour: int


@dataclass
class _Sample:
    """One claimed `GET /rate_limit`, read before any ledger lock is taken.

    Attributes:
        taken: the clock when the sample was claimed.
        counts: (bucket, used, limit, reset) for each bucket GitHub counted, empty when the
            sample failed.
        caller: the sampler's session key, `gh` path and variable names.
        process: the sampler's cgroup line and process ancestry.
    """

    taken: float
    counts: list[tuple[str, int, int, int]] = field(default_factory=list)
    caller: tuple[str, str, str] = ("", "", "")
    process: tuple[str, str] = ("", "")


def _shared(windows: Iterable[tuple[float, Window]]) -> list[Window]:
    """Join the windows of a bucket whose limits match and whose resets chain within RESET_DRIFT_S.

    Takes each window with the time of its sample. A member joins only while every sample in
    the group reads one counter: a later sample never counts less than an earlier one. A
    joined window carries GitHub's count from the freshest sample and the recorded calls of
    every member. Two users whose windows reset within RESET_DRIFT_S of each other and whose
    samples happen to rise with time are still joined: no sample names a token's owner.
    """
    groups: list[list[tuple[float, Window]]] = []
    ordered = sorted(
        windows, key=lambda sw: (BUCKETS.index(sw[1].bucket), sw[1].quota, sw[1].reset)
    )
    for sampled, window in ordered:
        last = groups[-1][-1][1] if groups else None
        if (
            last is not None
            and (last.bucket, last.quota) == (window.bucket, window.quota)
            and window.reset - last.reset <= RESET_DRIFT_S
            and all(_one_counter(member, (sampled, window)) for member in groups[-1])
        ):
            groups[-1].append((sampled, window))
        else:
            groups.append([(sampled, window)])
    joined = []
    for group in groups:
        _, freshest = max(group, key=lambda sw: sw[0])
        sessions: dict[str, Counter[str]] = {}
        for _, window in group:
            for session, calls in window.sessions.items():
                sessions.setdefault(session, Counter()).update(calls)
        identities = tuple(sorted(i for _, w in group for i in w.identities))
        joined.append(replace(freshest, identities=identities, sessions=sessions))
    return joined


def _one_counter(a: tuple[float, Window], b: tuple[float, Window]) -> bool:
    # One user's counter only rises within a window, so a later sample below an earlier one
    # is another user's.
    (_, earlier), (_, later) = sorted((a, b), key=lambda sw: sw[0])
    return earlier.used <= later.used


@contextmanager
def _write(db: sqlite3.Connection) -> Generator[None, None, None]:
    # IMMEDIATE takes the write lock at BEGIN, where the busy timeout applies. A deferred
    # transaction that reads first can fail its upgrade to a write without waiting at all.
    db.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        db.execute("ROLLBACK")
        raise
    db.execute("COMMIT")


@dataclass(frozen=True)
class Ledger:
    """The ledger files under one directory, read and written as one caller.

    Attributes:
        root: the directory that holds one SQLite file per identity and uid.
        conf: the file whose `mode=` line turns recording on.
        env: the environment of the `gh` call being counted.
        gh: the program the caller runs as `gh`, which also names its token and takes the
            sample.
        now: the clock.
        run: runs `gh auth token` and the `gh api rate_limit` sample.
        proc: the procfs root a sample reads its own cgroup and ancestors from.
    """

    root: Path = LEDGER_DIR
    conf: Path = CONF
    env: Mapping[str, str] = field(default_factory=lambda: os.environ)
    gh: str = "gh"
    now: Callable[[], float] = time.time
    run: Callable[..., subprocess.CompletedProcess] = subprocess.run
    proc: Path = Path("/proc")

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
        session = session_key(self.env)
        try:
            with closing(self._open()) as db:
                sample = self._take_sample(db)
                with _write(db):
                    self._store_sample(db, sample)
                    db.execute(
                        "INSERT INTO spend VALUES (?, ?, ?, ?, 1) "
                        "ON CONFLICT (window, session, bucket, argv_class) "
                        "DO UPDATE SET n = n + 1",
                        (self._window(db, bucket), session, bucket, argv_class),
                    )
                    if is_write(argv_class):
                        db.execute(
                            "INSERT INTO writes VALUES (?, ?)", (self.now(), session)
                        )
        except (sqlite3.Error, OSError) as exc:
            self._failed(exc)

    def windows(self) -> list[Window]:
        """The open window per bucket of each group of identities sharing one.

        Takes a fresh sample of the caller's own identity first.
        """
        with closing(self._open()) as db:
            sample = self._take_sample(db)
            with _write(db):
                self._store_sample(db, sample)
        now = self.now()
        found: dict[tuple[str, str], tuple[float, Window]] = {}
        for identity, db in self._read_each():
            with closing(db):
                for bucket in BUCKETS:
                    truth = db.execute(
                        "SELECT window, used, quota, sampled FROM truth "
                        "WHERE bucket = ? AND window > ? "
                        "ORDER BY sampled DESC LIMIT 1",
                        (bucket, now),
                    ).fetchone()
                    if truth is None:
                        continue
                    reset, used, quota, sampled = truth
                    _, window = found.setdefault(
                        (identity, bucket),
                        (sampled, Window((identity,), bucket, used, quota, reset, {})),
                    )
                    # Every window that has not ended, so a call recorded under the hour's
                    # end while no sample had named GitHub's reset still counts.
                    rows = db.execute(
                        "SELECT session, argv_class, n FROM spend WHERE window > ? AND bucket = ?",
                        (now, bucket),
                    )
                    for session, argv_class, n in rows:
                        window.sessions.setdefault(session, Counter())[argv_class] += n
        return _shared(found.values())

    def writes(self) -> list[Writes]:
        """Each identity's recorded writes in the last minute and hour, for those with any."""
        now = self.now()
        counts: dict[str, Counter[str]] = {}
        for identity, db in self._read_each():
            with closing(db):
                try:
                    minute, hour = db.execute(
                        "SELECT TOTAL(at > ?), COUNT(*) FROM writes WHERE at > ?",
                        (now - 60, now - 3600),
                    ).fetchone()
                except sqlite3.OperationalError:
                    # A ledger no writer has opened since the table was added has none.
                    continue
            counts.setdefault(identity, Counter()).update(minute=int(minute), hour=hour)
        return [
            Writes(identity, c["minute"], c["hour"])
            for identity, c in counts.items()
            if c["hour"]
        ]

    def _read_each(self) -> Generator[tuple[str, sqlite3.Connection], None, None]:
        # Each ledger file's identity and a read-only connection to it, which the caller closes.
        for path in sorted(self.root.glob("*@*.sqlite")):
            yield (
                path.name.split("@", 1)[0],
                sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=LOCK_WAIT_S),
            )

    def failures(self) -> int:
        """How many times a `record` or a sample failed within `KEEP_S`, across every caller."""
        return sum(
            len(self._recent_failures(path)) for path in self.root.glob("failures.*")
        )

    def _recent_failures(self, log: Path) -> list[str]:
        oldest = self.now() - KEEP_S
        return [
            line
            for line in log.read_text().splitlines()
            if (stamp := line.partition(" ")[0]).isdigit() and int(stamp) >= oldest
        ]

    def _caller(self) -> tuple[str, str, str]:
        """The session key, the resolved `gh` path, and the caller's `GH_*`/`GITHUB_*` names.

        Names only, never values: the values include tokens.
        """
        found = shutil.which(self.gh, path=self.env.get("PATH"))
        gh = os.path.realpath(found) if found else f"unresolved:{self.gh}"
        names = sorted(n for n in self.env if n.startswith(("GH_", "GITHUB_")))
        return session_key(self.env), gh, ",".join(names)

    def _process(self) -> tuple[str, str]:
        """The sampling process's cgroup line, and its own and its ancestors' `comm`, `<`-joined.

        `comm` only, never the command line: an argv can carry a token. Either part is
        empty when procfs cannot be read, so a sample never fails on it.
        """
        try:
            cgroup = (self.proc / "self" / "cgroup").read_text(errors="replace").strip()
        except OSError:
            cgroup = ""
        names, pid = [], "self"
        for _ in range(_ANCESTRY_MAX):
            try:
                stat = (self.proc / pid / "stat").read_text(errors="replace")
            except OSError:
                break
            head, _, tail = stat.rpartition(")")
            fields = tail.split()
            if "(" not in head or len(fields) < 2:
                break
            names.append(head.partition("(")[2])
            pid = fields[1]
            if pid in ("0", "1"):
                break
        return cgroup, "<".join(names)

    def _gh_env(self) -> dict[str, str]:
        return dict(self.env, GH_PROMPT_DISABLED="1", GH_NO_UPDATE_NOTIFIER="1")

    def _identity(self) -> str:
        # GitHub meters per token owner, and `gh` alone knows which token this environment
        # gives it: a variable, another config directory, or the login it has stored.
        token = self.env.get("GH_TOKEN") or self.env.get("GITHUB_TOKEN")
        if not token:
            try:
                token = self.run(
                    [self.gh, "auth", "token"],
                    capture_output=True,
                    text=True,
                    timeout=10,
                    check=True,
                    env=self._gh_env(),
                ).stdout.strip()
            except (OSError, subprocess.SubprocessError):
                token = ""
        if token:
            return "tok-" + hashlib.sha256(token.encode()).hexdigest()[:12]
        return f"uid-{os.getuid()}"

    def _open(self) -> sqlite3.Connection:
        path = self.root / f"{self._identity()}@{os.getuid()}.sqlite"
        if not path.exists():
            path.touch()
            path.chmod(0o644)
        db = sqlite3.connect(path, timeout=LOCK_WAIT_S, isolation_level=None)
        try:
            db.executescript(_SCHEMA)
        except sqlite3.Error:
            db.close()
            raise
        return db

    def _window(self, db: sqlite3.Connection, bucket: str) -> int:
        # The freshest sample's window, because GitHub can answer one token from two counters.
        # Until a sample has named a reset, the hour's end does.
        now = self.now()
        row = db.execute(
            "SELECT window FROM truth WHERE bucket = ? AND window > ? "
            "ORDER BY sampled DESC LIMIT 1",
            (bucket, now),
        ).fetchone()
        return row[0] if row else (int(now) // 3600 + 1) * 3600

    def _named_window(self, db: sqlite3.Connection, bucket: str, reset: int) -> int:
        # A reset within RESET_DRIFT_S of a stored one is that window, reported late or early.
        row = db.execute(
            "SELECT window FROM truth WHERE bucket = ? AND ABS(window - ?) <= ? "
            "ORDER BY ABS(window - ?) LIMIT 1",
            (bucket, reset, RESET_DRIFT_S, reset),
        ).fetchone()
        return row[0] if row else reset

    def _take_sample(self, db: sqlite3.Connection) -> _Sample | None:
        # Claims the next sample in one short write, then asks GitHub while holding no lock:
        # the subprocess can take its full 10 s, and every other writer would wait on it.
        now = self.now()
        claimed = db.execute(
            "INSERT INTO meta VALUES ('next_sample', ?) "
            "ON CONFLICT (key) DO UPDATE SET value = excluded.value WHERE meta.value <= ?",
            (now + SAMPLE_EVERY_S, now),
        ).rowcount
        if not claimed:
            return None
        sample = _Sample(now)
        try:
            out = self.run(
                [self.gh, "api", "rate_limit"],
                capture_output=True,
                text=True,
                timeout=10,
                check=True,
                env=self._gh_env(),
            ).stdout
            resources = json.loads(out)["resources"]
            seen = [(bucket, resources[bucket]) for bucket in BUCKETS]
            # An unused bucket reports a reset an hour ahead of every sample, so it has no
            # window to name yet.
            sample.counts = [
                (bucket, int(s["used"]), int(s["limit"]), int(s["reset"]))
                for bucket, s in seen
                if s["used"]
            ]
            sample.caller = self._caller()
            sample.process = self._process()
        except (
            OSError,
            subprocess.SubprocessError,
            ValueError,
            KeyError,
            TypeError,
        ) as exc:
            self._failed(exc)
        return sample

    def _store_sample(self, db: sqlite3.Connection, sample: _Sample | None) -> None:
        if sample is None:
            return
        for bucket, used, limit, reset in sample.counts:
            window = self._named_window(db, bucket, reset)
            db.execute(
                "INSERT INTO truth VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT (window, bucket) DO UPDATE SET "
                "used = excluded.used, quota = excluded.quota, sampled = excluded.sampled",
                (window, bucket, used, limit, sample.taken),
            )
            db.execute(
                "INSERT OR REPLACE INTO sampler VALUES (?, ?, ?, ?, ?)",
                (window, bucket, *sample.caller),
            )
            db.execute(
                "INSERT OR REPLACE INTO sampler_process VALUES (?, ?, ?, ?)",
                (window, bucket, *sample.process),
            )
        oldest = sample.taken - KEEP_S
        for table in ("spend", "truth", "sampler", "sampler_process"):
            db.execute(f"DELETE FROM {table} WHERE window < ?", (oldest,))
        db.execute("DELETE FROM writes WHERE at < ?", (sample.taken - 3600,))

    def _failed(self, exc: Exception) -> None:
        log = self.root / f"failures.{os.getuid()}"
        try:
            if log.exists() and log.stat().st_size >= _FAILURE_LOG_MAX:
                kept = self._recent_failures(log)
                log.write_text("".join(f"{line}\n" for line in kept))
            if not log.exists() or log.stat().st_size < _FAILURE_LOG_MAX:
                with log.open("a") as out:
                    out.write(
                        f"{int(self.now())} {session_key(self.env)} "
                        f"{type(exc).__name__}: {exc}\n"
                    )
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
    ledger_for(os.environ if env is None else env, gh).record(args)


def ledger_for(env: Mapping[str, str], gh: str = "gh") -> Ledger:
    """The ledger a caller with this environment records in.

    `GH_QUOTA_DIR` in the environment moves it, for a test that drives a real wrapper.
    """
    return Ledger(root=Path(env.get("GH_QUOTA_DIR") or LEDGER_DIR), env=env, gh=gh)
