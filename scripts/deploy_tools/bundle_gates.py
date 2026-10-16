"""`promote.py check-bundle`'s three publish gates: source, host and no-stub.

They run beside the census (`bundle_census.py`) in the render job, before gitleaks and before
any push, so a refusal publishes nothing and `prod` stays where it was. Contracts C1 and C3
hold the rules, and docs/target-architecture/deploy/d2-first-leaf.md, *Publication and
verification*, the design:

- `source`: each Kustomization document in `flux/<name>.yaml` is judged against its own
  `spec.path` directory, so a tag's second document, `<tag>-deferred`, is judged against
  `cluster/<tag>-deferred/` (D4.3, the per-file rule). A document with no `postBuild` applies
  a directory that holds no `${K}` whose K is a key name of `ansible/vars/secrets.yml`, read
  as Flux reads it, each `$$` paired first. A document with `postBuild` names in
  `substituteFrom` only a Secret that a `secretGenerator` in the bundle makes. The gate fails
  a published name whose file or directories it could not examine, so it never passes over
  nothing.
- `host`: every `Host(...)` in an IngressRoute ends in `.<domain>`, the literal that
  `ansible/inventory/group_vars/all.yml` defines. While bento-pdf is published, its `.local.`
  route is the gate's named member, and a gate that finds no Host rule there fails.
- `no-stub`: no file under the bundle holds an R1 stub value, `stub-<key>` for a key name of
  `secrets.yml`, with each Secret's `data` decoded too.

Each gate returns one line per problem, opening with the gate's name.

Run: uv run python scripts/deploy_tools/promote.py check-bundle out
"""

import re
import sys as _sys
from collections.abc import Iterable
from pathlib import Path
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))  # scripts/
import yaml

from lib import yaml_fast
from lib.repo_paths import ALL_VARS, ANSIBLE, FILTER_PLUGINS
from renderer.renderer_lib.bundle import (
    CLUSTER_DIR,
    FLUX_DIR,
    KUSTOMIZATION,
    SHARED_KINDS,
)
from renderer.renderer_lib.placeholders import references
from renderer.renderer_lib.shadow import stub_tokens

# Ansible loads a filter plugin by file path; a script reaches it through this insert.
if str(FILTER_PLUGINS) not in _sys.path:
    _sys.path.insert(0, str(FILTER_PLUGINS))

from secret_stubs import homelab_secret_stub

SECRETS = ANSIBLE / "vars" / "secrets.yml"

# The host gate's named member: bento-pdf's `.local.` route, as (tag, namespace, name). The
# gate asserts it only while the tag is published, so a handback does not fail it.
NAMED_ROUTE = ("bento-pdf", "homelab", "bento-pdf")

_HOST = re.compile(r"(?<![A-Za-z])Host\(([^)]*)\)")
_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
# A Kustomization's `spec.path` as the renderer writes it: one directory under `cluster/`. A
# segment cannot start with `.`, so no path leaves the bundle.
_PATH = re.compile(rf"\./{CLUSTER_DIR}/([a-z0-9][a-z0-9.-]*)")


def secret_keys(secrets: Path = SECRETS) -> frozenset[str]:
    """Every top-level key name of the ciphertext file. SOPS leaves the names in plaintext.

    Raises:
        ValueError: the file is missing, does not parse, or names no key, so no gate that
            reads the set can pass over nothing.
    """
    try:
        data = yaml_fast.safe_load(secrets.read_text())
    except (OSError, yaml.YAMLError) as err:
        raise ValueError(f"{secrets}: {err}") from err
    keys = (
        frozenset(str(k) for k in data if k != "sops") if isinstance(data, dict) else ()
    )
    if not keys:
        raise ValueError(f"{secrets} names no key")
    return frozenset(keys)


def inventory_domain(all_vars: Path = ALL_VARS) -> str:
    """The literal `domain` that `all_vars` defines.

    Raises:
        ValueError: the file does not parse, or `domain` is missing or is not a literal.
    """
    try:
        data = yaml_fast.safe_load(all_vars.read_text())
    except (OSError, yaml.YAMLError) as err:
        raise ValueError(f"{all_vars}: {err}") from err
    value = data.get("domain") if isinstance(data, dict) else None
    if not isinstance(value, str) or not value or "{" in value or "$" in value:
        raise ValueError(f"{all_vars} defines no literal domain, got {value!r}")
    return value


def _files(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*") if p.is_file()) if root.is_dir() else []


def _docs(path: Path) -> list[dict]:
    """The mapping documents of `path`; a file that is missing or does not parse yields none.

    The census reports either, so a gate need not repeat it.
    """
    try:
        docs = yaml_fast.safe_load_all(path.read_text())
        return [d for d in docs if isinstance(d, dict)]
    except OSError, UnicodeDecodeError, yaml.YAMLError:
        return []


def _generated_secrets(out: Path) -> set[str]:
    """The name of every Secret a `secretGenerator` under the bundle's `cluster/` makes."""
    return {
        str(entry.get("name"))
        for path in (out / CLUSTER_DIR).glob(f"*/{KUSTOMIZATION}")
        for doc in _docs(path)
        for entry in doc.get("secretGenerator") or []
        if isinstance(entry, dict) and entry.get("name")
    }


def source(
    out: Path, published: Iterable[str], keys: frozenset[str], kinds=SHARED_KINDS
) -> list[str]:
    """The source gate: what substitutes is what each Kustomization document says substitutes."""
    found, generated = [], _generated_secrets(out)
    for name in sorted({*published, *kinds}):
        file = f"{FLUX_DIR}/{name}.yaml"
        docs = [d for d in _docs(out / file) if d.get("kind") == "Kustomization"]
        if not docs:
            found.append(
                f"source: could not examine {name}: it needs {file} and "
                f"{CLUSTER_DIR}/{name}/"
            )
        for doc in docs:
            found += _document_problems(out, file, doc, keys, generated)
    return found


def _document_problems(
    out: Path, file: str, doc: dict, keys: frozenset[str], generated: set[str]
) -> list[str]:
    """The source gate's problems with one Kustomization document of `file`."""
    spec = doc.get("spec")
    spec = spec if isinstance(spec, dict) else {}
    kustomization = (doc.get("metadata") or {}).get("name")
    where = f"the Kustomization {kustomization} in {file}"
    path = _PATH.fullmatch(str(spec.get("path", "")))
    directory = out / CLUSTER_DIR / path.group(1) if path else None
    if directory is None or not directory.is_dir():
        return [
            f"source: could not examine {where}: it applies {spec.get('path')!r}, "
            f"which is no directory under {CLUSTER_DIR}/"
        ]
    post_build = spec.get("postBuild")
    if post_build is None:
        return [
            f"source: {held.relative_to(out).as_posix()} holds ${{{key.group()}}}, a key "
            f"of secrets.yml, and {where} has no postBuild"
            for held in _files(directory)
            for body in references(held.read_text(errors="replace"))
            if (key := _KEY.match(body)) and key.group() in keys
        ]
    found = []
    for ref in (post_build or {}).get("substituteFrom") or [{}]:
        ref = ref if isinstance(ref, dict) else {}
        if ref.get("kind") != "Secret" or ref.get("name") not in generated:
            found.append(
                f"source: {where} substitutes from {ref.get('kind')}/{ref.get('name')}, "
                "which no secretGenerator in the bundle makes"
            )
    return found


def host(out: Path, published: Iterable[str], domain: str) -> list[str]:
    """The host gate: every Host rule in an IngressRoute ends in `.<domain>`."""
    found, routes = [], {}
    for path in _files(out / CLUSTER_DIR):
        for doc in _docs(path):
            if doc.get("kind") != "IngressRoute":
                continue
            meta = doc.get("metadata") or {}
            where = f"IngressRoute/{meta.get('namespace', '')}/{meta.get('name')}"
            hosts = [
                h.strip().strip("`\"'")
                for route in (doc.get("spec") or {}).get("routes") or []
                if isinstance(route, dict)
                for args in _HOST.findall(str(route.get("match", "")))
                for h in args.split(",")
            ]
            routes[(meta.get("namespace"), meta.get("name"))] = hosts
            found += [
                f"host: {where} in {path.relative_to(out).as_posix()} routes {h!r}, "
                f"which does not end in .{domain}"
                for h in hosts
                if not h.endswith(f".{domain}")
            ]
    tag, namespace, name = NAMED_ROUTE
    hosts = routes.get((namespace, name), [])
    if tag in set(published) and not any(h.endswith(f".local.{domain}") for h in hosts):
        found.append(
            f"host: IngressRoute/{namespace}/{name}, the gate's named member while {tag} "
            f"is published, holds no Host rule ending in .local.{domain}"
        )
    return found


def no_stub(out: Path, keys: frozenset[str]) -> list[str]:
    """The no-stub gate: no file under `out` holds an R1 stub value."""
    stubs = {homelab_secret_stub(key) for key in keys}
    found = []
    for path in _files(out):
        held = sorted(set(stub_tokens(path.read_bytes())) & stubs)
        if held:
            found.append(
                f"no-stub: {path.relative_to(out).as_posix()} holds the stub value "
                f"{', '.join(held)}"
            )
    return found


def gates(
    out: Path,
    published: Iterable[str],
    secrets: Path = SECRETS,
    all_vars: Path = ALL_VARS,
    kinds=SHARED_KINDS,
) -> list[str]:
    """Every problem the three gates find in the bundle at `out`, one line each."""
    published = list(published)
    try:
        keys, domain = secret_keys(secrets), inventory_domain(all_vars)
    except ValueError as err:
        return [f"gates: {err}"]
    return (
        source(out, published, keys, kinds)
        + host(out, published, domain)
        + no_stub(out, keys)
    )
