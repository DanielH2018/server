"""`render_share` computes a shared render once per pytest run, and only for that run.

Run: uv run pytest ansible/tests/repo/test_render_share.py

The two-process tests use real subprocesses, because the claim is about separate interpreters
taking one `flock`. The last one drives a real `-n 2` pytest run, because only that
exercises the plugin hooks: the controller reserving the run id, and deleting the directory
after the workers finish.
"""

import os
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

import render_share

TESTS = Path(render_share.__file__).parent

# Appends one line to a counter file each time `compute` runs, then sleeps so that two
# children started together overlap. Without the lock both would compute.
_CHILD = """
import sys, time
sys.path.insert(0, sys.argv[1])
import render_share

def compute():
    with open(sys.argv[2], "a") as counter:
        counter.write("computed\\n")
    time.sleep(1)
    return {"answer": 42}

assert render_share.shared_value(["fleet"], compute) == {"answer": 42}
"""


def _computed(counter: Path) -> int:
    return len(counter.read_text().splitlines()) if counter.exists() else 0


def _run_pair(tmp_path: Path, env_extra: dict[str, str]) -> int:
    """How many times two overlapping children computed the same key."""
    script = tmp_path / "child.py"
    script.write_text(_CHILD)
    counter = tmp_path / "counter"
    env = {k: v for k, v in os.environ.items() if k != render_share.RUN_ID_ENV}
    env.update(TMPDIR=str(tmp_path), **env_extra)
    argv = [sys.executable, "-I", str(script), str(TESTS), str(counter)]
    children = [subprocess.Popen(argv, env=env) for _ in range(2)]
    assert [child.wait(timeout=60) for child in children] == [0, 0]
    return _computed(counter)


def test_two_processes_sharing_one_run_id_compute_once(tmp_path):
    assert _run_pair(tmp_path, {render_share.RUN_ID_ENV: "one-run"}) == 1


def test_two_processes_with_no_run_id_each_compute_their_own(tmp_path):
    assert _run_pair(tmp_path, {}) == 2


def test_different_keys_compute_separately(tmp_path):
    calls = []

    def compute(value):
        calls.append(value)
        return value

    first = render_share.shared_value(["a"], lambda: compute("a"), root=tmp_path)
    second = render_share.shared_value(["b"], lambda: compute("b"), root=tmp_path)
    again = render_share.shared_value(["a"], lambda: compute("a"), root=tmp_path)
    assert (first, second, again) == ("a", "b", "a")
    assert calls == ["a", "b"]


def test_a_leftover_partial_file_is_recomputed_rather_than_read(tmp_path):
    # A worker killed mid-write leaves its temporary name behind and never publishes.
    (tmp_path / "left-behind.partial").write_bytes(b"\x80not a pickle")
    value = render_share.shared_value(["k"], lambda: {"fresh": True}, root=tmp_path)
    assert value == {"fresh": True}


def test_each_call_returns_its_own_copy(tmp_path):
    render_share.shared_value(["k"], lambda: {"files": []}, root=tmp_path)[
        "files"
    ].append("edited in place")
    assert render_share.shared_value(["k"], lambda: {"files": []}, root=tmp_path) == {
        "files": []
    }


def test_a_directory_others_can_write_is_refused(tmp_path):
    shared = tmp_path / "shared"
    shared.mkdir()
    shared.chmod(0o777)
    with pytest.raises(RuntimeError, match="not private"):
        render_share.shared_value(["k"], lambda: 1, root=shared)


def test_the_controller_deletes_the_run_directory_and_a_worker_does_not(
    tmp_path, monkeypatch
):
    # `gettempdir` caches its first answer, so the variable alone would not move it.
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    root = render_share.run_root("some-run")
    assert root is not None
    root.mkdir()

    def finish(**attributes):
        config = SimpleNamespace(
            getoption=lambda *_: "some-run", pluginmanager=_WITH_XDIST, **attributes
        )
        session = cast(pytest.Session, SimpleNamespace(config=config))
        render_share.pytest_sessionfinish(session)

    finish(workerinput={})
    assert root.is_dir()
    finish()
    assert not root.exists()


def test_with_no_run_id_one_process_computes_once_and_copies_each_call(
    tmp_path, monkeypatch
):
    monkeypatch.delenv(render_share.RUN_ID_ENV, raising=False)
    calls = []

    def compute():
        calls.append(1)
        return {"files": []}

    key = ["in-process", str(tmp_path)]
    render_share.shared_value(key, compute)["files"].append("edited in place")
    assert render_share.shared_value(key, compute) == {"files": []}
    assert calls == [1]


# The modules that read the shared fleet render. The census below must find each one, so a
# consumer whose import moved stops being prebuilt only with a red test.
KNOWN_CONSUMERS = frozenset(
    {
        "scripts/renderer/tests/test_renderer_collision_census.py",
        "scripts/renderer/tests/test_renderer_restart.py",
        "scripts/renderer/tests/test_renderer_secret_checks_tree.py",
        "scripts/renderer/tests/test_renderer_standalone_map.py",
        "scripts/renderer/tests/test_renderer_string_values_census.py",
        "scripts/secrets_mgmt/tests/test_cluster_vars_check.py",
        "scripts/secrets_mgmt/tests/test_cluster_vars_tree.py",
    }
)


def test_the_consumer_census_finds_every_known_reader_of_the_fleet_render():
    repo = TESTS.parents[1]
    tracked = subprocess.run(
        ["git", "-C", str(repo), "ls-files", "*test_*.py"],
        capture_output=True,
        text=True,
        check=True,
        timeout=60,  # one local git read of the index
    ).stdout.split()
    found = {path for path in tracked if render_share.is_consumer(repo / path)}
    assert found == KNOWN_CONSUMERS


def test_a_run_that_collected_a_consumer_builds_the_render(tmp_path):
    consumer = tmp_path / "test_reads.py"
    # Built from the constant: the literal here would make this module a consumer too.
    consumer.write_text(f"{render_share.CONSUMER_IMPORT} placeholder_renderer\n")
    other = tmp_path / "test_other.py"
    other.write_text("import render_share\n")
    built = []
    assert render_share.build_if_consumed({other, consumer}, lambda: built.append(1))
    assert built == [1]


def test_a_run_that_collected_no_consumer_builds_nothing(tmp_path):
    other = tmp_path / "test_other.py"
    other.write_text("import render_share\n")
    built = []
    missing = tmp_path / "test_gone.py"
    assert not render_share.build_if_consumed({other, missing}, lambda: built.append(1))
    assert built == []


_WITH_XDIST = SimpleNamespace(hasplugin=lambda name: True)

_MODULE = """
import sys
sys.path.insert(0, {tests!r})
import render_share

def test_it_shares():
    assert render_share.shared_value(["fleet"], compute) == 7

def compute():
    import time
    with open({counter!r}, "a") as counter:
        counter.write("computed\\n")
    time.sleep(1)
    return 7
"""


def test_a_real_xdist_run_computes_once_and_leaves_no_directory(tmp_path):
    for name in ("test_a.py", "test_b.py"):
        counter = tmp_path / "counter"
        (tmp_path / name).write_text(
            _MODULE.format(tests=str(TESTS), counter=str(counter))
        )
    (tmp_path / "pytest.ini").write_text("[pytest]\n")
    scratch = tmp_path / "tmp"
    scratch.mkdir()
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYTEST_")}
    env["TMPDIR"] = str(scratch)
    env["PYTHONPATH"] = str(TESTS)
    done = subprocess.run(
        [sys.executable, "-m", "pytest", "-n", "2", "-p", "render_share", "-q"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert done.returncode == 0, done.stdout + done.stderr
    assert _computed(tmp_path / "counter") == 1
    assert list(scratch.glob("homelab-pytest-share-*")) == []
