"""Placeholder mode: each secret reference renders as Flux's `${<key>}` (target slice R2).

`cluster` without `--secret-stubs` renders in placeholder mode, and with it in stub mode, R1's
shadow mode. `select_mode` makes that call, and no other flag does (C1 *Modes*).

A placeholder cannot be emitted before the escape, because the escape would escape it too. A
render therefore runs in three passes:

1. The context maps each secret key `K` to the sentinel `HOMELAB_SECRET__K__END`. Letters and
   underscores survive `default`, `indent`, `to_json` and `~` intact, and a sentinel is a
   valid YAML plain scalar, so a render the renderer parses itself still parses.
2. In a directory that substitutes, every `$` followed by `$`, by `{` or by a sentinel becomes
   `$$`. Flux reads `$$` as one literal `$`, so every run of dollars survives substitution as
   written.
3. In a directory that substitutes, every sentinel becomes `${K}`.

A directory substitutes when its key set, the keys whose sentinel its render holds, is not
empty. The unit is the Kustomization directory under `cluster/`, so `<tag>` and
`<tag>-deferred` each have their own set. A directory with an empty set is written exactly as
rendered, every `$` included: Flux applies it with no `postBuild` and never reads that `$` as
syntax. `PlaceholderRender.key_sets` is the one source for every reader that asks whether a
directory substitutes, and each reads it in process.

After pass 3, each document of a directory that substitutes is parsed, and a key whose `${K}`
is the entire parsed value of a scalar, in any style, joins the directory's standalone key
set. Flux types a substituted standalone value by its text, so the standalone-value guard
checks those keys' values where they are decrypted. `PlaceholderRender.standalone` holds the
sets, and `scripts/renderer/standalone_map.py` projects them into the guard's committed map.

`docs/homelab-render.md` is the operator's page, and
`docs/target-architecture/render/r2-secret-placeholders.md` the plan.
"""

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))

import enum
import json
import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path

import yaml

from lib import yaml_fast
from lib.ansible_inventory import K8S_HOST
from lib.repo_paths import K8S_ROLES
from renderer.renderer_lib.cluster import DEFERRED_SUFFIX, RenderedTag, render_tag
from renderer.renderer_lib.environment import RenderError
from renderer.renderer_lib.task_inputs import PINNED_LIVE_FACTS, PinnedFact

SENTINEL_PREFIX = "HOMELAB_SECRET__"
SENTINEL_SUFFIX = "__END"

# The tags placeholder mode skips. Stub mode renders every tag, and the summary line of a
# placeholder render names each tag it skipped. R2's PR 5 empties the set, and R2's exit
# criterion requires it empty.
PLACEHOLDER_DEFERRED = frozenset(
    {
        # templates/config/secrets.yaml.j2 parses `google_assistant_service_account` with
        # `from_json`, which raises on a sentinel. R2's PR 5 moves the key into a Secret of
        # its own and drops the parse.
        "home-assistant",
    }
)

# The keys with a standalone use that the guard does not check, each with its reason. The
# guard's map leaves them out, and the round trip's second pass keeps their R1 stub. R2's PR
# 7b empties the set, and R2's exit criterion requires it empty.
GUARD_DEFERRED = frozenset(
    {
        # qbittorrent's Secret renders `account: "{{ mullvad_account }}"`, and the real value
        # is all digits, so the guard would refuse every qbittorrent deploy. R2's PR 7b moves
        # the key into a SOPS-encrypted Secret of its own, which no placeholder reaches. The
        # operator ruled on 2026-10-13 for this pairing (R2's Open question 13).
        "mullvad_account",
    }
)

# The guard's committed map: each deploy tag to the keys standing alone in its render, less
# GUARD_DEFERRED. `scripts/renderer/standalone_map.py` writes it, and the shared
# `k8s/manifests` role reads it before every render.
GUARD_MAP = K8S_ROLES / "manifests" / "files" / "standalone_secret_keys.json"


def read_guard_map(path: Path = GUARD_MAP) -> dict[str, list[str]]:
    """The guard's map at `path`."""
    return json.loads(path.read_text())


class Mode(enum.Enum):
    """The two render modes of `cluster`."""

    STUB = "stub"
    PLACEHOLDER = "placeholder"


def select_mode(secret_stubs: Path | None) -> Mode:
    """Stub mode when a stub file is passed, placeholder mode when none is (C1 *Modes*)."""
    return Mode.PLACEHOLDER if secret_stubs is None else Mode.STUB


def sentinel(key: str) -> str:
    """The sentinel that stands in for secret key `key` until pass 3."""
    return f"{SENTINEL_PREFIX}{key}{SENTINEL_SUFFIX}"


def placeholder_vars(keys: Iterable[str]) -> dict[str, object]:
    """The overrides of a placeholder render: each key's sentinel and the pinned facts.

    The pinned live-read facts take the values the stub file holds, so a round trip sees the
    same value on both sides. Until R3 deletes `PINNED_LIVE_FACTS`, no tag that reads one may
    be published, and `pinned_fact_refusals` refuses one marked `owner: flux`.

    Raises:
        ValueError: a pinned fact shares a name with a secret key, so one value would
            silently replace the other.
    """
    overrides: dict[str, object] = {key: sentinel(key) for key in keys}
    clashes = sorted(PINNED_LIVE_FACTS.keys() & overrides.keys())
    if clashes:
        raise ValueError(f"{clashes} are both secret keys and pinned live-read facts")
    overrides.update({name: fact.value for name, fact in PINNED_LIVE_FACTS.items()})
    return overrides


def pinned_fact_refusals(
    tags: Iterable[str],
    flux_owned: Iterable[str],
    facts: dict[str, PinnedFact] | None = None,
) -> list[tuple[str, str]]:
    """Each `(tag, fact)` where a requested tag marked `owner: flux` reads a pinned fact.

    A pinned value renders a valid object that fails logins or backups, and only a flux-owned
    tag is published, so a placeholder render refuses that combination (R2 *Mode selection*).
    R3's PR 5 deletes this refusal with `PINNED_LIVE_FACTS`.
    """
    facts = PINNED_LIVE_FACTS if facts is None else facts
    owned = set(tags) & set(flux_owned)
    return sorted(
        (tag, name)
        for name, fact in facts.items()
        for tag in fact.readers
        if tag in owned
    )


def references(text: str) -> Iterator[str]:
    """The body of every `${...}` Flux reads as a reference in `text`.

    Flux's parser treats `$$` as an escape and drops one `$`, and opens a reference only on
    `${` (fluxcd/pkg `envsubst/parse`), so each `$$` pairs before a `${` is looked for.
    """
    i, end = 0, len(text)
    while i < end:
        if text[i] != "$":
            i += 1
        elif text.startswith("$$", i):
            i += 2
        elif text.startswith("${", i):
            close = text.find("}", i + 2)
            close = end if close < 0 else close
            yield text[i + 2 : close]
            i = close + 1
        else:
            i += 1


# A line that separates two YAML documents, kept whole by `re.split`.
DOCUMENT_SEPARATOR = re.compile(r"(^---[ \t]*(?:\n|$))", re.MULTILINE)
# kustomize-controller skips substitution on a resource whose label or annotation of this
# name is `disabled`. The Flux CRDs in the `flux` tag carry it.
SUBSTITUTE_OPT_OUT = "kustomize.toolkit.fluxcd.io/substitute"


def substitution_disabled(doc: str) -> bool:
    """Whether Flux skips substitution on the resource `doc`, by its label or annotation."""
    if SUBSTITUTE_OPT_OUT not in doc:
        return False
    try:
        obj = yaml_fast.safe_load(doc)
    except yaml.YAMLError:
        return False
    meta = (obj.get("metadata") if isinstance(obj, dict) else None) or {}
    return any(
        (meta.get(field) or {}).get(SUBSTITUTE_OPT_OUT) == "disabled"
        for field in ("labels", "annotations")
    )


def documents(text: str) -> list[tuple[str, str]]:
    """Each YAML document of a rendered file as `(Kind/name, text)`, in order."""
    pieces, current = [], []
    for line in text.splitlines(keepends=True):
        if line.rstrip() == "---":
            pieces.append("".join(current))
            current = []
        else:
            current.append(line)
    pieces.append("".join(current))
    found = []
    for piece in pieces:
        if not piece.strip():
            continue
        try:
            obj = yaml_fast.safe_load(piece)
        except yaml.YAMLError:
            obj = None
        if isinstance(obj, dict):
            meta = obj.get("metadata") or {}
            found.append((f"{obj.get('kind')}/{meta.get('name')}", piece))
        else:
            found.append(("no document", piece))
    return found


class Sentinels:
    """Finds the sentinels of a fixed key list in rendered text, and runs passes 2 and 3."""

    def __init__(self, keys: Iterable[str]):
        self._key_of = {sentinel(key): key for key in keys}
        # Longest first, so a key that is a prefix of another cannot claim its sentinel.
        alternation = "|".join(
            re.escape(s) for s in sorted(self._key_of, key=len, reverse=True)
        )
        self._sentinel = re.compile(alternation or r"(?!)")
        self._escapable = re.compile(r"\$(?=\$|\{|" + (alternation or r"(?!)") + ")")

    def key_set(self, files: dict[str, str]) -> frozenset[str]:
        """The keys whose sentinel appears in any of `files`."""
        return frozenset(
            self._key_of[m.group(0)]
            for text in files.values()
            for m in self._sentinel.finditer(text)
        )

    def standalone_keys(self, files: dict[str, str]) -> frozenset[str]:
        """The keys whose `${K}` is the whole parsed value of a scalar in any of `files`.

        `files` are a directory's files after pass 3. A mapping key counts as a scalar too.

        Raises:
            RenderError: A file does not parse as YAML, so its uses cannot be read.
        """
        placeholders = {"${" + key + "}": key for key in self._key_of.values()}
        found: set[str] = set()

        def walk(node: object) -> None:
            if isinstance(node, dict):
                for key, value in node.items():
                    walk(key)
                    walk(value)
            elif isinstance(node, list):
                for value in node:
                    walk(value)
            elif isinstance(node, str) and node in placeholders:
                found.add(placeholders[node])

        for name, text in files.items():
            try:
                for document in yaml_fast.safe_load_all(text):
                    walk(document)
            except yaml.YAMLError as exc:
                raise RenderError(f"{name} does not parse after pass 3: {exc}") from exc
        return frozenset(found)

    def finish(self, text: str) -> str:
        """Passes 2 and 3 on one file of a directory that substitutes.

        A document Flux does not substitute keeps its text, because Flux would never read
        an escape there. A sentinel left in it fails rule 2.
        """
        return "".join(
            piece
            if DOCUMENT_SEPARATOR.fullmatch(piece) or substitution_disabled(piece)
            else self._sentinel.sub(
                lambda m: "${" + self._key_of[m.group(0)] + "}",
                self._escapable.sub("$$", piece),
            )
            for piece in DOCUMENT_SEPARATOR.split(text)
        )


@dataclass(frozen=True)
class PlaceholderRender:
    """A placeholder render after all three passes.

    Attributes:
        tags: Each rendered tag, its files holding placeholders where its directory
            substitutes.
        key_sets: Each directory name under `cluster/` mapped to its placeholder key set.
            A directory substitutes exactly when its set is not empty.
        standalone: Each directory name mapped to its standalone key set, the subset of its
            key set whose placeholder is the whole value of a scalar.
        skipped: The requested tags in `PLACEHOLDER_DEFERRED`, which were not rendered.
    """

    tags: list[RenderedTag]
    key_sets: dict[str, frozenset[str]]
    standalone: dict[str, frozenset[str]]
    skipped: list[str]

    def substituting(self) -> list[str]:
        """The sorted names of the directories that substitute."""
        return sorted(name for name, keys in self.key_sets.items() if keys)


def apply_placeholders(
    rendered: list[RenderedTag], keys: Iterable[str], skipped: Iterable[str] = ()
) -> PlaceholderRender:
    """Run passes 2 and 3 over sentinel renders, directory by directory, and find standalone uses.

    Raises:
        RenderError: A file of a directory that substitutes does not parse after pass 3.
    """
    sentinels = Sentinels(keys)
    tags, key_sets, standalone = [], {}, {}
    for tag in rendered:
        finished = {}
        for name, files in tag.directories().items():
            key_sets[name] = sentinels.key_set(files)
            if key_sets[name]:
                finished[name] = {f: sentinels.finish(t) for f, t in files.items()}
                standalone[name] = sentinels.standalone_keys(finished[name])
            else:
                finished[name] = dict(files)
                standalone[name] = frozenset()
        deferred = finished.get(tag.tag + DEFERRED_SUFFIX, {})
        tags.append(RenderedTag(tag.tag, finished[tag.tag], deferred))
    return PlaceholderRender(tags, key_sets, standalone, sorted(skipped))


def render_placeholders(
    tags: list[str], keys: list[str], host: str = K8S_HOST
) -> PlaceholderRender:
    """Render `tags` in placeholder mode, skipping those in `PLACEHOLDER_DEFERRED`.

    Raises:
        RenderError: A template fails to render.
        ValueError: A pinned fact shares a name with a secret key.
    """
    overrides = placeholder_vars(keys)
    skipped = [tag for tag in tags if tag in PLACEHOLDER_DEFERRED]
    rendered = [
        render_tag(tag, overrides, host)
        for tag in tags
        if tag not in PLACEHOLDER_DEFERRED
    ]
    return apply_placeholders(rendered, keys, skipped)
