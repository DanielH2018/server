"""No pre_task of the k8s play applies a Namespace or a LimitRange, owned or handed back.

The shared kind `homelab-namespace` is the only writer of the `homelab` Namespace and its
LimitRange (D5 step 30). D5's namespace PR deleted the two `ansible/deploy.yml` pre_tasks that
applied them, rather than gating them on role ownership such as
`containers_list | ansible_owned | length > 0`. Every role PR's rollback is D2's handback, so one
handback would open that gate and re-arm Ansible's client-side apply beside `homelab-namespace`,
with no end to the overlap.

`test_no_pre_task_would_apply_a_namespace_or_limitrange` evaluates every pre_task of the k8s
play against two inventory fixtures built from daniel-box's k8s entries: every entry flux-owned,
and the same list with one entry handed back, its `owner` removed as D2's second handback PR
removes it. An `import_tasks` or `include_tasks` pre_task contributes its file's tasks under its
own `when`. A task applies a Namespace or a LimitRange when it runs a `kubectl` write or a
`kubernetes.core` module and its text, or a fact an earlier task set, names either kind. Its
`when` is evaluated with the play's filters, `ansible_owned` among them, and a condition that
cannot be evaluated counts as true. The red half is a fixture play keeping the deleted apply
under the ownership gate, which runs against the handed-back fixture.

`test_homelab_namespace_is_rendered_and_a_shared_kind` fails when the kind is missing from the
render or from `SHARED_KINDS`.

The plan is docs/target-architecture/deploy/d5-bulk-move.md, step 30.

Run: uv run pytest ansible/tests/deploy/test_k8s_play_applies_no_namespace.py
"""

import json
import re
from collections.abc import Iterable, Iterator

import jinja2
from lib import yaml_fast
from lib.ansible_jinja_env import make_ansible_env

import toposort
from _helpers import ALL_VARS, ANSIBLE, HOST_VARS
from _k8s_render import deploy_play
from renderer.renderer_lib.bundle import FLUX_INDEX, SHARED_KINDS, bundle_files
from renderer.renderer_lib.namespace import HOMELAB_NAMESPACE

_WRITE = re.compile(r"\bkubectl\b[^\n]*\b(apply|create|replace)\b")
_KIND = re.compile(r"""\bkind["']?\s*:\s*["']?(Namespace|LimitRange)\b""")
_CREATE = re.compile(r"\bkubectl\b[^\n]*\bcreate\s+(namespace|ns|limitrange)\b")
_INCLUDES = (
    "ansible.builtin.import_tasks",
    "ansible.builtin.include_tasks",
    "import_tasks",
    "include_tasks",
)
# The deleted pre_tasks, gated on role ownership: the gate step 30 refuses.
_GATED_PLAY = """
- name: Deploy k8s workloads
  hosts: localhost
  pre_tasks:
    - name: Build the workload namespace manifest
      ansible.builtin.set_fact:
        deploy_k8s_ns_manifest:
          apiVersion: v1
          kind: List
          items:
            - apiVersion: v1
              kind: Namespace
              metadata:
                name: "{{ k8s_namespace }}"
      when: containers_list | ansible_owned | length > 0
    - name: Apply the workload namespace
      ansible.builtin.command:
        cmd: k3s kubectl apply -f -
        stdin: "{{ deploy_k8s_ns_manifest | to_nice_yaml }}"
      when:
        - not k8s_dry_run | bool
        - containers_list | ansible_owned | length > 0
"""


def _env() -> jinja2.Environment:
    env = make_ansible_env(undefined_cls=jinja2.StrictUndefined)
    env.filters.update(toposort.FilterModule().filters())
    return env


def _conditions(task: dict) -> list[str]:
    when = task.get("when", [])
    return [str(c) for c in (when if isinstance(when, list) else [when])]


def walk(tasks: Iterable[dict], whens: tuple[str, ...] = ()) -> Iterator[tuple]:
    """Each task with its effective conditions, an include's file's tasks under its own."""
    for task in tasks:
        conditions = whens + tuple(_conditions(task))
        yield task, conditions
        path = next((task[k] for k in _INCLUDES if k in task), None)
        if path and "{{" not in str(path):
            yield from walk(
                yaml_fast.safe_load((ANSIBLE / path).read_text()), conditions
            )


def would_run(
    conditions: Iterable[str], context: dict, env: jinja2.Environment
) -> bool:
    """Whether every condition holds; one that cannot be evaluated counts as holding."""
    for condition in conditions:
        try:
            if not env.compile_expression(condition)(**context):
                return False
        except jinja2.exceptions.UndefinedError, TypeError, AttributeError:
            continue
    return True


def namespace_appliers(pre_tasks: list[dict], containers: list[dict]) -> list[str]:
    """The names of the pre_tasks that would apply a Namespace or a LimitRange."""
    env = _env()
    context = {
        **(yaml_fast.safe_load(ALL_VARS.read_text()) or {}),
        "k8s_dry_run": False,
        "containers_list": containers,
    }
    facts: set[str] = set()
    found = []
    for task, conditions in walk(pre_tasks):
        text = json.dumps(task)
        set_fact = task.get("ansible.builtin.set_fact") or task.get("set_fact")
        if isinstance(set_fact, dict) and _KIND.search(text):
            facts |= set(set_fact)
        writes = _WRITE.search(text) or any(
            k.startswith("kubernetes.core.") for k in task
        )
        names = (
            _KIND.search(text)
            or _CREATE.search(text)
            or any(re.search(rf"\b{re.escape(f)}\b", text) for f in facts)
        )
        if writes and names and would_run(conditions, context, env):
            found.append(str(task.get("name")))
    return found


def _fixtures() -> dict[str, list[dict]]:
    """Every k8s entry flux-owned, and the same list with its first entry handed back."""
    entries = yaml_fast.safe_load((HOST_VARS / "daniel-box.yml").read_text())
    k8s = toposort.filter_by_platform(entries["containers_list"], "k8s")
    owned = [{**entry, "owner": "flux"} for entry in k8s]
    handed_back = [{k: v for k, v in owned[0].items() if k != "owner"}, *owned[1:]]
    return {"every entry flux-owned": owned, "one entry handed back": handed_back}


def test_the_walk_reaches_every_pre_task_and_the_included_files():
    names = [str(task.get("name")) for task, _ in walk(deploy_play()["pre_tasks"])]
    for member in (
        "Load encrypted secrets and set the become password (shared preamble)",
        "Tear down retired k8s services",
        "Start with an empty rollout queue",
    ):
        assert member in names, names
    # k8s_retire.yml's delete, reached through the include, under the include's own gate.
    assert any(
        "kubectl delete" in json.dumps(task)
        for task, _ in walk(deploy_play()["pre_tasks"])
    )


def test_no_pre_task_would_apply_a_namespace_or_limitrange():
    pre_tasks = deploy_play()["pre_tasks"]
    for fixture, containers in _fixtures().items():
        assert namespace_appliers(pre_tasks, containers) == [], fixture


def test_a_play_gating_the_apply_on_role_ownership_is_flagged_on_a_handback():
    [play] = yaml_fast.safe_load(_GATED_PLAY)
    fixtures = _fixtures()
    assert (
        namespace_appliers(play["pre_tasks"], fixtures["every entry flux-owned"]) == []
    )
    assert namespace_appliers(play["pre_tasks"], fixtures["one entry handed back"]) == [
        "Apply the workload namespace"
    ]


def namespace_kind_problems(files: dict[str, str], kinds: Iterable[str]) -> list[str]:
    """One line per way `homelab-namespace` is missing from a bundle or from `kinds`."""
    problems = []
    if HOMELAB_NAMESPACE not in set(kinds):
        problems.append(f"{HOMELAB_NAMESPACE} is not in SHARED_KINDS")
    flux = f"flux/{HOMELAB_NAMESPACE}.yaml"
    if flux not in files:
        problems.append(f"{flux} is not rendered")
    elif (
        f"{HOMELAB_NAMESPACE}.yaml"
        not in yaml_fast.safe_load(files[FLUX_INDEX])["resources"]
    ):
        problems.append(f"{FLUX_INDEX} does not list {HOMELAB_NAMESPACE}.yaml")
    rendered = {
        (doc["kind"], doc["metadata"]["name"])
        for path, text in files.items()
        if path.startswith(f"cluster/{HOMELAB_NAMESPACE}/")
        for doc in yaml_fast.safe_load_all(text)
        if isinstance(doc, dict) and "metadata" in doc
    }
    for kind, name in (
        ("Namespace", "homelab"),
        ("LimitRange", "default-container-limits"),
    ):
        if (kind, name) not in rendered:
            problems.append(f"cluster/{HOMELAB_NAMESPACE}/ renders no {kind} {name}")
    return problems


def _shared_bundle() -> dict[str, str]:
    return bundle_files({}, {}, [], "600s", "x=ENC[y]\n")


def test_homelab_namespace_is_rendered_and_a_shared_kind():
    assert namespace_kind_problems(_shared_bundle(), SHARED_KINDS) == []


def test_a_bundle_without_homelab_namespace_is_flagged():
    files = {
        path: text
        for path, text in _shared_bundle().items()
        if HOMELAB_NAMESPACE not in path
    }
    assert namespace_kind_problems(files, SHARED_KINDS - {HOMELAB_NAMESPACE}) == [
        f"{HOMELAB_NAMESPACE} is not in SHARED_KINDS",
        f"flux/{HOMELAB_NAMESPACE}.yaml is not rendered",
        f"cluster/{HOMELAB_NAMESPACE}/ renders no Namespace homelab",
        f"cluster/{HOMELAB_NAMESPACE}/ renders no LimitRange default-container-limits",
    ]
