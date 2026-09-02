"""Which snapshot `k8s/volume-revert` reverts to.

The selection takes the newest by creation timestamp among the snapshots this deploy's own
prefix owns, reads an unpopulated `markRemoved` as live, and stops the prefix at a claim
boundary so `code-server-config` never matches `code-server-configmap`. The synthetic listings
injected here enter at the seam `test_volume_revert.py`'s `test_the_listing_jsonpath_parses`
proves against the live API server.
"""

from __future__ import annotations

from _helpers import render_expr as _render
from _volume_revert import _CLAIM, _named


def _selection(
    lines: list[str], sha: str = "abc12345", claim: str = "speedtest-config"
):
    """Render the role's own selection expression over a synthetic listing.

    The expression is read out of the live role by task name rather than copied here, so an
    edit to the role is what these tests see. `split`, `match` and `regex_escape` come from
    Ansible's own plugins, so they render against the code Ansible runs.
    """
    task = _named(_CLAIM, "Choose the newest matching snapshot")
    expression = task["ansible.builtin.set_fact"]["volume_revert_candidates"]
    prefix = _named(_CLAIM, "Name the snapshot prefix")["ansible.builtin.set_fact"][
        "volume_revert_prefix"
    ]
    rendered_prefix = _render(
        prefix,
        volume_revert_service="speedtest",
        volume_revert_sha=sha,
        volume_revert_claim=claim,
    )
    return _render(
        expression,
        volume_revert_existing={"stdout_lines": lines},
        volume_revert_prefix=str(rendered_prefix).strip(),
    )


_NEWEST = (
    "2026-08-21T18:00:00Z|false|autodeploy-speedtest-abc12345-config-20260821180000"
)

_OLDER = (
    "2026-08-21T09:00:00Z|false|autodeploy-speedtest-abc12345-config-20260821090000"
)


def test_the_selection_takes_the_newest_by_creation_timestamp() -> None:
    """One SHA can own several snapshots: volume-snapshot appends a per-run token, so a dirty
    tree deployed twice, or a retried deploy, leaves two CRs sharing the prefix. CR names are
    not chronologically sortable as strings, so the choice is made on `creationTimestamp`."""
    assert _selection([_OLDER, _NEWEST])[0].endswith("20260821180000")
    assert _selection([_NEWEST, _OLDER])[0].endswith("20260821180000")
    # The name and the timestamp deliberately disagree here: the newer CR carries the smaller
    # token. Sorting on the name would pick the other one, which is the whole reason the sort
    # names `creationTimestamp` — a listing where the two agree cannot tell the two sorts apart.
    misnamed = (
        "2026-08-21T19:00:00Z|false|autodeploy-speedtest-abc12345-config-00000000000001"
    )
    assert _selection([_NEWEST, misnamed])[0].endswith("00000000000001")


def test_the_selection_rejects_a_markremoved_snapshot() -> None:
    """Measured 2026-08-21: a snapshot already `markRemoved` cannot be reverted to. Taking one
    would fail the revert after the scale-down — and a retention pass racing a rollback is
    exactly how the newest becomes markRemoved."""
    removed = _NEWEST.replace("|false|", "|true|")
    assert _selection([_OLDER, removed]) == [
        "autodeploy-speedtest-abc12345-config-20260821090000"
    ]


def test_the_selection_reads_an_unpopulated_markremoved_as_live() -> None:
    """A snapshot read moments after creation can have `status.markRemoved` unwritten, which
    renders as an empty field. Not-removed is the correct read of an empty value; only an
    explicit `true` means removed, and `equalto 'false'` would drop a live snapshot."""
    assert _selection([_NEWEST.replace("|false|", "||")]) == [
        "autodeploy-speedtest-abc12345-config-20260821180000"
    ]


def test_the_selection_ignores_another_deploys_snapshots() -> None:
    """The prefix carries the service, the deploy's SHA and the claim. Reverting to a snapshot
    from a different commit restores data this deploy never wrote, and a different claim's
    snapshot belongs to a different volume entirely."""
    other_sha = _NEWEST.replace("abc12345", "def67890")
    other_claim = _NEWEST.replace("-config-", "-other-")
    assert _selection([other_sha, other_claim]) == []


def test_the_prefix_ends_at_a_claim_boundary() -> None:
    """Without the trailing separator, the prefix for claim `code-server-config` also matches a
    snapshot of `code-server-configmap`. The prefix ends on the `-` that precedes the run token,
    so a claim name that merely starts with another cannot answer for it.

    The residual case the separator does NOT close — a claim named `<other-claim>-something` —
    cannot matter, because the listing this filters is already scoped to THIS claim's own
    Longhorn volume in the jsonpath. No claim's snapshot ever appears in another claim's
    listing. Measured 2026-08-21: no pair of the thirteen declared claims has either shape.
    """
    prefix = _named(_CLAIM, "Name the snapshot prefix")["ansible.builtin.set_fact"][
        "volume_revert_prefix"
    ]
    assert str(prefix).strip().endswith("-")
    longer = "2026-08-21T18:00:00Z|false|autodeploy-speedtest-abc12345-speedtest-configmap-20260821180000"
    assert _selection([longer]) == []


def test_the_prefix_uses_the_callers_sha_verbatim() -> None:
    """`git rev-parse --short=8` returns MORE than eight characters when eight are ambiguous,
    and volume-snapshot names the snapshot from that raw stdout. Truncating here would fail to
    match a nine-character name at the `-<claim>-` boundary, so the caller's string is used as
    given and only its shape is checked."""
    prefix = str(
        _named(_CLAIM, "Name the snapshot prefix")["ansible.builtin.set_fact"][
            "volume_revert_prefix"
        ]
    )
    assert "volume_revert_sha }}" in prefix or "volume_revert_sha}}" in prefix
    assert "[:8]" not in prefix and "truncate" not in prefix
    nine = "abc123456"
    line = _NEWEST.replace("abc12345-", f"{nine}-")
    assert _selection([line], sha=nine) == [
        f"autodeploy-speedtest-{nine}-config-20260821180000"
    ]
