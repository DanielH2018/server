"""A workload that reads a rotated Secret through env must be restarted by its own role.

`roles/k8s/manifests` restarts a workload after a ConfigMap/Secret change, because
`kubectl apply` updates the object and nothing else. Env is resolved once at pod start, so a
rotated secret reaches the Secret while the running pods keep using the old value.

That restart covers exactly one workload: `manifests_rollout | default(manifests_service)`.
Every OTHER workload a role renders is restarted only if the role names it in
`manifests_extra_rollouts`. A role that forgets is green either way, which is what makes this
worth a test rather than a comment.

A role setting `manifests_rollout: ""` to opt out of the shared WAIT takes the RESTART with it:
rotating a Secret updates the Secret, restarts nothing, and leaves the pods on the old value
behind a `failed=0` recap.

A role can also set an ordinary `manifests_rollout` while the workload that misses out is a
DaemonSet. So the selector is derived from the mechanism, not from `manifests_rollout == ""` or
`kind: Deployment`: any workload of any kind, in any role that renders a Secret, which reads a
Secret through `secretKeyRef` or `envFrom.secretRef`.

WHY ENV AND NOT EVERY SECRET CONSUMER. A Secret consumed through a plain volume mount is
refreshed in place by the kubelet, so it is a different mechanism with a different remedy and it
is out of scope here. This is not a carve-out for a role: `scrutiny` renders three workloads and
mounts its Secret as a volume in two of them, so it is out of the corpus by the rule rather than
by name.

THE LIMIT OF THAT RULE, stated rather than left to be discovered. A `subPath` mount is NOT
refreshed — the kubelet only updates the whole projected directory — so a subPath-mounted Secret
is the same defect class as `secretKeyRef` and this selector does not see it. Three roles
subPath-mount a Secret today (janitorr, peanut, qbittorrent) and all three are safe for a reason
that has nothing to do with this guard: each renders exactly one workload, named after its
service, so the shared restart already covers it. A role that grows a second workload around a
subPath-mounted Secret would slip past. Widening to cover it means associating a volumeMount
with its volume across a Jinja template, which is a bigger and more fragile parse than the one
below; it is a deliberate deferral, not an oversight.

Both halves are asserted, per the repo's paired accept/reject rule: the live tree must stay
clean (with the pinned exceptions below), a synthetic role in the broken shape must be flagged,
and the gate the fix depends on must stay ungated — if someone adds a `manifests_rollout`
length condition to the extras task, every fix this guard demands becomes inert while this file
still passes.

The FILENAME predates the widened selector and no longer describes it; it is kept so the history
of this guard stays on one path. Read the code, not the name.
"""

import re
from pathlib import Path

from lib import yaml_fast
from _helpers import K8S_ROLES
from _k8s_render import rendered_texts

_MANIFESTS = K8S_ROLES / "manifests/tasks/main.yml"

_WORKLOAD_KIND = re.compile(r"^kind:\s*(?:Deployment|DaemonSet|StatefulSet)\s*$", re.M)
# `secretKeyRef` is a single env var; `secretRef` is the `envFrom` whole-Secret form. Both
# resolve at pod start and neither is refreshed in a running container.
_ENV_SECRET = re.compile(r"secretKeyRef|secretRef")
_METADATA_NAME = re.compile(
    r"^metadata:\s*$(?:\n(?:[ \t].*)?)*?\n\s+name:\s*(\S+)", re.M
)

# Workloads that read a rendered Secret through env and are NOT restarted by their role.
#
# Empty: every workload this guard's selector reaches is named in some role's manifests_rollout
# or manifests_extra_rollouts. Left as a set literal, not deleted, so the next gap has somewhere
# to be pinned with its reasoning.
_KNOWN_UNCOVERED: set[tuple[str, str]] = set()


def _include_vars(role_tasks: Path) -> list[dict]:
    """The `vars:` of every `include_role: k8s/manifests` in a role's task file."""
    out = []
    for task in yaml_fast.safe_load(role_tasks.read_text()) or []:
        if not isinstance(task, dict):
            continue
        include = (
            task.get("ansible.builtin.include_role") or task.get("include_role") or {}
        )
        if include.get("name") == "k8s/manifests":
            out.append(task.get("vars") or {})
    return out


def _restarts_privately(role_dir: Path) -> bool:
    """Does the role run its own `rollout restart`, anywhere in its task files?

    Two roles legitimately do: observability rolls a Deployment and a DaemonSet together in a
    private loop, and pihole rolls its two instances one at a time from an included
    `roll_one.yml`. Searching the whole `tasks/` tree rather than `main.yml` is the point —
    pihole's restart lives in the include, so a main.yml-only check would call it broken.
    """
    tasks_dir = role_dir / "tasks"
    if not tasks_dir.is_dir():
        return False
    return any(
        "rollout restart" in path.read_text() for path in tasks_dir.rglob("*.yml")
    )


def _template_texts(role_dir: Path) -> dict[str, str]:
    """`role_dir`'s templates by name, as the deploy renders them where a render reaches the role.

    `_k8s_render.rendered_texts` renders every role with a `containers_list` entry, which is
    every role this guard's corpus can contain. A synthetic `widget` role under `tmp_path` has
    no entry and no render, so it falls back to its source (#3218) — that is the only reader of
    the fallback, and `test_the_render_reaches_every_role_rendering_a_secret` holds it.

    Reading the render is what lets the selector see a `secretKeyRef` that arrives through a
    variable, and it resolves a templated `metadata.name` into the name an
    `manifests_extra_rollouts` entry has to match.
    """
    if role_dir.parent == K8S_ROLES:
        rendered = {
            template: text
            for role, template, text in rendered_texts()
            if role == role_dir.name
        }
        if rendered:
            return rendered
    templates = role_dir / "templates"
    if not templates.is_dir():
        return {}
    return {path.name: path.read_text() for path in sorted(templates.glob("*.j2"))}


def _env_secret_workloads(role_dir: Path) -> list[str]:
    """Names of the workloads `role_dir` renders that read a Secret through env.

    Scanned per YAML document as text rather than parsed, because a document can hold Jinja the
    render leaves in place. A workload whose `metadata.name` still reads as unresolved text
    matches no `manifests_extra_rollouts` entry and therefore reads as uncovered, which is the
    direction that fails loudly instead of silently passing.
    """
    found = []
    for name, text in sorted(_template_texts(role_dir).items()):
        for document in re.split(r"^---\s*$", text, flags=re.M):
            if not _WORKLOAD_KIND.search(document):
                continue
            if not _ENV_SECRET.search(document):
                continue
            found.append(
                match.group(1)
                if (match := _METADATA_NAME.search(document))
                else f"<unnamed in {name}>"
            )
    return found


def uncovered_env_secret_workloads(role_dir: Path) -> list[tuple[str, str]]:
    """(role, workload) for every env-Secret reader the role's own deploy never restarts.

    The corpus is roles rendering `manifests_secret_files` — a role with no Secret of its own
    has nothing for a rotation to miss. A workload reading a Secret rendered by a DIFFERENT role
    is still counted: matching Secret names across roles would need the Jinja resolved, and
    counting it is the fail-loud direction. No role in the tree is in that shape today.
    """
    if _restarts_privately(role_dir):
        return []
    tasks = role_dir / "tasks/main.yml"
    if not tasks.is_file():
        return []
    uncovered = []
    for variables in _include_vars(tasks):
        if not variables.get("manifests_secret_files"):
            continue
        covered = {
            variables.get("manifests_rollout", variables.get("manifests_service"))
        }
        covered |= {
            entry["name"] for entry in variables.get("manifests_extra_rollouts") or []
        }
        uncovered += [
            (role_dir.name, name)
            for name in _env_secret_workloads(role_dir)
            if name not in covered
        ]
    return uncovered


def _live_tree() -> list[tuple[str, str]]:
    found = []
    for tasks in sorted(K8S_ROLES.glob("*/tasks/main.yml")):
        found += uncovered_env_secret_workloads(tasks.parent.parent)
    return found


def test_every_env_secret_workload_is_restarted_by_its_role():
    """The reject half, against the live tree: no unpinned workload may miss its restart."""
    unexpected = set(_live_tree()) - _KNOWN_UNCOVERED
    assert not unexpected, (
        "these workloads read a Secret their role renders through env, and no rollout in that "
        "role restarts them — a rotated value reaches the Secret and never reaches the running "
        f"pods, behind a green deploy: {sorted(unexpected)}. Name each one in "
        "manifests_extra_rollouts (with `kind:` when it is not a Deployment)."
    )


def test_the_known_uncovered_pin_is_not_stale():
    """A pinned gap that got fixed must leave the pin, or the pin hides the next regression."""
    stale = _KNOWN_UNCOVERED - set(_live_tree())
    assert not stale, (
        f"_KNOWN_UNCOVERED pins workloads this guard no longer flags: {sorted(stale)}. If the "
        "gap was fixed, delete the entry; leaving it in place would silently absorb a future "
        "regression on the same workload."
    )


def test_the_render_reaches_every_role_rendering_a_secret():
    """The denylisted half of `_template_texts`: no real role falls back to its source.

    Every role whose `k8s/manifests` include names `manifests_secret_files` is in
    `_k8s_render.rendered_texts`, so the fallback serves only the synthetic roles the proofs
    below write under `tmp_path`. A role dropping out of the render would read as source again
    and stop seeing a `secretKeyRef` that arrives through a variable.
    """
    rendered = {role for role, _, _ in rendered_texts()}
    unrendered = sorted(
        tasks.parent.parent.name
        for tasks in sorted(K8S_ROLES.glob("*/tasks/main.yml"))
        if any(
            variables.get("manifests_secret_files")
            for variables in _include_vars(tasks)
        )
        and tasks.parent.parent.name not in rendered
    )
    assert unrendered == [], (
        "these roles render a Secret but no render reaches them, so this guard reads their "
        f"template source and cannot follow a variable: {unrendered}"
    )


def test_the_shape_this_guard_protects_actually_exists():
    """A guard that matches nothing cannot fail. Prove the selector still reaches real roles."""
    corpus = [
        tasks.parent.parent.name
        for tasks in sorted(K8S_ROLES.glob("*/tasks/main.yml"))
        if any(
            variables.get("manifests_secret_files")
            for variables in _include_vars(tasks)
        )
        and _env_secret_workloads(tasks.parent.parent)
    ]
    assert len(corpus) > 1, (
        "no role renders a Secret read through env — either the shape is gone (delete this "
        f"file) or the selector broke and the guard is now inert. Matched: {corpus}"
    )


def _write_role(
    root: Path, name: str, *, tasks: str, templates: dict[str, str]
) -> Path:
    role = root / name
    (role / "tasks").mkdir(parents=True)
    (role / "tasks/main.yml").write_text(tasks)
    (role / "templates").mkdir()
    for filename, body in templates.items():
        (role / "templates" / filename).write_text(body)
    return role


_TASKS = """---
- name: Deploy {name} to the cluster
  ansible.builtin.include_role:
    name: k8s/manifests
  vars:
    manifests_service: {name}
    manifests_secret_files:
      - secret.yaml
{extras}"""

_DAEMONSET = """---
apiVersion: apps/v1
kind: DaemonSet
metadata:
  name: {name}-agent
  namespace: homelab
spec:
  template:
    spec:
      containers:
        - name: agent
          env:
            - name: AGENT_PASSWORD
              valueFrom:
                secretKeyRef:
                  name: {name}-env
                  key: AGENT_PASSWORD
"""

_MOUNTED_DAEMONSET = """---
apiVersion: apps/v1
kind: DaemonSet
metadata:
  name: {name}-agent
  namespace: homelab
spec:
  template:
    spec:
      containers:
        - name: agent
          volumeMounts:
            - name: creds
              mountPath: /etc/creds
      volumes:
        - name: creds
          secret:
            secretName: {name}-env
"""


def test_a_second_workload_reading_the_secret_by_env_is_flagged(tmp_path):
    """The reject half, against a synthetic role: the defect this guard exists to catch."""
    role = _write_role(
        tmp_path,
        "widget",
        tasks=_TASKS.format(name="widget", extras=""),
        templates={"daemonset.yaml.j2": _DAEMONSET.format(name="widget")},
    )
    assert uncovered_env_secret_workloads(role) == [("widget", "widget-agent")]


def test_a_second_workload_named_in_extra_rollouts_is_clean(tmp_path):
    """The accept half: naming it in manifests_extra_rollouts is what clears the finding."""
    extras = (
        "    manifests_extra_rollouts:\n"
        "      - name: widget-agent\n"
        "        kind: daemonset\n"
    )
    role = _write_role(
        tmp_path,
        "widget",
        tasks=_TASKS.format(name="widget", extras=extras),
        templates={"daemonset.yaml.j2": _DAEMONSET.format(name="widget")},
    )
    assert uncovered_env_secret_workloads(role) == []


def test_a_second_workload_mounting_the_secret_as_a_volume_is_clean(tmp_path):
    """The scope boundary, asserted: a volume mount is kubelet-refreshed and out of scope."""
    role = _write_role(
        tmp_path,
        "widget",
        tasks=_TASKS.format(name="widget", extras=""),
        templates={"daemonset.yaml.j2": _MOUNTED_DAEMONSET.format(name="widget")},
    )
    assert uncovered_env_secret_workloads(role) == []


def test_a_role_restarting_its_workloads_privately_is_clean(tmp_path):
    """observability and pihole roll their own workloads; the exemption must keep working."""
    role = _write_role(
        tmp_path,
        "widget",
        tasks=_TASKS.format(name="widget", extras="")
        + "\n- name: Roll it\n  ansible.builtin.command: kubectl rollout restart ds/widget-agent\n",
        templates={"daemonset.yaml.j2": _DAEMONSET.format(name="widget")},
    )
    assert uncovered_env_secret_workloads(role) == []


def test_the_extra_rollouts_restart_is_not_gated_on_manifests_rollout():
    """The accept half for the mechanism: the escape hatch must stay ungated."""
    tasks = yaml_fast.safe_load(_MANIFESTS.read_text()) or []
    extras = [
        task
        for task in tasks
        if isinstance(task, dict)
        and "extra deployments" in (task.get("name") or "")
        and "rollout restart" in str(task.get("ansible.builtin.command", ""))
    ]
    assert len(extras) == 1, (
        "expected exactly one extra-rollouts restart task in k8s/manifests; found "
        f"{len(extras)} — the selector below is matching the wrong thing"
    )
    conditions = " ".join(str(c) for c in extras[0].get("when") or [])
    # `manifests_rollout_kind` is a DIFFERENT variable and legitimately appears here, so match
    # the name only where no identifier character follows it. A bare substring test flags the
    # kind and fails on correct code.
    gated = re.search(r"\bmanifests_rollout\b(?!_)", conditions)
    assert not gated, (
        "the extra-rollouts restart has grown a manifests_rollout condition. That is the exact "
        "gate roles escape by naming their Deployments here, so adding it makes the fix inert "
        "and re-breaks every role in the shape — silently, behind a green deploy."
    )
