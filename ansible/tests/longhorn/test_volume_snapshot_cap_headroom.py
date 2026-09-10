"""The snapshot-space cap gate `k8s/volume-snapshot` runs before it snapshots (#1560).

Longhorn refuses new snapshots once a volume reaches `spec.snapshotMaxSize`; it does not
prune to make room. `claim.yml` snapshots first and prunes second -- correct against a
failure mid-run -- so a reached cap latches: the snapshot is refused, the deploy fails on the
wait, the prune that would free headroom is never reached, and every later deploy of that
service fails identically.

The gate reads the cap and the space in use and fails with both numbers named. What is
guarded here is that it fires when the cap is genuinely full AND that it stays silent
everywhere else, because "everywhere else" is all 13 opted-in services on every deploy:

  * `"0"` is Longhorn's UNCAPPED value and the fleet default -- a gate that read it as a
    cap of zero bytes would stop every deploy in the fleet;
  * an absent field, a listing that matches nothing and a listing whose shape changed all
    render as no cap / no usage, because a bad read must not become a deploy stop;
  * `volume-head` is the live writable head, not a snapshot file, and Longhorn's own
    snapshot-space accounting excludes it -- counting it is the only way to overstate usage;
  * a `markRemoved` snapshot is on its way out and is not counted either.

Every case is a `..._is_flagged` / `..._is_clean` pair over the role's OWN Jinja, pulled by
task name via `render_expr` rather than reimplemented here, so a rename fails loudly and a
reworded expression is still tested.

Run: uv run pytest ansible/tests/longhorn/test_volume_snapshot_cap_headroom.py
"""

import shutil
import subprocess

import pytest
from _helpers import render_expr as _render
from _volume_snapshot import _CLAIM, _DEFAULTS, _named

from lib import yaml_fast


_CAP_TASK = "Measure the snapshot-space headroom"
_FAIL_TASK = "Fail on a snapshot-space cap with no headroom"
_WARN_TASK = "Warn on a snapshot-space cap that is nearly full"


def _measured(cap_stdout: str, listing: list[str]) -> tuple[str, str]:
    """The role's real cap/usage measurement over a synthetic pair of reads.

    Returned as strings: Ansible stores a `>-` folded `set_fact` value as text, and every
    consumer downstream coerces with `| int` / `| float`. Passing the strings through is the
    path that actually runs.
    """
    facts = _named(_CLAIM, _CAP_TASK)["ansible.builtin.set_fact"]
    context = {
        "volume_snapshot_cap_read": {"stdout": cap_stdout},
        "volume_snapshot_used_read": {"stdout_lines": listing},
    }
    return (
        str(_render(facts["volume_snapshot_cap_bytes"], **context)).strip(),
        str(_render(facts["volume_snapshot_used_bytes"], **context)).strip(),
    )


def _fires(task_name: str, cap_stdout: str, listing: list[str]) -> bool:
    """Whether every `when:` condition of the named task holds for these two reads."""
    cap, used = _measured(cap_stdout, listing)
    ratio = yaml_fast.safe_load(_DEFAULTS.read_text())["volume_snapshot_cap_warn_ratio"]
    return all(
        bool(
            _render(
                "{{ " + condition + " }}",
                volume_snapshot_cap_bytes=cap,
                volume_snapshot_used_bytes=used,
                volume_snapshot_cap_warn_ratio=ratio,
            )
        )
        for condition in _named(_CLAIM, task_name)["when"]
    )


def _snap(name: str, size: int, removed: str = "false") -> str:
    """One line of the role's `<name>|<markRemoved>|<size>` snapshot listing."""
    return f"{name}|{removed}|{size}"


# --- the gate fires ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("cap", "listing"),
    [
        # exactly at the cap: Longhorn refuses the next snapshot here, not one byte later
        ("100", [_snap("autodeploy-widget-a", 60), _snap("autodeploy-widget-b", 40)]),
        # past the cap, which a cap raised after the fact can produce
        ("100", [_snap("autodeploy-widget-a", 120)]),
    ],
)
def test_a_volume_at_its_cap_is_flagged(cap, listing):
    assert _fires(_FAIL_TASK, cap, listing), (
        f"a cap of {cap} bytes against {listing} left the pre-snapshot gate silent, so the "
        "deploy would take the 120s snapshot wait and fail with a message naming neither the "
        "cap nor the headroom -- which is the whole failure #1560 describes"
    )


def test_a_nearly_full_cap_is_flagged_by_the_warning():
    assert _fires(_WARN_TASK, "100", [_snap("autodeploy-widget-a", 95)]), (
        "95 bytes against a 100-byte cap is past volume_snapshot_cap_warn_ratio and did not "
        "warn -- the warning is the only advance notice before the cap latches"
    )


# --- the gate stays silent -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("case", "cap", "listing"),
    [
        # "0" is Longhorn's UNCAPPED value and the fleet default. Reading it as a zero-byte
        # cap would stop every deploy of every opted-in service.
        ("the fleet-default uncapped value", "0", [_snap("autodeploy-widget-a", 99)]),
        # spec.snapshotMaxSize absent: jsonpath prints nothing, rc 0.
        ("an absent cap field", "", [_snap("autodeploy-widget-a", 99)]),
        # a cap Longhorn renamed or reshaped: unparseable, so uncapped, so silent.
        ("a cap that is not an integer", "16Gi", [_snap("autodeploy-widget-a", 99)]),
        # ordinary headroom, which is every volume in the fleet today
        ("plenty of headroom", "100", [_snap("autodeploy-widget-a", 40)]),
        # volume-head is the live writable head, excluded from Longhorn's own accounting
        (
            "volume-head excluded from usage",
            "100",
            [_snap("volume-head", 90), _snap("autodeploy-widget-a", 10)],
        ),
        # a markRemoved snapshot is on its way out and is not holding space back
        (
            "markRemoved snapshots excluded from usage",
            "100",
            [
                _snap("autodeploy-widget-a", 40),
                _snap("autodeploy-widget-b", 99, "true"),
            ],
        ),
        # a listing that matches nothing: rc 0, empty stdout
        ("an empty listing", "100", []),
        # a listing whose shape changed must render 0 bytes rather than raise
        ("a listing whose shape changed", "100", ["autodeploy-widget-a", "garbage"]),
    ],
)
def test_a_volume_with_headroom_is_clean(case, cap, listing):
    assert not _fires(_FAIL_TASK, cap, listing), (
        f"{case}: cap {cap!r} against {listing} fired the pre-snapshot gate. This gate runs "
        "in front of every opted-in service on every deploy, so a false stop here is a "
        "fleet-wide deploy outage -- it must fail open on anything it cannot positively read "
        "as a full cap"
    )


def test_an_uncapped_volume_does_not_warn():
    assert not _fires(_WARN_TASK, "0", [_snap("autodeploy-widget-a", 10**12)]), (
        "an uncapped volume warned about headroom it does not have a cap for"
    )


# --- non-vacuity ---------------------------------------------------------------------------


def test_the_gate_reads_the_volume_and_the_snapshot_listing():
    """The two reads the measurement is computed from, by name and by what they select.

    `_named` already asserts exactly one match, so a rename fails here rather than leaving
    the pairs above rendering an expression nobody feeds.
    """
    cap_read = _named(_CLAIM, "Read the snapshot-space cap on the volume backing")
    used_read = _named(
        _CLAIM, "Read the snapshot space already in use on the volume backing"
    )
    argv = " ".join(cap_read["ansible.builtin.command"]["argv"])
    assert "snapshotMaxSize" in argv, (
        "the cap read no longer selects spec.snapshotMaxSize, so the gate measures nothing"
    )
    listing = " ".join(used_read["ansible.builtin.command"]["argv"])
    for field in ("metadata.name", "status.markRemoved", "status.size"):
        assert field in listing, (
            f"the snapshot listing no longer selects {field}, which the usage expression "
            "splits on by position -- dropping one silently shifts every field"
        )
    for read in (cap_read, used_read):
        assert read["check_mode"] is False, (
            f"{read['name']} is a read the gate below depends on; a command task is SKIPPED "
            "under --check, and a skipped read starves its consumer rather than failing itself"
        )
        assert read["failed_when"] is False, (
            f"{read['name']} must not fail the deploy on its own: a bad read has to render as "
            "'no cap' and let the deploy through, not stop 13 services"
        )


@pytest.mark.skipif(shutil.which("kubectl") is None, reason="no kubectl on this host")
def test_the_gates_reads_parse_and_return_the_field_it_sums() -> None:
    """Run both of the gate's own argvs against a real API server, then read `status.size`.

    Everything above injects synthetic `stdout` at the seam these two reads produce. A
    jsonpath kubectl rejects returns rc 1 and an empty read, which this gate is deliberately
    built to treat as "no cap" -- so a broken expression would leave every pair above passing
    while the gate silently never fires. This is the same seam test
    `test_the_listing_jsonpath_parses` runs for the prune listing, for the same reason.

    Allowlisted in `ansible/tests/leakguard.py::_LIVE_API_TESTS`: a stubbed kubectl would
    make this test prove nothing, which is the one thing it exists to rule out.
    """
    for task_name in (
        "Read the snapshot-space cap on the volume backing",
        "Read the snapshot space already in use on the volume backing",
    ):
        argv = _named(_CLAIM, task_name)["ansible.builtin.command"]["argv"]
        rendered = [
            str(_render(token, volume_snapshot_volume="pvc-does-not-exist"))
            for token in argv
        ]
        # Drop the `k3s` wrapper: the tests run as an unprivileged user against the read-only
        # kubeconfig, and `k3s kubectl` needs root here.
        assert rendered[0] == "k3s"
        result = subprocess.run(
            rendered[1:], capture_output=True, text=True, timeout=30, check=False
        )
        unreachable = (
            "connection refused",
            "was refused",
            "i/o timeout",
            "no configuration has been provided",
        )
        if any(token in result.stderr for token in unreachable):
            pytest.skip("no reachable cluster")
        # The cap read names a volume that does not exist, so kubectl exits 1 with NotFound —
        # which is exactly the rc the task's `failed_when: false` absorbs into "no cap". What
        # must NOT appear is a jsonpath parse error, which is a different failure entirely.
        assert "error executing jsonpath" not in result.stderr.lower(), (
            f"kubectl rejected {task_name!r}'s jsonpath: {result.stderr.strip()}"
        )
        assert "unrecognized character" not in result.stderr.lower(), (
            f"kubectl rejected {task_name!r}'s jsonpath: {result.stderr.strip()}"
        )

    # The other way to go silently green: `status.size` is the only field the measurement
    # sums, so a rename empties every third column, sums to 0 and never fires again.
    sizes = subprocess.run(
        [
            "kubectl",
            "-n",
            "longhorn-system",
            "get",
            "snapshots.longhorn.io",
            "-o",
            'jsonpath={range .items[0:1]}{.metadata.name}{"|"}{.status.size}{end}',
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if sizes.returncode != 0 or not sizes.stdout.strip():
        pytest.skip("no reachable cluster, or no Longhorn snapshots to read")
    name, _, size = sizes.stdout.strip().partition("|")
    assert name, "a Snapshot CR came back with no metadata.name"
    assert size.isdigit(), (
        f"snapshots.longhorn.io/{name} reports status.size={size!r}, which is not a byte "
        "count — the cap gate sums this field, and a non-numeric one sums to 0 and never fires"
    )
