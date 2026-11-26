"""`forge.GitHub` reads every page of a commit's combined status (#5974).

GitHub's combined-status endpoint holds 100 contexts to a page. Commit c65600e1a carried 113,
so the 13 most recently updated sat on page 2. A reader of page 1 alone left verify reading
an expected `flux/<name>` as `none`, and promote run 39855439990 waited on it with no end.

Run: uv run pytest scripts/deploy_tools/tests/release/test_forge_status_pages.py
"""

import json
import subprocess
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlsplit

import pytest

from deploy_tools import forge
from deploy_tools.promote_lib import verify_promotion as vp

T = "a" * 40
STAMP = "2026-10-22T21:20:00+00:00"


def two_page_statuses(page_two: dict[str, str]):
    """A combined-status endpoint for T: 100 filler contexts, then `page_two`'s on page 2.

    A request with no `page` gets page 1, as GitHub answers it.
    """
    page_one = [
        {"context": f"ci/filler-{i:03d}", "state": "success", "updated_at": STAMP}
        for i in range(100)
    ]
    rest = [
        {"context": c, "state": s, "updated_at": STAMP} for c, s in page_two.items()
    ]
    requests: list[str] = []

    def transport(method, url, headers):
        requests.append(url)
        page = int(parse_qs(urlsplit(url).query).get("page", ["1"])[0])
        found = {1: page_one, 2: rest}.get(page, [])
        body = {"total_count": len(page_one) + len(rest), "statuses": found}
        return forge.Response(200, body=json.dumps(body).encode())

    return transport, requests


class Clock:
    """A monotonic clock that `sleep` advances, so the red path's wait costs no real time."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def test_verify_reads_an_expected_status_that_sits_on_the_second_page():
    expected = vp.Expected(frozenset({vp.ROOT}), frozenset(), "", 600)
    transport, requests = two_page_statuses({f"flux/{vp.ROOT}": "success"})
    clock = Clock()
    reads = vp.StatusReads(
        forge.GitHub(transport, lambda: "token").statuses, clock.sleep, clock
    )
    since = datetime(2026, 10, 22, 21, 15, tzinfo=UTC)
    assert vp.verify(reads, T, "b" * 40, expected, since) == vp.Verdict(
        vp.VerifyVerdict.GOOD
    )
    assert any("page=2" in url for url in requests), "the reader never asked for page 2"


def test_status_finds_a_context_that_sits_on_the_second_page():
    transport, _ = two_page_statuses({"promote/soak": "success"})
    found = forge.GitHub(transport, lambda: "token").status(T, "promote/soak")
    assert found == ("success", datetime.fromisoformat(STAMP))


def test_a_status_read_with_no_token_is_refused_before_any_request():
    transport, requests = two_page_statuses({"flux/soak": "success"})
    with pytest.raises(forge.ForgeError, match="no GitHub token"):
        forge.GitHub(transport, lambda: None).status(T, "flux/soak")
    assert requests == []


def _recording_statuses():
    statuses, _ = two_page_statuses({"flux/soak": "success"})
    sent: list[dict] = []

    def transport(method, url, headers):
        sent.append(headers)
        return statuses(method, url, headers)

    return transport, sent


def _gh_auth_token(failures: int):
    """A `run` whose `gh auth token` fails `failures` times, then prints a login token."""
    calls: list[list[str]] = []

    def run(argv, **_):
        assert argv == ["gh", "auth", "token"]
        calls.append(argv)
        if len(calls) <= failures:
            return subprocess.CompletedProcess(argv, 1, stdout="", stderr="keyring")
        return subprocess.CompletedProcess(argv, 0, stdout="gho-login\n", stderr="")

    return run


def test_a_failed_gh_lookup_fails_one_status_read_and_the_next_authenticates():
    transport, sent = _recording_statuses()
    github = forge.connect({}, transport, run=_gh_auth_token(failures=1)).github
    with pytest.raises(forge.ForgeError, match="no GitHub token"):
        github.status(T, "flux/soak")
    assert sent == []
    github.status(T, "flux/soak")
    assert [h["Authorization"] for h in sent] == ["Bearer gho-login"] * 2


def test_connect_keeps_the_job_token_over_gh_token_for_rest():
    transport, sent = _recording_statuses()
    env = {"GH_TOKEN": "promote-token", "GITHUB_TOKEN": "job-token"}
    forge.connect(env, transport, run=_gh_auth_token(failures=0)).github.status(
        T, "flux/soak"
    )
    assert [h["Authorization"] for h in sent] == ["Bearer job-token"] * 2
