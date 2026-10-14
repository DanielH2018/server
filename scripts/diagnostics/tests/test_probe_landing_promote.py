"""`probe.py landing`'s promotion lines: both holds, then `prod` and `last-good`.

The three reads are fakes handed to `collect`, so no test reaches GitHub, GHCR or the cluster.

Run: uv run pytest scripts/diagnostics/tests/test_probe_landing_promote.py
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from diagnostics.probe_lib import landing_blockers as landing
from diagnostics.probe_lib.promote_state import PromoteReads, artifact_of, hold_lines
from test_probe_landing import CLEAR, GREEN, fake_deploy_ui, fake_run

PROD = {"commit": "c" * 40, "digest": "sha256:" + "1" * 64}
OTHER = {"commit": "d" * 40, "digest": "sha256:" + "2" * 64}
HELD = {"hold_sha": "f" * 40, "hold_plane_entries": []}


def _broken():
    raise TimeoutError("the read timed out")


def _read(value):
    """`value` as a read: a callable stays one, and anything else is returned as it is."""
    return value if callable(value) else lambda: value


def _snap(deployer=CLEAR, deploy_hold="none", prod=PROD, last_good=PROD) -> dict:
    reads = PromoteReads(_read(deploy_hold), _read(prod), _read(last_good))
    return landing.collect(
        lambda: None, fake_deploy_ui(deployer), fake_run(GREEN), promote=reads
    )


def _lines(snap: dict) -> dict[str, str]:
    """Each promotion line by the words before its colon."""
    return {line.split(":", 1)[0]: line for line in hold_lines(snap)}


HOLD_SHA = "hold_sha (GitOps deployer, Ansible-owned tags)"
DEPLOY_HOLD = "DEPLOY_HOLD (promote workflow, Flux-owned tags)"


@pytest.mark.parametrize(
    "deployer, deploy_hold, want_sha, want_hold",
    [
        (HELD, f"{'e' * 40} failed=bento-pdf", "ffffffff", "failed=bento-pdf"),
        (HELD, "none", "ffffffff", ": none"),
        (CLEAR, "none", ": none", ": none"),
    ],
    ids=["both-set", "one-set", "both-none"],
)
def test_both_hold_lines_print_whatever_each_reads(
    deployer, deploy_hold, want_sha, want_hold
):
    lines = _lines(_snap(deployer, deploy_hold))
    assert want_sha in lines[HOLD_SHA]
    assert lines[DEPLOY_HOLD].endswith(want_hold)
    text = landing.format_text({**_snap(deployer, deploy_hold), "claims": None})
    assert lines[HOLD_SHA] in text and lines[DEPLOY_HOLD] in text


@pytest.mark.parametrize("dropped", [HOLD_SHA, DEPLOY_HOLD])
def test_a_view_that_drops_either_hold_line_is_flagged(dropped):
    """The red half: a text view missing one hold line fails the check the green half makes."""
    text = landing.format_text({**_snap(HELD), "claims": None})
    kept = "\n".join(line for line in text.splitlines() if not line.startswith(dropped))
    assert all(name in text for name in (HOLD_SHA, DEPLOY_HOLD))
    assert not all(name in kept for name in (HOLD_SHA, DEPLOY_HOLD))


def test_prod_equal_to_last_good_is_reported_when_both_digests_match():
    assert "last-good: the same digest as prod" in hold_lines(_snap())[2]


def test_a_failed_last_good_read_reads_unknown_and_never_equal_to_prod():
    snap = _snap(last_good=_broken)
    line = hold_lines(snap)[2]
    assert line.endswith("last-good: unknown")
    assert "same digest" not in line
    assert any(e.startswith("last_good: TimeoutError") for e in snap["errors"])
    assert snap["promote"]["deploy_hold"] == "none", "one failed read leaves the others"


def test_a_last_good_not_yet_seeded_reads_none():
    assert hold_lines(_snap(last_good=None))[2].endswith("last-good: none")


def test_a_last_good_on_another_digest_names_it():
    line = hold_lines(_snap(last_good=OTHER))[2]
    assert line.endswith(f"last-good: dddddddd {OTHER['digest'][:19]}")


def test_a_failed_deploy_hold_read_reads_unknown():
    assert _lines(_snap(deploy_hold=_broken))[DEPLOY_HOLD].endswith(": unknown")


def test_collect_without_the_promotion_reads_reads_none_of_them():
    snap = landing.collect(lambda: None, fake_deploy_ui(CLEAR), fake_run(GREEN))
    assert snap["promote"] == {"deploy_hold": None, "prod": None, "last_good": None}


def test_prod_comes_from_the_ocirepositorys_artifact_revision_and_metadata():
    artifact = {
        "revision": f"prod@{PROD['digest']}",
        "metadata": {
            "org.opencontainers.image.revision": f"master@sha1:{PROD['commit']}"
        },
    }
    assert artifact_of({"status": {"artifact": artifact}}) == PROD


def test_an_ocirepository_without_a_revision_is_an_error():
    with pytest.raises(ValueError, match=r"status\.artifact"):
        artifact_of({"status": {"artifact": {"revision": f"prod@{PROD['digest']}"}}})
