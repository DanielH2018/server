"""`forge.GitHub` reads every page of a commit's combined status (#5974).

GitHub's combined-status endpoint holds 100 contexts to a page. Commit c65600e1a carried 113,
so the 13 most recently updated sat on page 2. A reader of page 1 alone left verify reading
an expected `flux/<name>` as `none`, and promote run 39855439990 waited on it with no end.

Run: uv run pytest scripts/deploy_tools/tests/test_forge_status_pages.py
"""

import json
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlsplit

import forge
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
    reads = vp.StatusReads(forge.GitHub(transport, None).statuses, clock.sleep, clock)
    since = datetime(2026, 10, 22, 21, 15, tzinfo=UTC)
    assert vp.verify(reads, T, "b" * 40, expected, since) == vp.Verdict("good")
    assert any("page=2" in url for url in requests), "the reader never asked for page 2"


def test_status_finds_a_context_that_sits_on_the_second_page():
    transport, _ = two_page_statuses({"promote/soak": "success"})
    found = forge.GitHub(transport, None).status(T, "promote/soak")
    assert found == ("success", datetime.fromisoformat(STAMP))
