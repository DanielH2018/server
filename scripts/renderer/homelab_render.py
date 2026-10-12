#!/usr/bin/env python3
"""homelab-render: render the k8s manifests from the git tree alone, in C1's layout.

`cluster` renders each deploy tag into `DIR/cluster/<tag>/`, with a `kustomization.yaml`
listing its files. It reads the git tree and the stub file, and never contacts a cluster, a
host, a registry or a decryption key. Ansible stays the only writer to the cluster.

Target slice R1 implements stub mode only. `--secret-stubs FILE` takes the file that
`shadow_diff.py stubs` writes, and every secret renders as its stub. Without it, `cluster` is
placeholder mode, which arrives with target slice R2. Until then it exits 2 before writing
anything. A stub render is a comparison input and is never published.

Exit codes: 0 rendered; 1 a template failed to render; 2 refused before writing anything;
64 a usage error.

Run: uv run python scripts/renderer/shadow_diff.py stubs --out /tmp/r1-stubs.yml
     uv run python scripts/renderer/homelab_render.py cluster --out /tmp/r1-render \\
         --secret-stubs /tmp/r1-stubs.yml
     uv run pytest scripts/renderer/tests
"""

import sys
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

# Ansible loads a filter plugin by file path; a script reaches it through this insert.
if str(FILTER_PLUGINS) not in _sys.path:
    _sys.path.insert(0, str(FILTER_PLUGINS))

from secret_stubs import homelab_stub_keys, homelab_unstubbed_keys

SECRETS = ANSIBLE / "vars" / "secrets.yml"


def unstubbed(stubs: dict, secrets: Path = SECRETS) -> list[str]:
    """The secret keys the stub file leaves unset or sets to anything but their stub.

    Names only, never a value. A stub file written before a key was added names that key.
    """
    keys = homelab_stub_keys(yaml_fast.safe_load(secrets.read_text()))
    return homelab_unstubbed_keys(keys, stubs)


def cluster(out: Path, roles: list[str] | None, secret_stubs: Path | None) -> int:
    """Render `roles`, or every renderable tag, into `out`; return the exit code."""
    if secret_stubs is None:
        print(
            "homelab-render: cluster without --secret-stubs is placeholder mode, which "
            "arrives with target slice R2. Nothing was written.",
            file=sys.stderr,
        )
        return RENDER_REFUSED
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
        help="a deploy tag to render; repeatable. Default: every tag that renders so far",
    )
    render.add_argument(
        "--secret-stubs",
        type=Path,
        metavar="FILE",
        help="the stub file shadow_diff.py stubs writes; required until target slice R2",
    )
    args = parser.parse_args(argv)
    return cluster(args.out, args.roles, args.secret_stubs)


if __name__ == "__main__":
    sys.exit(main())
