"""grafana_dashboard_uids.py and asset_pins.py print nothing on stdout when they exit non-zero.

Most tests run a script as a subprocess against a scratch tree holding a copy of `scripts/lib`
and the script. Both scripts read their repo from their own location, so the copy runs
unpatched. A subprocess also sidesteps `asset_pins.main`'s `out=sys.stdout` default, which is
bound at import, where `capsys` cannot see it. Every pin in the asset_pins tree has a URL that
does not render, so each check and refresh fails on `Pin.error` before it fetches anything.
The suite runs under `-p leakguard`.

A run where some pins pass before the run fails needs a successful fetch, which a subprocess
cannot fake. Those runs call `asset_pins.main` in-process with a fake fetcher instead.

Run: uv run pytest scripts/validate/tests/test_failure_lines_go_to_stderr.py
"""

import hashlib
import json
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest
from lib import proc_testing
from lib.repo_paths import REPO
from validate import asset_pins

# The `[FAIL] <path>: <reason>` line every validator shares (#4480).
FAIL_LINE = re.compile(r"\s*\[FAIL\] \S+: \S")
UNRENDERABLE_URL = "https://example.invalid/{{ undefined_version }}/asset"
DIGEST = "ab" * 32
UNPAIRED_DIGEST_KEY = "stray_tool_sha256"


def _copy_script(root: Path, script: str) -> Path:
    """Copy `scripts/lib` and one validator under `root`; return the copied validator."""
    shutil.copytree(
        REPO / "scripts/lib",
        root / "scripts/lib",
        ignore=shutil.ignore_patterns("tests", "__pycache__"),
    )
    target = root / "scripts/validate" / script
    target.parent.mkdir(parents=True)
    shutil.copy2(REPO / "scripts/validate" / script, target)
    return target


def _lines_beyond_fail_lines(err: str) -> list[str]:
    return [
        line for line in err.splitlines() if line.strip() and not FAIL_LINE.match(line)
    ]


# ── grafana_dashboard_uids.py ───────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def grafana_failure(tmp_path_factory) -> subprocess.CompletedProcess[str]:
    """One run over a tree holding one board whose panel names an unprovisioned uid."""
    root = tmp_path_factory.mktemp("grafana")
    script = _copy_script(root, "grafana_dashboard_uids.py")
    role = root / "ansible/roles/k8s/observability"
    (role / "templates").mkdir(parents=True)
    (role / "templates/grafana.yaml.j2").write_text(
        "datasources:\n  - name: Prometheus\n    uid: prometheus\n"
    )
    (role / "files/dashboards").mkdir(parents=True)
    board = {
        "uid": "u",
        "title": "Board",
        "panels": [{"title": "P", "datasource": {"uid": "not-provisioned"}}],
    }
    (role / "files/dashboards/board.json").write_text(json.dumps(board))
    return proc_testing.run([sys.executable, str(script)])


def test_grafana_dashboards_prints_nothing_on_stdout_when_it_fails(grafana_failure):
    assert grafana_failure.returncode == 1, grafana_failure.stderr
    assert grafana_failure.stdout == "", grafana_failure.stdout


def test_grafana_dashboards_prints_its_summary_on_stderr(grafana_failure):
    """The line after the `[FAIL]` lines, which counts them, moves to stderr with them."""
    assert grafana_failure.returncode == 1, grafana_failure.stderr
    assert _lines_beyond_fail_lines(grafana_failure.stderr), (
        grafana_failure.stdout + grafana_failure.stderr
    )


# ── asset_pins.py ───────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Refusal:
    """One asset_pins run that exits non-zero.

    Attributes:
        argv: the arguments after the script.
        exit_code: the code the run exits with.
        names: a name from this run's input that its refusal carries, or None where the
            only line beyond the `[FAIL]` lines is the summary.
        drop: a known pin the scratch tree leaves out, so the census refuses.
    """

    argv: tuple[str, ...]
    exit_code: int
    names: str | None
    drop: str | None = None


REFUSALS = [
    pytest.param(Refusal((), 1, UNPAIRED_DIGEST_KEY), id="a-pin-that-fails-its-check"),
    pytest.param(
        Refusal(("--refresh", "jellyfin_k8s_anisync"), 1, None),
        id="a-pin-that-fails-its-refresh",
    ),
    pytest.param(
        Refusal((), 1, "k3s_install_script", drop="k3s_install_script"),
        id="a-census-missing-a-known-pin",
    ),
    pytest.param(Refusal(("--only", "nope"), 64, "nope"), id="an-unknown-only-name"),
    pytest.param(
        Refusal(("--refresh", "vendor/nope"), 64, "vendor/nope"),
        id="an-unknown-refresh-name",
    ),
]


def _defaults_carrying(pins: set[str]) -> str:
    """A defaults file carrying each named pin with an unrenderable URL, plus an unpaired digest.

    A flat name becomes a `<name>_url` / `<name>_sha256` pair. A `<key>[<item>]` name becomes
    a one-item list under `<key>`, the shape `valheim_k8s_mods` has.
    """
    lines = []
    for name in sorted(pins):
        listed = re.fullmatch(r"(\w+)\[(.+)\]", name)
        if listed:
            lines += [
                f"{listed[1]}:",
                f"  - name: {listed[2]}",
                f'    url: "{UNRENDERABLE_URL}"',
                f'    sha256: "{DIGEST}"',
            ]
        else:
            lines += [f'{name}_url: "{UNRENDERABLE_URL}"', f'{name}_sha256: "{DIGEST}"']
    lines.append(f'{UNPAIRED_DIGEST_KEY}: "{DIGEST}"')
    return "\n".join(lines) + "\n"


@pytest.fixture(scope="module", params=REFUSALS)
def asset_pins_refusal(request, tmp_path_factory):
    """`(refusal, completed run)` for one REFUSALS case, over a tree built for it."""
    refusal: Refusal = request.param
    root = tmp_path_factory.mktemp("asset_pins")
    script = _copy_script(root, "asset_pins.py")
    (root / "renovate.json").write_text(json.dumps({"customManagers": []}))
    defaults = root / "ansible/roles/k8s/scratch/defaults/main.yml"
    defaults.parent.mkdir(parents=True)
    defaults.write_text(_defaults_carrying(set(asset_pins.KNOWN_PINS) - {refusal.drop}))
    return refusal, proc_testing.run([sys.executable, str(script), *refusal.argv])


def test_asset_pins_prints_nothing_on_stdout_when_it_refuses(asset_pins_refusal):
    refusal, run = asset_pins_refusal
    assert run.returncode == refusal.exit_code, run.stdout + run.stderr
    assert run.stdout == "", run.stdout


def test_asset_pins_prints_its_refusal_on_stderr(asset_pins_refusal):
    """What the run used to print on stdout reaches stderr instead of being dropped."""
    refusal, run = asset_pins_refusal
    assert run.returncode == refusal.exit_code, run.stdout + run.stderr
    if refusal.names is None:
        assert _lines_beyond_fail_lines(run.stderr), run.stdout + run.stderr
    else:
        assert refusal.names in run.stderr, run.stdout + run.stderr


# ── asset_pins.py: lines printed before the run knows it fails ──────────────────────────────

SERVED_URL = "https://example.invalid/served"
MISSING_URL = "https://example.invalid/missing"
PAYLOAD = b"asset bytes"
PAYLOAD_SHA256 = hashlib.sha256(PAYLOAD).hexdigest()
OLD_DIGEST = "0" * 64


def _serve(url: str):
    """200 and PAYLOAD for SERVED_URL, 404 for any other URL."""
    return (200, iter([PAYLOAD])) if url == SERVED_URL else (404, iter(()))


def _pin(name: str, url: str, digest: str) -> asset_pins.Pin:
    return asset_pins.Pin(name, url, "sha256", digest, "defaults.yml")


MIXED_RUNS = [
    pytest.param(
        ["-v"],
        [
            _pin("fresh", SERVED_URL, PAYLOAD_SHA256),
            _pin("stale", SERVED_URL, OLD_DIGEST),
        ],
        1,
        id="a-verbose-check-with-one-stale-pin",
    ),
    pytest.param(
        ["--refresh", "fresh,gone"],
        [_pin("fresh", SERVED_URL, OLD_DIGEST), _pin("gone", MISSING_URL, "1" * 64)],
        1,
        id="a-refresh-with-one-unreadable-pin",
    ),
    pytest.param(
        ["--refresh", "vendor/digestless,vendor/nope"],
        [_pin("fresh", SERVED_URL, PAYLOAD_SHA256)],
        64,
        id="a-refresh-naming-a-digestless-and-an-unknown-dep",
    ),
]


@pytest.mark.parametrize(("argv", "pins", "exit_code"), MIXED_RUNS)
def test_asset_pins_keeps_lines_printed_before_the_failure_off_stdout(
    argv, pins, exit_code, tmp_path, capsys
):
    """The `ok` and `nothing to refresh` lines a run prints before it fails stay off stdout."""
    (tmp_path / "defaults.yml").write_text(f"fresh_sha256: {OLD_DIGEST}\n")
    code = asset_pins.main(
        argv,
        fetcher=_serve,
        # Passed explicitly: capsys replaced sys.stdout after the default was bound at import.
        out=sys.stdout,
        discover=lambda: (pins, []),
        known=frozenset(pin.name for pin in pins),
        root=tmp_path,
        annotated=lambda: {"vendor/digestless"},
    )
    assert code == exit_code, capsys.readouterr()
    assert capsys.readouterr().out == ""
