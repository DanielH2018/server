"""The publish bundle beside `DIR/cluster/`: `DIR/flux/` and `DIR/digests.json` (D2.6b).

A placeholder render with `--role` writes, for each tag it names:

- `DIR/flux/<tag>.yaml`, the Kustomization `<tag>` (C3 *Per-tag Kustomizations*). It carries
  `spec.postBuild`, naming `homelab-cluster-vars`, exactly when the tag's placeholder key set
  is not empty (C1 *The projection*);
- `DIR/flux/<kind>.yaml` and `DIR/cluster/<kind>/` for each `SHARED_KINDS` member;
- `DIR/flux/kustomization.yaml`, which lists every other file in `DIR/flux/`, so the root
  Kustomization `homelab` applies each of them;
- `DIR/digests.json`, each published name's `own` and `footprint` digest (C1 *Cluster output*).

`SHARED_KINDS` names every Kustomization that belongs to no deploy tag. D3.3a adds the first,
`cluster-vars`. Its directory holds a copy of the cluster file and a `secretGenerator` that
makes the Secret `homelab-cluster-vars`, and its Kustomization decrypts the copy with the
cluster's own key (`cluster_vars_files`). Each member needs a rule in `FOOTPRINT_RULES` that
names the tags its files count for. A deploy tag may not take the root's name or a member's
name (`reserved_tags`).

The classification `digests` applies is D6's *What a footprint counts*:

- a tag's own directories count for it, `-deferred` stripped, and so does `flux/<tag>.yaml`;
- a shared kind's files count for itself and for each tag its rule names;
- `flux/kustomization.yaml`, `digests.json` and `images/` count for no name and are left out;
- every other path, and a shared kind with no rule, lands under `_unclassified`.

This module imports neither Ansible nor Jinja, so `promote.py check-bundle` reads
`SHARED_KINDS` and the file names from it. The plan is
docs/target-architecture/deploy/d2-first-leaf.md, *Publication and verification*.
"""

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))

import hashlib
import json
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

import yaml

KUSTOMIZATION = "kustomization.yaml"
DEFERRED_SUFFIX = "-deferred"
DIGESTS = "digests.json"
FLUX_DIR = "flux"
CLUSTER_DIR = "cluster"
FLUX_INDEX = f"{FLUX_DIR}/{KUSTOMIZATION}"
# The reserved key of `digests.json` for the paths that count for no name (C1).
UNCLASSIFIED = "_unclassified"
# The root Kustomization the flux role applies. It reads `./flux`, so it applies every file
# `flux/kustomization.yaml` lists.
ROOT = "homelab"
FLUX_NAMESPACE = "flux-system"
# C3's one substitution Secret. D3 produces it, and D2.6c's source gate refuses a tag that
# names it until a Kustomization in the same bundle generates it.
CLUSTER_VARS_SECRET = "homelab-cluster-vars"
FLUX_API_VERSION = "kustomize.toolkit.fluxcd.io/v1"

# The shared kind that delivers `CLUSTER_VARS_SECRET` (D3.3a), and the name its directory
# gives the copy of the cluster file. kustomize-controller decrypts a `secretGenerator`
# source as dotenv when its name ends in `.env`, and .gitleaks.toml's allowlist names the
# copy's path.
CLUSTER_VARS = "cluster-vars"
CLUSTER_VARS_FILE = "cluster-vars.sops.env"
# The Secret in `flux-system` that holds the cluster's age key (D3.0, operator-only).
SOPS_AGE_SECRET = "sops-age"

# Every shared kind, a closed literal set (C3 *Shared kinds*). D3.3a adds `cluster-vars`,
# D5.3b2 `soak` and D5's namespace PR `homelab-namespace`, each in the PR that renders it.
# Membership is unconditional: a render that cannot copy the cluster file fails, so deleting
# the file fails CI rather than narrowing the census.
SHARED_KINDS: frozenset[str] = frozenset({CLUSTER_VARS})


@dataclass(frozen=True)
class FootprintFacts:
    """What a footprint rule may read: facts of the one render.

    Attributes:
        tags: The deploy tags the bundle publishes.
        substituting: The tags with a directory whose placeholder key set is not empty.
    """

    tags: frozenset[str]
    substituting: frozenset[str]


# Each SHARED_KINDS member to the deploy tags its files count for. D6's *What a footprint
# counts* states the rule each member gets, such as `cluster-vars` counting for every
# substituting tag. `rules_missing` and its census test fail a member without one.
FOOTPRINT_RULES: dict[str, Callable[[FootprintFacts], Iterable[str]]] = {
    CLUSTER_VARS: lambda facts: facts.substituting,
}


def rules_missing(
    kinds: Iterable[str] = SHARED_KINDS,
    rules: Mapping[str, object] = FOOTPRINT_RULES,
) -> list[str]:
    """The sorted shared kinds that have no footprint rule."""
    return sorted(set(kinds) - set(rules))


def reserved_tags(
    tags: Iterable[str], kinds: Iterable[str] = SHARED_KINDS
) -> list[str]:
    """The sorted deploy tags in `tags` that take the root's name or a shared kind's (C3).

    D4.2 adds the `-deferred` suffix to this check, and D5.1a1 adds `-snapshot` and `-verify`.
    """
    return sorted(set(tags) & ({ROOT} | set(kinds)))


def kustomization_text(files: Iterable[str]) -> str:
    """A `kustomization.yaml` listing `files` in lexical order.

    Lexical order is the order `kubectl apply -f <dir>/` reads a directory in.
    """
    return yaml.safe_dump(
        {
            "apiVersion": "kustomize.config.k8s.io/v1beta1",
            "kind": "Kustomization",
            "resources": sorted(files),
        },
        sort_keys=False,
    )


def tag_kustomization(tag: str, substitutes: bool, timeout: str) -> dict:
    """The Kustomization `tag`, as D2's *The per-tag Kustomization* shows it.

    Args:
        tag: The deploy tag, the Kustomization's name and its directory under `cluster/`.
        substitutes: Whether the tag's placeholder key set is not empty, which alone decides
            `postBuild` (C1 *The projection*).
        timeout: `manifests_rollout_timeout_default`, the manifests role's rollout timeout.
    """
    spec: dict = {
        "sourceRef": {"kind": "OCIRepository", "name": ROOT},
        "path": f"./{CLUSTER_DIR}/{tag}",
        "interval": "10m",
        "retryInterval": "2m",
        "timeout": timeout,
        "prune": True,
        "wait": True,
        # Removing a tag from the bundle, by a handback or a render bug, leaves its objects
        # serving rather than deleting them. D6 decides whether to switch (C3).
        "deletionPolicy": "Orphan",
    }
    if substitutes:
        spec["postBuild"] = {
            "substituteFrom": [{"kind": "Secret", "name": CLUSTER_VARS_SECRET}]
        }
    return {
        "apiVersion": FLUX_API_VERSION,
        "kind": "Kustomization",
        "metadata": {"name": tag, "namespace": FLUX_NAMESPACE},
        "spec": spec,
    }


def cluster_vars_kustomization(timeout: str) -> dict:
    """The Kustomization `cluster-vars`: a tag's fields without `postBuild`, plus `decryption`.

    `deletionPolicy: Orphan` keeps the Secret serving if the Kustomization is ever deleted.
    """
    doc = tag_kustomization(CLUSTER_VARS, False, timeout)
    doc["spec"]["decryption"] = {
        "provider": "sops",
        "secretRef": {"name": SOPS_AGE_SECRET},
    }
    return doc


def cluster_vars_files(dotenv: str, timeout: str) -> dict[str, str]:
    """The `cluster-vars` kind's files, by bundle path, around a copy of the cluster file.

    Args:
        dotenv: The cluster file's text, copied byte for byte. It is SOPS ciphertext, and
            the publish job asserts so with `cluster_vars.py check --ciphertext-only`.
        timeout: The `timeout` every Kustomization in the bundle sets.
    """
    generator = {
        "apiVersion": "kustomize.config.k8s.io/v1beta1",
        "kind": "Kustomization",
        "namespace": FLUX_NAMESPACE,
        "generatorOptions": {"disableNameSuffixHash": True},
        "secretGenerator": [{"name": CLUSTER_VARS_SECRET, "envs": [CLUSTER_VARS_FILE]}],
    }
    directory = f"{CLUSTER_DIR}/{CLUSTER_VARS}"
    return {
        f"{FLUX_DIR}/{CLUSTER_VARS}.yaml": yaml.safe_dump(
            cluster_vars_kustomization(timeout), sort_keys=False
        ),
        f"{directory}/{CLUSTER_VARS_FILE}": dotenv,
        f"{directory}/{KUSTOMIZATION}": yaml.safe_dump(generator, sort_keys=False),
    }


def flux_files(
    substitutes: Mapping[str, bool],
    timeout: str,
    shared: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Each `flux/` file of a bundle publishing the tags in `substitutes`, by bundle path.

    Args:
        substitutes: Each published tag mapped to whether it substitutes.
        timeout: The `timeout` every tag's Kustomization sets.
        shared: The shared kinds' `flux/` files by bundle path, which the root lists beside
            the tags' files.
    """
    files = {
        f"{FLUX_DIR}/{tag}.yaml": yaml.safe_dump(
            tag_kustomization(tag, wants, timeout), sort_keys=False
        )
        for tag, wants in sorted(substitutes.items())
    }
    files |= dict(shared or {})
    files[FLUX_INDEX] = kustomization_text(path.split("/", 1)[1] for path in files)
    return files


def _excluded(path: str) -> bool:
    """Whether `path` is an index path, which counts for no name and no footprint (C1)."""
    return path in (FLUX_INDEX, DIGESTS) or path.startswith("images/")


def _name_of(path: str) -> tuple[str, bool] | None:
    """The name a bundle path belongs to and whether it is under `cluster/<name>/` exactly.

    None when the path's shape belongs to no name: neither `cluster/<dir>/...` nor
    `flux/<name>.yaml`.
    """
    parts = path.split("/")
    if len(parts) >= 3 and parts[0] == CLUSTER_DIR:
        directory = parts[1]
        if directory.endswith(DEFERRED_SUFFIX):
            return directory.removesuffix(DEFERRED_SUFFIX), False
        return directory, True
    if len(parts) == 2 and parts[0] == FLUX_DIR and parts[1].endswith(".yaml"):
        return parts[1].removesuffix(".yaml"), False
    return None


def digest(files: Mapping[str, bytes]) -> str:
    """One SHA-256 over `files`, each bundle path with the SHA-256 of its bytes, sorted.

    The path is hashed too, so a rename moves the digest. NUL never occurs in a path and a
    hex digest holds no newline, so no two inputs produce the same hashed text.
    """
    hashed = hashlib.sha256()
    for path in sorted(files):
        hashed.update(f"{path}\0{hashlib.sha256(files[path]).hexdigest()}\n".encode())
    return hashed.hexdigest()


def digests(
    files: Mapping[str, bytes],
    tags: Iterable[str],
    substituting: Iterable[str] = (),
    kinds: Iterable[str] = SHARED_KINDS,
    rules: Mapping[str, Callable[[FootprintFacts], Iterable[str]]] = FOOTPRINT_RULES,
) -> dict:
    """`digests.json` for a bundle holding `files`, as C1 defines it.

    Args:
        files: Every bundle path mapped to its bytes. The index paths are left out here.
        tags: The deploy tags the bundle publishes.
        substituting: The tags with a directory whose placeholder key set is not empty.
        kinds: The shared kinds; each gets an entry beside the tags.
        rules: Each shared kind's footprint rule. A kind without one counts for no tag, and
            its files land under `_unclassified` as well as in its own entry.

    Returns:
        Each tag and each shared kind mapped to `{"own": ..., "footprint": ...}`, plus
        `_unclassified` in the same form with `paths`, the sorted paths it covers. Every
        entry carries both digests, the shape `changed_set.footprints` reads.
    """
    tags, kinds = frozenset(tags), frozenset(kinds)
    facts = FootprintFacts(tags, frozenset(substituting) & tags)
    own: dict[str, dict[str, bytes]] = {name: {} for name in tags | kinds}
    footprint: dict[str, dict[str, bytes]] = {name: {} for name in tags | kinds}
    unclassified: dict[str, bytes] = {}
    for path, data in files.items():
        if _excluded(path):
            continue
        found = _name_of(path)
        if found is None or found[0] not in own:
            unclassified[path] = data
            continue
        name, is_own = found
        footprint[name][path] = data
        if is_own:
            own[name][path] = data
        if name in kinds:
            if name not in rules:
                unclassified[path] = data
                continue
            for tag in set(rules[name](facts)) & tags:
                footprint[tag][path] = data
    result: dict = {
        name: {"own": digest(own[name]), "footprint": digest(footprint[name])}
        for name in sorted(own)
    }
    whole = digest(unclassified)
    result[UNCLASSIFIED] = {
        "own": whole,
        "footprint": whole,
        "paths": sorted(unclassified),
    }
    return result


def digests_text(value: Mapping) -> str:
    """`digests.json`'s bytes: one line of JSON with sorted keys, so two renders agree."""
    return json.dumps(value, sort_keys=True) + "\n"


def bundle_files(
    directories: Mapping[str, Mapping[str, str]],
    key_sets: Mapping[str, frozenset[str]],
    tags: Iterable[str],
    timeout: str,
    cluster_vars: str,
) -> dict[str, str]:
    """The files a bundle holds beside its tags' `cluster/` directories, by bundle path.

    They are the `flux/` files, each shared kind's `cluster/<kind>/` and `digests.json`. With
    no tag, the bundle holds the shared kinds alone, as `--shared-only` writes it.

    Args:
        directories: Each tag directory's name under `cluster/` mapped to its files' texts.
        key_sets: Each directory name mapped to its placeholder key set, the one source for
            whether it substitutes (C1).
        tags: The published tags, one Kustomization each.
        timeout: The `timeout` every Kustomization sets.
        cluster_vars: The cluster file's text, which the `cluster-vars` kind copies.

    Raises:
        ValueError: A published tag has a `-deferred` directory. Its Kustomization is
            `<tag>-deferred`, which D4.2 adds with the suffix's reservation.
    """
    tags = sorted(set(tags))
    deferred = [tag for tag in tags if tag + DEFERRED_SUFFIX in directories]
    if deferred:
        raise ValueError(
            f"{deferred} render a {DEFERRED_SUFFIX} directory, and no Kustomization applies "
            f"it until D4.2 adds <tag>{DEFERRED_SUFFIX}"
        )
    substituting = {
        tag
        for tag in tags
        if any(
            key_sets.get(name)
            for name in (tag, tag + DEFERRED_SUFFIX)
            if name in directories
        )
    }
    shared = cluster_vars_files(cluster_vars, timeout)
    flux = {p: t for p, t in shared.items() if p.startswith(f"{FLUX_DIR}/")}
    files = flux_files({tag: tag in substituting for tag in tags}, timeout, flux)
    files |= shared
    cluster = {
        f"{CLUSTER_DIR}/{name}/{file}": text
        for name, held in directories.items()
        for file, text in held.items()
    }
    everything = {path: text.encode() for path, text in {**cluster, **files}.items()}
    files[DIGESTS] = digests_text(digests(everything, tags, substituting))
    return files


def write_bundle(files: Mapping[str, str], out: Path) -> list[Path]:
    """Write `files`, keyed by bundle path, under `out`, and return the paths written."""
    written = []
    for path, text in sorted(files.items()):
        dest = out / path
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(text)
        written.append(dest)
    return written
