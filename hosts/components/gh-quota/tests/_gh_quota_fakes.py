"""The fake `gh`, clock and ledger factory the gh-quota test modules share.

Not a test module. Both suites import it by bare name, because pytest names test modules by
basename repo-wide, so one suite cannot import the other.
"""

import hashlib
import json
import subprocess
from collections.abc import Callable
from pathlib import Path

from gh_quota import Ledger

NOW = 1_794_668_400.0  # 15:00:00 UTC
RESET = int(NOW) + 1200
SESSION = "7c1e90aa-0000-4000-8000-000000000001"


class FakeGh:
    """Stands in for `gh`: names its stored token and answers `gh api rate_limit`.

    `logins` maps a `GH_CONFIG_DIR`, None when the caller sets none, to the token
    `gh auth token` prints there. A directory it does not name is not logged in.
    `fail` makes the rate-limit call fail the way `gh` can. `reply` replaces the JSON
    body it prints.
    """

    def __init__(self):
        self.logins: dict[str | None, str] = {None: "gho_operator"}
        self.graphql_used = 100
        self.graphql_reset = RESET
        self.core_used = 0
        self.fail: Exception | None = None
        self.reply: str | None = None
        self.during_sample: Callable[[], None] | None = None
        self.env: dict[str, str] = {}

    def __call__(self, argv, *, env, check=False, **_kwargs):
        self.env = env
        if argv == ["gh", "auth", "token"]:
            token = self.logins.get(env.get("GH_CONFIG_DIR"))
            if token is not None:
                return subprocess.CompletedProcess(argv, 0, f"{token}\n", "")
            if check:
                raise subprocess.CalledProcessError(1, argv)
            return subprocess.CompletedProcess(argv, 1, "", "no oauth token found\n")
        assert argv == ["gh", "api", "rate_limit"]
        if self.during_sample is not None:
            self.during_sample()
        if self.fail is not None:
            raise self.fail
        if self.reply is not None:
            return subprocess.CompletedProcess(argv, 0, self.reply, "")
        resources = {
            "graphql": {
                "used": self.graphql_used,
                "limit": 5000,
                "reset": self.graphql_reset,
            },
            "core": {"used": self.core_used, "limit": 5000, "reset": RESET + 60},
        }
        return subprocess.CompletedProcess(
            argv, 0, json.dumps({"resources": resources}), ""
        )


class Clock:
    def __init__(self):
        self.t = NOW

    def __call__(self) -> float:
        return self.t


def make_ledger(
    tmp_path: Path, mode: str, env: dict[str, str]
) -> tuple[Ledger, Clock, FakeGh]:
    conf = tmp_path / "gh-quota.conf"
    conf.write_text(f"mode={mode}\n")
    root = tmp_path / "ledger"
    root.mkdir()
    clock, gh = Clock(), FakeGh()
    return Ledger(root=root, conf=conf, env=env, now=clock, run=gh), clock, gh


def identity_of(token: str) -> str:
    return "tok-" + hashlib.sha256(token.encode()).hexdigest()[:12]
