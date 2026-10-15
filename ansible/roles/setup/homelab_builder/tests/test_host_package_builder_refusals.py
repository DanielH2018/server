"""The builder's refusals that must keep their own reason: a refused token and a keyring check.

#5089: a 401 or 403 from the check-runs read is the builder's token, not a pending verdict.
#5090: a worktree cleanup that fails must not replace the keyring refusal in flight. The
fixture history, the stub GitHub and the stub render are test_host_package_builder.py's.

Run: uv run pytest ansible/roles/setup/homelab_builder/tests/test_host_package_builder_refusals.py
"""

import json
import re
import subprocess
import urllib.error
from email.message import Message

import pytest

import host_package_builder as hb
from lib.repo_paths import REPO
from test_host_package_builder import (
    GREEN,
    FakeGitHub,
    builder,
    lab,  # noqa: F401 (the fixture, found by name)
    metric,
    staged,
)

PLAN = REPO / "docs" / "target-architecture" / "host" / "h3-pi-pilot.md"


class RefusingGitHub(FakeGitHub):
    """The check-runs API refusing the token with `status`, as GitHub does a revoked one."""

    def __init__(self, status: int):
        super().__init__()
        self.status = status

    def __call__(self, request, timeout=None):
        self.asked.append(request.full_url.split("/commits/")[1].split("/")[0])
        refusal = urllib.error.HTTPError(
            request.full_url, self.status, "Unauthorized", Message(), None
        )
        # Its empty body is a temporary file that warns when collected unclosed.
        refusal.close()
        raise refusal


def failure_reason(state) -> str:
    return json.loads((state / "state.json").read_text())["failure"]["reason"]


@pytest.mark.parametrize("status", [401, 403])
def test_a_refused_token_reads_token_and_pages_once(lab, status):  # noqa: F811
    # #5089: the anonymous fetch keeps succeeding, so only the check-runs read sees the
    # revoked token. Read as pending, it ended the run `no-green` with exit 0.
    origin, state, etc, _ = lab
    origin.commit("a")
    origin.commit("b")
    refusing = RefusingGitHub(status)

    def run() -> int:
        return builder((origin, state, etc, refusing)).main()

    assert run() == 1
    assert refusing.asked, "the run never read the check-runs"
    assert metric(state)[0] == "token"
    assert failure_reason(state) == "token"
    assert run() == 0, "the same refusal on the same tip pages once"
    assert staged(state) == []


def test_token_is_a_metric_reason():
    assert "token" in hb.REASONS


def plan_series_words(series: str) -> set[str]:
    """The backticked words of the plan's *Shared names* bullet that names `series`."""
    lines = PLAN.read_text().splitlines()
    start = next(i for i, line in enumerate(lines) if f"`{series}" in line)
    indent = len(lines[start]) - len(lines[start].lstrip())
    bullet = [lines[start]]
    for line in lines[start + 1 :]:
        if line.strip() and len(line) - len(line.lstrip()) <= indent:
            break
        bullet.append(line)
    return set(re.findall(r"`([a-z-]+)`", " ".join(bullet)))


def test_the_plans_metrics_list_names_token():
    values = plan_series_words("homelab_builder_unbuilt_since_timestamp_seconds{")
    assert "no-green" in values, values
    # `build` is the one value the builder carries that the plan does not name.
    assert hb.REASONS - {"build"} <= values, hb.REASONS - values
    assert "token" in values, values


def test_a_failed_worktree_cleanup_keeps_the_keyring_reason(lab):  # noqa: F811
    # #5090: the `finally` cleanup raised RuntimeError over the in-flight keyring Failure,
    # and the run read `build`.
    origin, state, _, github = lab
    tip = origin.commit("a")
    github.runs[tip] = GREEN

    def run(argv, *args, **kwargs):
        if argv[:3] == ["git", "worktree", "remove"]:
            return subprocess.CompletedProcess(argv, 1, "", "fatal: worktree is locked")
        return subprocess.run(argv, *args, **kwargs)

    b = builder(lab)
    b.run = run
    b.keyring = None
    assert b.main() == 1
    assert metric(state)[0] == "keyring"
    assert failure_reason(state) == "keyring"
    assert staged(state) == []


class RateLimitedGitHub(FakeGitHub):
    """The check-runs API answering 403 for a spent rate limit, which is not a refused token."""

    def __call__(self, request, timeout=None):
        self.asked.append(request.full_url.split("/commits/")[1].split("/")[0])
        headers = Message()
        headers["X-RateLimit-Remaining"] = "0"
        refusal = urllib.error.HTTPError(
            request.full_url, 403, "Forbidden", headers, None
        )
        refusal.close()
        raise refusal


def test_a_spent_rate_limit_stays_pending(lab):  # noqa: F811
    # A spent limit clears on its own, so it reads as pending, not as a token page.
    origin, state, etc, _ = lab
    origin.commit("a")
    limited = RateLimitedGitHub()
    assert builder((origin, state, etc, limited)).main() == 0
    assert limited.asked
    assert metric(state)[0] == "no-green"
