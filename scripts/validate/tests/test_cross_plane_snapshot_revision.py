"""The gate's snapshot sets the revision in the environment, never as a flag.

An older renderer, such as the base side's, has no --revision flag and refuses one with exit 2.
"""

import subprocess

import pytest

from validate import cross_plane_order as gate


def test_the_snapshot_sets_the_revision_by_env_and_passes_no_flag(monkeypatch):
    seen = {}

    def fake_run(cmd, **kwargs):
        seen.update(cmd=cmd, env=kwargs["env"])
        return subprocess.CompletedProcess(cmd, 1, "", "stop here")

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(gate.RenderFailed):
        gate.snapshot(gate.REPO, roles=["bento-pdf"])
    assert "--revision" not in seen["cmd"]
    assert seen["env"]["HOMELAB_RENDER_REVISION"] == "0" * 40
