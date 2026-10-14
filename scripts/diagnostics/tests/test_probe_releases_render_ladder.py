"""The render-digest verdict over a single pair of records, with no git history behind it.

Split from `test_probe_releases_render.py`, which drives the same ladder through
`compute_stale` over throwaway repos. Every refusal here is a row the clearing case differs
from by one field.

Run: uv run pytest scripts/diagnostics/tests/test_probe_releases_render_ladder.py
"""

import pytest

from diagnostics.probe_lib import releases_render as rr

from _release_fixtures import _release, _render


TIP = "b" * 40


def test_a_trusted_render_proves_current_is_clean():
    assert rr.render_proves_current(_release("x", "a" * 40), _render("x", TIP), TIP)


SECRET = ["s.yaml"]


def test_a_trusted_render_with_matching_secret_digests_is_clean():
    assert rr.render_proves_current(
        _release("x", "a" * 40, secrets=SECRET, secret_digest="d1"),
        _render("x", TIP, secrets=SECRET, secret_digest="d1"),
        TIP,
    )


@pytest.mark.parametrize(
    ("release", "render"),
    [
        pytest.param(
            _release("x", "a" * 40), _render("x", "c" * 40), id="other-commit"
        ),
        pytest.param(
            _release("x", "a" * 40), _render("x", TIP, dirty=True), id="dirty"
        ),
        pytest.param(
            _release("x", "a" * 40),
            _render("x", TIP, dirty="False"),
            id="dirty-as-string",
        ),
        pytest.param(
            _release("x", "a" * 40), _render("x", TIP, host="daniel-server"), id="host"
        ),
        pytest.param(
            _release("x", "a" * 40, host=None), _render("x", TIP), id="no-host"
        ),
        pytest.param(
            _release("x", "a" * 40),
            _render("x", TIP, secrets=["s.yaml"]),
            id="render-secret",
        ),
        pytest.param(
            _release("x", "a" * 40, secrets=["s.yaml"]),
            _render("x", TIP),
            id="release-secret",
        ),
        pytest.param(_release("x", "a" * 40), None, id="no-render"),
        pytest.param(
            _release("x", "a" * 40, secrets=SECRET, secret_digest="d1"),
            _render("x", TIP, secrets=SECRET, secret_digest="d2"),
            id="secret-digest-differs",
        ),
        pytest.param(
            _release("x", "a" * 40, secrets=SECRET),
            _render("x", TIP, secrets=SECRET, secret_digest="d1"),
            id="release-predates-secret-digest",
        ),
        pytest.param(
            _release("x", "a" * 40, secrets=SECRET, secret_digest="d1"),
            _render("x", TIP, secrets=SECRET),
            id="render-predates-secret-digest",
        ),
        pytest.param(
            _release("x", "a" * 40, secrets=SECRET, secret_digest=""),
            _render("x", TIP, secrets=SECRET, secret_digest=""),
            id="both-keyless",
        ),
        pytest.param(
            _release("x", "a" * 40, secrets=SECRET, secret_digest="d1"),
            _render("x", TIP, secrets=["t.yaml"], secret_digest="d1"),
            id="secret-names-differ",
        ),
    ],
)
def test_an_untrusted_render_is_flagged(release, render):
    assert not rr.render_proves_current(release, render, TIP)


def test_an_unresolved_ref_is_flagged():
    assert not rr.render_proves_current(
        _release("x", "a" * 40), _render("x", TIP), None
    )


@pytest.mark.parametrize(
    ("release", "render", "expected"),
    [
        pytest.param(
            _release("x", "a" * 40), _render("x", TIP), rr.CURRENT, id="current"
        ),
        pytest.param(
            _release("x", "a" * 40),
            _render("x", TIP, digest="other"),
            rr.DRIFTED,
            id="drifted",
        ),
        pytest.param(
            _release("x", "a" * 40, secrets=SECRET, secret_digest="d1"),
            _render("x", TIP, secrets=SECRET, secret_digest="d2"),
            rr.DRIFTED,
            id="drifted-secret-digest",
        ),
        pytest.param(
            _release("x", "a" * 40, secrets=SECRET),
            _render("x", TIP, secrets=SECRET),
            rr.UNKNOWN,
            id="unknown-no-secret-digest",
        ),
        pytest.param(
            _release("x", "a" * 40),
            _render("x", "c" * 40, digest="other"),
            rr.UNKNOWN,
            id="unknown-other-commit-despite-a-mismatch",
        ),
    ],
)
def test_the_verdict_ladder_names_its_branch(release, render, expected):
    """A mismatch an untrustworthy record reports is UNKNOWN, never DRIFTED."""
    assert rr.digest_verdict(release, render, TIP)[0] == expected
