#!/usr/bin/env python3
"""YAML parsing for rendered k8s manifests, and the PVC names they declare and reference.

The module holds the strict loaders, the ``lookup()`` stub and the PersistentVolumeClaim
helpers. The validator cross-references declared and referenced claim names across the whole tree,
which is why ``find_pvc_names`` and ``find_claim_name_refs`` are separate functions rather
than one walk.
"""

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))

import base64
import json
from pathlib import Path
from typing import Any, cast

import yaml
import yaml.constructor

from lib.render_guard import make_env

__all__ = [
    "AppTagLoader",
    "StrictKeyLoader",
    "find_claim_name_refs",
    "find_pvc_names",
    "make_lookup",
    "parse_docs",
    "to_json_stub",
    "yaml_error",
]


def yaml_error(rendered: str) -> str | None:
    """Return an error string if ``rendered`` is not parseable YAML, else None.

    Also parses YAML *embedded* in ConfigMap/Secret values. The manifest wrapping Traefik's
    static config and Authelia's configuration.yml is trivially valid whatever those blobs
    contain — they are opaque block scalars to the outer document — so checking only the
    outer YAML would miss precisely the indentation bugs that matter most here.
    """
    try:
        docs = list(yaml.load_all(rendered, Loader=StrictKeyLoader))
    except yaml.YAMLError as exc:
        return f"invalid YAML: {exc}"

    for doc in docs:
        if not isinstance(doc, dict) or doc.get("kind") not in ("ConfigMap", "Secret"):
            continue
        for field in ("data", "stringData"):
            for key, value in (doc.get(field) or {}).items():
                if not key.endswith((".yml", ".yaml")) or not isinstance(value, str):
                    continue
                try:
                    yaml.load(value, Loader=AppTagLoader)
                except yaml.YAMLError as exc:
                    return f"invalid embedded YAML in {field}.{key}: {exc}"
    return None


class StrictKeyLoader(yaml.SafeLoader):
    """SafeLoader that rejects a duplicate mapping key instead of letting the last one win.

    Plain YAML treats a repeated key as an overwrite: the document stays valid, kubectl
    applies it, and only the final value takes effect. That is how homepage ended up with
    both `automountServiceAccountToken: true` (needed by its kubernetes widget) and a
    `false` inherited from the estate-wide 02e9cfac sweep in one pod spec — the widget would
    have gone dark with every check green. A rebase or a merge that lands two edits in the
    same block is the way this arrives, so it needs catching at render time, not by reading.
    """

    def construct_mapping(self, node, deep=False):
        """Construct `node` as a mapping, raising on a duplicate key instead of overwriting it.

        Raises:
            yaml.constructor.ConstructorError: `node` has two entries for the same key.
        """
        seen = set()
        for key_node, _ in node.value:
            key = self.construct_object(key_node, deep=deep)
            if key in seen:
                raise yaml.constructor.ConstructorError(
                    "while constructing a mapping",
                    node.start_mark,
                    f"duplicate key {key!r} — the later value silently wins",
                    key_node.start_mark,
                )
            seen.add(key)
        return super().construct_mapping(node, deep=deep)


class AppTagLoader(StrictKeyLoader):
    """SafeLoader that tolerates application-defined tags in EMBEDDED config.

    home-assistant's configuration.yaml uses ``!include``/``!secret``. The structure is still
    fully parsed — only the tag resolves to a placeholder. HA's own files get deep validation
    from validate-ha-config; this guard just proves the ConfigMap embeds well-formed YAML.
    """


AppTagLoader.add_multi_constructor("!", lambda loader, suffix, node: f"<{suffix}>")


def _from_json(value) -> object:
    """Ansible's ``from_json`` for looked-up templates.

    home-assistant's secrets.yaml.j2 parses a SOPS-stored service-account blob with it. Under
    this guard the value is a StubUndefined, not JSON — return an empty dict so attribute
    access on the result stubs out like any other undefined instead of aborting the render.
    """
    try:
        return json.loads(str(value))
    except ValueError, TypeError:
        return {}


def to_json_stub(value) -> str:
    """Ansible's ``to_json`` for looked-up templates.

    ``default=str`` so a StubUndefined serializes as its placeholder instead of aborting the render.
    """
    return json.dumps(value, default=str)


def make_lookup(ctx: dict):
    """Minimal stand-in for Ansible's ``lookup``, supporting the ``file`` and ``template`` plugins.

    A ConfigMap that embeds a config file the Docker role already owns reads it with a lookup
    rather than keeping a second copy. Stubbing the result would let a malformed embed through,
    so the real file is read here — the whole point of this guard is that what renders in CI is
    what a deploy renders.

    ``template`` needs the render context, hence the closure: livesync's CouchDB local.ini is a
    Jinja template on the Docker side, and reading it with ``file`` would leave any variable
    added to it later embedded as literal ``{{ ... }}`` in the ConfigMap.
    """

    def lookup(kind: str, *args: str, **kwargs) -> str:
        """Resolve one `lookup()` call, supporting `file`, `pipe` (base64 only) and `template`.

        Args:
            kind: The lookup plugin name.
            args: The plugin's positional arguments; `args[0]` is always a path or command.
            kwargs: Plugin options. Only `file`'s `rstrip` is recognised (default `True`,
                matching Ansible's own `file` lookup) — `checksum-annotation.yml.j2`'s
                path-mode call passes `rstrip=False` so the bytes it hashes match a `copy`
                task's checksum of the same file, which is not stripped either.

        Raises:
            ValueError: `kind` is not one of the three supported plugins, or `kind == "pipe"`
                names something other than `base64 -w0 <path>`.
        """
        path = Path(args[0])
        if kind == "file":
            text = path.read_text()
            return text.rstrip("\n") if kwargs.get("rstrip", True) else text
        if kind == "pipe":
            # Only the binary-embed idiom, done hermetically in Python rather than by
            # running a shell: lookup('file') utf-8-decodes and would mangle binary, so
            # templates embedding images (homepage's icons ConfigMap) pipe base64 instead.
            cmd = args[0].split()
            if cmd[:2] == ["base64", "-w0"] and len(cmd) == 3:
                return base64.b64encode(Path(cmd[2]).read_bytes()).decode()
            raise ValueError(
                f"lookup('pipe') is only supported for 'base64 -w0 <path>', got {args[0]!r}"
            )
        if kind == "template":
            env = make_env([path.parent])
            # jinja2 leaves `Environment.globals` unannotated, so its type is inferred from
            # the default namespace's values and rejects any other callable.
            cast(dict[str, Any], env.globals)["lookup"] = lookup
            env.filters["from_json"] = _from_json
            env.filters["to_json"] = to_json_stub
            return env.get_template(path.name).render(ctx).rstrip("\n")
        raise ValueError(
            "lib.k8s_yaml implements lookup('file'), lookup('pipe') and lookup('template'), "
            f"got {kind!r}"
        )

    return lookup


def parse_docs(rendered: str) -> list:
    """Parse a rendered manifest into its YAML documents, the same way yaml_error does.

    Only called after yaml_error has already confirmed the render is valid YAML — a raise here would
    be a bug in this function, not in the manifest.
    """
    return list(yaml.load_all(rendered, Loader=StrictKeyLoader))


def find_pvc_names(doc) -> list[str]:
    """Return the name of the PVC `doc` declares, if it is one.

    A rendered manifest is one object per document, so this is a direct check, not a
    recursive search.
    """
    if isinstance(doc, dict) and doc.get("kind") == "PersistentVolumeClaim":
        name = (doc.get("metadata") or {}).get("name")
        if isinstance(name, str):
            return [name]
    return []


def find_claim_name_refs(node) -> list[str]:
    """Every `persistentVolumeClaim.claimName` in a parsed manifest, wherever it is nested.

    A Deployment/DaemonSet has it at spec.template.spec.volumes[]; a CronJob one level deeper
    through spec.jobTemplate; a bare Pod at spec.volumes[] directly. Walked generically instead
    of hardcoded per-kind paths, so a shape this wasn't written for (a future StatefulSet, say)
    is still covered rather than silently skipped.
    """
    refs: list[str] = []
    if isinstance(node, dict):
        pvc = node.get("persistentVolumeClaim")
        if isinstance(pvc, dict) and isinstance(pvc.get("claimName"), str):
            refs.append(pvc["claimName"])
        for value in node.values():
            refs.extend(find_claim_name_refs(value))
    elif isinstance(node, list):
        for item in node:
            refs.extend(find_claim_name_refs(item))
    return refs
