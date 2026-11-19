"""The host wait's renders run the snapshot's renderer through `lib.snapshot_uv`.

A stub `uv` on `PATH` records what the render handed it and fails, so the render returns
None and nothing is rendered.

Run: uv run pytest scripts/deploy_tools/tests/test_land_host_render_snapshot.py
"""

from deploy_tools.land_lib import host_wait
from lib.proc_testing import fake_bin, path_with

STUB = 'printf "%s\\n" "PYTHONPATH=$PYTHONPATH" "UV_NO_SYNC=$UV_NO_SYNC" "$@" > "$0.argv"\nexit 1\n'


def test_a_render_imports_the_snapshots_code_without_syncing(monkeypatch, tmp_path):
    stubs = fake_bin(tmp_path / "bin", uv=STUB)
    monkeypatch.setenv("PATH", path_with(stubs))
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    assert host_wait.render_host_tree(snapshot, "daniel-pi") is None
    seen = (stubs / "uv.argv").read_text().splitlines()
    assert seen[:2] == [f"PYTHONPATH={snapshot / 'scripts'}", "UV_NO_SYNC=1"]
    assert seen[2:12] == [
        "run",
        "--no-sync",
        "--project",
        str(snapshot),
        "python",
        host_wait.RENDERER,
        "host",
        "--host",
        "daniel-pi",
        "--out",
    ]
