"""The workloads a role's vendored files declare, for `probe.py health` and `probe.py shed-set`.

H1's plumbing roles keep what Flux applies besides their templates in two places (H1,
contracts.md *Vendored files*): upstream manifests byte for byte under `files/upstream/`, and
the kustomize patches the role's generated `kustomization.yaml` lists under `files/patches/`.
Neither is a Jinja template, so `rendered_docs()` and the template render in health.py never
read them. Without this module, `metallb` and `longhorn` would resolve no workload and pass the
health gate with nothing checked (H1.1d).

Every function takes the role's directory, so a test hands it a fixture role and patches
nothing.
"""

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))

VENDORED_SUBDIRS = ("upstream", "patches")


def vendored_files(role_dir):
    """Every `*.yaml`/`*.yml` under `role_dir`'s vendored subdirectories, upstream first."""
    return [
        path
        for sub in VENDORED_SUBDIRS
        for path in sorted((role_dir / "files" / sub).glob("*.y*ml"))
    ]


def vendored_targets(role_dir, default_namespace, kinds):
    """{(namespace, kind, name)} for every doc of `kinds` the role's upstream files declare.

    An upstream object takes its own namespace, or `default_namespace` when it names none. A
    patch only changes an object something else declares, so it adds no target. It must name
    a (kind, name) an upstream file declares, because kustomize refuses a build whose patch
    matches nothing, and a gate that ignored it would pass a role Flux cannot apply.

    Raises:
        RuntimeError: a vendored file does not parse, or a patch names a (kind, name) no
            upstream file declares. This fails closed, as a template that does not render
            does.
    """
    # Deferred, as health.py defers its render imports: most probe.py subcommands never
    # resolve a role, and PyYAML would otherwise load on every one of them.
    from lib import yaml_fast

    found = {}
    patched = []
    for path in vendored_files(role_dir):
        try:
            docs = list(yaml_fast.safe_load_all(path.read_text()))
        except yaml_fast.yaml.YAMLError as exc:
            raise RuntimeError(
                f"{role_dir.name}/{path.name} does not parse: {exc}"
            ) from exc
        for doc in docs:
            if not isinstance(doc, dict) or doc.get("kind") not in kinds:
                continue
            meta = doc.get("metadata") or {}
            key = (doc["kind"], meta.get("name"))
            if path.parent.name == "patches":
                patched.append((path.name, key))
            else:
                found[key] = meta.get("namespace") or default_namespace
    for name, (kind, workload) in patched:
        if (kind, workload) not in found:
            raise RuntimeError(
                f"{role_dir.name}/files/patches/{name} patches {kind} {workload}, "
                "which no file under files/upstream/ declares"
            )
    return {(namespace, kind, name) for (kind, name), namespace in found.items()}


def _only_crds(files):
    """True when every document in `files` is a CustomResourceDefinition."""
    from lib import yaml_fast

    docs = [
        doc
        for path in files
        for doc in yaml_fast.safe_load_all(path.read_text())
        if isinstance(doc, dict)
    ]
    return bool(docs) and all(d.get("kind") == "CustomResourceDefinition" for d in docs)


def require_a_vendored_workload(role_dir, workload_targets):
    """Raise when `role_dir` ships vendored files and `workload_targets` is empty.

    A vendored role exists to run an upstream workload, such as MetalLB's speaker or
    Longhorn's manager, so resolving none means the parse or the kind filter missed it. The
    pass a workload-free role gets would then gate nothing on the roles whose loss stops the
    cluster. The message carries none of `detach_notify.NOT_APPLICABLE_MARKERS`, so the deploy
    notifier reports a failure rather than a skip.

    A role whose vendored files declare only CustomResourceDefinitions, such as
    `traefik-crds`, runs no workload by design, so it passes with nothing to check.

    Raises:
        RuntimeError: the role ships vendored files, resolves no workload, and declares
            something other than a CRD.
    """
    if workload_targets:
        return
    files = vendored_files(role_dir)
    if files and not _only_crds(files):
        raise RuntimeError(
            f"{role_dir.name} ships vendored files but they resolve no Deployment, "
            "DaemonSet or StatefulSet"
        )
