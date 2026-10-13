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

`docs/homelab-render.md` is the operator's page, and
`docs/target-architecture/render/r2-secret-placeholders.md` the plan.
"""

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))

import enum
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from lib.ansible_inventory import K8S_HOST
from renderer.renderer_lib.cluster import DEFERRED_SUFFIX, RenderedTag, render_tag
from renderer.renderer_lib.task_inputs import PINNED_LIVE_FACTS

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
    be published; R2's PR 2 adds that refusal.

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

    def finish(self, text: str) -> str:
        """Passes 2 and 3 on one file of a directory that substitutes."""
        escaped = self._escapable.sub("$$", text)
        return self._sentinel.sub(
            lambda m: "${" + self._key_of[m.group(0)] + "}", escaped
        )


@dataclass(frozen=True)
class PlaceholderRender:
    """A placeholder render after all three passes.

    Attributes:
        tags: Each rendered tag, its files holding placeholders where its directory
            substitutes.
        key_sets: Each directory name under `cluster/` mapped to its placeholder key set.
            A directory substitutes exactly when its set is not empty.
        skipped: The requested tags in `PLACEHOLDER_DEFERRED`, which were not rendered.
    """

    tags: list[RenderedTag]
    key_sets: dict[str, frozenset[str]]
    skipped: list[str]

    def substituting(self) -> list[str]:
        """The sorted names of the directories that substitute."""
        return sorted(name for name, keys in self.key_sets.items() if keys)


def apply_placeholders(
    rendered: list[RenderedTag], keys: Iterable[str], skipped: Iterable[str] = ()
) -> PlaceholderRender:
    """Run passes 2 and 3 over sentinel renders, directory by directory."""
    sentinels = Sentinels(keys)
    tags, key_sets = [], {}
    for tag in rendered:
        finished = {}
        for name, files in tag.directories().items():
            key_sets[name] = sentinels.key_set(files)
            finished[name] = (
                {f: sentinels.finish(text) for f, text in files.items()}
                if key_sets[name]
                else dict(files)
            )
        deferred = finished.get(tag.tag + DEFERRED_SUFFIX, {})
        tags.append(RenderedTag(tag.tag, finished[tag.tag], deferred))
    return PlaceholderRender(tags, key_sets, sorted(skipped))


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
