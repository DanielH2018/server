"""The ghcr-mirror store's offline prune, `files/ghcr-mirror-prune.sh` (#5541).

The script runs under `sh` against a store tree built in `tmp_path` with the distribution
filesystem layout: a revision link per cached manifest, a blob holding each manifest's body,
and a tag directory whose `current/link` names a digest. A revision's age is its link's mtime.
"""

import hashlib
import json
import os
from pathlib import Path

from _helpers import ANSIBLE
from lib.proc_testing import run

SCRIPT = (
    ANSIBLE / "roles" / "setup" / "k8s_host_files" / "files" / "ghcr-mirror-prune.sh"
)
REPO = "danielh2018/nut"


class Store:
    """A mirror store under `root`, with helpers to cache manifests and read back what is left."""

    def __init__(self, root: Path):
        self.root = root
        self.v2 = root / "docker" / "registry" / "v2"

    def _manifests(self, repo: str) -> Path:
        return self.v2 / "repositories" / repo / "_manifests"

    def cache(self, age: int, children: tuple[str, ...] = (), repo: str = REPO) -> str:
        """Cache one manifest `age` days old, an index when it has `children`; return its digest."""
        body = {"schemaVersion": 2, "age": age}
        if children:
            body["manifests"] = [{"digest": c} for c in children]
        data = json.dumps(body).encode()
        hex_ = hashlib.sha256(data).hexdigest()
        blob = self.v2 / "blobs" / "sha256" / hex_[:2] / hex_ / "data"
        blob.parent.mkdir(parents=True, exist_ok=True)
        blob.write_bytes(data)
        link = self._manifests(repo) / "revisions" / "sha256" / hex_ / "link"
        link.parent.mkdir(parents=True)
        link.write_text(f"sha256:{hex_}")
        stamp = 1_700_000_000 - age * 86_400
        os.utime(link, (stamp, stamp))
        return f"sha256:{hex_}"

    def tag(self, name: str, digest: str, repo: str = REPO) -> None:
        current = self._manifests(repo) / "tags" / name / "current" / "link"
        current.parent.mkdir(parents=True)
        current.write_text(digest)

    def revisions(self, repo: str = REPO) -> set[str]:
        revs = self._manifests(repo) / "revisions" / "sha256"
        return {f"sha256:{p.name}" for p in revs.iterdir()}

    def tags(self, repo: str = REPO) -> set[str]:
        return {p.name for p in (self._manifests(repo) / "tags").iterdir()}


def _prune(tmp_path: Path, keep: list[str], newest: int = 2) -> str:
    keep_file = tmp_path / "keep"
    keep_file.write_text("".join(f"{d}\n" for d in keep))
    env = {
        **os.environ,
        "MIRROR_ROOT": str(tmp_path / "store"),
        "KEEP_FILE": str(keep_file),
        "KEEP_NEWEST": str(newest),
    }
    return run(["sh", str(SCRIPT)], env=env, check=True).stdout


def _store(tmp_path: Path) -> Store:
    return Store(tmp_path / "store")


# A digest no cached revision has, so a keep file holding it is non-empty and keeps nothing.
UNRELATED = "sha256:" + "f" * 64


def test_the_newest_revisions_are_kept_and_older_ones_pruned_by_name(tmp_path):
    store = _store(tmp_path)
    new, mid, old = store.cache(1), store.cache(5), store.cache(9)
    store.tag("v3", new)
    store.tag("v1", old)

    out = _prune(tmp_path, [UNRELATED])

    assert store.revisions() == {new, mid}
    assert store.tags() == {"v3"}
    assert f"pruned {REPO}@{old} tags=v1" in out.splitlines(), out
    assert "1 revision(s) pruned across 1 repositories" in out, out


def test_a_digest_in_the_keep_set_survives_below_the_rank_cut(tmp_path):
    store = _store(tmp_path)
    new, mid, old = store.cache(1), store.cache(5), store.cache(9)

    out = _prune(tmp_path, [old])

    assert store.revisions() == {new, mid, old}, out


def test_a_kept_index_keeps_its_children_and_a_pruned_one_loses_them(tmp_path):
    store = _store(tmp_path)
    # Children are cached at the same time as their index; they are older here on purpose, so
    # only membership in a kept index can save them from the rank cut.
    kept_child, gone_child = store.cache(30), store.cache(40)
    kept_index = store.cache(1, children=(kept_child,))
    gone_index = store.cache(9, children=(gone_child,))
    store.cache(5)

    out = _prune(tmp_path, [UNRELATED])

    assert kept_child in store.revisions(), out
    assert kept_index in store.revisions(), out
    assert not {gone_index, gone_child} & store.revisions(), out


def test_children_do_not_take_a_place_in_the_ranking(tmp_path):
    """A child cached a moment after its index must not push an older image out of the top N."""
    store = _store(tmp_path)
    child = store.cache(0)
    index = store.cache(1, children=(child,))
    older = store.cache(5)

    _prune(tmp_path, [UNRELATED])

    assert store.revisions() == {child, index, older}


def test_an_empty_keep_set_prunes_nothing(tmp_path):
    store = _store(tmp_path)
    digests = {store.cache(age) for age in (1, 5, 9, 13)}

    out = _prune(tmp_path, [])

    assert store.revisions() == digests
    assert "pruned nothing" in out, out


def test_a_revision_whose_blob_is_missing_is_kept(tmp_path):
    store = _store(tmp_path)
    store.cache(1), store.cache(5)
    orphan = store.cache(9)
    hex_ = orphan.removeprefix("sha256:")
    (store.v2 / "blobs" / "sha256" / hex_[:2] / hex_ / "data").unlink()

    out = _prune(tmp_path, [UNRELATED])

    assert orphan in store.revisions(), out
    assert f"keeping {REPO}@{orphan}" in out, out


def test_each_repository_is_ranked_on_its_own(tmp_path):
    store = _store(tmp_path)
    other = "fluxcd/source-controller"
    for age in (1, 5, 9):
        store.cache(age)
    flux = {store.cache(age, repo=other) for age in (100, 200)}

    _prune(tmp_path, [UNRELATED])

    assert store.revisions(other) == flux
    assert len(store.revisions()) == 2


def test_a_tag_in_the_keep_set_keeps_the_revision_it_points_at(tmp_path):
    """A tag-pulled workload scaled to zero reaches the keep set only as its spec's tag."""
    store = _store(tmp_path)
    store.cache(1), store.cache(5)
    old = store.cache(9)
    store.tag("abc123", old)

    out = _prune(tmp_path, [UNRELATED, f"{REPO}:abc123"])

    assert old in store.revisions(), out
    assert "abc123" in store.tags(), out


def test_a_tag_of_another_repository_keeps_nothing_here(tmp_path):
    store = _store(tmp_path)
    store.cache(1), store.cache(5)
    old = store.cache(9)
    store.tag("abc123", old)

    out = _prune(tmp_path, [UNRELATED, "danielh2018/ical-proxy:abc123"])

    assert old not in store.revisions(), out
