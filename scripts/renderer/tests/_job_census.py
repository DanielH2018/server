"""D5.1a3's Job census: every Job Flux applies is safe to re-run and cannot wedge an apply.

The plan is `docs/target-architecture/deploy/d5-bulk-move.md`, *Rules for every Job Flux
applies (D5.1a3)*. The census reads a publish bundle's `cluster/` tree: each directory is one
Kustomization's path, so the census's scope, the Jobs in a flux-owned tag's render and in the
non-tag Kustomizations, is every directory there. It has seven rows, each a predicate over one
Job and its directory:

- `kind`: the Job's `homelab/job-kind` annotation is a kind its directory admits (`ADMITTED`):
  `snapshot` in `<tag>-snapshot/`, `prepull` in `<tag>-prepull/`, `soak` in `soak/`, `precheck`
  in `<tag>-precheck/`, `verify` and `mutation` in `<tag>-verify/`, and none elsewhere. Only
  the annotation tells a verify Job from a mutation Job, and a Job without it fails;
- `ttl`: no `spec.ttlSecondsAfterFinished`, because Flux re-creates a Job its TTL deleted;
- `name`: the name ends in `-<job_spec_digest8>`, because a Job's pod template is immutable,
  and the spec holds no `${...}` placeholder, which Flux would substitute after the digest.
  Each ConfigMap the Job mounts ends its name in `-<configmap_digest8>`, so the spec digest
  covers the script and a script fix renames the Job;
- `backoff`: `spec.backoffLimit` is set, to 1 for a snapshot Job and 0 for a prepull, soak,
  precheck or verify Job. A mutation Job states its own value;
- `rerun-safe`: a mutation Job's `homelab/rerun-safe` annotation names a test, as
  `<path>::<function>`, that exists in the tree;
- `delivery`: every container runs `python3 <mount>/<key>.py`, where `<mount>` mounts a
  ConfigMap whole and `<key>` is one of its keys. No other command, `-c` or `-m` included, so
  every script the Job runs is one the `helper` row reads, or a prepull Job's shell no-op;
- `helper`: no Python script in a ConfigMap the Job mounts opens a connection except through
  `flux_job_connect.py`, which must sit beside it as a byte-identical copy. A script imports
  only the modules in `ALLOWED_IMPORTS`, none of which opens a connection, and uses no name in
  `FORBIDDEN`. It reads from `flux_job_connect` only the names in the helper's `__all__`, and
  reaches `urllib` only through a submodule `ALLOWED_IMPORTS` holds, so it cannot walk through
  an allowed module to `urllib.request`. It uses neither module as a value and reads no dunder,
  as an attribute or a string, so `getattr`, `vars` and `__globals__` cannot reach around those
  two rules. The row guards against a mistake, not an adversary: a name spelled out at run time
  passes it. A `SIBLINGS` copy is byte-identical.

A CronJob's gate Job (`cronjob_gate.is_gate_copy`) skips `delivery` and `helper`.

The test module is `test_renderer_job_census.py`.
"""

import ast
import json
import re
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path, PurePosixPath

from lib import yaml_fast
from lib.repo_paths import REPO
from renderer.renderer_lib.bundle import CLUSTER_DIR
from renderer.renderer_lib.cronjob_gate import is_gate_copy
from renderer.renderer_lib.digest8 import configmap_digest8, job_spec_digest8
from renderer.renderer_lib import ordering, prepull

# DECIDED: a Job declares its kind in this annotation on the Job's own `metadata`, which the
# plan does not name. Verify and mutation Jobs share `<tag>-verify`, so nothing else tells a
# mutation Job, which must carry `RERUN_SAFE`, from a verify Job, which must not retry. A Job
# without it fails closed. Both annotations sit on the Job, not its pod template, so adding
# one moves no spec digest and renames no Job.
JOB_KIND = "homelab/job-kind"
RERUN_SAFE = "homelab/rerun-safe"
SOAK = "soak"
# The `backoffLimit` each kind must set. A mutation Job is absent: it states its own value.
BACKOFF = {"snapshot": 1, "prepull": 0, SOAK: 0, "precheck": 0, "verify": 0}
ADMITTED = {
    ordering.SNAPSHOT_SUFFIX: frozenset({"snapshot"}),
    ordering.PREPULL_SUFFIX: frozenset({"prepull"}),
    ordering.PRECHECK_SUFFIX: frozenset({"precheck"}),
    ordering.VERIFY_SUFFIX: frozenset({"verify", "mutation"}),
}
HELPER = REPO / "ansible/roles/k8s/manifests/files/flux_job_connect.py"
# Modules a script may import, each carried beside it (`IMPORTS`) as a byte-identical copy.
CHANGED_SET = REPO / "scripts/deploy_tools/changed_set.py"
SIBLINGS = {HELPER.name: HELPER, CHANGED_SET.name: CHANGED_SET}
# DECIDED: an allowlist of imports, not a denylist of connecting calls. A denylist missed
# `urlretrieve`, `asyncio.open_connection`, `ftplib` and `importlib` in review. No module
# here opens a connection; a script builds its `Request` from `flux_job_connect`, so
# `urllib.request` is not here either. A module joins this list only if it cannot connect.
#
# The census catches a Job script that opens a connection without `flux_job_connect.open_url`
# by accident, which is the shape an author writes when they forget the helper. It does not
# stop a script built to dodge it through runtime indirection, such as `getattr`, `vars()` or
# `globals()` subscripted with a string like `'urllib'`, or a frame's globals. The helper
# exists so that a brand-new pod's first connection retries for 60 seconds while kube-router
# learns the pod's address (`docs/target-architecture/deploy/d5-bulk-move.md`, "Rules for
# every Job Flux applies (D5.1a3)", "A first connection retries"). That is a reliability
# rule, so a deliberate bypass is outside it.
# A module reached as an attribute of an allowed module, such as `dataclasses.inspect`, is
# outside the row for the same reason: the row does not resolve an allowed module's re-export
# chains. The literal names a connection needs on any such route are refused instead:
# `socket` as an attribute, `import_module` and the frame names.
ALLOWED_IMPORTS = frozenset(
    {
        "flux_job_connect",
        "changed_set",
        "argparse",
        "base64",
        "collections",
        "collections.abc",
        "dataclasses",
        "datetime",
        "enum",
        "functools",
        "hashlib",
        "hmac",
        "io",
        "itertools",
        "json",
        "math",
        "os",
        "os.path",
        "pathlib",
        "re",
        "string",
        "sys",
        "tarfile",
        "textwrap",
        "time",
        "typing",
        "urllib.error",
        "urllib.parse",
    }
)
# Names a script never uses, as a name, an attribute or a string: a connecting call reached
# through an allowed module's re-exports, any module looked up by name
# (`sys.modules['socket']`), a process that could run `curl` (`os.system`), and code built at
# run time, which the census cannot read.
FORBIDDEN = frozenset(
    {
        "urlopen",
        "urlretrieve",
        "build_opener",
        "OpenerDirector",
        "URLopener",
        "FancyURLopener",
        "HTTPConnection",
        "HTTPSConnection",
        "create_connection",
        "system",
        "popen",
        "fork",
        "posix_spawn",
        "posix_spawnp",
        "execl",
        "execle",
        "execlp",
        "execlpe",
        "execv",
        "execve",
        "execvp",
        "execvpe",
        "spawnl",
        "spawnle",
        "spawnlp",
        "spawnlpe",
        "spawnv",
        "spawnve",
        "spawnvp",
        "spawnvpe",
        "__import__",
        "import_module",
        "exec",
        "eval",
        "modules",
        "AbstractHTTPHandler",
        "HTTPHandler",
        "HTTPSHandler",
        "_getframe",
        "f_globals",
        "tb_frame",
    }
)
# Packages `ALLOWED_IMPORTS` admits only through a submodule: `urllib`, for `urllib.error`
# and `urllib.parse`. `import urllib.error` binds `urllib`, whose `request` attribute
# connects once anything has imported it, and `urllib.error.urllib` is that same package.
SUBMODULE_ONLY = (
    frozenset(name.split(".")[0] for name in ALLOWED_IMPORTS if "." in name)
    - ALLOWED_IMPORTS
)
# A dunder name, which a script never reads as an attribute or a string: `__main__` aside,
# each one reaches past the names the rules here check.
DUNDER = re.compile(r"__\w+__")
PYTHONS = frozenset({"python", "python3"})
IMPORTS = {k: re.compile(rf"^\s*(?:from|import)\s+{k[:-3]}\b", re.M) for k in SIBLINGS}

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
    return ADMITTED.get(ordering.split_kind(directory)[1], frozenset())


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
    offences = []
    want = job_spec_digest8(job)
    if not job["metadata"]["name"].endswith(f"-{want}"):
        offences.append(f"name does not end in its spec digest -{want}")
    # Flux substitutes `${key}` after the digest is taken, so a cluster-vars change would move
    # the applied spec and keep the name. A substituted value reaches a Job through a Secret.
    # `$${` is Flux's escape for a literal `${`.
    spec = json.dumps(job.get("spec"), sort_keys=True).replace("$$", "")
    if "${" in spec:
        offences.append(
            "spec holds a ${...} placeholder, which Flux substitutes after the name's "
            "digest is taken; pass the value through a Secret"
        )
    for configmap in _configmap_docs(job, docs).values():
        name, digest = configmap["metadata"]["name"], configmap_digest8(configmap)
        if not name.endswith(f"-{digest}"):
            offences.append(
                f"mounts ConfigMap {name}, whose name does not end in its content digest "
                f"-{digest}, so a script fix would not rename the Job"
            )
    return offences


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


def _configmap_docs(job: Mapping, docs: list[dict]) -> dict[str, dict]:
    """Each ConfigMap the Job mounts that its directory holds, by name."""
    mounted = _mounted_configmaps(job)
    return {
        doc["metadata"]["name"]: doc
        for doc in docs
        if doc.get("kind") == "ConfigMap" and doc["metadata"]["name"] in mounted
    }


def _containers(job: Mapping) -> list[dict]:
    pod = ((job.get("spec") or {}).get("template") or {}).get("spec") or {}
    return [*(pod.get("initContainers") or []), *(pod.get("containers") or [])]


def delivery_offence(job: Mapping, directory: str, docs: list[dict]) -> list[str]:
    pod = ((job.get("spec") or {}).get("template") or {}).get("spec") or {}
    volumes = {v.get("name"): v for v in pod.get("volumes") or []}
    configmaps = _configmap_docs(job, docs)
    offences = []
    shell = _annotations(job).get(JOB_KIND) == "prepull"
    for container in _containers(job):
        label = f"container {container.get('name')}"
        command = container.get("command") or []
        if not (shell and command == prepull.COMMAND) and (
            len(command) != 2
            or command[0] not in PYTHONS
            or not str(command[1]).endswith(".py")
        ):
            offences.append(
                f"{label} runs {command!r}; run python3 <mount>/<script>.py from a "
                "mounted ConfigMap"
            )
            continue
        script = PurePosixPath(command[1])
        mounts = [
            m
            for m in container.get("volumeMounts") or []
            if m.get("mountPath", "").rstrip("/") == str(script.parent)
            and not m.get("subPath")
        ]
        source = (volumes.get(mounts[0].get("name")) or {}) if mounts else {}
        configmap = source.get("configMap") or {}
        data = (configmaps.get(configmap.get("name")) or {}).get("data") or {}
        if not configmap or configmap.get("items") or script.name not in data:
            offences.append(
                f"{label} runs {script}, which is no key of a ConfigMap mounted whole at "
                f"{script.parent}"
            )
        elif script.suffix == ".sh" and data[script.name] != prepull.SCRIPT:
            offences.append(f"{label} runs {script}, which is not prepull.SCRIPT")
    return offences


def _helper_exports() -> frozenset[str]:
    """The names in `flux_job_connect`'s `__all__`, the only ones a script may read from it."""
    for node in ast.parse(HELPER.read_text()).body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "__all__" for t in node.targets
        ):
            return frozenset(ast.literal_eval(node.value))
    raise AssertionError(f"{HELPER} declares no __all__")


def script_offence(name: str, text: str) -> list[str]:
    """Each way the Python `text` of the script `name` could connect without the helper."""
    try:
        tree = ast.parse(text)
    except SyntaxError as err:
        return [f"{name} does not parse ({err.msg}), so the census cannot read it"]
    exports = _helper_exports()
    # The local names `import flux_job_connect [as f]` binds to the helper module.
    helpers = {
        a.asname or a.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for a in node.names
        if a.name == "flux_job_connect"
    }
    # Each `Name` node that is the object of an attribute read, `f` in `f.open_url`.
    read_from = {
        id(node.value) for node in ast.walk(tree) if isinstance(node, ast.Attribute)
    }
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found |= {
                f"import {a.name}" for a in node.names if a.name not in ALLOWED_IMPORTS
            }
        elif isinstance(node, ast.ImportFrom):
            if node.level or node.module not in ALLOWED_IMPORTS:
                found.add(f"from {'.' * node.level}{node.module or ''} import")
            found |= {a.name for a in node.names if a.name in FORBIDDEN}
            if node.module == "flux_job_connect":
                found |= {
                    f"flux_job_connect.{a.name}"
                    for a in node.names
                    if a.name not in exports
                }
        elif isinstance(node, ast.Name):
            if node.id in FORBIDDEN:
                found.add(node.id)
            # The helper module and `urllib` are read only as `f.<export>` and
            # `urllib.<submodule>`. Bound to another name or passed to `getattr` or `vars`,
            # either one reaches what the attribute checks below refuse.
            if (node.id in helpers or node.id in SUBMODULE_ONLY) and id(
                node
            ) not in read_from:
                found.add(f"{node.id} as a value")
        elif isinstance(node, ast.Attribute):
            if node.attr in FORBIDDEN:
                found.add(node.attr)
            # A dunder reaches a function's or a module's namespace: `open_url.__globals__`
            # holds every module the helper imports.
            if DUNDER.fullmatch(node.attr):
                found.add(f".{node.attr}")
            if node.attr in SUBMODULE_ONLY:
                found.add(f".{node.attr}")
            # `socket` is read as a name only when imported, which `ALLOWED_IMPORTS` refuses.
            # As an attribute it is a module reached through some object, such as
            # `x.request.socket`. The string `'socket'` stays allowed.
            if node.attr == "socket":
                found.add(".socket")
            if isinstance(node.value, ast.Name):
                root = node.value.id
                if root in helpers and node.attr not in exports:
                    found.add(f"{root}.{node.attr}")
                if (
                    root in SUBMODULE_ONLY
                    and f"{root}.{node.attr}" not in ALLOWED_IMPORTS
                ):
                    found.add(f"{root}.{node.attr}")
        elif isinstance(node, ast.Constant) and (
            node.value in FORBIDDEN
            or (
                isinstance(node.value, str)
                and DUNDER.fullmatch(node.value)
                and node.value != "__main__"
            )
        ):
            found.add(repr(node.value))
    return [
        f"{name} uses {use}; import only ALLOWED_IMPORTS and connect with "
        "flux_job_connect.open_url"
        for use in sorted(found)
    ]


def helper_offence(job: Mapping, directory: str, docs: list[dict]) -> list[str]:
    configmaps = {
        doc["metadata"]["name"]: doc for doc in docs if doc.get("kind") == "ConfigMap"
    }
    offences = []
    for name in sorted(_mounted_configmaps(job)):
        if name not in configmaps:
            offences.append(
                f"mounts ConfigMap {name}, which {directory}/ does not hold, so the census "
                "cannot read its scripts"
            )
            continue
        data = configmaps[name].get("data") or {}
        for key, text in sorted(data.items()):
            if not key.endswith(".py"):
                continue
            offences += [
                f"{name}/{key} imports {module}, which {name} lacks"
                for module, imports in IMPORTS.items()
                if module not in data and imports.search(text)
            ]
            if key in SIBLINGS:
                if text.rstrip("\n") != SIBLINGS[key].read_text().rstrip("\n"):
                    offences.append(f"{name}/{key} differs from the tracked {key}")
                if key == HELPER.name:
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
    "delivery": delivery_offence,
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
        if row not in ("delivery", "helper") or not is_gate_copy(job, directory, bundle)
        for line in offence(job, directory, docs)
    ]
