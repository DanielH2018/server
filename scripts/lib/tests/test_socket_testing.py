"""`lib.socket_testing` — a socket path that fits `sun_path`, and a bind that cannot leak.

Run: uv run pytest scripts/lib/tests/test_socket_testing.py
"""

import gc
import socket
import tempfile
import warnings
from pathlib import Path

import pytest

from lib.socket_testing import SUN_PATH_MAX, listening, short_socket_path


@pytest.fixture
def long_root(tmp_path, monkeypatch) -> Path:
    """A temp root long enough that `<root>/worktree-holders.sock` overflows `sun_path`."""
    root = tmp_path / ("r" * max(1, SUN_PATH_MAX + 1 - len(str(tmp_path))))
    root.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(root))
    return root


def test_a_socket_path_ignores_a_long_temp_root_and_binds_is_clean(long_root):
    with short_socket_path() as sock:
        assert len(str(sock).encode()) <= SUN_PATH_MAX
        assert not sock.is_relative_to(long_root)
        with listening(sock):
            assert sock.is_socket()
    assert not sock.parent.exists()


def test_a_socket_under_the_long_temp_root_is_flagged(long_root):
    # The shape before #4763: the socket sat in tmp_path, so the temp root's length decided.
    sock = Path(tempfile.gettempdir()) / "worktree-holders.sock"

    assert len(str(sock).encode()) > SUN_PATH_MAX
    with pytest.raises(OSError, match="AF_UNIX path too long"):
        listening(sock).close()


def _bind_then_listen(path: Path) -> socket.socket:
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(path))
    listener.listen(1)
    return listener


def _resource_warnings(bind, path: Path) -> list[warnings.WarningMessage]:
    """The `ResourceWarning`s left behind when `bind(path)` raises."""
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        with pytest.raises(OSError, match="AF_UNIX path too long"):
            bind(path)
        gc.collect()
    return [w for w in caught if issubclass(w.category, ResourceWarning)]


def test_a_failed_bind_closes_its_socket_is_clean(long_root):
    assert _resource_warnings(listening, long_root / "worktree-holders.sock") == []


def test_a_failed_bind_without_the_close_leaks_its_socket_is_flagged(long_root):
    assert _resource_warnings(_bind_then_listen, long_root / "worktree-holders.sock")
