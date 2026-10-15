"""The assertions of `cluster_vars.py check`, over file text alone (target slice D3).

Every function here reads text or parsed data and returns problem lines. Nothing opens a
process, decrypts or renders, so CI can run all of it without an age key. `cluster_vars.py`
gathers the inputs (the files, their base versions, the render's expectations) and calls these.

The cluster files are three kinds:

- the cluster file `ansible/vars/cluster-vars.sops.env`, a SOPS dotenv whose metadata SOPS
  writes as plain `sops_*` lines;
- one SOPS manifest per `render_sops_manifests` entry under `ansible/vars/cluster-manifests/`,
  a Secret whose `data` and `stringData` values alone are encrypted;
- `ansible/vars/cluster-vars.source`, plaintext freshness evidence: one line per key held in
  the other two, naming the key, the SHA-256 of its ciphertext in `secrets.yml`, and, for a
  manifest's key, the manifest's file name. The ciphertext is public, so the digest exposes
  nothing. The third field lets `check --no-render` see which manifest a key belongs to.

`docs/target-architecture/deploy/d3-secrets.md` (*Design*) is the plan.
"""

import hashlib
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

import yaml

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))  # scripts/

from lib import yaml_fast

# Printed after every failing check, because the obvious response to a dropped key, deleting
# its line by hand, keeps the names green and breaks the SOPS MAC.
FIX = (
    "The only fix is `cluster_vars.py write` (operator-only). A hand edit of a cluster file "
    "breaks its SOPS MAC, and kustomize-controller then refuses to decrypt it."
)
# kustomize-controller skips substitution on a resource carrying this annotation as
# `disabled`, which keeps a `${` inside a decrypted value literal.
SUBSTITUTE_OPT_OUT = "kustomize.toolkit.fluxcd.io/substitute"
SOPS_PREFIX = "sops_"
MANIFEST_SUFFIX = ".sops.yaml"
_ENC_TYPE = re.compile(r",type:(\w+)\]$")


@dataclass(frozen=True)
class Tree:
    """The repo-relative paths `cluster_vars.py` reads and writes, under one root.

    Attributes:
        root: The repository root; a test passes a fixture tree.
    """

    root: Path

    @property
    def ansible(self) -> Path:
        return self.root / "ansible"

    @property
    def sops_config(self) -> Path:
        return self.ansible / ".sops.yaml"

    @property
    def secrets(self) -> Path:
        return self.ansible / "vars" / "secrets.yml"

    @property
    def cluster_file(self) -> Path:
        return self.ansible / "vars" / "cluster-vars.sops.env"

    @property
    def source(self) -> Path:
        return self.ansible / "vars" / "cluster-vars.source"

    @property
    def manifests(self) -> Path:
        return self.ansible / "vars" / "cluster-manifests"

    def manifest_files(self) -> list[Path]:
        """The SOPS manifests present, sorted."""
        return sorted(self.manifests.glob("*" + MANIFEST_SUFFIX))

    def rel(self, path: Path) -> str:
        """`path` relative to the root, as git names it."""
        return path.relative_to(self.root).as_posix()


@dataclass(frozen=True)
class ManifestEntry:
    """One `render_sops_manifests` entry, as the placeholder render saw it.

    Attributes:
        tag: The deploy tag whose role lists the entry.
        entry: The rendered file's basename, as the role lists it.
        text: The entry's sentinel render. Its apiVersion, kind, metadata, type and data-key
            names are those of the stub render, because only the values differ.
        keys: The `SOPS_MANIFEST_KEYS` keys the render names.
    """

    tag: str
    entry: str
    text: str
    keys: frozenset[str]

    @property
    def name(self) -> str:
        """The manifest's file name under `ansible/vars/cluster-manifests/`."""
        # DECIDED: `<tag>.<entry stem>.sops.yaml`. The tag alone would collide on a role with
        # two entries, and a tag holds no dot, so the name splits back into both halves.
        # D3.3c's copy into `cluster/<tag>/` reads this property rather than rebuilding it.
        return f"{self.tag}.{self.entry.removesuffix('.yaml')}{MANIFEST_SUFFIX}"


@dataclass(frozen=True)
class Expected:
    """What the cluster render requires of the cluster files.

    Attributes:
        keys: The keys the cluster file must hold: every placeholder across every tag's
            render, less `SOPS_MANIFEST_KEYS`.
        standalone: The subset of `keys` with a standalone use somewhere.
        manifests: One entry per `render_sops_manifests` entry.
    """

    keys: frozenset[str]
    standalone: frozenset[str]
    manifests: tuple[ManifestEntry, ...]


def dotenv_pairs(text: str) -> dict[str, str]:
    """Each `key=value` line of a dotenv file; any other line is skipped.

    A skipped line is never named by its text, which could be a value: `ciphertext_problems`
    reports it by line number instead.
    """
    pairs = {}
    for line in text.splitlines():
        if "=" not in line or line.lstrip().startswith("#"):
            continue
        key, _, value = line.partition("=")
        pairs[key] = value
    return pairs


def dotenv_names(text: str) -> set[str]:
    """The value keys of a SOPS dotenv, less SOPS's own `sops_*` metadata lines."""
    return {k for k in dotenv_pairs(text) if not k.startswith(SOPS_PREFIX)}


def _is_ciphertext(value: object) -> bool:
    return isinstance(value, str) and value.startswith("ENC[") and value.endswith("]")


def _manifest_values(obj: dict) -> dict[str, object]:
    """Each `data` or `stringData` value of a parsed manifest, as `field.key`."""
    found = {}
    for field in ("data", "stringData"):
        for key, value in (obj.get(field) or {}).items():
            found[f"{field}.{key}"] = value
    return found


def _load_manifest(label: str, text: str) -> tuple[dict | None, list[str]]:
    try:
        obj = yaml_fast.safe_load(text)
    except yaml.YAMLError as exc:
        return None, [f"{label} does not parse as YAML: {exc}"]
    if not isinstance(obj, dict):
        return None, [f"{label} is not a YAML mapping"]
    return obj, []


def is_dotenv(path: Path | str) -> bool:
    """Whether `path` names the dotenv kind of cluster file, by its extension."""
    return str(path).endswith(".env")


# The metadata lines SOPS writes into a dotenv file (`stores/metadata.go` and the key groups'
# `__list_N__map_` flattening). Every other `sops_` name is a value line in disguise, and
# `write` refuses to place a key so named.
_SOPS_METADATA = re.compile(
    r"sops_(mac|lastmodified|version|mac_only_encrypted|shamir_threshold"
    r"|(un)?encrypted_(suffix|regex|comment_regex)"
    r"|(age|pgp|kms|gcp_kms|azure_kv|hc_vault|hckms)__list_\d+__map_[a-z_]+)"
)


def _stray_lines(label: str, text: str, dotenv: bool) -> list[str]:
    """Lines a cluster file holds that no `write` produces, by line number, never quoted.

    SOPS writes no comment into either kind, and no blank line or bare word into a dotenv. A
    comment is invisible to every parsed check, so a value pasted into one would otherwise
    pass them all.
    """
    out = []
    for number, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        if stripped.startswith("#") or (not dotenv and " #" in line):
            out.append(f"{label}: line {number} is a comment, which sops never writes")
        elif dotenv and "=" not in line:
            out.append(f"{label}: line {number} is not a `key=value` line")
    return out


def ciphertext_problems(label: str, text: str, dotenv: bool) -> list[str]:
    """The values of one cluster file that are not `ENC[...]` strings, named never quoted.

    A dotenv `sops_` line passes only when its name is SOPS's own metadata, and a comment or
    a line that is no `key=value` fails in either kind.
    """
    out = _stray_lines(label, text, dotenv)
    if dotenv:
        pairs = dotenv_pairs(text)
        bad = sorted(
            k
            for k, v in pairs.items()
            if not k.startswith(SOPS_PREFIX) and not _is_ciphertext(v)
        )
        out += [
            f"{label}: {k} is a `sops_` line SOPS does not write"
            for k in sorted(pairs)
            if k.startswith(SOPS_PREFIX) and not _SOPS_METADATA.fullmatch(k)
        ]
        if "sops_mac" in pairs and not _is_ciphertext(pairs["sops_mac"]):
            out.append(f"{label}: sops_mac is not SOPS ciphertext")
    else:
        obj, problems = _load_manifest(label, text)
        if obj is None:
            return out + problems
        bad = sorted(
            k for k, v in _manifest_values(obj).items() if not _is_ciphertext(v)
        )
    return out + [f"{label}: {key} is not SOPS ciphertext" for key in bad]


def _mac_fields(text: str, dotenv: bool) -> tuple[object, object, object]:
    """(the content outside SOPS metadata, the MAC, `lastmodified`) of one cluster file."""
    if dotenv:
        pairs = dotenv_pairs(text)
        body = sorted((k, v) for k, v in pairs.items() if not k.startswith(SOPS_PREFIX))
        return body, pairs.get("sops_mac"), pairs.get("sops_lastmodified")
    obj = yaml_fast.safe_load(text) or {}
    meta = obj.pop("sops", None) or {}
    return obj, meta.get("mac"), meta.get("lastmodified")


def mac_proxy_problems(
    label: str, text: str, base: str | None, dotenv: bool
) -> list[str]:
    """A cluster file whose content moved since `base` while its MAC or `lastmodified` did not.

    SOPS rewrites both lines on every write, and a hand edit leaves both as they were, so this
    stands in for "the file decrypts", which CI cannot test without a key. A file absent from
    the base is exempt.
    """
    if base is None:
        return []
    try:
        now, then = _mac_fields(text, dotenv), _mac_fields(base, dotenv)
    except yaml.YAMLError, AttributeError:
        return [f"{label} or its base version does not parse"]
    if now[0] == then[0]:
        return []
    stale = [name for name, i in (("MAC", 1), ("lastmodified", 2)) if now[i] == then[i]]
    if not stale:
        return []
    return [
        f"{label} changed since its base, and its {' and '.join(stale)} did not: it was "
        "edited by hand, not written by sops"
    ]


def file_recipients(text: str, dotenv: bool) -> set[str]:
    """The age recipients recorded in one SOPS file's metadata."""
    if dotenv:
        return {
            v
            for k, v in dotenv_pairs(text).items()
            if re.fullmatch(r"sops_age__list_\d+__map_recipient", k)
        }
    obj = yaml_fast.safe_load(text) or {}
    return {
        entry.get("recipient") for entry in (obj.get("sops") or {}).get("age") or []
    }


def rule_recipients(sops_config: str, rel_to_ansible: str) -> set[str] | None:
    """The `age:` list of the creation rule SOPS picks for a path relative to `ansible/`.

    SOPS matches each rule's `path_regex` in order and takes the first match
    (`parseCreationRuleForFile` in sops `config/config.go`). None when no rule matches.
    """
    for rule in (yaml_fast.safe_load(sops_config) or {}).get("creation_rules") or []:
        if re.search(rule.get("path_regex", ""), rel_to_ansible):
            return {key.strip() for key in str(rule.get("age", "")).split(",") if key}
    return None


def recipient_problems(label: str, have: set[str], want: set[str] | None) -> list[str]:
    """A cluster file whose recipients are not exactly its own rule's full `age:` list."""
    if want is None:
        return [f"{label} matches no creation rule in ansible/.sops.yaml"]
    if have == want:
        return []
    return [
        f"{label} is encrypted to {len(have)} recipient(s), its rule lists {len(want)}; "
        f"{len(want - have)} missing and {len(have - want)} not on the rule"
    ]


@dataclass(frozen=True)
class SourceLine:
    """One `.source` line.

    Attributes:
        digest: The SHA-256 of the key's `ENC[...]` string in `secrets.yml`.
        manifest: The manifest's file name for a manifest's key; None for a dotenv key.
    """

    digest: str
    manifest: str | None = None


def digest(ciphertext: str) -> str:
    """The SHA-256 hex digest of one `ENC[...]` string."""
    return hashlib.sha256(ciphertext.encode()).hexdigest()


def parse_source(text: str) -> dict[str, SourceLine]:
    """The `.source` file's lines, keyed by key name; comments skipped."""
    lines = {}
    for line in text.splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        key, hashed, *rest = line.split()
        lines[key] = SourceLine(hashed, rest[0] if rest else None)
    return lines


SOURCE_HEADER = (
    "# Written by scripts/secrets_mgmt/cluster_vars.py write. Never edit by hand.\n"
    "# <key> <sha256 of its ENC[...] string in secrets.yml> [<manifest it is in>]\n"
)


def source_text(lines: dict[str, SourceLine]) -> str:
    """The `.source` file for `lines`, sorted by key so a no-change write is byte-identical."""
    body = "".join(
        " ".join(filter(None, (key, line.digest, line.manifest))) + "\n"
        for key, line in sorted(lines.items())
    )
    return SOURCE_HEADER + body


def hash_problems(
    source: dict[str, SourceLine], ciphertexts: dict[str, str]
) -> list[str]:
    """Each `.source` key whose digest no longer matches its ciphertext in `secrets.yml`."""
    out = []
    for key, line in sorted(source.items()):
        if key not in ciphertexts:
            out.append(f".source names {key}, which secrets.yml no longer holds")
        elif digest(ciphertexts[key]) != line.digest:
            out.append(
                f"{key} changed in secrets.yml since the cluster files were written"
            )
    return out


def coverage_problems(
    source: dict[str, SourceLine], dotenv: set[str], manifests: Iterable[str]
) -> list[str]:
    """Where `.source` and the cluster files disagree on which keys they hold.

    `.source`'s dotenv keys must equal the dotenv's keys, and its manifest fields must name
    exactly the manifests present, each at least once.
    """
    out = []
    recorded = {k for k, line in source.items() if line.manifest is None}
    for key in sorted(dotenv - recorded):
        out.append(f".source does not cover {key}, which the cluster file holds")
    for key in sorted(recorded - dotenv):
        out.append(f".source names {key}, which the cluster file does not hold")
    present = set(manifests)
    named = {line.manifest for line in source.values() if line.manifest}
    for name in sorted(present - named):
        out.append(f".source names no key of the manifest {name}")
    for name in sorted(named - present):
        out.append(f".source names the manifest {name}, which does not exist")
    return out


def names_problems(dotenv: set[str], keys: frozenset[str]) -> list[str]:
    """Where the dotenv's key names differ from the render's placeholder set."""
    out = [
        f"the cluster render references {k}, and the cluster file does not hold it"
        for k in sorted(keys - dotenv)
    ]
    out += [
        f"the cluster file holds {k}, and no cluster render references it"
        for k in sorted(dotenv - keys)
    ]
    return out


def manifest_set_problems(
    entries: Iterable[ManifestEntry], present: Iterable[str]
) -> list[str]:
    """Where the manifests present differ from one per `render_sops_manifests` entry."""
    want = {e.name: e for e in entries}
    have = set(present)
    out = [
        f"no manifest {name} for {want[name].tag}'s entry {want[name].entry}"
        for name in sorted(want.keys() - have)
    ]
    out += [
        f"the manifest {name} matches no render_sops_manifests entry"
        for name in sorted(have - want.keys())
    ]
    return out


def source_mapping_problems(
    source: dict[str, SourceLine], entries: Iterable[ManifestEntry]
) -> list[str]:
    """Where `.source` files a manifest's keys under another manifest, or not at all."""
    out = []
    for entry in entries:
        filed = {k for k, line in source.items() if line.manifest == entry.name}
        if filed != entry.keys:
            out.append(
                f".source files {sorted(filed)} under {entry.name}, whose template "
                f"names {sorted(entry.keys)}"
            )
    return out


def _structure(obj: dict) -> dict:
    """The plaintext shape `check` compares: everything but the values and the opt-out."""
    meta = dict(obj.get("metadata") or {})
    annotations = {
        k: v
        for k, v in (meta.get("annotations") or {}).items()
        if k != SUBSTITUTE_OPT_OUT
    }
    meta.pop("annotations", None)
    if annotations:
        meta["annotations"] = annotations
    return {
        "apiVersion": obj.get("apiVersion"),
        "kind": obj.get("kind"),
        "metadata": meta,
        "type": obj.get("type"),
        "data": sorted(obj.get("data") or {}),
        "stringData": sorted(obj.get("stringData") or {}),
    }


def manifest_problems(name: str, text: str, rendered: str | None) -> list[str]:
    """One manifest's annotation, `type:str` and, when `rendered` is given, structure.

    Args:
        name: The manifest's file name.
        text: The manifest as committed.
        rendered: Its entry's sentinel render, or None to skip the structure comparison.
    """
    obj, out = _load_manifest(name, text)
    if obj is None:
        return out
    annotations = (obj.get("metadata") or {}).get("annotations") or {}
    if annotations.get(SUBSTITUTE_OPT_OUT) != "disabled":
        out.append(f"{name} lacks the annotation {SUBSTITUTE_OPT_OUT}: disabled")
    for key, value in sorted(_manifest_values(obj).items()):
        found = _ENC_TYPE.search(value) if isinstance(value, str) else None
        if found and found.group(1) != "str":
            out.append(
                f"{name}: {key} was encrypted from a {found.group(1)}, not a string"
            )
    if rendered is not None:
        want, _ = _load_manifest(name, rendered)
        if want is not None and _structure(obj) != _structure(want):
            out.append(f"{name} no longer matches the structure its template renders")
    return out


def ciphertexts(secrets_text: str) -> dict[str, str]:
    """Each `secrets.yml` key's raw `ENC[...]` string, from the encrypted file alone."""
    data = yaml_fast.safe_load(secrets_text) or {}
    return {k: v for k, v in data.items() if k != "sops" and isinstance(v, str)}


def check_tree(
    tree: Tree,
    expected: Expected | None,
    base: Callable[[Path], str | None],
) -> list[str]:
    """Every `check` assertion over `tree`; empty when clean.

    Args:
        tree: The tree to read.
        expected: The render's expectations, or None for `--no-render`, which skips the
            assertions that need one: the file's existence, its names, the manifest set, the
            `.source` mapping and each manifest's structure.
        base: Returns a file's base version, or None where the base has no such file.
    """
    out = []
    exists = tree.cluster_file.is_file()
    manifests = tree.manifest_files()
    present = {path.name for path in manifests}
    if expected is not None and not exists:
        out.append(f"{tree.rel(tree.cluster_file)} does not exist")
    config = tree.sops_config.read_text()
    for path in ([tree.cluster_file] if exists else []) + manifests:
        label, text, dotenv = tree.rel(path), path.read_text(), is_dotenv(path)
        out += ciphertext_problems(label, text, dotenv)
        out += mac_proxy_problems(label, text, base(path), dotenv)
        want = rule_recipients(config, path.relative_to(tree.ansible).as_posix())
        out += recipient_problems(label, file_recipients(text, dotenv), want)
    rendered = {e.name: e.text for e in expected.manifests} if expected else {}
    for path in manifests:
        out += manifest_problems(path.name, path.read_text(), rendered.get(path.name))
    source = parse_source(tree.source.read_text()) if tree.source.is_file() else {}
    held = dotenv_names(tree.cluster_file.read_text()) if exists else set()
    out += hash_problems(source, ciphertexts(tree.secrets.read_text()))
    out += coverage_problems(source, held, present)
    if expected is not None:
        # An absent file has already said so once; a line per missing key adds nothing.
        if exists:
            out += names_problems(held, expected.keys)
        out += manifest_set_problems(expected.manifests, present)
        out += source_mapping_problems(
            source, (e for e in expected.manifests if e.name in present)
        )
    return out
