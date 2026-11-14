"""Which keys a rotation leaves stale after its own landing, read from ciphertext alone.

`land.sh` asks `unapplied_keys` whether a PR that changed `secrets/secrets.yml` refreshed
every copy of each key it rotated, so a rotation that did ends `settled` rather than
`needs-manual-apply`. Nothing here decrypts: a rotated key is one whose ciphertext differs
between two revisions.

Its own module rather than part of `consumers.py`, because it reads the host files through
`host_secrets`, and the registry audit's prek hook lists `consumers.py`'s import closure.
"""

from collections.abc import Mapping
from types import MappingProxyType

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))  # scripts/

from lib.git import git
from secrets_mgmt.cluster_vars_check import ciphertexts, digest, parse_source
from secrets_mgmt.consumers import HOST_PLANE, K8S_PLANE, tree_consumers
from secrets_mgmt.host_secrets import tree_hosts, tree_keys
from secrets_mgmt.rotation_tools import REPO

# Copies of a secret that no deploy reaches, key -> where they sit. A rotation of one of these
# keys always owes a hand step; `docs/secret-rotation.md` gives each one's.
OUTSIDE_THE_TREE: Mapping[str, str] = MappingProxyType(
    {
        "lidarr_api_key": "Prowlarr's Lidarr application",
        "livesync_db_password": "each Obsidian LiveSync client's plugin settings",
        "livesync_sync_token": "each Obsidian LiveSync client's plugin settings",
        "mqtt_password": "Home Assistant's MQTT integration",
        "nut_ha_password": "Home Assistant's NUT integration",
        "prowlarr_api_key": "the Prowlarr indexers in Sonarr, Radarr and Lidarr",
        "qbittorrent_password": "the qBittorrent download client in Sonarr, Radarr and Lidarr",
        "radarr_api_key": "Bazarr's config on the bazarr-config PVC and Prowlarr's Radarr "
        "application",
        "sonarr_api_key": "Bazarr's config on the bazarr-config PVC and Prowlarr's Sonarr "
        "application",
    }
)

_STORE = "secrets/secrets.yml"
_CLUSTER_SOURCE = "secrets/cluster-vars.source"


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


def unapplied_keys(old: str, new: str, repo: str = REPO) -> dict[str, str]:
    """Each key `old..new` changed in secrets.yml that a landing of `new` leaves stale, with why.

    A changed key is applied when every copy the tree holds is fresh at `new`. A k8s reader's
    copy is fresh when `cluster-vars.source` records the digest of the key's new ciphertext,
    because `promote` then applies the cluster file. A host's copy is fresh when its
    `secrets/hosts/<host>.sops.yaml` ciphertext for the key changed in the range too, because
    the landing waits on that host's package. A key in `OUTSIDE_THE_TREE`, or one no file in
    the tree reads, is never applied: whatever holds it is out of every deploy's reach.

    The readers are counted in `repo`'s working tree, which the landing has fast-forwarded.

    Raises:
        subprocess.CalledProcessError: `old` or `new` does not name a commit.
    """
    for ref in (old, new):
        git("rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}", cwd=repo)
    before, after = (_ciphertexts_at(repo, ref, _STORE) for ref in (old, new))
    recorded = parse_source(_text_at(repo, new, _CLUSTER_SOURCE) or "")
    out = {}
    for name in sorted(k for k, value in after.items() if before.get(k) != value):
        why = []
        if name in OUTSIDE_THE_TREE:
            why.append(f"no deploy reaches its copy in {OUTSIDE_THE_TREE[name]}")
        k8s = K8S_PLANE in tree_consumers(name, repo).values()
        hosts = _host_readers(name, repo)
        if not (k8s or hosts or why):
            why.append(
                "no file in the tree reads it, so whatever holds it is outside it"
            )
        line = recorded.get(name)
        if k8s and (line is None or line.digest != digest(after[name])):
            why.append("the cluster file's copy is stale (`cluster_vars.py write`)")
        for host in sorted(hosts):
            path = f"secrets/hosts/{host}.sops.yaml"
            copy = _ciphertexts_at(repo, new, path).get(name)
            if copy is None or copy == _ciphertexts_at(repo, old, path).get(name):
                why.append(
                    f"{host}'s host file is not synced (`host_secrets.py sync {host}`)"
                )
        if why:
            out[name] = "; ".join(why)
    return out
