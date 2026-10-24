#!/usr/bin/env python3
"""The restore drill runs under the pinned runner image, not only under GNU coreutils (H2.3d).

`alpine/k8s` is built from `alpine:edge` and installs no `coreutils`, so `mktemp`, `date`, `sort`
and `stat` are BusyBox applets over musl. A script that passes on the host can fail there. These
tests run the rendered script, the `kubectl` stub and the fixtures inside the pinned digest with
`docker run --rm`.

The leg skips when no Docker daemon answers and `CI` is unset, and fails when `CI` is set, so CI
always runs it.

Run: uv run pytest ansible/tests/longhorn/test_longhorn_restore_drill_image.py
"""

import os
from pathlib import Path

import pytest

from _restore_drill import harness, image_leg_available, launch_in_image

A = "a-config"

# The template as it stood before H2.3c: a suffix after the X's, which BusyBox's mktemp refuses.
_SUFFIXED = "mktemp /tmp/restore-drill.XXXXXX.yaml"


@pytest.fixture
def docker_available() -> None:
    if image_leg_available():
        return
    if os.environ.get("CI"):
        pytest.fail("CI must run the image leg, and no Docker daemon answered")
    pytest.skip("no Docker daemon here; CI runs the image leg")


def test_a_passing_drill_completes_under_the_pinned_image(
    tmp_path: Path, docker_available: None
) -> None:
    run = harness(tmp_path, [A])
    proc = launch_in_image(run, restore="pass")
    assert proc.returncode == 0, proc.stderr
    assert f"longhorn-restore-drill: PASS: {A} restored" in proc.stdout, proc.stdout
    assert run.epoch(f"success.{A}") > 0


def test_a_suffixed_mktemp_template_passes_on_the_host_and_fails_under_the_image(
    tmp_path: Path, docker_available: None
) -> None:
    """The red case: GNU `mktemp` accepts the suffix, BusyBox does not, and the apply then fails."""
    run = harness(tmp_path, [A])
    text = run.script.read_text()
    assert "mktemp /tmp/restore-drill.XXXXXX" in text
    run.script.write_text(text.replace("mktemp /tmp/restore-drill.XXXXXX", _SUFFIXED))
    assert run(restore="pass").returncode == 0, (
        "the host's GNU mktemp accepts the suffix"
    )
    (tmp_path / "image").mkdir()
    red = harness(tmp_path / "image", [A])
    red.script.write_text(
        red.script.read_text().replace("mktemp /tmp/restore-drill.XXXXXX", _SUFFIXED)
    )
    proc = launch_in_image(red, restore="pass")
    assert proc.returncode == 1, proc.stdout
    assert "could not create the drill volume" in proc.stderr, proc.stderr
