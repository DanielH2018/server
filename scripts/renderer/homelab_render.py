#!/usr/bin/env python3
"""homelab-render: render the k8s manifests from the git tree alone, in C1's layout.

`cluster` renders each deploy tag into `DIR/cluster/<tag>/`, with a `kustomization.yaml`
listing its files. It reads the git tree and, in stub mode, the stub file, and never contacts
a cluster, a host, a registry or a decryption key. Ansible stays the only writer to the
cluster.

Without `--secret-stubs`, `cluster` renders in placeholder mode (target slice R2): every secret
reference renders as `${<key>}`, and a directory whose render holds one escapes every other `$`
Flux would read as syntax. `renderer_lib/placeholders.py` holds the passes and the per-directory
key sets, and placeholder mode skips the tags in its `PLACEHOLDER_DEFERRED`.

`--secret-stubs FILE` selects stub mode, R1's shadow mode. It takes the file that
`shadow_diff.py stubs` writes, and every secret renders as its stub. A stub render is a
comparison input and is never published.

Exit codes: 0 rendered; 1 a template failed to render; 2 refused before writing anything;
64 a usage error.

Run: uv run python scripts/renderer/homelab_render.py cluster --out /tmp/r2-render
     uv run python scripts/renderer/shadow_diff.py stubs --out /tmp/r1-stubs.yml
     uv run python scripts/renderer/homelab_render.py cluster --out /tmp/r1-render \\
         --secret-stubs /tmp/r1-stubs.yml
     uv run pytest scripts/renderer/tests
"""

import os
import sys
import tempfile
from pathlib import Path

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))

from lib import yaml_fast
from lib.cli_help import ArgumentParser
from lib.exit_codes import RENDER_FAILED, RENDER_OK, RENDER_REFUSED
from lib.repo_paths import ANSIBLE, FILTER_PLUGINS
from renderer.renderer_lib.cluster import (
    RenderError,
    RenderRefused,
    render_tag,
    select_tags,
    write_cluster,
)
from renderer.renderer_lib.context import load_stubs
from renderer.renderer_lib.placeholders import (
    Mode,
    render_placeholders,
    select_mode,
)

# Ansible loads a filter plugin by file path; a script reaches it through this insert.
if str(FILTER_PLUGINS) not in _sys.path:
    _sys.path.insert(0, str(FILTER_PLUGINS))

from secret_stubs import homelab_stub_keys, homelab_unstubbed_keys

SECRETS = ANSIBLE / "vars" / "secrets.yml"


def secret_keys(secrets: Path = SECRETS) -> list[str]:
    """The secret key names a render replaces, read from the ciphertext file's keys alone."""
    return homelab_stub_keys(yaml_fast.safe_load(secrets.read_text()))


def unstubbed(stubs: dict, secrets: Path = SECRETS) -> list[str]:
    """The secret keys the stub file leaves unset or sets to anything but their stub.

    Names only, never a value. A stub file written before a key was added names that key.
    """
    return homelab_unstubbed_keys(secret_keys(secrets), stubs)


def cluster(out: Path, roles: list[str] | None, secret_stubs: Path | None) -> int:
    """Render `roles`, or every renderable tag, into `out`; return the exit code."""
    if select_mode(secret_stubs) is Mode.STUB and secret_stubs is not None:
        return _stub_cluster(out, roles, secret_stubs)
    return _placeholder_cluster(out, roles)


def _stub_cluster(out: Path, roles: list[str] | None, secret_stubs: Path) -> int:
    stubs = load_stubs(secret_stubs)
    missing = unstubbed(stubs)
    if missing:
        print(
            f"homelab-render: {secret_stubs} does not stub {missing}. Write a fresh one with "
            "shadow_diff.py stubs. Nothing was written.",
            file=sys.stderr,
        )
        return RENDER_REFUSED
    try:
        tags = select_tags(roles)
        rendered = [render_tag(tag, stubs) for tag in tags]
        written = write_cluster(rendered, out)
    except RenderRefused as exc:
        print(f"homelab-render: {exc}. Nothing was written.", file=sys.stderr)
        return RENDER_REFUSED
    except RenderError as exc:
        print(f"homelab-render: {exc}. Nothing was written.", file=sys.stderr)
        return RENDER_FAILED
    print(
        f"homelab-render: {len(tags)} tags, {len(written)} files under {out / 'cluster'}"
    )
    return RENDER_OK


def _placeholder_cluster(out: Path, roles: list[str] | None) -> int:
    try:
        tags = select_tags(roles)
        result = render_placeholders(tags, secret_keys())
        if not result.tags:
            raise RenderRefused(
                f"every requested tag is in PLACEHOLDER_DEFERRED: {result.skipped}"
            )
        written = write_cluster(result.tags, out)
    except RenderRefused as exc:
        print(f"homelab-render: {exc}. Nothing was written.", file=sys.stderr)
        return RENDER_REFUSED
    except (RenderError, ValueError) as exc:
        print(f"homelab-render: {exc}. Nothing was written.", file=sys.stderr)
        return RENDER_FAILED
    skipped = (
        f"; skipped, in PLACEHOLDER_DEFERRED: {', '.join(result.skipped)}"
        if result.skipped
        else ""
    )
    print(
        f"homelab-render: placeholder mode, {len(result.tags)} tags, "
        f"{len(result.substituting())} of {len(result.key_sets)} directories substitute, "
        f"{len(written)} files under {out / 'cluster'}{skipped}"
    )
    return RENDER_OK


def main(argv: list[str] | None = None) -> int:
    parser = ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    render = commands.add_parser(
        "cluster", help="render the k8s tags into DIR/cluster/<tag>/"
    )
    render.add_argument(
        "--out", type=Path, required=True, metavar="DIR", help="the directory to write"
    )
    render.add_argument(
        "--role",
        dest="roles",
        action="extend",
        nargs="+",
        metavar="TAG",
        help="a deploy tag to render; repeatable. Default: every k8s render target of "
        "daniel-box",
    )
    render.add_argument(
        "--secret-stubs",
        type=Path,
        metavar="FILE",
        help="the stub file shadow_diff.py stubs writes, for a stub-mode render. Without "
        "it, the render is in placeholder mode",
    )
    args = parser.parse_args(argv)
    # ansible-core creates its local temp directory when the render first imports its
    # constants, under `~/.ansible/tmp` by default. The system temp directory keeps the home
    # directory untouched, and ansible-core removes its own subdirectory at exit.
    os.environ.setdefault("ANSIBLE_LOCAL_TEMP", tempfile.gettempdir())
    return cluster(args.out, args.roles, args.secret_stubs)


if __name__ == "__main__":
    sys.exit(main())
