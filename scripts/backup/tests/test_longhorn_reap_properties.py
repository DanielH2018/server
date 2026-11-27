"""The backups reaper's classifier and its two selectors, over generated fleets.

The example tests beside this one hold fleets of two to four backups. These draw larger ones,
with mixed UTC offsets, fractional seconds, garbage stamps and ties on the creation time. The
oracle reads the drawn records, never the parser under test: each stamp carries the epoch it
was built from, truncated to whole seconds as `host_lib.rfc3339_to_epoch` truncates it. The
property tests run under the profile `tests/hypothesis_profiles.py` loads.

Run: uv run pytest scripts/backup/tests/test_longhorn_reap_properties.py
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone

import pytest
from hypothesis import given
from hypothesis import strategies as st

from backup.longhorn_reap_lib import logic
from backup.longhorn_reap_lib import selection
from _reap_entrypoint_harness import _backup

VOLUMES = ("vol-a", "vol-b", "vol-c")
GROUPS = ("daily-backup", "weekly-backup-d3", "weekly-backup-d6")
STRAY_JOBS = ("weekly-backup-d1", "daily-backup-r2")
CLAIMS = ("sonarr-config", "radarr-config")
COMPLETED = "Completed"

_BASE = datetime(2026, 11, 1, tzinfo=UTC)
_OFFSETS_MIN = (0, 60, -300, 330)
# The reaper reads 1970-01-01 as unparseable too; a stamp without an offset reads as local time.
_GARBAGE = (
    "",
    "yesterday",
    "2026-13-01T00:00:00Z",
    "2026-11-01T00:00:00",
    "1970-01-01T00:00:00Z",
)


@dataclass(frozen=True)
class Drawn:
    """One generated Backup CR and the epoch its stamp names, None when it names none."""

    name: str
    volume: str
    job: str
    state: str
    created: str
    epoch: float | None
    claim: str

    @property
    def labelled(self) -> bool:
        return bool(self.name and self.volume and self.state == COMPLETED and self.job)


@dataclass(frozen=True)
class Fleet:
    """A drawn backup list, the volume-to-group owner map and the live-volume set."""

    records: tuple[Drawn, ...]
    owner: dict[str, str]
    existing: frozenset[str]

    def items(self, records=None) -> list[dict]:
        return [
            _backup(r.name, r.volume, r.created, r.job, state=r.state, pvc=r.claim)
            for r in (self.records if records is None else records)
        ]

    def strays(self, volume: str) -> list[Drawn]:
        return [
            r
            for r in self.records
            if r.labelled and r.volume == volume and r.job != self.owner[volume]
        ]

    def rotation_count(self, volume: str) -> int:
        return sum(1 for r in self.records if r.labelled and r.volume == volume)


@st.composite
def _stamps(draw) -> tuple[str, float | None]:
    """An RFC 3339 stamp and its whole-second epoch, or garbage and None."""
    if draw(st.integers(0, 4)) == 0:
        return draw(st.sampled_from(_GARBAGE)), None
    instant = _BASE + timedelta(
        hours=draw(st.integers(0, 6)),
        microseconds=draw(st.sampled_from([0, 0, 250_000, 999_999])),
    )
    offset = timezone(timedelta(minutes=draw(st.sampled_from(_OFFSETS_MIN))))
    spec = draw(st.sampled_from(["seconds", "milliseconds", "microseconds"]))
    text = instant.astimezone(offset).isoformat(timespec=spec)
    if draw(st.booleans()):
        text = text.replace("+00:00", "Z")
    return text, instant.replace(microsecond=0).timestamp()


@st.composite
def _fleets(draw, min_live: int = 1) -> Fleet:
    """Up to a dozen backups over three volumes, some of them outside the live set."""
    suffixes = draw(
        st.lists(st.text("abc", max_size=3), unique=True, min_size=1, max_size=12)
    )
    records = []
    for suffix in suffixes:
        created, epoch = draw(_stamps())
        records.append(
            Drawn(
                name=draw(st.sampled_from(["", selection.SEED_NAME_PREFIX, "manual-"]))
                + suffix,
                volume=draw(st.sampled_from((*VOLUMES, "vol-gone", ""))),
                job=draw(st.sampled_from((*GROUPS, *STRAY_JOBS, "", ""))),
                state=draw(st.sampled_from([COMPLETED, COMPLETED, COMPLETED, "Error"])),
                created=created,
                epoch=epoch,
                claim=draw(st.sampled_from((*CLAIMS, ""))),
            )
        )
    owner = {v: draw(st.sampled_from(GROUPS)) for v in VOLUMES}
    existing = draw(st.frozensets(st.sampled_from(VOLUMES), min_size=min_live))
    return Fleet(tuple(records), owner, existing)


def _classify(fleet: Fleet, records=None) -> logic.BackupClassification:
    return logic.classify_backups(
        fleet.items(records), fleet.owner, set(fleet.existing)
    )


@given(_fleets(min_live=0))
def test_classify_backups_aborts_exactly_when_no_volume_is_live_and_labelled_backups_exist(
    fleet,
):
    if not fleet.existing and any(r.labelled for r in fleet.records):
        with pytest.raises(logic.ReapAbort):
            _classify(fleet)
    else:
        _classify(fleet)


@given(_fleets())
def test_classify_backups_puts_each_labelled_backup_but_the_current_ones_in_one_bucket(
    fleet,
):
    result = _classify(fleet)

    bucketed = [row[0] for row in (*result.kept, *result.candidates, *result.orphaned)]
    expected = [
        r.name
        for r in fleet.records
        if r.labelled
        and not (r.volume in fleet.existing and r.job == fleet.owner[r.volume])
    ]
    assert sorted(bucketed) == sorted(expected)


@given(_fleets())
def test_classify_backups_never_offers_a_backup_that_still_protects_its_volume(fleet):
    by_name = {r.name: r for r in fleet.records}
    candidates = [by_name[row[0]] for row in _classify(fleet).candidates]

    for c in candidates:
        assert c.job != fleet.owner[c.volume]
        assert c.epoch is not None
        assert any(
            r.labelled and r.volume == c.volume and r.job == fleet.owner[c.volume]
            for r in fleet.records
        )

    offered = {c.name for c in candidates}
    for volume in fleet.existing:
        dated = [r for r in fleet.strays(volume) if r.epoch is not None]
        if dated:
            newest = max(r.epoch for r in dated)
            assert any(r.name not in offered for r in dated if r.epoch == newest)


@given(_fleets(), st.data())
def test_classify_backups_does_not_depend_on_the_order_kubectl_lists_them(fleet, data):
    shuffled = data.draw(st.permutations(fleet.records))

    assert _classify(fleet, shuffled) == _classify(fleet)


def _seeds(fleet: Fleet, floor: int) -> selection.SeedSelection:
    return selection.select_seeds(fleet.items(), set(fleet.existing), floor=floor)


@given(_fleets(), st.integers(0, 5))
def test_select_seeds_supersedes_only_a_seed_its_volume_rotation_covers(fleet, floor):
    by_name = {r.name: r for r in fleet.records}

    for name, *_ in _seeds(fleet, floor).superseded:
        seed = by_name[name]
        assert name.startswith(selection.SEED_NAME_PREFIX)
        assert seed.job == ""
        assert seed.state == COMPLETED
        assert seed.volume in fleet.existing
        assert fleet.rotation_count(seed.volume) >= floor


@given(_fleets(), st.integers(0, 5))
def test_raising_the_seed_floor_never_adds_a_superseded_seed(fleet, floor):
    lower, higher = _seeds(fleet, floor), _seeds(fleet, floor + 1)

    assert set(higher.superseded) <= set(lower.superseded)
    assert higher.refusal == lower.refusal


@given(_fleets(min_live=0), st.integers(0, 5))
def test_select_seeds_refuses_any_fleet_when_no_volume_is_live(fleet, floor):
    result = selection.select_seeds(fleet.items(), set(), floor=floor)

    assert result.refusal is not None
    assert result.superseded == []


@given(_fleets(min_live=0), st.sampled_from(CLAIMS), st.sampled_from(VOLUMES))
def test_a_chain_selection_that_does_not_refuse_strands_no_live_volume(
    fleet, claim, current
):
    chain = selection.select_migrated_chain(
        fleet.items(), set(fleet.existing), claim=claim, current_volume=current
    )
    if chain.refusal is not None:
        return

    assert all(
        vol != current and vol not in fleet.existing for _, vol, *_ in chain.stranded
    )
    assert sorted(name for name, *_ in chain.stranded) == sorted(
        r.name
        for r in fleet.records
        if r.name
        and r.volume
        and r.state == COMPLETED
        and r.claim == claim
        and r.volume not in fleet.existing
    )
