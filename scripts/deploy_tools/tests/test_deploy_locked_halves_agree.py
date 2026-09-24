"""Until slice 4 of #2412, the foreground and `--detach` take the same locks from two files.

A foreground deploy runs `deploy_under_locks.py`; `--detach` still runs `deploy_locked.sh`.
Both reap the same snapshot root, recognise a live snapshot by the same owner lock, wait the
same budget, and read the same environment overrides. Two values that drifted apart would not
fail either arm on its own: a `--detach` run whose owner lock had another name would read
every foreground snapshot as dead and reap it from under a running playbook. So each value is
read from both files and compared here, and the test is deleted with `deploy_locked.sh`.

Run: uv run pytest scripts/deploy_tools/tests/test_deploy_locked_halves_agree.py
"""

import re
from pathlib import Path

import deploy_locks
import pytest

from deploy_tools import deploy_under_locks as py

_BASH = Path(__file__).resolve().parents[1] / "deploy_locked.sh"

# Each shared value: the bash assignment's name, and the Python constant that must equal it.
# The overridable ones also carry the environment variable both halves must read.
_PLAIN = {
    "LOCK_WAIT": str(py.LOCK_WAIT),
    "OWNER_LOCK": py.OWNER_LOCK,
}
_OVERRIDABLE = {
    "LOCK": ("HOMELAB_DEPLOY_TREE_LOCK", deploy_locks.TREE_LOCK),
    "SNAPSHOT_ROOT": ("HOMELAB_DEPLOY_SNAPSHOT_ROOT", py.SNAPSHOT_ROOT_DEFAULT),
    "TAG_LIST_TIMEOUT": (
        "HOMELAB_DEPLOY_TAG_LIST_TIMEOUT",
        str(py.TAG_LIST_TIMEOUT_DEFAULT),
    ),
    "REAP_MAX_PER_RUN": (
        "HOMELAB_DEPLOY_REAP_MAX_PER_RUN",
        str(py.REAP_MAX_PER_RUN_DEFAULT),
    ),
}


def _bash_assignment(name: str, text: str) -> str:
    """The right-hand side of `name=...` at the start of a line, quotes stripped."""
    m = re.search(rf"^{name}=(\S+)$", text, re.M)
    assert m, f"deploy_locked.sh no longer assigns {name} on a line of its own"
    return m[1].strip('"')


def _override(rhs: str) -> tuple[str, str]:
    """`${VAR:-default}` as (VAR, default)."""
    m = re.fullmatch(r"\$\{(\w+):-(.*)\}", rhs)
    assert m, f"{rhs!r} is not an overridable default"
    return m[1], m[2]


@pytest.mark.parametrize("name", sorted(_PLAIN))
def test_the_plain_values_agree(name):
    assert _bash_assignment(name, _BASH.read_text()) == _PLAIN[name]


@pytest.mark.parametrize("name", sorted(_OVERRIDABLE))
def test_the_overridable_values_agree_on_their_variable_and_their_default(name):
    variable, default = _OVERRIDABLE[name]
    assert _override(_bash_assignment(name, _BASH.read_text())) == (variable, default)
    assert f'"{variable}"' in Path(py.__file__).read_text(), (
        f"deploy_under_locks.py no longer reads {variable}, so the two halves honour "
        "different overrides"
    )


def test_a_drifted_value_is_flagged():
    """FLAGGED half: the reader returns the bash value, so a changed one would not compare."""
    text = 'LOCK_WAIT=1500\nSNAPSHOT_ROOT="${HOMELAB_DEPLOY_SNAPSHOT_ROOT:-/tmp/elsewhere}"\n'
    assert _bash_assignment("LOCK_WAIT", text) != _PLAIN["LOCK_WAIT"]
    assert (
        _override(_bash_assignment("SNAPSHOT_ROOT", text))
        != _OVERRIDABLE["SNAPSHOT_ROOT"]
    )


def test_the_python_half_honours_the_overrides_it_names(monkeypatch, tmp_path):
    """Driven, not read: the variable the text check found is the one the functions use."""
    monkeypatch.setenv("HOMELAB_DEPLOY_TREE_LOCK", str(tmp_path / "tree.lock"))
    monkeypatch.setenv("HOMELAB_DEPLOY_SNAPSHOT_ROOT", str(tmp_path / "snaps"))
    monkeypatch.setenv("HOMELAB_DEPLOY_LOCK_WAIT", "7")
    assert py.tree_lock_path() == str(tmp_path / "tree.lock")
    assert py.snapshot_root() == tmp_path / "snaps"
    assert py.lock_wait() == 7
