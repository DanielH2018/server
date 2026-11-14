"""Which keys a rotation leaves stale after its own landing, read from ciphertext alone.

`land.sh` asks `unapplied_keys` whether a PR that changed `secrets/secrets.yml` refreshed
every copy of each key it rotated, so a rotation that did ends `settled` rather than
`needs-manual-apply`. Nothing here decrypts: a rotated key is one whose ciphertext differs
between two revisions.

Its own module rather than part of `consumers.py`, because it reads the host files through
`host_secrets`, and the registry audit's prek hook lists `consumers.py`'s import closure.
"""

import tempfile
from collections.abc import Mapping
from types import MappingProxyType

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))  # scripts/

from lib.git import extract, git
from secrets_mgmt.cluster_vars_check import ciphertexts, digest, parse_source
from secrets_mgmt.consumers import HOST_PLANE, K8S_PLANE, tree_consumers
from secrets_mgmt.host_secrets import tree_hosts, tree_keys
from secrets_mgmt.rotation_tools import REPO

# Copies of a secret that no deploy reaches, key -> who holds it. A rotation of one of these
# keys always owes a hand step; `docs/secret-rotation.md` gives each one's.
OUTSIDE_THE_TREE: Mapping[str, str] = MappingProxyType(
    {
        "radarr_api_key": "Bazarr's config on the bazarr-config PVC",
        "sonarr_api_key": "Bazarr's config on the bazarr-config PVC",
    }
)

_STORE = "secrets/secrets.yml"
_CLUSTER_SOURCE = "secrets/cluster-vars.source"
_READER_TREES = ("k8s", "hosts")  # what `tree_consumers` and `_host_readers` walk


def _text_at(repo: str, ref: str, path: str) -> str | None:
    """`path`'s blob at `ref`, raw, or None when `ref` holds no such file."""
    if git("cat-file", "-e", f"{ref}:{path}", cwd=repo, check=False).returncode:
        return None
    return git("show", f"{ref}:{path}", cwd=repo).stdout


def _ciphertexts_at(repo: str, ref: str, path: str) -> dict[str, str]:
    """Each key's `ENC[...]` string in the SOPS file `path` at `ref`; empty when absent."""
    return ciphertexts(_text_at(repo, ref, path) or "")


def _host_readers(name: str, repo: str) -> set[str]:
    """The hosts whose package carries `name`: its tree names it, or a component file does."""
    root = _Path(repo)
    named = {host for host in tree_hosts(root) if name in tree_keys(root, host)}
    census = {
        r for r, plane in tree_consumers(name, repo).items() if plane == HOST_PLANE
    }
    return named | census


def unapplied_keys(
    old: str, new: str, repo: str = REPO, *, deployed: str | None = None
) -> dict[str, str]:
    """Each key `old..new` changed in secrets.yml that deploying `deployed` leaves stale, with why.

    `old..new` is the PR's own range, so it names the keys this PR rotated and no other PR's.
    `deployed` is the commit the landing applies, the merge commit, and defaults to `new`.
    Every copy and every reader is read there, because a PR that merged in between can add
    or remove a reader, or refresh a copy, that the PR's head does not hold.

    A changed key is applied when every copy the tree holds is fresh at `deployed`. A k8s
    reader's copy is fresh when `cluster-vars.source` records the digest of the key's
    ciphertext, because `promote` then applies the cluster file. A host's copy is fresh when
    its `secrets/hosts/<host>.sops.yaml` ciphertext for the key differs from `old`'s, because
    the landing waits on that host's package. A key in `OUTSIDE_THE_TREE`, or one no file in
    the tree reads, is never applied: whatever holds it is out of every deploy's reach.

    The readers are counted from `deployed`'s `k8s/` and `hosts/`, unpacked into a scratch
    directory, so `repo`'s working tree may sit at any commit.

    Raises:
        subprocess.CalledProcessError: `old`, `new` or `deployed` does not name a commit.
    """
    deployed = deployed or new
    for ref in (old, new, deployed):
        git("rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}", cwd=repo)
    before, after, landed = (
        _ciphertexts_at(repo, ref, _STORE) for ref in (old, new, deployed)
    )
    recorded = parse_source(_text_at(repo, deployed, _CLUSTER_SOURCE) or "")
    changed = sorted(k for k, value in after.items() if before.get(k) != value)
    with tempfile.TemporaryDirectory(prefix="stale-copies-") as tree:
        extract(deployed, _Path(tree), _READER_TREES, repo=repo)
        readers = {
            name: (
                K8S_PLANE in tree_consumers(name, tree).values(),
                _host_readers(name, tree),
            )
            for name in changed
        }
    out = {}
    for name in changed:
        why = []
        if name in OUTSIDE_THE_TREE:
            why.append(f"{OUTSIDE_THE_TREE[name]} holds a copy no deploy reaches")
        k8s, hosts = readers[name]
        if not (k8s or hosts or why):
            why.append(
                "no file in the tree reads it, so whatever holds it is outside it"
            )
        line = recorded.get(name)
        value = landed.get(name, after[name])
        if k8s and (line is None or line.digest != digest(value)):
            why.append("the cluster file's copy is stale (`cluster_vars.py write`)")
        for host in sorted(hosts):
            path = f"secrets/hosts/{host}.sops.yaml"
            copy = _ciphertexts_at(repo, deployed, path).get(name)
            if copy is None or copy == _ciphertexts_at(repo, old, path).get(name):
                why.append(
                    f"{host}'s host file is not synced (`host_secrets.py sync {host}`)"
                )
        if why:
            out[name] = "; ".join(why)
    return out
