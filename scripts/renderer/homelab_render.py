#!/usr/bin/env python3
"""homelab-render: render the k8s manifests and the host packages from the git tree alone.

`cluster` renders each deploy tag into `DIR/cluster/<tag>/`, with a `kustomization.yaml`
listing its files. It reads the git tree and, in stub mode, the stub file, and never contacts
a cluster, a host, a registry or a decryption key. Ansible stays the only writer to the cluster.

Without `--secret-stubs`, `cluster` renders in placeholder mode (target slice R2): every secret
reference renders as `${<key>}`, and a directory whose render holds one escapes every other `$`
Flux would read as syntax. `renderer_lib/placeholders.py` holds the passes and the per-directory
key sets, and placeholder mode renders every requested tag. Placeholder mode leaves out each role's `render_sops_manifests` files, and the `sops-manifest-entries` check of
`renderer_lib/sops_entries.py` holds those lists before anything is written. Before it writes
anything, a placeholder render also runs the five secret rules of
`renderer_lib/secret_checks.py`.

A placeholder render with `--role` also writes the publish bundle beside `DIR/cluster/`, for
the tags it names that carry `owner: flux` (target slice D2): `DIR/flux/<tag>.yaml`,
`DIR/flux/kustomization.yaml` and `DIR/digests.json`, plus each `SHARED_KINDS` member's
`DIR/flux/<kind>.yaml` and `DIR/cluster/<kind>/`. A render that names no such tag writes no
bundle. `cluster --shared-only` writes the shared kinds, a root listing them and
`digests.json`, and no tag: the publish job's call when no tag is flux-owned (D3.3a). The
`cluster-vars` kind copies the cluster file `ansible/vars/cluster-vars.sops.env` byte for
byte, and a render that cannot read it writes nothing. The `soak` kind judges `--revision`,
by default `HEAD`, the commit the publish job checks out. `renderer_lib/bundle.py` holds the
writer, `SHARED_KINDS` and the footprint rules. Every `cluster` render refuses a deploy tag
named `homelab` or after a `SHARED_KINDS` member, the names of Kustomizations that are no
deploy tag's (C3 *Reserved names*).

A published tag whose role lists `render_sops_manifests` gets a byte copy of each SOPS manifest
under `ansible/vars/cluster-manifests/` in `DIR/cluster/<tag>/`, and `decryption` on its
Kustomization (D3.3c); a manifest without the substitute-disabled annotation fails the render.
One with snapshot claims gets its snapshot Job in `DIR/cluster/<tag>-snapshot/` (`snapshot.py`).
One whose role sets `flux_prepull_timeout` gets its pre-pull Jobs in `DIR/cluster/<tag>-prepull/`
(`prepull.py`); one setting `flux_cronjob_gates`, its gate Jobs in `<tag>-verify/` (`cronjob_gate.py`).
One whose role ships `templates/precheck/` gets those check Jobs in `<tag>-precheck/`, which the
tag waits on (#5719).
A placeholder render also writes the context of each image its tags' roles build with
`k8s/image-builder`: `DIR/images/<name>/` and `DIR/images/tags.json`, whose content tags each
consumer's ref reads in the same render (`renderer_lib/images.py`, D5.4a1). A full render
writes all ten. `--all-images` writes all ten whatever `--role` names, which the publish job
passes because its `images` job builds every context (#5374).
In both modes, every flux-owned tag's restart targets get `checksum/restart`, hashed from the
placeholder-form documents and `ansible/vars/cluster-vars.source`
(`renderer_lib/restart.py`), and its claims and Namespaces the prune label (`prune_label.py`).

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
     uv run python scripts/renderer/homelab_render.py cluster --out /tmp/d3 --shared-only
     uv run python scripts/renderer/shadow_diff.py stubs --out /tmp/r1-stubs.yml
     uv run python scripts/renderer/homelab_render.py cluster --out /tmp/r1-render \\
         --secret-stubs /tmp/r1-stubs.yml
     uv run python scripts/renderer/homelab_render.py host --host daniel-pi --out /tmp/h
     uv run pytest scripts/renderer/tests
"""

import sys
from collections.abc import Callable
from functools import partial
from pathlib import Path

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))

from lib import yaml_fast
from lib.cli_help import ArgumentParser
from lib.exit_codes import RENDER_FAILED, RENDER_OK, RENDER_REFUSED
from lib.repo_paths import ANSIBLE, FILTER_PLUGINS, K8S_ROLES
from renderer.renderer_lib import secret_checks
from renderer.renderer_lib.bundle import (
    CLUSTER_DIR,
    CLUSTER_VARS_FILE,
    DIGESTS,
    FLUX_DIR,
    SHARED_KINDS,
    bundle_files,
    write_bundle,
)
from renderer.renderer_lib.ordering import NO_ORDERING, Ordering
from renderer.renderer_lib.cluster import (
    RenderedTag,
    RenderError,
    RenderRefused,
    attach_vendored,
    bundle_ordering,
    flux_owned_tags,
    render_tag,
    rollout_timeout_default,
    select_tags,
    tag_context,
    write_cluster,
)
from renderer.renderer_lib.images import TAGS_FILE, check_images, ghcr_refs, image_files
from renderer.renderer_lib.prune_label import label_tags
from renderer.renderer_lib.cronjob_gate import attach_gates
from renderer.renderer_lib.prepull import attach_prepulls
from renderer.renderer_lib.snapshot import attach_snapshots
from renderer.renderer_lib.restart import outside_secrets, restart_targets
from renderer.renderer_lib.restart import restart_values, stamp
from renderer.renderer_lib.restart import source_digests
from renderer.renderer_lib.context import load_stubs
from renderer.renderer_lib.environment import ansible_local_temp
from renderer.renderer_lib.host import render_host, render_package, write_packages
from renderer.renderer_lib.sops_entries import (
    copy_sops_manifests,
    sops_manifest_entries,
)
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
# The cluster file `cluster_vars.py write` keeps. The bundle's copy keeps its basename.
CLUSTER_FILE = ANSIBLE / "vars" / CLUSTER_VARS_FILE
# Each cluster key's ciphertext digest, which `checksum/restart` hashes in place of a value.
SOURCE_FILE = ANSIBLE / "vars" / "cluster-vars.source"
# The SOPS manifests `cluster_vars.py write` keeps, one per `render_sops_manifests` entry.
MANIFESTS_DIR = ANSIBLE / "vars" / "cluster-manifests"


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
    kinds: frozenset[str] = SHARED_KINDS,
    cluster_file: Path = CLUSTER_FILE,
    shared_only: bool = False,
    source_file: Path = SOURCE_FILE,
    manifests_dir: Path = MANIFESTS_DIR,
    ordering: Callable[[set[str]], Ordering] = bundle_ordering,
    vendored: Path = K8S_ROLES,
    revision: str | None = None,
) -> int:
    """Render `roles`, or every renderable tag, into `out`; return the exit code.

    Args:
        out: The directory to write.
        roles: The tags to render, or None for every render target.
        secret_stubs: The stub file of a stub-mode render, or None for placeholder mode.
        render: The placeholder render; a test replaces it with one over fixture sets.
        flux_owned: Returns the tags marked `owner: flux`; a test passes fixture tags.
        kinds: The shared kinds, whose names no deploy tag may take; a test passes others.
        cluster_file: The cluster file the `cluster-vars` kind copies; a test passes a
            fixture.
        shared_only: Write the shared kinds and the root alone, and render no tag. The
            caller rejects it beside `roles` or `secret_stubs`.
        source_file: `cluster-vars.source`, whose digests `checksum/restart` hashes; a test
            passes a fixture.
        manifests_dir: The SOPS manifests a published tag's directory copies; a test passes
            a fixture directory.
        ordering: Returns what `dependsOn` is generated from, given the flux-owned tags; a
            test passes a fixture map.
        vendored: The role tree vendored files are read from; a test passes a fixture.
        revision: The commit the soak Job judges; None renders it at the normalised SHA.
    """
    if shared_only:
        return _shared_cluster(out, cluster_file, revision)
    if select_mode(secret_stubs) is Mode.STUB and secret_stubs is not None:
        return _stub_cluster(
            out, roles, secret_stubs, kinds, render, flux_owned, source_file, vendored
        )
    return _placeholder_cluster(
        out,
        roles,
        render,
        flux_owned,
        kinds,
        cluster_file,
        source_file,
        manifests_dir,
        ordering,
        vendored,
        revision,
    )


def stamp_restarts(
    rendered: list[RenderedTag],
    placeholder: PlaceholderRender,
    owned: set[str],
    keys: list[str],
    source: dict[str, str],
) -> list[RenderedTag]:
    """`rendered` with `checksum/restart` stamped on each flux-owned tag's restart targets.

    Args:
        rendered: The tags to stamp, in either render mode.
        placeholder: A placeholder render holding every flux-owned tag of `rendered`. The
            value is hashed from it, so both modes stamp the same value.
        owned: The flux-owned tags.
        keys: The key names of `ansible/vars/secrets.yml`.
        source: `source_digests`.

    Raises:
        RenderError: A tag's restart targets do not resolve or name no workload, or a
            Secret a target reads holds a key `.source` does not record.
    """
    forms = {t.tag: t for t in placeholder.tags}
    stamped = []
    for tag in rendered:
        if tag.tag not in owned:
            stamped.append(tag)
            continue
        try:
            context = tag_context(K8S_ROLES / tag.tag, tag.tag, {})
        except ValueError as exc:
            raise RenderError(f"{tag.tag}: {exc}") from exc
        values = restart_values(
            tag.tag,
            forms[tag.tag].directories(),
            restart_targets(tag.tag, context),
            keys,
            source,
            placeholder.sops_manifests.get(tag.tag),
            outside_secrets(tag.tag),
        )
        dirs = {
            name: {
                file: stamp(text, values, f"{name}/{file}")
                for file, text in files.items()
            }
            for name, files in tag.directories().items()
        }
        stamped.append(tag.with_directories(dirs))
    return stamped


def _stub_cluster(
    out: Path,
    roles: list[str] | None,
    secret_stubs: Path,
    kinds: frozenset[str],
    render: Callable[[list[str], list[str]], PlaceholderRender],
    flux_owned: Callable[[], set[str]],
    source_file: Path,
    vendored: Path,
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
        # The stamp hashes the placeholder form, never the stub render, so the value equals
        # the placeholder render's.
        owned = set(tags) & flux_owned()
        if owned:
            keys = secret_keys()
            placeholder = render(sorted(owned), keys)
            rendered = stamp_restarts(
                ghcr_refs(rendered, owned, placeholder.images),
                placeholder,
                owned,
                keys,
                source_digests(source_file),
            )
            rendered = label_tags(rendered, owned)
        written = write_cluster(attach_vendored(rendered, roles=vendored), out)
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
    kinds: frozenset[str],
    cluster_file: Path,
    source_file: Path,
    manifests_dir: Path,
    ordering: Callable[[set[str]], Ordering],
    vendored: Path,
    revision: str | None,
) -> int:
    try:
        tags = select_tags(roles, kinds=kinds)
        owned = flux_owned()
        keys = secret_keys()
        result = render(tags, keys)
        problems = sops_manifest_entries(result, keys)
        if problems:
            raise RenderError("\n".join(problems))
        report = secret_checks.check(result, keys)
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
        tags = ghcr_refs(result.tags, owned, result.images)
        tags = stamp_restarts(tags, result, owned, keys, source_digests(source_file))
        tags = copy_sops_manifests(
            label_tags(tags, owned), result.sops_manifests, published, manifests_dir
        )
        tags = attach_vendored(tags, result.key_sets, vendored)
        tags = attach_snapshots(tags, published, rollout_timeout_default(), vendored)
        tags = attach_prepulls(tags, published, vendored)
        tags = attach_gates(tags, published, vendored, result.key_sets)
        bundle = (
            _bundle(
                tags, result, out, published, cluster_file, ordering(owned), revision
            )
            if published
            else {}
        )
        contexts = image_files(result.images) if result.images else {}
        check_images(tags, bundle, result.images, out)
        written = write_cluster(tags, out)
        written += write_bundle(bundle, out)
        written += write_bundle(contexts, out)
    except RenderRefused as exc:
        print(f"homelab-render: {exc}. Nothing was written.", file=sys.stderr)
        return RENDER_REFUSED
    except (RenderError, ValueError) as exc:
        print(f"homelab-render: {exc}. Nothing was written.", file=sys.stderr)
        return RENDER_FAILED
    publishing = (
        f", {FLUX_DIR}/ and {DIGESTS} for {', '.join(published)}" if bundle else ""
    )
    building = (
        f", {len(result.images)} image contexts and {TAGS_FILE}" if contexts else ""
    )
    print(
        f"homelab-render: placeholder mode, {len(result.tags)} tags, "
        f"{len(result.substituting())} of {len(result.key_sets)} directories substitute, "
        f"{len(written)} files under {out}{publishing}{building}, the five secret rules pass"
    )
    return RENDER_OK


def _bundle(
    tags: list[RenderedTag],
    result: PlaceholderRender,
    out: Path,
    published: list[str],
    cluster_file: Path,
    ordering: Ordering,
    revision: str | None,
) -> dict[str, str]:
    """The bundle files publishing the tags `published` of `tags` and every shared kind.

    Computed from the render in this process, before anything is written (C1). `tags` is
    `result`'s tags as written, stamped and with their SOPS manifest copies. A rendered
    directory of a tag outside `published` counts under `_unclassified`. A published tag
    whose role lists `render_sops_manifests` gets `decryption`.

    Raises:
        RenderRefused: `out` already holds a bundle file, the cluster file cannot be read,
            or the generated `dependsOn` edges form a cycle.
    """
    directories = {
        name: files for tag in tags for name, files in tag.directories().items()
    }
    decrypting = [tag for tag in published if result.sops_manifests.get(tag)]
    shared = (out, directories, result.key_sets, published, cluster_file)
    return _shared_bundle(*shared, decrypting, ordering, revision)


def _shared_bundle(
    out: Path,
    directories: dict[str, dict[str, str]],
    key_sets: dict[str, frozenset[str]],
    published: list[str],
    cluster_file: Path,
    decrypting: list[str] | None = None,
    ordering: Ordering = NO_ORDERING,
    revision: str | None = None,
) -> dict[str, str]:
    """`bundle_files` for `published`, once `out` holds no bundle file to overwrite.

    Raises:
        RenderRefused: `out` already holds a `flux/` file, `digests.json` or a shared
            kind's directory, the cluster file cannot be read, or `bundle_files` refuses.
    """
    occupied = (
        (out / DIGESTS).exists()
        or any((out / FLUX_DIR).glob("*"))
        or any(any((out / CLUSTER_DIR / kind).glob("*")) for kind in SHARED_KINDS)
    )
    if occupied:
        raise RenderRefused(
            f"{out} already holds {FLUX_DIR}/, {DIGESTS} or a shared kind's "
            f"{CLUSTER_DIR}/ directory; render into an empty --out"
        )
    try:
        dotenv = cluster_file.read_text()
    except OSError as exc:
        raise RenderRefused(
            f"the cluster-vars kind cannot copy the cluster file: {exc}. The bundle "
            "always carries it; cluster_vars.py write (operator-only) writes it"
        ) from exc
    try:
        return bundle_files(
            directories,
            key_sets,
            published,
            rollout_timeout_default(),
            dotenv,
            decrypting or [],
            ordering,
            revision=revision,
        )
    except ValueError as exc:
        raise RenderRefused(str(exc)) from exc


def _shared_cluster(out: Path, cluster_file: Path, revision: str | None) -> int:
    """Write the shared kinds, a root listing them and `digests.json`, and no tag.

    The publish job calls it when no tag is flux-owned, because `cluster` without `--role`
    renders every tag. It renders no template and reads no secret key, so it needs no
    Ansible.
    """
    try:
        written = write_bundle(
            _shared_bundle(out, {}, {}, [], cluster_file, revision=revision), out
        )
    except RenderRefused as exc:
        print(f"homelab-render: {exc}. Nothing was written.", file=sys.stderr)
        return RENDER_REFUSED
    print(
        f"homelab-render: shared kinds only, {', '.join(sorted(SHARED_KINDS))}: "
        f"{len(written)} files under {out}"
    )
    return RENDER_OK


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
    render.add_argument(
        "--shared-only",
        action="store_true",
        help="write the shared kinds, a root listing them and digests.json, and no tag: "
        "the publish job's bundle when no tag is flux-owned. Takes neither --role nor "
        "--secret-stubs",
    )
    render.add_argument(
        "--all-images",
        action="store_true",
        help="write every image context and tags.json, not only those the named tags' "
        "roles build: the publish job's render, whose images job builds all ten",
    )
    render.add_argument(
        "--revision",
        metavar="SHA",
        help="the commit the soak Job judges. Default: HEAD",
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
    if getattr(args, "shared_only", False) and (args.roles or args.secret_stubs):
        parser.error(
            "--shared-only renders no tag and only publishes, so it takes neither --role "
            "nor --secret-stubs"
        )
    if getattr(args, "all_images", False) and (args.shared_only or args.secret_stubs):
        parser.error(
            "--all-images writes image contexts, which only a placeholder render of tags "
            "writes, so it takes neither --shared-only nor --secret-stubs"
        )
    with ansible_local_temp():
        if args.command != "cluster":
            return packages(
                args.out, getattr(args, "host", None), getattr(args, "name", None)
            )
        # Function-level: a host render must not open the soak's deploy_tools imports.
        from renderer.renderer_lib.soak import resolve_revision

        try:
            revision = resolve_revision(args.revision)
        except ValueError as exc:
            print(f"homelab-render: {exc}. Nothing was written.", file=sys.stderr)
            return RENDER_REFUSED
        return cluster(
            args.out,
            args.roles,
            args.secret_stubs,
            render=partial(render_placeholders, all_images=args.all_images),
            shared_only=args.shared_only,
            revision=revision,
        )


if __name__ == "__main__":
    sys.exit(main())
