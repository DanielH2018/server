"""Read two render trees and compare them: the library behind `shadow_diff.py`.

Target slice R1 compares the Ansible manifests role's render with `homelab_render.py
cluster`'s for the same stub inputs. Either tree is an Ansible shadow tree, one directory per
service as `manifests_shadow_dir` keeps it, or a C1 tree, with `cluster/` at the top.

- `load_tree` keys both layouts by the C1 path, so the two compare file by file.
- `stubcheck` names each file whose Ansible render lacks a stub token its C1 render holds.
- `compare` reports every missing, extra and differing file over the expected tag set. It
  refuses an empty set and a missing named member, and prints no stub or secret value.
- `digest_lines` hashes each directory under `cluster/`, and the whole tree. R1's
  determinism tests write the same lines with `digest_line` and `directory_digests`.

`docs/homelab-render.md` is the operator's page.
"""

import base64
import binascii
import difflib
import hashlib
import os
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from pathlib import Path

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))

import yaml

from lib import yaml_fast
from lib.exit_codes import SHADOW_DIFFERENT, SHADOW_OK, SHADOW_REFUSED
from renderer.renderer_lib.cluster import DEFERRED_SUFFIX, KUSTOMIZATION

CLUSTER = "cluster"
# A stub token, as `homelab_secret_stub` and the string stand-ins of `PINNED_LIVE_FACTS` build it.
STUB_TOKEN = re.compile(r"stub-[a-z0-9-]+")
REDACTED = "<stub>"
# Conservative: a ConfigMap that embeds a Secret's text also has its hunk withheld.
_SECRET_KIND = re.compile(r"""^\s*kind:\s*["']?Secret["']?\s*$""", re.M)
HUNK_LINES = 60


class TreeRefused(ValueError):
    """A tree whose layout is neither an Ansible shadow tree nor a C1 tree."""


# -- the two tree layouts -----------------------------------------------------------------


@dataclass(frozen=True)
class Tree:
    """One render tree, with every file keyed by its place in the C1 layout.

    Attributes:
        root: The directory given on the command line.
        ansible: Whether it is an Ansible shadow tree rather than a C1 tree.
        files: `<dir>/<file>` under `cluster/` mapped to the file on disk. An Ansible tree's
            `<svc>/<file>` keeps its key, and its `<svc>/<deferred>/<file>` becomes
            `<svc>-deferred/<file>`. `kustomization.yaml` is left out, because Ansible
            writes none.
        sources: Each key's path relative to `root`, for matching a named member by the
            name the tree itself uses.
    """

    root: Path
    ansible: bool
    files: dict[str, Path]
    sources: dict[str, str] = field(default_factory=dict)


def _files_under(directory: Path) -> list[Path]:
    return sorted(p for p in directory.rglob("*") if p.is_file())


def load_tree(root: Path) -> Tree:
    """Read `root` as a C1 tree when it holds `cluster/`, and as an Ansible tree otherwise.

    The Ansible manifests role stages a deferred file in one subdirectory of the service's
    directory, named by `manifests_deferred_dir_name`, so any subdirectory there maps to
    `<svc>-deferred/`. A file outside `cluster/` in a C1 tree is not compared: `flux/` belongs
    to a later slice.

    Raises:
        TreeRefused: `root` is not a directory, or a file sits where neither layout puts one.
    """
    if not root.is_dir():
        raise TreeRefused(f"{root} is not a directory")
    ansible = not (root / CLUSTER).is_dir()
    base = root if ansible else root / CLUSTER
    files: dict[str, Path] = {}
    sources: dict[str, str] = {}
    for path in _files_under(base):
        parts = path.relative_to(base).parts
        if ansible and len(parts) == 3:
            key = f"{parts[0]}{DEFERRED_SUFFIX}/{parts[2]}"
        elif len(parts) == 2:
            key = "/".join(parts)
        else:
            layout = "an Ansible shadow tree" if ansible else "a C1 tree"
            raise TreeRefused(
                f"{path.relative_to(root)} is not where {layout} puts a manifest"
            )
        if parts[-1] == KUSTOMIZATION:
            continue
        if key in files:
            raise TreeRefused(
                f"{root} holds two deferred directories for {parts[0]}: {key} twice"
            )
        files[key] = path
        sources[key] = path.relative_to(root).as_posix()
    return Tree(root, ansible, files, sources)


def key_tag(key: str) -> str:
    """The deploy tag a C1 key belongs to: its directory, less any `-deferred` suffix."""
    directory = key.split("/", 1)[0]
    return directory.removesuffix(DEFERRED_SUFFIX)


# -- stub tokens --------------------------------------------------------------------------


def _secret_data_values(text: str) -> Iterable[str]:
    """The decoded `data` values of each `kind: Secret` document in `text`.

    A file that does not parse, or a value that is not base64, yields nothing; its raw text is
    still searched.
    """
    try:
        docs = list(yaml_fast.safe_load_all(text))
    except yaml.YAMLError:
        return
    for doc in docs:
        if not isinstance(doc, dict) or doc.get("kind") != "Secret":
            continue
        for value in (doc.get("data") or {}).values():
            try:
                yield base64.b64decode(str(value), validate=True).decode(
                    "utf-8", errors="replace"
                )
            except binascii.Error, ValueError:
                continue


def stub_tokens(data: bytes) -> set[str]:
    """Every stub token in a file, with the `data` of its Secret documents decoded first."""
    text = data.decode("utf-8", errors="replace")
    tokens = set(STUB_TOKEN.findall(text))
    for value in _secret_data_values(text):
        tokens.update(STUB_TOKEN.findall(value))
    return tokens


def lacking_stubs(ansible: bytes, c1: bytes) -> list[str]:
    """The stub tokens the C1 file holds and the Ansible file does not, sorted.

    Tokens compare as whole matches, so `stub-domain` inside `stub-domain-x` does not count.
    """
    return sorted(stub_tokens(c1) - stub_tokens(ansible))


def stubcheck(ansible: Tree, c1: Tree) -> dict[str, list[str]]:
    """Each file present in both trees mapped to the stub tokens its Ansible render lacks.

    Only failing files are returned. A file only one side holds, or one whose C1 render holds
    no token, passes unchecked; the manifests role's shadow-dir assert is its guard.
    """
    failing = {}
    for key in sorted(ansible.files.keys() & c1.files.keys()):
        lacking = lacking_stubs(
            ansible.files[key].read_bytes(), c1.files[key].read_bytes()
        )
        if lacking:
            failing[key] = lacking
    return failing


# -- compare ------------------------------------------------------------------------------


@dataclass(frozen=True)
class NamedMember:
    """A file `compare` must find on both sides, so an empty or partial tree cannot pass.

    Attributes:
        label: The name the report prints.
        tag: The tag whose directory holds it, or None for any compared tag.
        c1: An `fnmatch` pattern over a C1 key.
        ansible: The same over a path in an Ansible tree, which names the deferred
            directory as the role does.
    """

    label: str
    tag: str | None
    c1: str
    ansible: str

    def found_in(self, tree: Tree, keys: Iterable[str]) -> bool:
        pattern = self.ansible if tree.ansible else self.c1
        names = (tree.sources[k] if tree.ansible else k for k in keys)
        return any(fnmatchcase(name, pattern) for name in names)


# One applied file, the deferred directory and a volume claim: each channel the comparison
# covers (the plan's *What zero diff covers*).
NAMED_MEMBERS = (
    NamedMember(
        "traefik/deployment.yaml",
        "traefik",
        "traefik/deployment.yaml",
        "traefik/deployment.yaml",
    ),
    NamedMember(
        "pihole-instance-2",
        "pihole",
        f"pihole{DEFERRED_SUFFIX}/*",
        "pihole/pihole-instance-2/*",
    ),
    NamedMember("a claim-*.yaml file", None, "*/claim-*.yaml", "*/claim-*.yaml"),
)

# The classes a report groups its differences by. The three whitespace classes are the
# plan's expected renderer gaps; a stub-check failure outranks them.
ONLY_FIRST = "only in {first}"
ONLY_SECOND = "only in {second}"
OUTSIDE_SET = "in {side} for a tag outside the expected set"
NEITHER = "expected tag rendered on neither side"
STUB_CHECK = "lacks a stub token the C1 render holds; content withheld"
TRAILING = "differs in trailing newlines only"
WHITESPACE = "differs in whitespace only"
CONTENT = "differs in content"


def difference_class(first: bytes, second: bytes) -> str:
    """Which whitespace class a pair of differing files falls in."""
    if first.rstrip(b"\n") == second.rstrip(b"\n"):
        return TRAILING
    if b"".join(first.split()) == b"".join(second.split()):
        return WHITESPACE
    return CONTENT


def redact(line: str) -> str:
    """`line` with every stub token replaced by `<stub>`."""
    return STUB_TOKEN.sub(REDACTED, line)


def hunk(key: str, first: bytes, second: bytes, names: tuple[str, str]) -> list[str]:
    """A redacted unified diff of one file, cut at `HUNK_LINES` lines."""
    lines = list(
        difflib.unified_diff(
            first.decode("utf-8", errors="replace").splitlines(keepends=True),
            second.decode("utf-8", errors="replace").splitlines(keepends=True),
            fromfile=f"{names[0]}/{key}",
            tofile=f"{names[1]}/{key}",
            n=2,
        )
    )
    shown = [redact(line.rstrip("\n")) for line in lines[:HUNK_LINES]]
    if len(lines) > HUNK_LINES:
        shown.append(f"... {len(lines) - HUNK_LINES} more diff lines")
    return shown


@dataclass
class Report:
    """What `compare` found.

    Attributes:
        tags: The number of expected tags, `<t>` in the summary line.
        files: The number of distinct files compared, `<n>`.
        classes: Each class of difference mapped to the keys in it.
        hunks: The redacted diff lines of every printable differing file, in key order.
        refusals: Why the comparison cannot vouch for anything, even with no difference.
        notes: Named members dropped because their tag is flux-owned.
    """

    tags: int = 0
    files: int = 0
    classes: dict[str, list[str]] = field(default_factory=dict)
    hunks: list[str] = field(default_factory=list)
    refusals: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def differences(self) -> int:
        return sum(len(keys) for keys in self.classes.values())

    def add(self, cls: str, key: str) -> None:
        self.classes.setdefault(cls, []).append(key)

    def summary(self) -> str:
        return f"{self.tags} tags, {self.files} files, {self.differences} differences"

    def lines(self) -> list[str]:
        """The report as printed: the summary line, notes, refusals, classes, then hunks."""
        out = [self.summary(), *self.notes]
        out += [f"refused: {reason}" for reason in self.refusals]
        for cls, keys in self.classes.items():
            out.append(f"{cls} ({len(keys)}):")
            out += [f"  {key}" for key in sorted(keys)]
        return out + self.hunks

    def exit_code(self) -> int:
        if self.refusals:
            return SHADOW_REFUSED
        return SHADOW_DIFFERENT if self.differences else SHADOW_OK


def _side_names(first: Tree, second: Tree) -> tuple[str, str]:
    if first.ansible != second.ansible:
        return ("ansible", "render") if first.ansible else ("render", "ansible")
    return "first", "second"


def compare(
    first: Tree, second: Tree, targets: Iterable[str], flux_owned: Iterable[str]
) -> Report:
    """Compare two trees over the expected tag set, and say how they differ.

    When either side is an Ansible tree, the expected set is `targets` less `flux_owned`, a C1
    side's flux-owned directories are ignored, and an Ansible side's are reported as extra.
    Two C1 trees are compared over all of `targets`.

    The report withholds the content of a file that fails `stubcheck`'s rule and of one that
    holds a Secret, and prints the stub tokens of every other hunk as `<stub>`.
    """
    names = _side_names(first, second)
    targets, flux = set(targets), set(flux_owned) & set(targets)
    any_ansible = first.ansible or second.ansible
    expected = targets - flux if any_ansible else targets
    report = Report(tags=len(expected))

    def in_scope(tree: Tree, side: str) -> set[str]:
        keep = set()
        for key in tree.files:
            tag = key_tag(key)
            if tag in expected:
                keep.add(key)
            elif tree.ansible or tag not in flux:
                report.add(OUTSIDE_SET.format(side=side), key)
        return keep

    a, b = in_scope(first, names[0]), in_scope(second, names[1])
    report.files = len(a | b)
    for key in sorted(a - b):
        report.add(ONLY_FIRST.format(first=names[0]), key)
    for key in sorted(b - a):
        report.add(ONLY_SECOND.format(second=names[1]), key)
    rendered = {key_tag(key) for key in a | b}
    for tag in sorted(expected - rendered):
        report.add(NEITHER, f"{tag}/")

    # stubcheck's rule applies to an Ansible tree against a C1 tree only.
    mixed = first.ansible != second.ansible
    for key in sorted(a & b):
        one, two = first.files[key].read_bytes(), second.files[key].read_bytes()
        if one == two:
            continue
        ansible, c1 = (one, two) if first.ansible else (two, one)
        if mixed and lacking_stubs(ansible, c1):
            report.add(STUB_CHECK, key)
            continue
        report.add(difference_class(one, two), key)
        if _SECRET_KIND.search(one.decode("utf-8", "replace")) or _SECRET_KIND.search(
            two.decode("utf-8", "replace")
        ):
            report.hunks.append(f"{key}: holds a Secret; hunk withheld")
        else:
            report.hunks += hunk(key, one, two, names)

    if not expected:
        report.refusals.append("the expected tag set is empty")
    elif not report.files:
        report.refusals.append("neither tree holds a file for an expected tag")
    for member in NAMED_MEMBERS:
        if member.tag is not None and member.tag not in expected:
            if member.tag in flux:
                report.notes.append(
                    f"named member {member.label} dropped: {member.tag} is flux-owned"
                )
                continue
        for tree, keys, side in ((first, a, names[0]), (second, b, names[1])):
            if not member.found_in(tree, keys):
                report.refusals.append(
                    f"no file for named member {member.label} in {side}"
                )
    return report


# -- digest -------------------------------------------------------------------------------


def files_digest(base: Path) -> str:
    """A sha256 over every file under `base`: each relative path and its bytes.

    Paths sort bytewise and each field is length-prefixed, so no line depends on the absolute
    directory or the locale, and no path and content can run together.
    """
    digest = hashlib.sha256()
    entries = sorted(
        (os.fsencode(p.relative_to(base).as_posix()), p) for p in _files_under(base)
    )
    for rel, path in entries:
        data = path.read_bytes()
        for part in (rel, data):
            digest.update(len(part).to_bytes(8, "big"))
            digest.update(part)
    return digest.hexdigest()


def digest_line(name: str, hexdigest: str) -> str:
    """One digest line, the format CI's job summary and `digest` share."""
    return f"{name} sha256:{hexdigest}"


def directory_digests(root: Path) -> dict[str, str]:
    """Each directory under `root/cluster/` mapped to its `files_digest`, in bytewise order.

    A directory's line depends on its own files alone, so a render of some tags gives the same
    lines for those tags as a render of all of them.
    """
    cluster = root / CLUSTER
    dirs = sorted(
        (p for p in cluster.iterdir() if p.is_dir()), key=lambda p: os.fsencode(p.name)
    )
    return {d.name: files_digest(d) for d in dirs}


def digest_lines(root: Path) -> list[str]:
    """`digest`'s output: one line per directory under `root/cluster/`, then `total`.

    Raises:
        TreeRefused: `root` holds no `cluster/` directory with a file in it.
    """
    if not (root / CLUSTER).is_dir() or not _files_under(root / CLUSTER):
        raise TreeRefused(f"{root} holds no {CLUSTER}/ directory with a file in it")
    lines = [digest_line(name, h) for name, h in directory_digests(root).items()]
    return [*lines, digest_line("total", files_digest(root))]
