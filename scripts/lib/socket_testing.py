"""Give a test a unix socket path the kernel accepts, and a listener that cannot leak.

WHY A SHARED MODULE. Linux's `sun_path` holds 107 bytes plus the terminator, and a path
under pytest's `tmp_path` reaches 108 on daniel-box. `TMPDIR` there is `/tmp/user/1000`, and
xdist workers gw10 to gw15 add a second digit to `popen-gwN`. The bind then fails with
`AF_UNIX path too long`, but only on the runs where `--dist loadscope` hands the module to
one of those workers (#4763). `short_socket_path` roots the socket in `/tmp` itself, so its
length no longer depends on the temp root, the worker number or the test's name.

A bind that raises inside a bare `socket.socket()` leaves the socket for the garbage
collector. Its `ResourceWarning` then fails whichever test runs next on that worker, because
`pyproject.toml` sets `filterwarnings = ["error"]`. `listening` closes the socket when
`bind` or `listen` raises.

Import it from any test, at any depth. `pyproject.toml` puts `scripts/` on `pythonpath`::

    from lib.socket_testing import listening, short_socket_path

    with short_socket_path() as sock, listening(sock) as listener:
        ...

Run: uv run pytest scripts/lib/tests/test_socket_testing.py
"""

import shutil
import socket
import tempfile
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

__all__ = ["SUN_PATH_MAX", "listening", "short_socket_path"]

# The longest path `bind` accepts: `sun_path` is 108 bytes and the last one is the terminator.
SUN_PATH_MAX = 107


@contextmanager
def short_socket_path(name: str = "worktree-holders.sock") -> Generator[Path]:
    """Yield a socket path in a fresh directory under `/tmp`, and remove the directory on exit.

    The directory is `/tmp` itself rather than `tempfile.gettempdir()`, which reads `TMPDIR`
    and so brings back the long root this exists to avoid.

    Args:
        name: The socket's file name. The yielded path is about 20 bytes longer than it.
    """
    directory = Path(tempfile.mkdtemp(prefix="hs", dir="/tmp"))
    try:
        yield directory / name
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def listening(path: Path, backlog: int = 1) -> socket.socket:
    """Return a unix stream socket bound to `path` and listening.

    Raises:
        OSError: `bind` or `listen` failed. The socket is closed before the error propagates.
    """
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        listener.bind(str(path))
        listener.listen(backlog)
    except BaseException:
        listener.close()
        raise
    return listener
