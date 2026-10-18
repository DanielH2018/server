"""The publish bundle beside `DIR/cluster/`: `DIR/flux/` and `DIR/digests.json` (D2.6b).

A placeholder render with `--role` writes, for each tag it names:

- `DIR/flux/<tag>.yaml`, the Kustomization `<tag>` (C3 *Per-tag Kustomizations*). It carries
  `spec.postBuild`, naming `homelab-cluster-vars`, and a `dependsOn` entry for `cluster-vars`
  exactly when the placeholder key set of the directory `<tag>` is not empty (C1 *The
  projection*, D3.3b). It carries `decryption` exactly when the tag's role lists
  `render_sops_manifests`, whose SOPS manifests the renderer copies into `cluster/<tag>/`
  (C3 *Per-role fields*, D3.3c). A tag that renders a per-tag kind's directory, such as
  `<tag>-snapshot`, gets that Kustomization as a further document of the file, in
  `TAG_KIND_SUFFIXES` order (D4.2, D5.1a1). `<tag>` and `<tag>-deferred` take `prune` and
  `timeout` from the role's defaults where `role_fields` finds them (C3 *Per-role fields*).
  A role that sets `flux_suspend: true` renders `suspend: true` on every one of its tag's
  Kustomizations (C3 *Suspension*, D5.1b);
- `DIR/flux/<kind>.yaml` and `DIR/cluster/<kind>/` for each `SHARED_KINDS` member: `cluster-vars`
  and `soak` (`soak.py`);
- `DIR/flux/kustomization.yaml`, which lists every other file in `DIR/flux/`, so the root
  Kustomization `homelab` applies each of them;
- `DIR/digests.json`, each published name's `own` and `footprint` digest, and each tag's
  `claims` and `suspended` (C1 *Cluster output*, D5.1a2);
- `DIR/README.md`, a static note on the bundle's layout (`BUNDLE_README`). No Kustomization's
  path reaches it, and it lands under `_unclassified`.

`SHARED_KINDS` names every Kustomization that belongs to no deploy tag. D3.3a adds the first,
`cluster-vars`. Its directory holds a copy of the cluster file and a `secretGenerator` that
makes the Secret `homelab-cluster-vars`, and its Kustomization decrypts the copy with the
cluster's own key (`cluster_vars_files`). Each member needs a rule in `FOOTPRINT_RULES` that
names the tags its files count for. A deploy tag may not take the root's name, a member's
name, a name in `RESERVED_NAMES` or a per-tag kind's suffix (`reserved_tags`).

The classification `digests` applies is D6's *What a footprint counts*:

- a tag's own directories count for it, each per-tag suffix stripped, and so does
  `flux/<tag>.yaml`. Its `own` digest covers `cluster/<tag>/` alone;
- a shared kind's files count for itself and for each tag its rule names;
- `flux/kustomization.yaml`, `digests.json` and `images/` count for no name and are left out;
- every other path, and a shared kind with no rule, lands under `_unclassified`.

`dependsOn` is generated, never hand-written (D4.2): `ordering.py` derives each
Kustomization's edges, and a cycle among them fails the render, as `suspend: false` does on
any Flux object (`suspension.py`, C3 *Suspension*).

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
from dataclasses import dataclass, replace
from pathlib import Path

import yaml

from lib.k8s_roles import role_fields, snapshot_claims
from lib.repo_paths import K8S_ROLES
from renderer.renderer_lib.bundle_readme import BUNDLE_README, README
from renderer.renderer_lib.suspension import refuse_suspend_false, suspends_tag
from renderer.renderer_lib.ordering import (
    ANSIBLE_OWNED_DEPS,
    DEFERRED_SUFFIX,
    NO_EDGES,
    NO_ORDERING,
    PREPULL_SUFFIX,
    SUSPENDED_DEPS,
    TAG_KIND_SUFFIXES,
    Edges,
    Ordering,
    dependency_cycle,
    kustomization_edges,
    prepull_timeouts,
    split_kind,
)

KUSTOMIZATION = "kustomization.yaml"
# The subdirectory of `cluster/<tag>/` holding a role's Kustomize patches (H1.1a).
PATCHES_DIR = "patches"
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
# Kustomization `<tag>`'s `own` digest, C6's `content_digest` and C7's flux reference (#5338).
CONTENT_DIGEST = "homelab/content-digest"

# The shared kind that delivers `CLUSTER_VARS_SECRET` (D3.3a), and the name its directory
# gives the copy of the cluster file. kustomize-controller decrypts a `secretGenerator`
# source as dotenv when its name ends in `.env`, and .gitleaks.toml's allowlist names the
# copy's path.
CLUSTER_VARS = "cluster-vars"
CLUSTER_VARS_FILE = "cluster-vars.sops.env"
# The Secret in `flux-system` that holds the cluster's age key (D3.0, operator-only).
SOPS_AGE_SECRET = "sops-age"
DECRYPTION = {"provider": "sops", "secretRef": {"name": SOPS_AGE_SECRET}}
# kustomize-controller substitutes nothing in a resource carrying this annotation as
# `disabled`, so a `${` inside a decrypted value stays literal. Every SOPS manifest carries
# it in its plaintext metadata, written before encryption (C3 *Secrets*).
SUBSTITUTE_OPT_OUT = "kustomize.toolkit.fluxcd.io/substitute"
# Every shared kind, a closed literal set (C3 *Shared kinds*). D3.3a adds `cluster-vars`,
# D5.3b2 `soak` and D5's namespace PR `homelab-namespace`, each in the PR that renders it.
# Membership is unconditional: a render that cannot copy the cluster file fails, so deleting
# the file fails CI rather than narrowing the census.
SHARED_KINDS: frozenset[str] = frozenset({CLUSTER_VARS, "soak"})
# The shared kinds no PR renders yet, a closed literal set (D5.1a1). A member cannot join
# `SHARED_KINDS` before its file exists, so this list keeps a deploy tag from taking its name
# meanwhile. A name stays here after it joins.
RESERVED_NAMES: frozenset[str] = frozenset({"soak", "homelab-namespace"})


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
    # The soak judges the tags and changes none of them; C2 adds it to any set holding a tag.
    "soak": lambda facts: (),
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
    """The sorted deploy tags in `tags` that take a name the bundle reserves (C3).

    A reserved name is the root's, a shared kind's, one in `RESERVED_NAMES`, or any name
    ending in a per-tag kind's suffix, so no tag's Kustomization can collide with another
    tag's `-deferred`, `-snapshot`, `-prepull` or `-verify` one (D4.2, D5.1a1).
    """
    names = {ROOT} | set(kinds) | RESERVED_NAMES
    return sorted(t for t in set(tags) if t in names or t.endswith(TAG_KIND_SUFFIXES))


def kustomization_text(files: Iterable[str]) -> str:
    """A `kustomization.yaml` listing `files` in lexical order.

    Lexical order is the order `kubectl apply -f <dir>/` reads a directory in. A file under
    `patches/` goes under `patches`, never `resources`, which would apply its partial object.
    """
    files = sorted(files)
    patches = [f for f in files if f.startswith(f"{PATCHES_DIR}/")]
    text: dict = {
        "apiVersion": "kustomize.config.k8s.io/v1beta1",
        "kind": "Kustomization",
        "resources": [f for f in files if f not in patches],
    }
    if patches:
        text["patches"] = [{"path": f} for f in patches]
    return yaml.safe_dump(text, sort_keys=False)


def tag_kustomization(
    tag: str,
    substitutes: bool,
    timeout: str,
    decrypts: bool = False,
    edges: Edges = NO_EDGES,
    prune: bool = True,
    suspend: bool = False,
    content: str | None = None,
) -> dict:
    """The Kustomization `tag`, as D2's *The per-tag Kustomization* shows it.

    Args:
        tag: The Kustomization's name and its directory under `cluster/`: a deploy tag, or
            `<tag>-deferred`.
        substitutes: Whether the directory's placeholder key set is not empty, which alone
            decides `postBuild` (C1 *The projection*) and the `cluster-vars` edge. The edge
            makes Flux wait for `homelab-cluster-vars` before it substitutes from it (D3.3b).
        timeout: The role's `manifests_rollout_timeout`, or `manifests_rollout_timeout_default`,
            the manifests role's rollout timeout (`role_fields`).
        decrypts: Whether the tag's role lists `render_sops_manifests`, which alone decides
            `decryption`. kustomize-controller decrypts nothing without it (D3.3c).
        edges: The generated edges (D4.2). `dependsOn` lists `edges.kept` and the
            `cluster-vars` edge in sorted order, `ANSIBLE_OWNED_DEPS` lists `edges.dropped`
            and `SUSPENDED_DEPS` lists `edges.suspended`; each is left out when empty.
        prune: False for a role whose defaults set `flux_prune: false` (`role_fields`).
        suspend: True for a role whose defaults set `flux_suspend: true`, the one value written.
        content: The tag's `own` digest, written as `CONTENT_DIGEST` unless None.
    """
    spec: dict = {
        "sourceRef": {"kind": "OCIRepository", "name": ROOT},
        "path": f"./{CLUSTER_DIR}/{tag}",
        "interval": "10m",
        "retryInterval": "2m",
        "timeout": timeout,
        "prune": prune,
        "wait": True,
        # Removing a tag from the bundle, by a handback or a render bug, leaves its objects
        # serving rather than deleting them. D6 decides whether to switch (C3).
        "deletionPolicy": "Orphan",
    }
    depends = set(edges.kept)
    if substitutes:
        spec["postBuild"] = {
            "substituteFrom": [{"kind": "Secret", "name": CLUSTER_VARS_SECRET}]
        }
        depends.add(CLUSTER_VARS)
    if depends:
        spec["dependsOn"] = [{"name": name} for name in sorted(depends)]
    if decrypts:
        spec["decryption"] = dict(DECRYPTION)
    if suspend:
        spec["suspend"] = True
    metadata: dict = {"name": tag, "namespace": FLUX_NAMESPACE}
    annotations = {ANSIBLE_OWNED_DEPS: edges.dropped, SUSPENDED_DEPS: edges.suspended}
    annotations = {k: ",".join(sorted(v)) for k, v in annotations.items() if v}
    if content is not None:
        annotations[CONTENT_DIGEST] = content
    if annotations:
        metadata["annotations"] = annotations
    return {
        "apiVersion": FLUX_API_VERSION,
        "kind": "Kustomization",
        "metadata": metadata,
        "spec": spec,
    }


def cluster_vars_kustomization(timeout: str) -> dict:
    """The Kustomization `cluster-vars`: a tag's fields, never substituting, plus `decryption`.

    Its directory holds no rendered template, so it gets neither `postBuild` nor the edge.

    `deletionPolicy: Orphan` keeps the Secret serving if the Kustomization is ever deleted.
    """
    return tag_kustomization(CLUSTER_VARS, False, timeout, decrypts=True)


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
    decrypting: Iterable[str] = (),
    edges: Mapping[str, Edges] | None = None,
    fields: Mapping[str, Mapping] | None = None,
    contents: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Each `flux/` file of a bundle publishing the tags in `substitutes`, by bundle path.

    Args:
        substitutes: Each Kustomization the published tags own mapped to whether it
            substitutes: `<tag>` for each tag, and `<tag><suffix>` for each per-tag kind
            whose directory the tag renders. Those follow `<tag>` in `flux/<tag>.yaml`, in
            `TAG_KIND_SUFFIXES` order.
        timeout: The `timeout` every tag's Kustomization sets unless `fields` names its own.
        shared: The shared kinds' `flux/` files by bundle path, which the root lists beside
            the tags' files.
        decrypting: The published tags whose role lists `render_sops_manifests`.
        edges: Each Kustomization's generated edges; a name it does not map has none.
        fields: Each tag's per-role fields: `prune` and `timeout` (`role_fields`) for `<tag>`
            and `<tag>-deferred`, `prepull_timeout` for `<tag>-prepull`, `suspend` for all.
        contents: Each tag's `own` digest, stamped on `<tag>` alone.
    """
    decrypting = frozenset(decrypting)
    edges = edges or {}
    fields = fields or {}
    files = {}
    for tag in sorted(n for n in substitutes if not split_kind(n)[1]):
        names = [tag] + [
            tag + suffix for suffix in TAG_KIND_SUFFIXES if tag + suffix in substitutes
        ]
        own = fields.get(tag, {})
        role_own = {tag, tag + DEFERRED_SUFFIX}
        timeouts = {name: own.get("timeout", timeout) for name in role_own}
        timeouts[tag + PREPULL_SUFFIX] = own.get("prepull_timeout", timeout)
        docs = [
            tag_kustomization(
                name,
                substitutes[name],
                timeouts.get(name, timeout),
                name in decrypting,
                edges.get(name, NO_EDGES),
                own.get("prune", True) if name in role_own else True,
                own.get("suspend", False),
                (contents or {}).get(name) if name == tag else None,
            )
            for name in names
        ]
        files[f"{FLUX_DIR}/{tag}.yaml"] = yaml.safe_dump_all(docs, sort_keys=False)
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
        name, suffix = split_kind(parts[1])
        return name, not suffix
    if len(parts) == 2 and parts[0] == FLUX_DIR and parts[1].endswith(".yaml"):
        return parts[1].removesuffix(".yaml"), False
    return None


def own_digests(cluster: Mapping[str, str], tags: Iterable[str]) -> dict[str, str]:
    """Each tag's `own` digest over the `cluster/` files in `cluster`, as `digests` computes it.

    DECIDED: `CONTENT_DIGEST` carries `own`, not `footprint`, which covers the annotation's
    own file. A missed `<tag>-deferred` change leaves C7's reference earlier (#5338).
    """
    held = {path: _name_of(path) for path in cluster}
    return {
        tag: digest(
            {p: t.encode() for p, t in cluster.items() if held[p] == (tag, True)}
        )
        for tag in tags
    }


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
    claims: Mapping[str, Iterable[str]] | None = None,
) -> dict:
    """`digests.json` for a bundle holding `files`, as C1 defines it.

    Args:
        files: Every bundle path mapped to its bytes. The index paths are left out here.
        tags: The deploy tags the bundle publishes.
        substituting: The tags with a directory whose placeholder key set is not empty.
        kinds: The shared kinds; each gets an entry beside the tags.
        rules: Each shared kind's footprint rule. A kind without one counts for no tag, and
            its files land under `_unclassified` as well as in its own entry.
        claims: Each tag's snapshot claims (`snapshot_claims`); a tag it does not map
            declares none.

    Returns:
        Each tag and each shared kind mapped to `{"own": ..., "footprint": ...}`, plus
        `_unclassified` in the same form with `paths`, the sorted paths it covers. Every
        entry carries both digests, the shape `changed_set.footprints` reads. A tag's entry
        also carries `claims`, its sorted snapshot claims, and `suspended`, whether its
        rendered Kustomization `<tag>` sets `spec.suspend: true` (D5.1a2).
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
    claims = claims or {}
    for tag in tags:
        result[tag]["claims"] = sorted(claims.get(tag, ()))
        result[tag]["suspended"] = suspends_tag(files.get(f"{FLUX_DIR}/{tag}.yaml"))
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


def substitution(
    tags: Iterable[str],
    directories: Mapping[str, object],
    key_sets: Mapping[str, frozenset[str]],
) -> tuple[dict[str, bool], set[str]]:
    """Which Kustomizations substitute, and which tags count as substituting (D3.3b).

    Args:
        tags: The published tags.
        directories: Each rendered directory's name under `cluster/`.
        key_sets: Each directory name mapped to its placeholder key set.

    Returns:
        Each directory name in `directories` that a tag in `tags` owns, `<tag>` and each
        `<tag><suffix>` alike, mapped to whether its own key set is non-empty. That alone
        decides `postBuild` and the `cluster-vars` edge on its Kustomization (C1
        *Substitution and escaping*). Second, the tags with any of their directories
        substituting, the per-tag set the `cluster-vars` footprint rule reads.
    """
    per_directory = {
        name: bool(key_sets.get(name))
        for tag in tags
        for name in (tag, *(tag + suffix for suffix in TAG_KIND_SUFFIXES))
        if name in directories
    }
    substituting = {
        split_kind(name)[0]
        for name, substitutes in per_directory.items()
        if substitutes
    }
    return per_directory, substituting


def bundle_files(
    directories: Mapping[str, Mapping[str, str]],
    key_sets: Mapping[str, frozenset[str]],
    tags: Iterable[str],
    timeout: str,
    cluster_vars: str,
    decrypting: Iterable[str] = (),
    ordering: Ordering = NO_ORDERING,
    roles: Path = K8S_ROLES,
    revision: str | None = None,
) -> dict[str, str]:
    """The files a bundle holds beside its tags' `cluster/` directories, by bundle path.

    They are the `flux/` files, each shared kind's `cluster/<kind>/` and `digests.json`. With
    no tag, the bundle holds the shared kinds alone, as `--shared-only` writes it.

    Args:
        directories: Each tag directory's name under `cluster/` mapped to its files' texts.
        key_sets: Each directory name mapped to its placeholder key set, the one source for
            whether it substitutes (C1).
        tags: The published tags, one Kustomization each.
        timeout: The `timeout` every Kustomization sets, unless its role's defaults set
            `manifests_rollout_timeout` (`role_fields`).
        cluster_vars: The cluster file's text, which the `cluster-vars` kind copies.
        decrypting: The published tags whose role lists `render_sops_manifests`, each of
            which gets `decryption` (C3 *Per-role fields*).
        ordering: What `dependsOn` is generated from (D4.2). The default generates no edge
            but `cluster-vars`.
        roles: The role tree whose defaults declare each tag's snapshot claims, which
            `digests.json` records (`snapshot_claims`), and its per-role fields
            (`role_fields`).
        revision: R's commit SHA, which the soak Job judges. None renders the soak at
            `soak.NORMALISED_REVISION`, for a bundle nobody publishes.

    Raises:
        ValueError: The edges form a cycle, a tag's `k8s_autodeploy_snapshot_pvcs` is not a
            literal list or a `role_fields` field not a literal, or a Flux object in the
            bundle sets `suspend: false` (`suspension.py`).
    """
    tags = sorted(set(tags))
    fields, problems = role_fields(tags, roles)
    pulls, unread = prepull_timeouts(tags, roles)
    problems += unread
    fields |= {t: {**fields.get(t, {}), "prepull_timeout": v} for t, v in pulls.items()}
    if problems:
        raise ValueError("; ".join(problems))
    per_directory, substituting = substitution(tags, directories, key_sets)
    # A published tag's own defaults decide its suspend, whatever the ordering was read from.
    suspended = {tag for tag, own in fields.items() if own.get("suspend")}
    ordering = replace(ordering, suspended=ordering.suspended | suspended)
    edges = kustomization_edges(tags, directories, ordering)
    graph = {
        name: set(edge.kept) | ({CLUSTER_VARS} if per_directory.get(name) else set())
        for name, edge in edges.items()
    }
    cycle = dependency_cycle(graph)
    if cycle:
        raise ValueError(
            f"the dependsOn edges form a cycle, {' -> '.join(cycle)}, and Flux would hold "
            "every Kustomization on it at DependencyNotReady"
        )
    # DECIDED: deferred, a documented cycle: soak.py builds on this module's writers, and
    # SHARED_KINDS makes every bundle_files caller render soak (test_scripts_import_direction).
    from renderer.renderer_lib.soak import soak_pair

    shared = cluster_vars_files(cluster_vars, timeout)
    flux = {p: t for p, t in shared.items() if p.startswith(f"{FLUX_DIR}/")}
    cluster = {
        f"{CLUSTER_DIR}/{name}/{file}": text
        for name, held in directories.items()
        for file, text in held.items()
    }
    files = flux_files(
        {name: per_directory.get(name, False) for name in edges},
        timeout,
        flux,
        set(decrypting) & set(tags),
        edges,
        fields,
        own_digests(cluster, tags),
    )
    # The soak sizes its wait from the other Kustomizations, so it renders after them.
    soak, published = soak_pair(revision, tags, directories, ordering.suspended, files)
    files |= shared | published
    files[README] = BUNDLE_README
    refuse_suspend_false({**cluster, **files})
    # `soak` is digested at the normalised SHA, so R's own SHA never moves its footprint (C1).
    digested = {**cluster, **files, **soak}
    everything = {path: text.encode() for path, text in digested.items()}
    claims, problems = snapshot_claims(tags, roles)
    if problems:
        raise ValueError("; ".join(problems))
    files[DIGESTS] = digests_text(
        digests(everything, tags, substituting, claims=claims)
    )
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
