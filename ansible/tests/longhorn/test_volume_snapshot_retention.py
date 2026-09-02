"""What `k8s/volume-snapshot` retains, and how it names what it takes.

The two decisions that can be wrong without anyone noticing, pinned against the role's own
Jinja rather than a reimplementation of it:

  * **the retention window** -- newest-first, `markRemoved` CRs excluded from the count, and
    the newest never a candidate whatever `volume_snapshot_retain` says;
  * **the name/prefix coupling** -- the prune selects on `volume_snapshot_prefix`, so a
    snapshot named without that prefix would be invisible to its own retention pass and
    accumulate forever.

The synthetic `stdout_lines` injected here enter at the seam `test_volume_snapshot.py`'s
`test_the_listing_jsonpath_parses` proves against the live API server. `split` and `match`
are pulled from Ansible's own plugins via `render_expr`, so the expressions render against
the code Ansible runs; `jinja2.nativetypes` returns real Python objects where Ansible renders
"True"/"False" strings, which the role's `| int` and `| bool` coercions collapse identically.
"""

from __future__ import annotations

from _helpers import load_tasks as _tasks

from _helpers import render_expr as _render
from _volume_snapshot import _CLAIM, _MAIN, _named


# The retention expressions, read out of the live role by task name rather than copied here. A
# rename fails the extraction loudly; a copy would drift silently.
def _live_expression() -> str:
    return _named(_CLAIM, "Choose which older snapshots to prune")[
        "ansible.builtin.set_fact"
    ]["volume_snapshot_live"]


def _keep_expression() -> str:
    return _named(_CLAIM, "Choose which older snapshots to prune")[
        "ansible.builtin.set_fact"
    ]["volume_snapshot_keep"]


def _stale_expression() -> str:
    return _named(_CLAIM, "Prune snapshots beyond the retention window")["loop"]


def _prune(
    lines: list[str], retain: int, prefix: str = "autodeploy-widget-"
) -> list[str]:
    """The role's real retention decision, end to end, over a synthetic listing."""
    live = _render(
        _live_expression(),
        volume_snapshot_existing={"stdout_lines": lines},
        volume_snapshot_prefix=prefix,
    )
    keep = _render(_keep_expression(), volume_snapshot_retain=retain)
    return _render(
        _stale_expression(), volume_snapshot_live=live, volume_snapshot_keep=keep
    )


def _kept(
    lines: list[str], retain: int, prefix: str = "autodeploy-widget-"
) -> list[str]:
    live = _render(
        _live_expression(),
        volume_snapshot_existing={"stdout_lines": lines},
        volume_snapshot_prefix=prefix,
    )
    return [name for name in live if name not in _prune(lines, retain, prefix)]


def _line(created: str, name: str, removed: str = "false") -> str:
    return f"{created}|{removed}|{name}"


_FIVE = [
    _line("2026-08-17T10:00:00Z", "autodeploy-widget-11111111-widget-config"),
    _line("2026-08-21T10:00:00Z", "autodeploy-widget-55555555-widget-config"),
    _line("2026-08-18T10:00:00Z", "autodeploy-widget-22222222-widget-config"),
    _line("2026-08-20T10:00:00Z", "autodeploy-widget-44444444-widget-config"),
    _line("2026-08-19T10:00:00Z", "autodeploy-widget-33333333-widget-config"),
]

_NEWEST = "autodeploy-widget-55555555-widget-config"


def test_the_newest_snapshot_is_never_pruned() -> None:
    """Slice 7b reverts to the most recent snapshot.

    A retention pass that races a rollback destroys the recovery point it exists to protect, so
    this holds at every retain value including the ones a caller should not pass.
    """
    for retain in (0, 1, 2, 3, 5, 99):
        assert _NEWEST not in _prune(_FIVE, retain), (
            f"retain={retain} made the newest snapshot a deletion candidate"
        )


def test_retain_zero_clamps_to_two_rather_than_deleting_the_rollback_recovery_point() -> (
    None
):
    """Not a floor of 1:

    a rollback run takes its own snapshot and prunes BEFORE k8s/volume-revert reads the chain, so
    the chain must keep this run's own snapshot AND the earlier one the revert needs. See
    CLAUDE.md's "The revert needs two, not one".
    """
    assert _kept(_FIVE, 0) == [_NEWEST, "autodeploy-widget-44444444-widget-config"]
    assert len(_prune(_FIVE, 0)) == 3


def test_retain_one_also_clamps_to_two() -> None:
    assert _kept(_FIVE, 1) == [_NEWEST, "autodeploy-widget-44444444-widget-config"]
    assert len(_prune(_FIVE, 1)) == 3


def test_it_keeps_the_newest_n_and_prunes_the_rest_oldest_first() -> None:
    assert _kept(_FIVE, 3) == [
        _NEWEST,
        "autodeploy-widget-44444444-widget-config",
        "autodeploy-widget-33333333-widget-config",
    ]
    assert _prune(_FIVE, 3) == [
        "autodeploy-widget-22222222-widget-config",
        "autodeploy-widget-11111111-widget-config",
    ]


def test_a_window_that_is_not_full_prunes_nothing() -> None:
    assert _prune(_FIVE[:2], 3) == []


def test_creation_order_decides_not_listing_order() -> None:
    """kubectl returns items in name order, which for a SHA-tagged name is arbitrary.

    Sorting on the wrong field would keep three arbitrary snapshots and delete the newest often
    enough to look like bad luck rather than a bug.
    """
    assert _prune(list(reversed(_FIVE)), 3) == _prune(_FIVE, 3)


def test_markremoved_snapshots_are_excluded_from_the_window_not_counted_in_it() -> None:
    """A CR that survives its own delete is normal — the finalizer coalesces asynchronously.

    Counting three of those as the retained three would make the next pass delete live
    snapshots to make room, which is the opposite of what retention is for.
    """
    lines = _FIVE + [
        _line(
            "2026-08-22T10:00:00Z", "autodeploy-widget-66666666-widget-config", "true"
        ),
        _line(
            "2026-08-23T10:00:00Z", "autodeploy-widget-77777777-widget-config", "true"
        ),
        _line(
            "2026-08-24T10:00:00Z", "autodeploy-widget-88888888-widget-config", "true"
        ),
    ]
    kept = _kept(lines, 3)
    assert kept == _kept(_FIVE, 3), (
        "a markRemoved CR displaced a live snapshot from the window"
    )
    # And they are not re-deleted: deleting an already-removed snapshot is churn that reads as
    # progress, the failure mode longhorn-reap-orphan-snapshots.sh.j2 documents having shipped.
    assert not [name for name in _prune(lines, 3) if "6666" in name or "7777" in name]


def test_an_unpopulated_markremoved_is_treated_as_not_removed() -> None:
    """R14:

    a snapshot read moments after creation can have `status.markRemoved` still unpopulated,
    rendering `<ts>||<name>` rather than `<ts>|false|<name>`.

    An `equalto 'false'` filter drops that line out of the listing entirely — including THIS run's
    own snapshot, which fails the "found this run's snapshot" assert and the whole deploy before the
    apply, over a field that just hasn't been written yet. It must be counted as live, the same as
    an explicit 'false'.
    """
    empty_removed_line = (
        "2026-08-21T10:05:00Z||autodeploy-widget-99999999-widget-config"
    )
    lines = _FIVE + [empty_removed_line]
    live = _render(
        _live_expression(),
        volume_snapshot_existing={"stdout_lines": lines},
        volume_snapshot_prefix="autodeploy-widget-",
    )
    assert "autodeploy-widget-99999999-widget-config" in live


def test_snapshots_this_role_did_not_take_are_never_candidates() -> None:
    """Longhorn's own RecurringJob snapshots share the volume and must be left alone.

    Deleting one would silently break the incremental-backup chain the daily and weekly tiers
    diff against.
    """
    lines = _FIVE + [
        _line("2026-08-10T10:00:00Z", "daily-ba-4cd1b236-7e1e-4de1-bd0f-d419ffd6d5ad"),
        _line("2026-08-11T10:00:00Z", "c3f2c932-d89d-46f2-ac2c-34cafbab297e"),
    ]
    assert _prune(lines, 3) == _prune(_FIVE, 3)


def test_an_empty_listing_prunes_nothing_rather_than_erroring() -> None:
    assert _prune([], 3) == []


def test_the_snapshot_name_starts_with_the_prefix_the_prune_selects_on() -> None:
    """The one coupling that makes retention work at all.

    The prune matches `volume_snapshot_prefix`; the create uses `volume_snapshot_name`. Drift
    between them is invisible — every deploy takes a snapshot, no deploy ever prunes one, and
    the volume slowly fills with recovery points that also pin every block beneath them against
    `filesystem trim`.
    """
    facts = _named(_CLAIM, "Name the pre-deploy snapshot")["ansible.builtin.set_fact"]
    context = {
        "volume_snapshot_service": "widget",
        "volume_snapshot_claim": "widget-config",
        "volume_snapshot_sha": {"stdout": "a1b2c3d4\n"},
        "volume_snapshot_pvc": {"stdout": "pvc-0000\n"},
        "volume_snapshot_run_token": "20260821120000",
    }
    name = str(_render(facts["volume_snapshot_name"], **context)).strip()
    prefix = str(_render(facts["volume_snapshot_prefix"], **context))

    assert name.startswith(prefix)
    # The design's `autodeploy-<svc>-<sha8>` survives verbatim as a prefix, because slice 7b
    # reconstructs that string from the service and the deploy tag and matches on it.
    assert name.startswith("autodeploy-widget-a1b2c3d4")
    assert _prune([_line("2026-08-21T10:00:00Z", name)], 0, prefix) == []


def test_the_full_name_has_the_sha_claim_string_as_a_strict_prefix() -> None:
    """R2:

    the run token makes `autodeploy-<svc>-<sha8>-<claim>` non-unique by design (a rollback redeploy
    must not collide with that commit's earlier snapshot), so 7b's reconstruction is a prefix match
    rather than an equality test. Pin that the reconstructable string — service, sha8, and claim,
    with no run token — is still an exact prefix of whatever this role actually names the CR, and
    that the token is what comes after it.

    THE CLAIM SEGMENT DROPS A LEADING `<service>-`, so `widget-config` contributes `config`. That is
    not cosmetic: Longhorn's delete webhook caps a snapshot name at 63 bytes, and spending the
    service name twice put four real claims over it. See
    ansible/tests/longhorn/test_snapshot_name_length.py. volume-revert applies the same
    transformation when it reconstructs this prefix.
    """
    facts = _named(_CLAIM, "Name the pre-deploy snapshot")["ansible.builtin.set_fact"]
    context = {
        "volume_snapshot_service": "widget",
        "volume_snapshot_claim": "widget-config",
        "volume_snapshot_sha": {"stdout": "a1b2c3d4\n"},
        "volume_snapshot_run_token": "20260821120000",
    }
    name = str(_render(facts["volume_snapshot_name"], **context)).strip()
    assert name == "autodeploy-widget-a1b2c3d4-config-20260821120000"
    assert name.startswith("autodeploy-widget-a1b2c3d4-config")


def test_two_deploys_of_the_same_sha_get_two_names() -> None:
    """R2:

    redeploying an older commit is the manual rollback this slice exists to enable, and it must not
    be refused by its own snapshot step colliding with that commit's earlier,
    markRemoved-but-not-gone CR. Two runs with different tokens for the same service/claim/sha must
    produce two distinct names, both sharing the reconstructable prefix.
    """
    expression = _named(_CLAIM, "Name the pre-deploy snapshot")[
        "ansible.builtin.set_fact"
    ]["volume_snapshot_name"]
    names = {
        str(
            _render(
                expression,
                volume_snapshot_service="widget",
                volume_snapshot_claim="widget-config",
                volume_snapshot_sha={"stdout": "a1b2c3d4"},
                volume_snapshot_run_token=token,
            )
        ).strip()
        for token in ("20260810090000", "20260821120000")
    }
    assert len(names) == 2
    assert all(name.startswith("autodeploy-widget-a1b2c3d4-config") for name in names)


def test_two_claims_of_one_service_get_two_names() -> None:
    """pihole has two RWO claims.

    One name for both would make the second `apply` fight the first over `spec.volume` instead of
    taking a second snapshot.
    """
    expression = _named(_CLAIM, "Name the pre-deploy snapshot")[
        "ansible.builtin.set_fact"
    ]["volume_snapshot_name"]
    names = {
        str(
            _render(
                expression,
                volume_snapshot_service="pihole",
                volume_snapshot_claim=claim,
                volume_snapshot_sha={"stdout": "a1b2c3d4"},
                volume_snapshot_run_token="20260821120000",
            )
        ).strip()
        for claim in ("pihole-etc", "pihole-dnsmasq")
    }
    assert len(names) == 2


def test_the_run_token_is_computed_once_in_main_not_inside_the_claim_loop() -> None:
    """A token recomputed per claim would drift mid-role:

    the wait task in claim.yml polls for the exact name the apply task in the SAME claim.yml pass
    created, so two different `now()` calls for the same claim would never agree on it. Pin that the
    fact is set in main.yml (which runs once per role, before the per-claim loop) and nowhere in
    claim.yml (which runs once per claim, inside that loop).
    """
    main_task = _named(_MAIN, "Compute a per-run token")["ansible.builtin.set_fact"]
    assert "volume_snapshot_run_token" in main_task
    assert not any(
        "volume_snapshot_run_token" in (task.get("ansible.builtin.set_fact") or {})
        for task in _tasks(_CLAIM)
    ), (
        "the run token must be set once in main.yml, not recomputed inside claim.yml's loop"
    )


def test_the_run_token_uses_now_with_no_gathered_facts() -> None:
    """`now(utc=true, ...)` needs no `ansible_date_time` fact, unlike the alternative.

    Pinned so a future edit can't quietly reintroduce a `gather_facts` dependency this role doesn't
    have.
    """
    expression = _named(_MAIN, "Compute a per-run token")["ansible.builtin.set_fact"][
        "volume_snapshot_run_token"
    ]
    assert "now(utc=true" in expression.replace(" ", "").replace("'", "")
    assert "ansible_date_time" not in expression
