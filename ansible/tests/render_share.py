"""Let the xdist workers of one pytest run share one placeholder render of the fleet.

Registered as a plugin from `addopts` in pyproject.toml, beside `leakguard` and `xdist_width`.
The tests import `shared_value`, `placeholder_renderer` and `sharing_placeholder_renders`
from here by bare name, because `ansible/tests` is on `pythonpath`.

## Why

`--dist loadscope` keeps a module on one worker, so a module-scoped fixture that renders the
fleet runs once per module. Six modules rendered the same fleet, 10 to 16 seconds each:
the standalone-map, secret-checks-tree, restart and string-values tests, the two
`cluster_vars` tests, and the collision census through `homelab_render.cluster`. The render is
a pure function of the tree, and the tree does not change during a run.

## What is shared

`render_placeholders(tags, keys)` with its default host, roles and image contexts. The first
worker to need a render takes an exclusive `flock`, renders, and pickles the result to a
temporary name. `os.replace` then publishes it under its final name, which is the completion
marker, so a reader never sees a partial file. The others wait on the lock and unpickle the
file. Each call unpickles afresh and returns its own copy: a test edits a render in place, and
no two tests may see each other's edit.

The key is the tags and the keys the caller passed. A call with any other argument bypasses
the cache and renders directly, since another host, roles tree or contexts function changes
the output. The `job_census` module renders the published tags only, which is a different
key, so nothing shares it.

## Lifetime

The cache is per run. The controller reserves the run id at `pytest_configure` and deletes the
run directory then and again at `pytest_sessionfinish`. The first delete clears a directory
left by an earlier run that reused a `--testrunuid`. The second runs after xdist has shut its
workers down, so no worker is still reading. A run killed before that leaves its directory in
the temporary directory, where nothing reads it: the next run has a new id.

Without xdist there is no run id, and `shared_value` keeps the pickled value in the process
instead. No cache survives between runs, and nothing hashes the tree, because a stale cache
would let a test pass while checking nothing.

## Who pays for the render

DECIDED: the fleet render leaves `--durations`, as the k8s render in `conftest.py` does.
`pytest_collection_finish` builds it before any test's timed phases, in a run that collected
a module importing from here. Charged to a test instead, it landed on whichever module asked
first, which varied by shard and by run, while the serial `pytest_shard.py --record` run
charged every module alone. The shard weight gate then failed CI on every module that did
not pay: a recorded 11.58s against a measured 0.03s (#5734). Under xdist each worker waits on
the one build before its first test, so a shard that holds a consumer pays about one render
of wall time that no weight models.

Run: uv run pytest ansible/tests/repo/test_render_share.py
"""

import contextlib
import fcntl
import hashlib
import json
import os
import pickle
import shutil
import tempfile
import uuid
from collections.abc import Callable, Generator
from pathlib import Path
from types import ModuleType

import pytest

RUN_ID_ENV = "PYTEST_XDIST_TESTRUNUID"
# The text that marks a test module as a reader of the shared fleet render.
CONSUMER_IMPORT = "from render_share import"

# The pickled values of a run with no xdist run id, by key digest.
_IN_PROCESS: dict[str, bytes] = {}


def run_root(run_id: str | None = None) -> Path | None:
    """The directory shared by every worker of the run `run_id`, or None outside xdist.

    Args:
        run_id: The run's id; None reads the id xdist sets in each worker's environment.
    """
    run_id = run_id or os.environ.get(RUN_ID_ENV)
    if not run_id:
        return None
    return Path(tempfile.gettempdir()) / f"homelab-pytest-share-{os.getuid()}-{run_id}"


def _private(root: Path) -> Path:
    """`root`, created if need be, after checking that only this user can write to it.

    The cache holds pickles, and unpickling a file another user wrote runs their code.

    Raises:
        RuntimeError: `root` belongs to another user or is writable by group or others.
    """
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    status = root.stat()
    if status.st_uid != os.getuid() or status.st_mode & 0o022:
        raise RuntimeError(f"{root} is not private to this user; refusing to read it")
    return root


def shared_value[T](
    key: object, compute: Callable[[], T], *, root: Path | None = None
) -> T:
    """The value `compute` returns, computed once per run and key, and a fresh copy per call.

    Args:
        key: Every input that changes what `compute` returns, as JSON-serialisable data.
        compute: Returns the value to share, which must be picklable.
        root: The shared directory; None derives it from the xdist run id. With neither,
            the value is kept in this process only.

    Raises:
        RuntimeError: `root` is not private to this user.
    """
    root = root or run_root()
    digest = hashlib.sha256(json.dumps(key, sort_keys=True).encode()).hexdigest()[:24]
    if root is None:
        if digest not in _IN_PROCESS:
            _IN_PROCESS[digest] = pickle.dumps(compute())
        return pickle.loads(_IN_PROCESS[digest])
    final = _private(root) / f"{digest}.pickle"
    if not final.exists():
        with open(root / f"{digest}.lock", "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            # Another worker may have published while this one waited on the lock.
            if not final.exists():
                temporary = root / f"{digest}.{os.getpid()}.partial"
                temporary.write_bytes(pickle.dumps(compute()))
                os.replace(temporary, final)
    return pickle.loads(final.read_bytes())


def placeholder_renderer(real: Callable | None = None) -> Callable:
    """A drop-in for `render_placeholders` that shares the render of its default arguments.

    Args:
        real: The function to share; None imports `render_placeholders`. A patched module
            passes the original, because importing it by name would find this function.
    """
    if real is None:
        from renderer.renderer_lib.placeholders import render_placeholders as real

    def render(tags, keys, *rest, **kwargs):
        if rest or kwargs:
            return real(tags, keys, *rest, **kwargs)
        return shared_value(
            ["render_placeholders", list(tags), list(keys)],
            lambda: real(list(tags), list(keys)),
        )

    return render


@contextlib.contextmanager
def sharing_placeholder_renders(*modules: ModuleType) -> Generator[None]:
    """Inside the block, each of `modules` shares the renders of its `render_placeholders`.

    The block patches the name in each module's namespace, which is where a module that
    imported it by name, or a function that imports it at call time, looks it up.
    """
    with pytest.MonkeyPatch.context() as patch:
        for module in modules:
            patch.setattr(
                module,
                "render_placeholders",
                placeholder_renderer(module.render_placeholders),
            )
        yield


def is_consumer(path: Path) -> bool:
    """Whether the test module at `path` imports from here, and so reads the fleet render."""
    try:
        return CONSUMER_IMPORT in path.read_text()
    except OSError, UnicodeDecodeError:
        return False


def prebuild() -> None:
    """Build the shared render of the fleet: every render target, with every secret key."""
    from renderer.homelab_render import secret_keys
    from renderer.renderer_lib.cluster import select_tags

    placeholder_renderer()(select_tags(None), secret_keys())


def build_if_consumed(paths: set[Path], build: Callable[[], None] = prebuild) -> bool:
    """Run `build` when any test module in `paths` is a consumer, and say whether it ran."""
    consumed = any(is_consumer(path) for path in paths)
    if consumed:
        build()
    return consumed


def pytest_collection_finish(session: pytest.Session) -> None:
    """Build the fleet render before any test's timed phases, when a consumer was collected.

    Under xdist this runs in each worker, which all collect every module. The first takes
    the lock and builds, and the rest wait on it and read the file.
    """
    if not session.config.option.collectonly:
        build_if_consumed({item.path for item in session.items})


@pytest.hookimpl(tryfirst=True)
def pytest_configure(config: pytest.Config) -> None:
    """Reserve the run's id on the controller, and clear any directory a reused id left."""
    if hasattr(config, "workerinput") or not config.pluginmanager.hasplugin("xdist"):
        return
    if config.getoption("testrunuid", None) is None:
        config.option.testrunuid = uuid.uuid4().hex
    _remove(run_root(config.getoption("testrunuid")))


@pytest.hookimpl(trylast=True)
def pytest_sessionfinish(session: pytest.Session) -> None:
    """Delete the run directory once xdist has shut every worker down."""
    config = session.config
    if hasattr(config, "workerinput") or not config.pluginmanager.hasplugin("xdist"):
        return
    _remove(run_root(config.getoption("testrunuid", None)))


def _remove(root: Path | None) -> None:
    if root is not None and root.is_dir() and root.stat().st_uid == os.getuid():
        shutil.rmtree(root, ignore_errors=True)
