#!/usr/bin/env python3
"""homelab-render: render the k8s manifests and the host packages from the git tree alone.

`cluster` renders each deploy tag into `DIR/cluster/<tag>/`, with a `kustomization.yaml`
listing its files. It reads the git tree and, in stub mode, the stub file, and never contacts
a cluster, a host, a registry or a decryption key. Ansible stays the only writer to the
cluster.

Without `--secret-stubs`, `cluster` renders in placeholder mode (target slice R2): every secret
reference renders as `${<key>}`, and a directory whose render holds one escapes every other `$`
Flux would read as syntax. `renderer_lib/placeholders.py` holds the passes and the per-directory
key sets, and placeholder mode skips the tags in its `PLACEHOLDER_DEFERRED`. Placeholder mode
leaves out each role's `render_sops_manifests` files, and the `sops-manifest-entries` check of
`renderer_lib/sops_entries.py` holds those lists before anything is written. Before it writes
anything, a placeholder render also runs the five secret rules of
`renderer_lib/secret_checks.py`.

A placeholder render with `--role` also writes the publish bundle beside `DIR/cluster/`, for
the tags it names that carry `owner: flux` (target slice D2): `DIR/flux/<tag>.yaml`,
`DIR/flux/kustomization.yaml` and `DIR/digests.json`. A render that names no such tag writes
no bundle. `renderer_lib/bundle.py` holds the writer, `SHARED_KINDS` and the footprint
rules. Every `cluster` render refuses a deploy tag named `homelab` or after a `SHARED_KINDS`
member, the names of Kustomizations that are no deploy tag's (C3 *Reserved names*).

`host --host NAME` renders `hosts/NAME/package.yml` into `DIR/hosts/NAME/`, and `package --name
PKG` renders the shared package `hosts/packages/PKG/` into `DIR/packages/PKG/`. Each writes
`root/`, the tree the package installs, and `nfpm.yaml`. Host and package modes render no
secret and keep every `${` literal (`renderer_lib/host.py`).

`--secret-stubs FILE` selects stub mode, R1's shadow mode. It takes the file that
`shadow_diff.py stubs` writes, and every secret renders as its stub. A stub render is a
comparison input and is never published.

Exit codes: 0 rendered; 1 a template failed to render or a secret check failed; 2 refused
before writing anything, a reserved tag name among the causes; 64 a usage error.

Run: uv run python scripts/renderer/homelab_render.py cluster --out /tmp/r2-render
     uv run python scripts/renderer/shadow_diff.py stubs --out /tmp/r1-stubs.yml
     uv run python scripts/renderer/homelab_render.py cluster --out /tmp/r1-render \\
         --secret-stubs /tmp/r1-stubs.yml
     uv run python scripts/renderer/homelab_render.py host --host daniel-pi --out /tmp/h
     uv run pytest scripts/renderer/tests
"""

import os
import sys
import tempfile
from collections.abc import Callable, Generator
from contextlib import contextmanager
from pathlib import Path

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))

from lib import yaml_fast
from lib.cli_help import ArgumentParser
from lib.exit_codes import RENDER_FAILED, RENDER_OK, RENDER_REFUSED
from lib.repo_paths import ANSIBLE, FILTER_PLUGINS
from renderer.renderer_lib import secret_checks
from renderer.renderer_lib.bundle import (
    DIGESTS,
    FLUX_DIR,
    SHARED_KINDS,
    bundle_files,
    write_bundle,
)
from renderer.renderer_lib.cluster import (
    RenderError,
    RenderRefused,
    flux_owned_tags,
    render_tag,
    rollout_timeout_default,
    select_tags,
    write_cluster,
)
from renderer.renderer_lib.context import load_stubs
from renderer.renderer_lib.host import render_host, render_package, write_packages
from renderer.renderer_lib.sops_entries import sops_manifest_entries
from renderer.renderer_lib.placeholders import (
    Mode,
    PlaceholderRender,
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


def cluster(
    out: Path,
    roles: list[str] | None,
    secret_stubs: Path | None,
    render: Callable[[list[str], list[str]], PlaceholderRender] = render_placeholders,
    flux_owned: Callable[[], set[str]] = flux_owned_tags,
    known: frozenset[secret_checks.Violation] = secret_checks.KNOWN_VIOLATIONS,
    kinds: frozenset[str] = SHARED_KINDS,
) -> int:
    """Render `roles`, or every renderable tag, into `out`; return the exit code.

    Args:
        out: The directory to write.
        roles: The tags to render, or None for every render target.
        secret_stubs: The stub file of a stub-mode render, or None for placeholder mode.
        render: The placeholder render; a test replaces it with one over fixture sets.
        flux_owned: Returns the tags marked `owner: flux`; a test passes fixture tags.
        known: The ratchet the secret checks hold the render to; a test passes another.
        kinds: The shared kinds, whose names no deploy tag may take; a test passes others.
    """
    if select_mode(secret_stubs) is Mode.STUB and secret_stubs is not None:
        return _stub_cluster(out, roles, secret_stubs, kinds)
    return _placeholder_cluster(out, roles, render, flux_owned, known, kinds)


def _stub_cluster(
    out: Path, roles: list[str] | None, secret_stubs: Path, kinds: frozenset[str]
) -> int:
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
        tags = select_tags(roles, kinds=kinds)
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


def _placeholder_cluster(
    out: Path,
    roles: list[str] | None,
    render: Callable[[list[str], list[str]], PlaceholderRender],
    flux_owned: Callable[[], set[str]],
    known: frozenset[secret_checks.Violation],
    kinds: frozenset[str],
) -> int:
    try:
        tags = select_tags(roles, kinds=kinds)
        owned = flux_owned()
        keys = secret_keys()
        result = render(tags, keys)
        if not result.tags:
            raise RenderRefused(
                f"every requested tag is in PLACEHOLDER_DEFERRED: {result.skipped}"
            )
        problems = sops_manifest_entries(result, keys)
        if problems:
            raise RenderError("\n".join(problems))
        report = secret_checks.check(result, keys, known)
        if not report.ok():
            print("\n".join(report.lines()), file=sys.stderr)
            print(
                "homelab-render: the secret checks failed. Nothing was written.",
                file=sys.stderr,
            )
            return RENDER_FAILED
        # Only a flux-owned tag is published, so only one gets a Kustomization. A --role
        # render of any other tag, a deferring one included, writes its cluster/ alone.
        published = [t.tag for t in result.tags if t.tag in owned] if roles else []
        bundle = _bundle(result, out, published) if published else {}
        written = write_cluster(result.tags, out)
        written += write_bundle(bundle, out)
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
    publishing = (
        f", {FLUX_DIR}/ and {DIGESTS} for {', '.join(published)}" if bundle else ""
    )
    print(
        f"homelab-render: placeholder mode, {len(result.tags)} tags, "
        f"{len(result.substituting())} of {len(result.key_sets)} directories substitute, "
        f"{len(written)} files under {out}{publishing}, the five secret rules pass with "
        f"{len(report.violations)} known violations{skipped}"
    )
    return RENDER_OK


def _bundle(
    result: PlaceholderRender, out: Path, published: list[str]
) -> dict[str, str]:
    """The `flux/` files and `digests.json` publishing the tags `published` of `result`.

    Computed from the render in this process, before anything is written (C1). A rendered
    directory of a tag outside `published` counts under `_unclassified`.

    Raises:
        RenderRefused: `out` already holds a `flux/` file or `digests.json`, or a published
            tag renders a `-deferred` directory, which no Kustomization applies before D4.2.
    """
    if (out / DIGESTS).exists() or any((out / FLUX_DIR).glob("*")):
        raise RenderRefused(
            f"{out} already holds {FLUX_DIR}/ or {DIGESTS}; render into an empty --out"
        )
    directories = {
        name: files for tag in result.tags for name, files in tag.directories().items()
    }
    try:
        return bundle_files(
            directories,
            result.key_sets,
            published,
            rollout_timeout_default(),
        )
    except ValueError as exc:
        raise RenderRefused(str(exc)) from exc


def packages(out: Path, host: str | None, name: str | None) -> int:
    """Render one host's package, or one shared package, into `out`; return the exit code."""
    try:
        package = render_host(host) if host else render_package(str(name))
        written = write_packages([package], out)
    except RenderRefused as exc:
        print(f"homelab-render: {exc}. Nothing was written.", file=sys.stderr)
        return RENDER_REFUSED
    except RenderError as exc:
        print(f"homelab-render: {exc}. Nothing was written.", file=sys.stderr)
        return RENDER_FAILED
    print(
        f"homelab-render: {package.name}, {len(written)} files under {out / package.directory}"
    )
    return RENDER_OK


@contextmanager
def ansible_local_temp() -> Generator[None]:
    """Point ansible-core's local temp directory at the system temp directory for the block.

    ansible-core creates its local temp directory when the render first imports its
    constants, under `~/.ansible/tmp` by default. The system temp directory keeps the home
    directory untouched, and ansible-core removes its own subdirectory at exit. A value the
    caller already set is left alone. The block restores the environment on exit, so an
    in-process caller such as a pytest worker does not hand the variable to every later
    test and subprocess (#5002).
    """
    if "ANSIBLE_LOCAL_TEMP" in os.environ:
        yield
        return
    os.environ["ANSIBLE_LOCAL_TEMP"] = tempfile.gettempdir()
    try:
        yield
    finally:
        os.environ.pop("ANSIBLE_LOCAL_TEMP", None)


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
    host = commands.add_parser(
        "host", help="render hosts/NAME/package.yml into DIR/hosts/NAME/"
    )
    host.add_argument(
        "--host", required=True, metavar="NAME", help="the inventory host"
    )
    host.add_argument(
        "--out", type=Path, required=True, metavar="DIR", help="the directory to write"
    )
    package = commands.add_parser(
        "package", help="render hosts/packages/PKG/ into DIR/packages/PKG/"
    )
    package.add_argument(
        "--name", required=True, metavar="PKG", help="the shared package"
    )
    package.add_argument(
        "--out", type=Path, required=True, metavar="DIR", help="the directory to write"
    )
    args = parser.parse_args(argv)
    with ansible_local_temp():
        if args.command != "cluster":
            return packages(
                args.out, getattr(args, "host", None), getattr(args, "name", None)
            )
        return cluster(args.out, args.roles, args.secret_stubs)


if __name__ == "__main__":
    sys.exit(main())
