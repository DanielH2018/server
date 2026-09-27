#!/usr/bin/env python3
"""The daily sweep that catches a rotated log on daniel-pi that stops decompressing.

`gzip -t` is the only reader that disagrees with a clobbered gzip trailer: the #2694 corruption
left every CRC32 matching, so `zcat` recovered every byte and nothing else on the host noticed
(#2715). The sweep runs that test daily and writes a verdict; `pi-sd-health.sh` reads the verdict
every 5 minutes and folds it into the Kuma push.

Two failure modes get their own tests here, because both read GREEN from the passing side:

1. **Vacuity.** A glob that matches nothing passes `gzip -t` trivially. The floor, and the
   missing-root branch, are what make "nothing was checked" a down instead.
2. **A stale verdict.** A sweep that stops running leaves yesterday's file on disk, and a
   heartbeat that trusted it would report the last good day forever.

The sweep is RUN, not pattern-matched -- what breaks here is shell logic and bash's own array
and quoting rules, none of which a textual guard can see.

Run: uv run pytest ansible/tests/setup/test_pi_gz_integrity_sweep.py
"""

import gzip
import os
import subprocess

import jinja2
import pytest
from _helpers import ANSIBLE
from _pi_health import OPTIMIZE_PI_DEFAULTS, run

SWEEP = ANSIBLE / "roles/setup/optimize_pi/templates/pi-gz-integrity.sh.j2"

# The floor the role ships. Read, not restated: a test carrying its own number would keep passing
# after someone lowered the real one to zero, which is the one edit that makes the check vacuous.
FLOOR = OPTIMIZE_PI_DEFAULTS["optimize_pi_gz_integrity_min_files"]


def _render(roots, state_file, floor=FLOOR):
    return (
        jinja2.Environment(undefined=jinja2.StrictUndefined)
        .from_string(SWEEP.read_text())
        .render(
            optimize_pi_gz_integrity_roots=[str(root) for root in roots],
            optimize_pi_gz_integrity_state_file=str(state_file),
            optimize_pi_gz_integrity_min_files=floor,
        )
    )


def sweep(tmp_path, roots, floor=FLOOR):
    """Run the real sweep against `roots`; return (verdict, detail, stdout)."""
    state = tmp_path / "state" / "gz-integrity.state"
    script = tmp_path / "pi-gz-integrity.sh"
    script.write_text(_render(roots, state, floor))
    script.chmod(0o755)

    done = subprocess.run(["bash", str(script)], capture_output=True, text=True)
    assert done.returncode == 0, f"sweep failed: {done.stderr}"

    verdict, detail = state.read_text().splitlines()
    return verdict, detail, done.stdout


def good_gz(directory, count, start=0):
    directory.mkdir(parents=True, exist_ok=True)
    for i in range(start, start + count):
        path = directory / f"log.{i}.gz"
        with gzip.open(path, "wb") as handle:
            handle.write(b"a log line\n" * 8)


def clobber(path):
    """Overwrite the gzip ISIZE trailer, exactly as #2694's rsync did.

    The content and its CRC32 stay intact -- only the last four bytes change -- so this is the
    corruption that `zcat` survives and `gzip -t` reports.
    """
    raw = bytearray(path.read_bytes())
    raw[-4:] = b"\x7a\x18\x12\x78"
    path.write_bytes(bytes(raw))


def test_a_healthy_tree_reads_up_and_counts_what_it_checked(tmp_path):
    """ACCEPT: every .gz decompresses, above the floor."""
    roots = [tmp_path / "log", tmp_path / "hdd.log"]
    good_gz(roots[0], FLOOR)
    good_gz(roots[1], FLOOR)

    verdict, detail, stdout = sweep(tmp_path, roots)

    assert verdict == "up", detail
    assert f"{FLOOR * 2} rotated .gz pass" in detail, detail
    assert detail in stdout, (
        "the journal line must carry the same detail as the verdict file"
    )


def test_a_clobbered_trailer_reads_down_and_names_the_file(tmp_path):
    """REJECT: the #2694 corruption, on the tree it actually landed on."""
    roots = [tmp_path / "log", tmp_path / "hdd.log"]
    good_gz(roots[0], FLOOR)
    good_gz(roots[1], FLOOR)
    broken = roots[1] / "log.3.gz"
    clobber(broken)

    verdict, detail, _ = sweep(tmp_path, roots)

    assert verdict == "down", detail
    assert str(broken) in detail, (
        f"the verdict must name the failing file so it can be repaired: {detail}"
    )
    assert detail.startswith(f"1 of {FLOOR * 2}"), detail


def test_many_clobbered_files_are_summarised_rather_than_all_named(tmp_path):
    """Kuma truncates a push msg at 900 chars, so 37 paths would lose the count as well."""
    roots = [tmp_path / "log"]
    good_gz(roots[0], FLOOR)
    for i in range(6):
        clobber(roots[0] / f"log.{i}.gz")

    verdict, detail, _ = sweep(tmp_path, roots)

    assert verdict == "down", detail
    assert "(+3 more)" in detail, detail


def test_a_tree_below_the_floor_reads_down(tmp_path):
    """REJECT the vacuous pass: too few files means the sweep stopped seeing its subject."""
    roots = [tmp_path / "log"]
    good_gz(roots[0], FLOOR - 1)

    verdict, detail, _ = sweep(tmp_path, roots)

    assert verdict == "down", detail
    assert f"floor {FLOOR}" in detail, detail


def test_an_empty_tree_reads_down_rather_than_passing_over_nothing(tmp_path):
    """The extreme of the same case: `gzip -t` over no files exits 0."""
    roots = [tmp_path / "log"]
    roots[0].mkdir()

    verdict, detail, _ = sweep(tmp_path, roots)

    assert verdict == "down", detail
    assert "only 0 rotated .gz" in detail, detail


def test_a_missing_root_reads_down(tmp_path):
    """/var/hdd.log absent means log2ram is not mounted -- the SD-side tree is unswept."""
    roots = [tmp_path / "log", tmp_path / "hdd.log"]
    good_gz(roots[0], FLOOR * 2)

    verdict, detail, _ = sweep(tmp_path, roots)

    assert verdict == "down", detail
    assert str(roots[1]) in detail, detail
    assert "log2ram may not be mounted" in detail, detail


@pytest.mark.skipif(os.geteuid() == 0, reason="root can read a 0000 file")
def test_an_unreadable_gz_reads_down_instead_of_being_skipped(tmp_path):
    """`find -readable` keeps the sweep quiet, so the file it cannot open must be reported."""
    roots = [tmp_path / "log"]
    good_gz(roots[0], FLOOR)
    (roots[0] / "log.0.gz").chmod(0o000)

    verdict, detail, _ = sweep(tmp_path, roots)

    assert verdict == "down", detail
    assert "unreadable" in detail, detail


# -- the half the heartbeat owns: it reads the verdict and pushes it ------------------------


def _verdict_file(tmp_path, verdict, detail):
    state = tmp_path / "gz-integrity.state"
    state.write_text(f"{verdict}\n{detail}\n")
    return state


def _heartbeat(tmp_path, state):
    return run(
        "pi-sd-health",
        tmp_path,
        counter="0",
        jinja_vars={"optimize_pi_gz_integrity_state_file": str(state)},
    )


def test_the_heartbeat_pushes_down_on_a_down_verdict(tmp_path):
    """A clean ext4 counter must not outvote a corrupt rotated log."""
    state = _verdict_file(
        tmp_path, "down", "2 of 107 rotated .gz fail gzip -t: /var/hdd.log/x.gz"
    )

    status, msg, _, _ = _heartbeat(tmp_path, state)

    assert status == "down", msg
    assert "fail gzip -t" in msg, msg
    assert "errors_count=0" in msg, "the ext4 reading must survive alongside it"


def test_the_heartbeat_stays_up_on_an_up_verdict(tmp_path):
    """The RED half of the test above: without this, a check wired to always push down passes."""
    state = _verdict_file(tmp_path, "up", "107 rotated .gz pass gzip -t")

    status, msg, _, _ = _heartbeat(tmp_path, state)

    assert status == "up", msg
    assert "pass gzip -t" in msg, msg


def test_a_missing_verdict_reads_down(tmp_path):
    """A sweep that never ran leaves no file, and an absent verdict is not a passing one."""
    status, msg, _, _ = _heartbeat(tmp_path, tmp_path / "never-written.state")

    assert status == "down", msg
    assert "the daily sweep is not running" in msg, msg


def test_a_stale_verdict_reads_down(tmp_path):
    """Yesterday's verdict is the failure mode a file-backed source adds over a live read."""
    state = _verdict_file(tmp_path, "up", "107 rotated .gz pass gzip -t")
    age_h = OPTIMIZE_PI_DEFAULTS["optimize_pi_gz_integrity_max_age_h"] + 1
    stale = state.stat().st_mtime - age_h * 3600
    os.utime(state, (stale, stale))

    status, msg, _, _ = _heartbeat(tmp_path, state)

    assert status == "down", msg
    assert "older than" in msg, msg
