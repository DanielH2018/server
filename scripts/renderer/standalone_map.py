#!/usr/bin/env python3
"""Write the standalone-value guard's key map from a placeholder render (target slice R2).

A standalone use is a placeholder that is the whole scalar of a field, such as
`email: ${email}`. Flux types a substituted standalone value by its text, so the shared
`k8s/manifests` role refuses, before it renders, a value of such a key that a YAML reader
would type as anything but the same string. The deploy path never runs the renderer, so it
reads the renderer's standalone key sets through this projection: the committed file
`ansible/roles/k8s/manifests/files/standalone_secret_keys.json`, which maps each deploy tag
to the sorted keys that stand alone in its directories, every one of them. A tag with no
such key is left out.

`scripts/renderer/tests/test_renderer_standalone_map.py` holds the committed map
equal to the render, so CI fails a template change that adds a standalone use until the map
is regenerated. `docs/homelab-render.md` is the operator's page.

Exit codes: 0 the map was written, or under `--check` it equals the render; 1 under `--check`
it differs, or the render failed and nothing was written; 64 a usage error.

Run: uv run python scripts/renderer/standalone_map.py           # rewrite the map
     uv run python scripts/renderer/standalone_map.py --check   # exit 1 when it is stale
"""

import sys

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))

import json

from lib.cli_help import ArgumentParser
from lib.exit_codes import FAILED, OK
from renderer.homelab_render import ansible_local_temp, secret_keys
from renderer.renderer_lib.cluster import DEFERRED_SUFFIX, select_tags
from renderer.renderer_lib.environment import RenderError
from renderer.renderer_lib.placeholders import (
    GUARD_MAP,
    PlaceholderRender,
    render_placeholders,
)

REGENERATE = "uv run python scripts/renderer/standalone_map.py"


def guard_map(render: PlaceholderRender) -> dict[str, list[str]]:
    """Each rendered tag mapped to the sorted keys standing alone in its directories.

    A tag's `-deferred` directory counts as the tag's, because one deploy of the tag renders
    both. A tag with no such key is left out.
    """
    keys: dict[str, set[str]] = {}
    for name, standalone in render.standalone.items():
        tag = name.removesuffix(DEFERRED_SUFFIX)
        keys.setdefault(tag, set()).update(standalone)
    return {tag: sorted(found) for tag, found in sorted(keys.items()) if found}


def coverage_problems(
    committed: dict[str, list[str]],
    render: PlaceholderRender,
    known_keys: set[str],
) -> list[str]:
    """Each way `committed` fails to guard the standalone uses of `render`, one line apiece.

    The map must equal the render's projection and name only keys in `known_keys`.
    """
    problems = []
    expected = guard_map(render)
    for tag in sorted(committed.keys() | expected.keys()):
        have, want = set(committed.get(tag, [])), set(expected.get(tag, []))
        if have != want:
            problems.append(
                f"{tag}: the map holds {sorted(have)}, the render stands alone "
                f"{sorted(want)}. Run `{REGENERATE}` and commit the map."
            )
    mapped = {key for keys in committed.values() for key in keys}
    for key in sorted(mapped - known_keys):
        problems.append(f"{key} is in the map and not in ansible/vars/secrets.yml")
    return problems


def map_text(mapping: dict[str, list[str]]) -> str:
    """The committed file's text for `mapping`: sorted, two-space indent, one final newline."""
    return json.dumps(mapping, indent=2, sort_keys=True) + "\n"


def fleet_render() -> PlaceholderRender:
    """A placeholder render of every k8s render target.

    Raises:
        RenderError: A template fails to render.
    """
    return render_placeholders(select_tags(None), secret_keys())


def main(argv: list[str] | None = None) -> int:
    parser = ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--check",
        action="store_true",
        help="write nothing; exit 1 when the committed map differs from the render",
    )
    args = parser.parse_args(argv)
    try:
        with ansible_local_temp():
            text = map_text(guard_map(fleet_render()))
    except (RenderError, ValueError) as exc:
        print(f"standalone_map: {exc}. Nothing was written.", file=sys.stderr)
        return FAILED
    if args.check:
        if GUARD_MAP.is_file() and GUARD_MAP.read_text() == text:
            print(f"standalone_map: {GUARD_MAP.name} matches the render")
            return OK
        print(
            f"standalone_map: {GUARD_MAP} differs from the render. Run `{REGENERATE}` "
            "and commit the map.",
            file=sys.stderr,
        )
        return FAILED
    GUARD_MAP.write_text(text)
    print(f"standalone_map: wrote {len(json.loads(text))} tags to {GUARD_MAP}")
    return OK


if __name__ == "__main__":
    sys.exit(main())
