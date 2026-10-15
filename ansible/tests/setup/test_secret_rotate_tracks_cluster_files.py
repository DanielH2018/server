"""secret-rotate.sh adds the three cluster paths to TRACKED only once the cluster file exists.

`git add` and `git checkout --` both fail on a pathspec that matches nothing, so before slice
D3.2c commits the cluster file an unconditional entry would break every rotation's commit and
make `revert()` restore nothing. Each test runs the RENDERED `track_cluster_files` in bash,
against a tree in `tmp_path`, and reads the resulting array.

Run: uv run pytest ansible/tests/setup/test_secret_rotate_tracks_cluster_files.py
"""

from pathlib import Path

from _shell_render import rendered_shell_function, rendered_shell_text
from lib.proc_testing import run

BASE = ["ansible/vars/secrets.yml", "ansible/secret_rotation.yml"]
CLUSTER = [
    "ansible/vars/cluster-vars.sops.env",
    "ansible/vars/cluster-vars.source",
    "ansible/vars/cluster-manifests",
]


def _function() -> str:
    return rendered_shell_function(
        "setup", "initial_setup", "secret-rotate.sh.j2", "track_cluster_files"
    )


def tracked_after(function: str, root: Path) -> list[str]:
    """TRACKED, one entry per line, after `function` runs in `root`."""
    script = (
        f"TRACKED=({' '.join(BASE)})\n{function}\n"
        'track_cluster_files\nprintf "%s\\n" "${TRACKED[@]}"\n'
    )
    out = run(["bash", "-c", script], cwd=root, check=True)
    return out.stdout.splitlines()


def _with_cluster_file(root: Path) -> Path:
    (root / "ansible" / "vars").mkdir(parents=True)
    (root / CLUSTER[0]).write_text("")
    return root


def test_the_cluster_paths_join_tracked_when_the_cluster_file_exists(tmp_path):
    assert tracked_after(_function(), _with_cluster_file(tmp_path)) == BASE + CLUSTER


def test_tracked_is_unchanged_while_the_cluster_file_is_absent(tmp_path):
    assert tracked_after(_function(), tmp_path) == BASE


def test_an_unconditional_append_is_flagged(tmp_path):
    """The red half: a function without the existence check fails the absent-file case."""
    unconditional = f"track_cluster_files() {{\n  TRACKED+=({' '.join(CLUSTER)})\n}}"
    assert tracked_after(unconditional, tmp_path) != BASE


def test_the_call_runs_after_the_pull_and_before_rotate():
    """The call sits after the pull and before `rotate`.

    Before the pull, the first run after the file lands would miss it. After `rotate`,
    `revert()` would not cover what `write` changed.
    """
    text = rendered_shell_text("setup", "initial_setup", "secret-rotate.sh.j2")
    call = text.index("\ntrack_cluster_files\n")
    pull = text.index("\ngit pull --rebase")
    rotate = text.index("python scripts/secrets_mgmt/secret_rotation.py rotate")
    assert pull < call < rotate
