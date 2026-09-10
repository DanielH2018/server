#!/usr/bin/env python3
"""Nothing may stage a file into a directory `k8s/manifests` prunes, or claim a reserved name.

`k8s/manifests` renders each role's manifests into `/etc/rancher/k3s/manifests/<service>/` and
its prune task deletes every file in that directory the caller does not name in
`manifests_files`/`manifests_secret_files`. A task that writes its own file there is therefore
deleted at the start of the next deploy and re-rendered a few tasks later — a permanently
`changed` prune item on an otherwise idempotent run, and for a file something else reads later
(`registry-gc.sh` reads `gc-job.yaml` at CRON time) a window in which the file is simply gone.

That is one class with three instances so far, guarded here rather than re-derived a fourth
time: `k8s/volume-claim`'s claim (#1654), n8n's broker-isolation probe (#1668), and registry's
four job manifests (#1669). The fix each time was the same — move the file to a sibling
directory under the manifest root that no role's `manifests_service` claims, the way
`headlamp-netpol`, `media-volume-probe`, `build-*`, `registry-jobs` and `<service>-claims` do.

Those sibling names are a reservation, and #1670 is that nothing enforced it: a future role
with `manifests_service: n8n-netpol` would render into that directory, prune every file it did
not list, and `kubectl apply -f <dir>/` the rest — #1654 reproduced exactly. Hence two
invariants, each with the input it must accept, the input it must reject, and a named census
so a rename cannot empty it and leave an assertion passing on nothing:

1. **No task stages a file into a directory some role's `manifests_service` names**, unless
   that role lists the filename. `state: absent` tasks are exempt: claude-otel deliberately
   removes copies it left behind in its own directory.
2. **No `manifests_service` claims a reserved sibling name** — a literal one already staged in
   the tree, or one matching the parametric shapes (`<x>-claims`, `<x>-netpol`, `<x>-probe`,
   `build-<x>`) that a role generates per service or per image.

Run: uv run pytest ansible/tests/k8s/test_no_role_stages_files_in_a_pruned_manifest_dir.py
"""

import re
from pathlib import Path

import pytest
from _helpers import K8S_ROLES, load_tasks, walk_tasks

MANIFEST_ROOT = "/etc/rancher/k3s/manifests"
MANIFESTS_ROLE = "k8s/manifests"

# The utility roles that legitimately render into a directory named by a variable. `manifests`
# IS the pruning role, and `volume-claim` has its own guard
# (test_volume_claim_pvc_path_collision.py). Both are skipped by the Jinja filter below anyway;
# naming them says so on purpose rather than by accident.
UTILITY_ROLES = frozenset({"manifests", "volume-claim"})

RESERVED_SUFFIXES = ("-claims", "-netpol", "-probe")
RESERVED_PREFIXES = ("build-",)

# Same pattern as test_volume_claim_pvc_path_collision.py, and for the same reason: a path
# segment is either a Jinja expression (which contains spaces, so `\S+` cannot be used) or
# ordinary path characters. Comment prose such as `manifests/<service>/` matches neither and is
# skipped rather than read as a path.
_PATH_RE = re.compile(
    rf"{re.escape(MANIFEST_ROOT)}(?:/(?:\{{\{{[^}}]*\}}\}}|[A-Za-z0-9._*-])+)+"
)

# Tasks files here open a task with `- name:` at some indent; a `block:` child is the same shape
# further in. Splitting on it gives one chunk per task, which is what the `state: absent`
# exemption needs — the raw-text path read cannot otherwise tell a render from a removal.
_TASK_SPLIT_RE = re.compile(r"^[ \t]*- name:", re.MULTILINE)


def _included_role(task: dict) -> str:
    include = task.get("ansible.builtin.include_role") or task.get("include_role") or {}
    return str(include.get("name", "")) if isinstance(include, dict) else ""


def pruned_dirs(roles_dir: Path) -> dict[str, tuple[str, ...]]:
    """`manifests_service` -> the filenames its caller names, for every role in the tree.

    The filenames decide what may legitimately sit in the directory. A `manifests_files` built
    as a Jinja string (freshrss builds its list conditionally) is kept as one string and
    matched by substring below, which over-reports permission rather than under-reports it —
    the alternative is a guard that flags a file the role does list.
    """
    found: dict[str, tuple[str, ...]] = {}
    for tasks_file in sorted(roles_dir.glob("*/tasks/*.yml")):
        for task in walk_tasks(load_tasks(tasks_file)):
            if _included_role(task) != MANIFESTS_ROLE:
                continue
            task_vars = task.get("vars") or {}
            service = str(task_vars.get("manifests_service", "")).strip()
            if not service or "{{" in service:
                continue
            listed: list[str] = []
            for key in ("manifests_files", "manifests_secret_files"):
                value = task_vars.get(key, [])
                listed.extend(value if isinstance(value, list) else [str(value)])
            found[service] = tuple(str(f) for f in listed)
    return found


def staged_files(tasks_file: Path) -> set[tuple[str, str]]:
    """`(directory, filename)` for every manifest-root file a tasks file writes or applies.

    Read from the raw text, so a `template: dest:`, a `file: path:` and the path inside a
    `kubectl apply` command are all covered by one pattern — the render and the apply have to
    move together, and a check reading only `dest:` would pass while the apply still pointed at
    the pruned directory (that is the shape #1669's `gc-job.yaml` had).

    A path with no filename component is a directory task and is skipped. A directory named by
    a Jinja expression is skipped too: only the utility roles write those, and resolving one
    would mean rendering the whole role.
    """
    found: set[tuple[str, str]] = set()
    for chunk in _TASK_SPLIT_RE.split(tasks_file.read_text()):
        if "state: absent" in chunk:
            continue
        for path in _PATH_RE.findall(chunk):
            parts = path[len(MANIFEST_ROOT) + 1 :].split("/")
            if len(parts) != 2 or "{{" in parts[0] or "." not in parts[1]:
                continue
            found.add((parts[0], parts[1]))
    return found


def _is_listed(filename: str, listed: tuple[str, ...]) -> bool:
    """Whether a staged filename is one the caller names.

    Exact membership for an ordinary list entry, substring only for the Jinja-string shape
    (freshrss builds its `manifests_files` conditionally). Substring-matching every entry would
    read a staged `policy.yaml` as listed because `networkpolicy.yaml` contains it — quietly,
    in the false-negative direction.
    """
    return filename in listed or any(
        "{{" in entry and filename in entry for entry in listed
    )


def files_in_pruned_dirs(roles_dir: Path) -> dict[str, list[str]]:
    """Role name -> the `<dir>/<file>` staged paths that the prune deletes on the next deploy."""
    owned = pruned_dirs(roles_dir)
    found: dict[str, list[str]] = {}
    for tasks_file in sorted(roles_dir.glob("*/tasks/*.yml")):
        role = tasks_file.parent.parent.name
        if role in UTILITY_ROLES:
            continue
        for directory, filename in sorted(staged_files(tasks_file)):
            if directory in owned and not _is_listed(filename, owned[directory]):
                found.setdefault(role, []).append(f"{directory}/{filename}")
    return found


def owning_roles(roles_dir: Path) -> dict[str, str]:
    """`manifests_service` -> the role whose tasks declare it."""
    found: dict[str, str] = {}
    for tasks_file in sorted(roles_dir.glob("*/tasks/*.yml")):
        for task in walk_tasks(load_tasks(tasks_file)):
            if _included_role(task) != MANIFESTS_ROLE:
                continue
            service = str((task.get("vars") or {}).get("manifests_service", "")).strip()
            if service and "{{" not in service:
                found[service] = tasks_file.parent.parent.name
    return found


def staged_dirs_by_role(roles_dir: Path) -> dict[str, set[str]]:
    """Sibling directory name -> the roles that write into it."""
    found: dict[str, set[str]] = {}
    for tasks_file in sorted(roles_dir.glob("*/tasks/*.yml")):
        role = tasks_file.parent.parent.name
        for path in _PATH_RE.findall(tasks_file.read_text()):
            head = path[len(MANIFEST_ROOT) + 1 :].split("/")[0]
            if "{{" not in head:
                found.setdefault(head, set()).add(role)
    return found


def reserved_name_claims(roles_dir: Path) -> dict[str, str]:
    """`manifests_service` -> reason, for every service claiming a reserved sibling name.

    A LITERAL reservation is a sibling directory some OTHER role already stages into. A
    PARAMETRIC one is generated per service or per image, so it cannot be enumerated from the
    tree at all — `<service>-claims`, `build-<image>`. The parametric half is what invariant 1
    structurally cannot cover, because the colliding directory need not exist yet.
    """
    owner = owning_roles(roles_dir)
    staged = staged_dirs_by_role(roles_dir)
    found: dict[str, str] = {}
    for service, role in owner.items():
        if service.endswith(RESERVED_SUFFIXES) or service.startswith(RESERVED_PREFIXES):
            found[service] = (
                "matches a reserved sibling-directory shape that another role generates per "
                "service or per image, so its prune would delete the files that role stages"
            )
        elif staged.get(service, set()) - {role}:
            others = sorted(staged[service] - {role})
            found[service] = (
                f"names a sibling directory {others} already stages files into, which its "
                "prune would then delete"
            )
    return found


# ── invariant 1: nothing stages a file in a pruned directory ─────────────────────────────────

_PRUNED_ROLE = """---
- name: Deploy widget to the cluster
  ansible.builtin.include_role:
    name: k8s/manifests
  vars:
    manifests_service: widget
    manifests_files:
      - deployment.yaml
      - service.yaml

- name: Render the widget probe
  ansible.builtin.template:
    src: probe-job.yaml.j2
    dest: {root}/{dir}/probe-job.yaml

- name: Run the widget probe
  ansible.builtin.command:
    cmd: "k3s kubectl apply -f {root}/{dir}/probe-job.yaml"
"""


def _write_role(root: Path, name: str, text: str) -> Path:
    tasks_file = root / name / "tasks" / "main.yml"
    tasks_file.parent.mkdir(parents=True)
    tasks_file.write_text(text)
    return root


def test_probe_staged_in_a_sibling_directory_is_clean(tmp_path):
    root = _write_role(
        tmp_path, "widget", _PRUNED_ROLE.format(root=MANIFEST_ROOT, dir="widget-probe")
    )
    assert files_in_pruned_dirs(root) == {}


def test_probe_staged_in_the_pruned_directory_is_flagged(tmp_path):
    root = _write_role(
        tmp_path, "widget", _PRUNED_ROLE.format(root=MANIFEST_ROOT, dir="widget")
    )
    assert files_in_pruned_dirs(root) == {"widget": ["widget/probe-job.yaml"]}


def test_a_listed_manifest_in_its_own_directory_is_clean(tmp_path):
    text = _PRUNED_ROLE.format(root=MANIFEST_ROOT, dir="widget").replace(
        "probe-job.yaml", "service.yaml"
    )
    assert files_in_pruned_dirs(_write_role(tmp_path, "widget", text)) == {}


def test_a_removal_task_in_the_pruned_directory_is_clean(tmp_path):
    """claude-otel's shape: an explicit cleanup of copies it left behind is not a staging."""
    text = """---
- name: Deploy widget to the cluster
  ansible.builtin.include_role:
    name: k8s/manifests
  vars:
    manifests_service: widget
    manifests_files:
      - deployment.yaml

- name: Remove the retired widget dashboard manifest
  ansible.builtin.file:
    path: {root}/widget/dashboard.yaml
    state: absent
""".format(root=MANIFEST_ROOT)
    assert files_in_pruned_dirs(_write_role(tmp_path, "widget", text)) == {}


def test_the_pre_fix_n8n_probe_is_flagged(tmp_path):
    """#1668 as it was written, so the guard is proven against the real defect it exists for."""
    text = """---
- name: Deploy n8n to the cluster
  ansible.builtin.include_role:
    name: k8s/manifests
  vars:
    manifests_service: n8n
    manifests_files:
      - deployment.yaml
      - ingressroute.yaml

- name: Render the broker-isolation probe
  ansible.builtin.template:
    src: netpol-probe-job.yaml.j2
    dest: {root}/n8n/netpol-probe-job.yaml

- name: Run the broker-isolation probe
  ansible.builtin.command:
    cmd: "k3s kubectl apply -f {root}/n8n/netpol-probe-job.yaml"
""".format(root=MANIFEST_ROOT)
    assert files_in_pruned_dirs(_write_role(tmp_path, "n8n", text)) == {
        "n8n": ["n8n/netpol-probe-job.yaml"]
    }


def test_the_pre_fix_registry_jobs_are_flagged(tmp_path):
    """#1669 as it was written: all four job manifests, including the cron-read gc-job."""
    text = """---
- name: Deploy the image registry to the cluster
  ansible.builtin.include_role:
    name: k8s/manifests
  vars:
    manifests_service: registry
    manifests_files:
      - pvc.yaml
      - deployment.yaml

- name: Render the registry self-test jobs
  ansible.builtin.template:
    src: "{{{{ item }}}}.j2"
    dest: "{root}/registry/{{{{ item }}}}"
  loop:
    - selftest-push-job.yaml

- name: Prove a pod can PUSH to the registry over its Service
  ansible.builtin.command:
    cmd: "k3s kubectl apply -f {root}/registry/selftest-push-job.yaml"

- name: Prove containerd can PULL from the registry
  ansible.builtin.command:
    cmd: "k3s kubectl apply -f {root}/registry/selftest-pull-job.yaml"

- name: Render the registry netpol-isolation probe
  ansible.builtin.template:
    src: netpol-probe-job.yaml.j2
    dest: {root}/registry/netpol-probe-job.yaml

- name: Render the registry garbage-collection job
  ansible.builtin.template:
    src: gc-job.yaml.j2
    dest: {root}/registry/gc-job.yaml
""".format(root=MANIFEST_ROOT)
    assert files_in_pruned_dirs(_write_role(tmp_path, "registry", text)) == {
        "registry": [
            "registry/gc-job.yaml",
            "registry/netpol-probe-job.yaml",
            "registry/selftest-pull-job.yaml",
            "registry/selftest-push-job.yaml",
        ]
    }


# Named rather than counted: an empty census would make the invariants below pass on nothing,
# and which member went missing says more than a count moving. Verified against the tree on
# 2026-09-10.
KNOWN_PRUNED_DIRS = frozenset({"n8n", "registry", "traefik", "authelia", "jellyfin"})
KNOWN_STAGED_FILES = frozenset(
    {
        ("n8n-netpol", "netpol-probe-job.yaml"),
        ("registry-jobs", "gc-job.yaml"),
        ("headlamp-netpol", "netpol-probe-job.yaml"),
        ("media-volume-probe", "hardlink-probe-job.yaml"),
    }
)


def test_the_census_finds_the_known_pruned_directories():
    missing = KNOWN_PRUNED_DIRS - set(pruned_dirs(K8S_ROLES))
    assert not missing, (
        f"no manifests_service found for {sorted(missing)}; the census is reading the wrong "
        "thing, so the invariant below would pass on an empty set"
    )


def test_the_path_reader_finds_the_known_staged_files():
    found: set[tuple[str, str]] = set()
    for tasks_file in K8S_ROLES.glob("*/tasks/*.yml"):
        found |= staged_files(tasks_file)
    missing = KNOWN_STAGED_FILES - found
    assert not missing, (
        f"the path reader no longer finds {sorted(missing)}; it is looking at the wrong thing"
    )


def test_no_role_stages_a_file_in_a_pruned_manifest_directory():
    found = files_in_pruned_dirs(K8S_ROLES)
    assert not found, "\n".join(
        f"{role} stages {paths} inside a directory k8s/manifests prunes, so those files are "
        "deleted on every deploy of the owning role (#1654, #1668, #1669)"
        for role, paths in sorted(found.items())
    )


# ── invariant 2: no manifests_service claims a reserved sibling name ──────────────────────────


def test_an_ordinary_service_name_is_clean(tmp_path):
    root = _write_role(
        tmp_path, "widget", _PRUNED_ROLE.format(root=MANIFEST_ROOT, dir="widget-probe")
    )
    assert reserved_name_claims(root) == {}


def test_a_service_named_for_a_reserved_suffix_is_flagged(tmp_path):
    text = _PRUNED_ROLE.format(root=MANIFEST_ROOT, dir="gadget-claims-probe").replace(
        "manifests_service: widget", "manifests_service: gadget-claims"
    )
    assert set(reserved_name_claims(_write_role(tmp_path, "gadget", text))) == {
        "gadget-claims"
    }


def test_a_service_named_for_a_reserved_prefix_is_flagged(tmp_path):
    text = _PRUNED_ROLE.format(root=MANIFEST_ROOT, dir="build-thing-probe").replace(
        "manifests_service: widget", "manifests_service: build-thing"
    )
    assert set(reserved_name_claims(_write_role(tmp_path, "builder", text))) == {
        "build-thing"
    }


def test_a_service_claiming_another_roles_staging_directory_is_flagged(tmp_path):
    """The literal half: a name with no reserved shape that a sibling already stages into."""
    prober = """---
- name: Render the gadget hardlink probe
  ansible.builtin.template:
    src: probe-job.yaml.j2
    dest: {root}/gadget-scratch/probe-job.yaml
""".format(root=MANIFEST_ROOT)
    claimer = """---
- name: Deploy gadget-scratch to the cluster
  ansible.builtin.include_role:
    name: k8s/manifests
  vars:
    manifests_service: gadget-scratch
    manifests_files:
      - deployment.yaml
"""
    root = _write_role(tmp_path, "gadget", prober)
    _write_role(root, "scratch", claimer)
    assert set(reserved_name_claims(root)) == {"gadget-scratch"}


def test_no_manifests_service_claims_a_reserved_sibling_name():
    found = reserved_name_claims(K8S_ROLES)
    assert not found, "\n".join(
        f"manifests_service {service!r} {why}" for service, why in sorted(found.items())
    )


# ── the half-move: a path baked into a script the guard above cannot read ────────────────────
#
# `staged_files` reads tasks files. `registry-gc.sh` is a script template, so a move that
# retargets the gc render's `dest:` and leaves `JOB_MANIFEST` pointing at the old directory
# passes every assertion above and breaks GC at CRON time — which is the whole reason #1669's
# `gc-job.yaml` was more than cosmetic. One targeted pair rather than a wider glob: manifest
# templates mention paths too, and reading them all would only add noise.

GC_SCRIPT = K8S_ROLES / "registry" / "templates" / "registry-gc.sh.j2"
REGISTRY_TASKS = K8S_ROLES / "registry" / "tasks" / "main.yml"

_JOB_MANIFEST_RE = re.compile(r"^JOB_MANIFEST=(\S+)", re.MULTILINE)


def gc_manifest_paths(tasks_text: str, script_text: str) -> tuple[str, str]:
    """`(the path the render stages, the path the cron script reads)`, or '' for each miss."""
    staged = [p for p in _PATH_RE.findall(tasks_text) if p.endswith("/gc-job.yaml")]
    read = _JOB_MANIFEST_RE.findall(script_text)
    return (staged[0] if staged else "", read[0] if read else "")


_GC_RENDER = """---
- name: Render the registry garbage-collection job
  ansible.builtin.template:
    src: gc-job.yaml.j2
    dest: {path}
"""
_GC_SCRIPT = "JOB_MANIFEST={path}\n"


def test_gc_render_and_cron_script_agreeing_is_clean():
    staged, read = gc_manifest_paths(
        _GC_RENDER.format(path=f"{MANIFEST_ROOT}/registry-jobs/gc-job.yaml"),
        _GC_SCRIPT.format(path=f"{MANIFEST_ROOT}/registry-jobs/gc-job.yaml"),
    )
    assert staged == read != ""


def test_gc_render_moved_without_the_cron_script_is_flagged():
    staged, read = gc_manifest_paths(
        _GC_RENDER.format(path=f"{MANIFEST_ROOT}/registry-jobs/gc-job.yaml"),
        _GC_SCRIPT.format(path=f"{MANIFEST_ROOT}/registry/gc-job.yaml"),
    )
    assert staged != read


def test_the_gc_reader_finds_both_real_paths():
    """Non-vacuity: two empty strings would make the invariant below pass on nothing."""
    staged, read = gc_manifest_paths(REGISTRY_TASKS.read_text(), GC_SCRIPT.read_text())
    assert staged, f"no gc-job.yaml render found in {REGISTRY_TASKS}"
    assert read, f"no JOB_MANIFEST assignment found in {GC_SCRIPT}"


def test_registry_gc_reads_the_manifest_the_deploy_stages():
    staged, read = gc_manifest_paths(REGISTRY_TASKS.read_text(), GC_SCRIPT.read_text())
    assert staged == read, (
        f"{REGISTRY_TASKS} stages the GC job at {staged} while {GC_SCRIPT} reads {read}; the "
        "cron would apply a manifest the deploy never writes (#1669)"
    )


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))
