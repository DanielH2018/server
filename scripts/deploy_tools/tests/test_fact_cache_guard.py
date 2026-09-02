"""Tests for the Ansible fact-cache guard.

Every rule here is a `..._is_clean` / `..._is_flagged` pair. A guard that fires on
everything and one that fires on nothing look identical from the passing side alone, so
each rule ships with the input it must accept as well as the input it must reject.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from deploy_tools import fact_cache_guard as g  # noqa: E402


def write_cache(
    cache_dir: Path, host: str, interpreter: str, wrapped: bool = True
) -> Path:
    """Write one host's fact cache entry naming `interpreter`."""
    facts = {
        "discovered_interpreter_python": interpreter,
        "ansible_python": {"executable": interpreter},
    }
    doc = {"__payload__": json.dumps(facts)} if wrapped else facts
    path = cache_dir / f"s1_{host}"
    path.write_text(json.dumps(doc))
    return path


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A fake checkout with a primary venv and two worktrees, one of them ours."""
    (tmp_path / ".venv" / "bin").mkdir(parents=True)
    (tmp_path / ".venv" / "bin" / "python3.14").touch()
    for name in ("ours", "theirs"):
        d = tmp_path / ".claude" / "worktrees" / name / ".venv" / "bin"
        d.mkdir(parents=True)
        (d / "python3.14").touch()
    return tmp_path


@pytest.fixture
def cache(tmp_path: Path) -> Path:
    d = tmp_path / "factcache"
    d.mkdir()
    return d


# --- the primary checkout's interpreter --------------------------------------------------


def test_primary_checkout_interpreter_is_clean(repo: Path, cache: Path):
    write_cache(cache, "daniel-box", str(repo / ".venv/bin/python3.14"))
    assert g.scan(cache, our_worktree=None) == []


def test_missing_interpreter_is_flagged(repo: Path, cache: Path):
    write_cache(cache, "daniel-box", str(repo / ".venv/bin/python3.99"))
    (entry, path, reason) = g.scan(cache, our_worktree=None)[0]
    assert entry.name == "s1_daniel-box"
    assert "no longer exists" in reason


# --- worktree ownership ------------------------------------------------------------------


def test_our_own_worktree_interpreter_is_clean(repo: Path, cache: Path):
    ours = repo / ".claude/worktrees/ours/.venv/bin/python3.14"
    write_cache(cache, "daniel-box", str(ours))
    assert g.scan(cache, our_worktree="ours") == []


def test_another_live_worktree_is_flagged(repo: Path, cache: Path):
    """A foreign path that still resolves is tomorrow's outage, so it is stale today."""
    theirs = repo / ".claude/worktrees/theirs/.venv/bin/python3.14"
    assert theirs.exists()  # the point of the case: it resolves right now
    write_cache(cache, "daniel-box", str(theirs))
    (_, _, reason) = g.scan(cache, our_worktree="ours")[0]
    assert "theirs" in reason


# --- cache file shapes -------------------------------------------------------------------


def test_unwrapped_payload_is_read(repo: Path, cache: Path):
    """Older ansible-core writes a bare fact dict; the guard must still see the interpreter."""
    write_cache(cache, "daniel-box", str(repo / ".venv/bin/python3.99"), wrapped=False)
    assert g.scan(cache, our_worktree=None) != []


def test_malformed_entry_is_flagged(cache: Path):
    (cache / "s1_daniel-box").write_text("{not json")
    (_, _, reason) = g.scan(cache, our_worktree=None)[0]
    assert "unreadable or malformed" in reason


def test_absent_cache_dir_is_clean(tmp_path: Path):
    assert g.scan(tmp_path / "nope", our_worktree=None) == []


def test_directories_in_the_cache_are_skipped(repo: Path, cache: Path):
    (cache / ".ansible").mkdir()
    write_cache(cache, "daniel-box", str(repo / ".venv/bin/python3.14"))
    assert g.scan(cache, our_worktree=None) == []


# --- worktree_name -----------------------------------------------------------------------


def test_worktree_name_reads_the_segment_pair():
    assert g.worktree_name("/srv/x/.claude/worktrees/foo/.venv/bin/python") == "foo"


def test_worktree_name_is_none_outside_a_worktree():
    assert g.worktree_name("/srv/x/.venv/bin/python") is None


# --- clearing ----------------------------------------------------------------------------


def test_clear_removes_only_the_stale_entry(repo: Path, cache: Path, monkeypatch):
    write_cache(cache, "daniel-box", str(repo / ".venv/bin/python3.99"))
    good = write_cache(cache, "daniel-pi", str(repo / ".venv/bin/python3.14"))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "fact_cache_guard.py",
            "--clear",
            "--cache-dir",
            str(cache),
            "--repo-root",
            str(repo),
        ],
    )
    assert g.main() == 0
    assert not (cache / "s1_daniel-box").exists()
    assert good.exists()


def test_report_without_clear_exits_nonzero_and_keeps_the_file(
    repo: Path, cache: Path, monkeypatch
):
    write_cache(cache, "daniel-box", str(repo / ".venv/bin/python3.99"))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "fact_cache_guard.py",
            "--cache-dir",
            str(cache),
            "--repo-root",
            str(repo),
        ],
    )
    assert g.main() == 1
    assert (cache / "s1_daniel-box").exists()


def test_clean_cache_exits_zero(repo: Path, cache: Path, monkeypatch):
    write_cache(cache, "daniel-box", str(repo / ".venv/bin/python3.14"))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "fact_cache_guard.py",
            "--clear",
            "--cache-dir",
            str(cache),
            "--repo-root",
            str(repo),
        ],
    )
    assert g.main() == 0


# --- ansible.cfg resolution --------------------------------------------------------------


def test_cache_dir_is_read_from_ansible_cfg(tmp_path: Path):
    (tmp_path / "ansible.cfg").write_text(
        "[defaults]\nfact_caching = jsonfile\nfact_caching_connection = ~/somewhere/facts\n"
    )
    assert g.cache_dir_from_cfg(tmp_path) == Path.home() / "somewhere" / "facts"


def test_cache_dir_falls_back_without_ansible_cfg(tmp_path: Path):
    assert (
        g.cache_dir_from_cfg(tmp_path) == Path.home() / ".cache" / "ansible" / "facts"
    )


def test_the_real_repo_cfg_still_names_a_cache_dir():
    """The guard is worthless if ansible.cfg stops setting fact_caching_connection."""
    repo_root = Path(__file__).resolve().parents[3]
    assert g.cache_dir_from_cfg(repo_root).name == "facts"
