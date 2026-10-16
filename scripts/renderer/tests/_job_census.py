"""D5.1a3's Job census: every Job Flux applies is safe to re-run and cannot wedge an apply.

The plan is `docs/target-architecture/deploy/d5-bulk-move.md`, *Rules for every Job Flux
applies (D5.1a3)*. The census reads a publish bundle's `cluster/` tree: each directory is one
Kustomization's path, so the census's scope, the Jobs in a flux-owned tag's render and in the
non-tag Kustomizations, is every directory there. It has six rows, each a predicate over one
Job and its directory:

- `kind`: the Job's `homelab/job-kind` annotation is a kind its directory admits.
  `<tag>-snapshot/` admits `snapshot`, `soak/` admits `soak`, and `<tag>-verify/` admits
  `verify` and `mutation`. Any other directory admits no Job. Verify and mutation Jobs share
  a directory, so only the annotation tells them apart, and a Job without it fails;
- `ttl`: no `spec.ttlSecondsAfterFinished`, because Flux re-creates a Job its TTL deleted;
- `name`: the name ends in `-<job_spec_digest8>`, because a Job's pod template is immutable;
- `backoff`: `spec.backoffLimit` is set, to 1 for a snapshot Job and 0 for a soak or verify
  Job. A mutation Job states its own value;
- `rerun-safe`: a mutation Job's `homelab/rerun-safe` annotation names a test, as
  `<path>::<function>`, that exists in the tree;
- `helper`: no Python script in a ConfigMap the Job mounts opens a connection except through
  `flux_job_connect.py`, which must sit beside it as a byte-identical copy.

The test module is `test_renderer_job_census.py`.
"""

import ast
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path

from lib import yaml_fast
from lib.repo_paths import REPO
from renderer.renderer_lib.bundle import CLUSTER_DIR, job_spec_digest8
from renderer.renderer_lib.ordering import SNAPSHOT_SUFFIX, VERIFY_SUFFIX, split_kind

JOB_KIND = "homelab/job-kind"
RERUN_SAFE = "homelab/rerun-safe"
SOAK = "soak"
# The `backoffLimit` each kind must set. A mutation Job is absent: it states its own value.
BACKOFF = {"snapshot": 1, SOAK: 0, "verify": 0}
HELPER = REPO / "ansible/roles/k8s/manifests/files/flux_job_connect.py"
# Each name whose use in a script opens a connection without the helper: a call, an
# attribute or a `from` import of it.
CONNECTS = frozenset(
    {
        "urlopen",
        "build_opener",
        "OpenerDirector",
        "HTTPConnection",
        "HTTPSConnection",
        "create_connection",
    }
)
# Modules a Job script never imports: each opens a connection below `urllib`.
CONNECTING_MODULES = frozenset({"socket", "http.client"})

# A bundle's directories, each name mapped to its documents.
Bundle = Mapping[str, list[dict]]


def load(out: Path) -> dict[str, list[dict]]:
    """Each directory under `out/cluster/`, mapped to the documents of its YAML files."""
    bundle = {}
    for directory in sorted(p for p in (out / CLUSTER_DIR).iterdir() if p.is_dir()):
        bundle[directory.name] = [
            doc
            for path in sorted(directory.glob("*.yaml"))
            for doc in yaml_fast.safe_load_all(path.read_text())
            if isinstance(doc, dict)
        ]
    return bundle


def admitted(directory: str) -> frozenset[str]:
    """The Job kinds a Kustomization directory may hold."""
    if directory == SOAK:
        return frozenset({SOAK})
    suffix = split_kind(directory)[1]
    if suffix == SNAPSHOT_SUFFIX:
        return frozenset({"snapshot"})
    if suffix == VERIFY_SUFFIX:
        return frozenset({"verify", "mutation"})
    return frozenset()


def _annotations(doc: Mapping) -> Mapping:
    return (doc.get("metadata") or {}).get("annotations") or {}


def kind_offence(job: Mapping, directory: str, docs: list[dict]) -> list[str]:
    kinds = admitted(directory)
    if not kinds:
        return [f"{directory}/ holds no Job kind; move the Job to <tag>-verify"]
    kind = _annotations(job).get(JOB_KIND)
    if kind not in kinds:
        return [f"{JOB_KIND} is {kind!r}, and {directory}/ admits {sorted(kinds)}"]
    return []


def ttl_offence(job: Mapping, directory: str, docs: list[dict]) -> list[str]:
    if "ttlSecondsAfterFinished" in (job.get("spec") or {}):
        return [
            "sets ttlSecondsAfterFinished, so Flux re-runs it once the TTL deletes it"
        ]
    return []


def name_offence(job: Mapping, directory: str, docs: list[dict]) -> list[str]:
    want = job_spec_digest8(job)
    if not job["metadata"]["name"].endswith(f"-{want}"):
        return [f"name does not end in its spec digest -{want}"]
    return []


def backoff_offence(job: Mapping, directory: str, docs: list[dict]) -> list[str]:
    limit = (job.get("spec") or {}).get("backoffLimit")
    if not isinstance(limit, int):
        return ["sets no backoffLimit"]
    want = BACKOFF.get(_annotations(job).get(JOB_KIND))
    if want is not None and limit != want:
        return [f"backoffLimit is {limit}, and its kind takes {want}"]
    return []


def rerun_safe_offence(job: Mapping, directory: str, docs: list[dict]) -> list[str]:
    annotations = _annotations(job)
    if annotations.get(JOB_KIND) != "mutation":
        return []
    test = annotations.get(RERUN_SAFE)
    if not test:
        return [f"is a mutation Job without {RERUN_SAFE}"]
    path, _, function = str(test).partition("::")
    source = REPO / path
    if (
        not function
        or not source.is_file()
        or f"def {function}(" not in source.read_text()
    ):
        return [f"{RERUN_SAFE} names {test!r}, which is no test in the tree"]
    return []


def _mounted_configmaps(job: Mapping) -> set[str]:
    volumes = ((job.get("spec") or {}).get("template") or {}).get("spec", {}).get(
        "volumes"
    ) or []
    names = set()
    for volume in volumes:
        if name := (volume.get("configMap") or {}).get("name"):
            names.add(name)
        for source in (volume.get("projected") or {}).get("sources") or []:
            if name := (source.get("configMap") or {}).get("name"):
                names.add(name)
    return names


def script_offence(name: str, text: str) -> list[str]:
    """Each way the Python `text` of the script `name` connects without the helper."""
    try:
        tree = ast.parse(text)
    except SyntaxError as err:
        return [f"{name} does not parse ({err.msg}), so the census cannot read it"]
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id in CONNECTS:
            found.add(node.id)
        elif isinstance(node, ast.Attribute) and node.attr in CONNECTS:
            found.add(node.attr)
        elif isinstance(node, ast.ImportFrom):
            if node.module in CONNECTING_MODULES:
                found.add(f"from {node.module} import")
            found |= {a.name for a in node.names if a.name in CONNECTS}
        elif isinstance(node, ast.Import):
            found |= {
                f"import {a.name}" for a in node.names if a.name in CONNECTING_MODULES
            }
    return [
        f"{name} uses {use}; open the connection with flux_job_connect.open_url"
        for use in sorted(found)
    ]


def helper_offence(job: Mapping, directory: str, docs: list[dict]) -> list[str]:
    configmaps = {
        doc["metadata"]["name"]: doc for doc in docs if doc.get("kind") == "ConfigMap"
    }
    helper = HELPER.read_text().rstrip("\n")
    offences = []
    for name in sorted(_mounted_configmaps(job)):
        if name not in configmaps:
            offences.append(
                f"mounts ConfigMap {name}, which {directory}/ does not hold, so the census "
                "cannot read its scripts"
            )
            continue
        for key, text in sorted((configmaps[name].get("data") or {}).items()):
            if not key.endswith(".py"):
                continue
            if key == HELPER.name:
                if text.rstrip("\n") != helper:
                    offences.append(
                        f"{name}/{key} differs from the tracked {HELPER.name}"
                    )
                continue
            offences += [f"{name}/{line}" for line in script_offence(key, text)]
    return offences


Offence = Callable[[Mapping, str, list[dict]], list[str]]
ROWS: dict[str, Offence] = {
    "kind": kind_offence,
    "ttl": ttl_offence,
    "name": name_offence,
    "backoff": backoff_offence,
    "rerun-safe": rerun_safe_offence,
    "helper": helper_offence,
}


def jobs(bundle: Bundle) -> Iterator[tuple[str, dict, list[dict]]]:
    """Each Job in `bundle`, with its directory and that directory's documents."""
    for directory, docs in sorted(bundle.items()):
        for doc in docs:
            if doc.get("kind") == "Job":
                yield directory, doc, docs


def problems(bundle: Bundle, rows: Mapping[str, Offence] = ROWS) -> list[str]:
    """One line per row a Job in `bundle` fails, naming the row, the directory and the Job."""
    return [
        f"{row}: {directory}/{job['metadata']['name']}: {line}"
        for directory, job, docs in jobs(bundle)
        for row, offence in rows.items()
        for line in offence(job, directory, docs)
    ]
