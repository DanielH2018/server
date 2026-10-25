"""The `fanout-headroom` source cc-wait reads, driven through the real placement gates.

Run: uv run pytest scripts/dev/tests/test_fanout_headroom_probe.py
"""

import pytest

from fanout_lib import headroom_probe
from fanout_lib.brief import Issue
from fanout_lib.manifest import Batch, Manifest, save
from fanout_lib.place import main as place_main
from _fanout_fakes import HOST_KEY, fake_tools, ok

GIB = 1024**3


def _reply(cap_gib: int) -> str:
    """A host read with 1 byte used under a `cap_gib` cap on both planes, signing verifiably."""
    cap = cap_gib * GIB
    return f"1\n{cap}\n1\n{cap}\n0\n{HOST_KEY}\n"


ROOMY = _reply(12)
# Under one 2.5 GiB reservation, so placement has no headroom here.
TIGHT = _reply(2)
# Room for one reservation, but less than ROOMY, so unpinned placement prefers the other host.
SMALLER = _reply(6)


def _tools(box: str, server: str, **kwargs):
    tools, _run = fake_tools(
        answers={"daniel-box": ok(box), "daniel-server": ok(server)},
        issues=[Issue(1, "one", "body", ("claude",))],
        **kwargs,
    )
    return tools


def _box_at_its_cap(root):
    """Three uncleaned batches on daniel-box; with no unit state read back, each counts as running."""
    earlier = [
        Batch(f"{n}", "daniel-box", "/w", "b", "u", [n], "t") for n in (91, 92, 93)
    ]
    save(Manifest("20260101T000010Z", "o", earlier), root=root)


def test_roomy_hosts_with_no_live_batches_is_room(tmp_path):
    reading = headroom_probe.read(_tools(ROOMY, ROOMY), 1, None, tmp_path)
    assert reading["state"] == "room"
    assert "would place on" in reading["detail"]


def test_no_memory_headroom_on_either_host_is_full(tmp_path):
    reading = headroom_probe.read(_tools(TIGHT, TIGHT), 1, None, tmp_path)
    assert reading["state"] == "full"
    assert reading["detail"].startswith("no headroom for batch 1")


def test_the_host_placement_prefers_being_at_its_batch_cap_is_full(tmp_path):
    """Launch checks the cap only on the host with the most memory, so the wait does too."""
    _box_at_its_cap(tmp_path)
    reading = headroom_probe.read(_tools(ROOMY, SMALLER), 1, None, tmp_path)
    assert reading["state"] == "full"
    assert "daniel-box already holds 3 live batch(es)" in reading["detail"]
    assert "`--host daniel-server` would place" in reading["detail"]


def test_a_launch_pinned_to_the_host_with_a_free_slot_is_room(tmp_path):
    _box_at_its_cap(tmp_path)
    reading = headroom_probe.read(_tools(ROOMY, SMALLER), 1, "daniel-server", tmp_path)
    assert reading == {
        "state": "room",
        "detail": "1 batch(es) would place on daniel-server",
    }


def test_no_host_signing_verifiably_ends_the_wait_no_signing_host(tmp_path):
    tools = _tools(ROOMY, ROOMY, signing_keys=frozenset())
    reading = headroom_probe.read(tools, 1, None, tmp_path)
    assert reading["state"] == "no-signing-host"
    assert "signs commits GitHub verifies" in reading["detail"]


def test_no_readable_host_is_a_failed_read_not_full(tmp_path):
    tools, _run = fake_tools()  # Every host answers ssh's 255.
    with pytest.raises(headroom_probe.Unreadable, match="headroom read failed"):
        headroom_probe.read(tools, 1, None, tmp_path)


@pytest.mark.parametrize(
    ("box", "server", "at_cap", "state", "launch_exit"),
    [
        (ROOMY, ROOMY, False, "room", 0),
        (TIGHT, TIGHT, False, "full", 3),
        (ROOMY, SMALLER, True, "full", 3),
    ],
    ids=["room", "no-memory", "batch-cap"],
)
def test_the_wait_agrees_with_what_launch_does(
    tmp_path, box, server, at_cap, state, launch_exit
):
    """`room` and a placing launch, `full` and launch's exit 3, on the same hosts and manifests."""
    if at_cap:
        _box_at_its_cap(tmp_path)
    assert headroom_probe.read(_tools(box, server), 1, None, tmp_path)["state"] == state
    argv = ["launch", "--batch", "1", "--manifest-root", str(tmp_path)]
    assert place_main(argv, _tools(box, server)) == launch_exit


def test_the_declared_states_keep_cc_wait_s_contract():
    codes = set(headroom_probe.TERMINAL.values())
    assert not {2, 75} & codes
    assert any(code != 0 for code in codes)
    assert "full" not in headroom_probe.TERMINAL
