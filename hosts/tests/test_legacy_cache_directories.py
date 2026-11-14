"""No host package lists a cache directory as a `legacy:` path (#8463).

Reconcile retires a legacy directory with `rmdir` and fails the run while it holds a file, which
holds every update on the host. A tool's cache directory collects files with unpredictable names,
such as the scratch tree a sweep leaves when reconcile stops its unit mid-run, and no `legacy:`
entry can name those. `docs/host-packages.md`, *A legacy directory*, says what an author does
instead.

Run: uv run pytest hosts/tests/test_legacy_cache_directories.py
"""

from pathlib import PurePosixPath

from _host_packages import HOSTS, package

CACHE_ROOTS = (PurePosixPath("/var/cache"), PurePosixPath("/root/.cache"))


def is_cache_root(path: PurePosixPath) -> bool:
    if path in CACHE_ROOTS:
        return True
    return path.name == ".cache" and path.parent.parent == PurePosixPath("/home")


def cache_directories(legacy: list[dict]) -> list[str]:
    """The `path` entries that are a cache root or a directory directly inside one."""
    found = []
    for entry in legacy:
        if "path" not in entry:
            continue
        path = PurePosixPath(entry["path"])
        if is_cache_root(path) or is_cache_root(path.parent):
            found.append(entry["path"])
    return found


def test_no_host_package_lists_a_cache_directory_as_a_legacy_path():
    listed = {host: cache_directories(package(host).legacy) for host in HOSTS}
    assert listed == {host: [] for host in HOSTS}


def test_a_tool_cache_directory_is_flagged():
    legacy = [
        {"path": "/home/ubuntu/.cache/local-sweep/lock"},
        {"path": "/home/ubuntu/.cache/local-sweep"},
        {"path": "/var/cache/claude-agent-node"},
        {"path": "/root/.cache"},
    ]
    assert cache_directories(legacy) == [
        "/home/ubuntu/.cache/local-sweep",
        "/var/cache/claude-agent-node",
        "/root/.cache",
    ]


def test_a_named_file_and_a_directory_outside_a_cache_are_clean():
    legacy = [
        {"path": "/home/ubuntu/.cache/local-sweep/lock"},
        {"path": "/etc/homelab/interim"},
        {"path": "/home/ubuntu/.config/environment.d/90-claude-pytest-cap.conf"},
        {"unit": "homelab-local-sweep.timer"},
    ]
    assert cache_directories(legacy) == []
