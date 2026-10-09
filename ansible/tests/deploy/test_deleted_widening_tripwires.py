"""Tripwires for three widenings that were deleted as memberless.

Each was a WIDENING -- it added something a narrower derivation structurally cannot see -- so
its absence turns a loud, wrong answer into a quiet, narrow one. Git history carries every
mechanism. This file owns the day a member of the class reappears.

One test per class, keyed on the SHAPE rather than on the name of the member that used to be in
it. Each reads the live tree and asserts it is clean today; each ships a planted fixture tree
that trips it (`.claude/rules/python-layout.md`). Each failure message names the `git show` that
restores the mechanism, so whoever trips it restores rather than re-derives.

Tests only. No derivation is widened here and no mechanism is restored.

Run: uv run pytest ansible/tests/deploy/test_deleted_widening_tripwires.py
"""

import re
from pathlib import Path

from lib import yaml_fast
from lib.k8s_roles import role_callers
from lib.render_guard import containers_entries

from _helpers import REPO
from _role_census import task_files

# The commit that deleted all three. Named in every failure message below.
DELETED_IN = "fb9e4017f"

# --- class 1: a split build role under-deploys ------------------------------------------------
#
# `_BUILD_ROLL_COUPLINGS` mapped a build role that renders no workload of its own to the role
# that runs what it builds, so deriving the build role from a changed path also derived its
# consumer. Without it a Renovate Dockerfile bump derives the build role alone, pushes a new
# image, rolls nothing and reports green, because `k8s_rebuilt_images` is play-scoped and two runs
# lose it.

# Manifest kinds that run a workload. A role rendering one of these deploys what it builds in
# the same role, so a single tag covers build and roll and the play-scoped fact never crosses a
# role boundary.
_WORKLOAD_KINDS = ("deployment", "daemonset", "statefulset", "cronjob", "job")

BUILDER = "image-builder"


def _renders_a_workload(role: Path) -> bool:
    templates = role / "templates"
    if not templates.is_dir():
        return False
    return any(
        name.startswith(_WORKLOAD_KINDS)
        for name in (p.name.lower() for p in templates.iterdir())
    )


def split_build_roles(repo: Path = REPO) -> set[str]:
    """Roles that include `k8s/image-builder` and render no workload to run what it builds.

    Keyed on the include edge rather than on a mention of `image_builder_name`, which is what
    excludes `image-builder` itself: the shared build machinery does not include itself, and its
    own `build-job.yaml.j2` is the build Job, not a workload the class is about.
    """
    callers = role_callers(repo).get(BUILDER) or set()
    k8s_roles = Path(repo) / "ansible" / "roles" / "k8s"
    return {name for name in callers if not _renders_a_workload(k8s_roles / name)}


def test_no_k8s_role_builds_an_image_and_renders_no_workload_is_clean():
    found = split_build_roles()
    assert not found, (
        f"these roles include k8s/{BUILDER} and render no workload manifest of their own: "
        f"{sorted(found)}. Deploying one alone builds an image nothing rolls onto, because "
        "k8s_rebuilt_images is play-scoped. The build-roll coupling that derived the consumer "
        f"alongside the build role was deleted as memberless in #2876; restore it with "
        f"`git show {DELETED_IN}^:ansible/tests/deploy/test_build_roll_couplings.py` and "
        f"`git show {DELETED_IN}` (the coupling reached about a dozen land and deploy call sites)."
    )


def test_a_role_that_builds_and_renders_no_workload_is_flagged(tmp_path):
    """The red proof: a planted role joining the class trips the census."""
    _plant_k8s_role(tmp_path, "widget-images", includes=BUILDER, templates=[])
    _plant_k8s_role(
        tmp_path, "widget", includes=BUILDER, templates=["deployment.yaml.j2"]
    )
    assert split_build_roles(tmp_path) == {"widget-images"}


def test_the_split_build_census_still_sees_real_builders():
    """Non-vacuity. An emptied caller graph would make the clean test pass over nothing.

    n8n and code-server each include image-builder AND render the workload that runs it, so
    they must be seen by one half of the census and rejected by the other.
    """
    callers = role_callers().get(BUILDER) or set()
    k8s_roles = REPO / "ansible" / "roles" / "k8s"
    for name in ("n8n", "code-server"):
        assert name in callers, (
            f"the census no longer sees {name} include k8s/{BUILDER}"
        )
        assert _renders_a_workload(k8s_roles / name), (
            f"the census no longer sees {name}'s workload manifest"
        )


# --- class 2: a sibling `import_tasks` under-reports callers ----------------------------------
#
# `lib.k8s_roles.role_callers` recognised two spellings of "this role runs that role's tasks":
# `include_role`/`import_role` naming `k8s/<role>`, and an `import_tasks` of a sibling role's
# file BY PATH. Without the second, a role reaching a sibling that way reads as
# reaching nothing, so `land_tags` reports its change `needs-manual-apply` and
# `Release Staleness Drift` narrows to the wrong consumer set.

_TASKS_KEYS = (
    "ansible.builtin.import_tasks",
    "import_tasks",
    "ansible.builtin.include_tasks",
    "include_tasks",
)
# The deleted pattern, verbatim. One `..` hop then one role segment: a sibling under the same
# plane. The cross-plane form live roles do use -- `{{ role_path }}/../../setup/common/tasks/…`
# -- has two segments before `tasks/` and does not match, which is the discriminator the
# must-not-flag assertion below pins.
_SIBLING_TASKS = re.compile(r"\.\./([^/]+)/tasks/")


def _task_file_paths(node) -> list[str]:
    """Every `import_tasks`/`include_tasks` target under `node`. Walks blocks, which nest tasks."""
    found: list[str] = []
    if isinstance(node, list):
        for item in node:
            found += _task_file_paths(item)
        return found
    if not isinstance(node, dict):
        return found
    for key in _TASKS_KEYS:
        value = node.get(key)
        path = value.get("file") if isinstance(value, dict) else value
        if isinstance(path, str):
            found.append(path)
    for value in node.values():
        if isinstance(value, list | dict):
            found += _task_file_paths(value)
    return found


def k8s_tasks_files(repo: Path = REPO) -> list[Path]:
    return task_files(Path(repo) / "ansible" / "roles" / "k8s")


def sibling_path_imports(repo: Path = REPO) -> dict[str, list[str]]:
    """Tasks file (repo-relative) -> the sibling-path targets it imports."""
    found: dict[str, list[str]] = {}
    for path in k8s_tasks_files(repo):
        hits = [
            target
            for target in _task_file_paths(yaml_fast.safe_load(path.read_text()) or [])
            if _SIBLING_TASKS.search(target)
        ]
        if hits:
            found[path.relative_to(repo).as_posix()] = hits
    return found


def test_no_k8s_role_reaches_a_sibling_by_path_is_clean():
    found = sibling_path_imports()
    assert not found, (
        f"these tasks files import a sibling role's tasks by path: {found}. The caller walk no "
        "longer recognises that spelling, so the importing role reads as reaching nothing: "
        "land_tags reports its change needs-manual-apply and Release Staleness Drift narrows to "
        "the wrong consumer set. Either spell the edge `include_role: name: k8s/<role>`, or "
        f"restore the path form with `git show {DELETED_IN} -- scripts/lib/k8s_roles.py`."
    )


def test_a_sibling_path_import_is_flagged(tmp_path):
    """The red proof, with the cross-plane form alongside it as the must-not-flag half."""
    _plant_k8s_role(
        tmp_path,
        "widget",
        imports=[
            "{{ role_path }}/../widget-lib/tasks/render.yml",
            "{{ role_path }}/../../setup/common/tasks/stamp_deployed.yml",
        ],
    )
    assert sibling_path_imports(tmp_path) == {
        "ansible/roles/k8s/widget/tasks/main.yml": [
            "{{ role_path }}/../widget-lib/tasks/render.yml"
        ]
    }


def test_the_tasks_file_census_is_not_empty():
    """Non-vacuity. A glob that matched nothing would make the clean test pass over nothing.

    configarr and janitorr both import a tasks file by path — the CROSS-PLANE form, which is not
    the shape this class is about. Naming them holds the census and the discriminator at once.
    """
    seen = {p.relative_to(REPO).as_posix() for p in k8s_tasks_files()}
    assert len(seen) >= 50, f"the k8s tasks-file census collapsed to {len(seen)} files"
    for name in (
        "ansible/roles/k8s/configarr/tasks/main.yml",
        "ansible/roles/k8s/janitorr/tasks/main.yml",
    ):
        assert name in seen, f"the census no longer reads {name}"
        assert "../" in (REPO / name).read_text(), (
            f"{name} no longer imports a tasks file by path, so it no longer holds the "
            "cross-plane-is-not-a-sibling discriminator — name another file that does"
        )


# --- class 3: an entry declaring a second tag hides a recording caller -------------------------
#
# `_role_tags` stops at a role that has a `containers_list` entry and never reads that entry's
# OTHER declared tags, so an entry declaring a second tag (the `tags: [n8n-images, n8n]` shape)
# hides a recording caller: a `--tags n8n` deploy runs the builder and writes `n8n.json` while
# the derivation sees no recording caller. That leaves a `k8s_unapplied` line nothing but a hand
# clears.


# Every containers_list source in the inventory. The symptom is k8s-only, because `k8s_unapplied`
# is; the shape check is deliberately wider, so a docker entry growing the shape is still seen.
def containers_list_sources(repo: Path = REPO) -> list[Path]:
    inventory = Path(repo) / "ansible" / "inventory"
    return sorted(inventory.glob("host_vars/*.yml")) + sorted(
        inventory.glob("group_vars/all.yml")
    )


def entries_declaring_other_tags(repo: Path = REPO) -> dict[str, list[str]]:
    """`<file>:<entry name>` -> the declared tags, for every entry whose tags are not just its name."""
    found: dict[str, list[str]] = {}
    for path in containers_list_sources(repo):
        for entry in containers_entries(path):
            declared = entry.get("tags")
            if declared is not None and list(declared) != [entry["name"]]:
                found[f"{path.relative_to(repo).as_posix()}:{entry['name']}"] = list(
                    declared
                )
    return found


def test_no_containers_list_entry_declares_another_tag_is_clean():
    found = entries_declaring_other_tags()
    assert not found, (
        f"these containers_list entries declare a tag other than their own name: {found}. The "
        "shared-role record derivation stops at a role that has an entry and never reads that "
        "entry's other tags, so a deploy under the second tag writes a release record the "
        "derivation cannot see and the role's k8s_unapplied line needs a hand to clear (#2666). "
        f"Restore the widening with `git show {DELETED_IN} -- "
        "scripts/deploy_tools/shared_role_callers.py`."
    )


def test_an_entry_declaring_a_second_tag_is_flagged(tmp_path):
    """The red proof. Keyed on the shape, so any entry trips it, not only `n8n-images`."""
    host_vars = tmp_path / "ansible" / "inventory" / "host_vars"
    host_vars.mkdir(parents=True)
    (host_vars / "planted.yml").write_text(
        "containers_list:\n"
        "  - name: widget\n"
        "    platform: k8s\n"
        "  - name: widget-images\n"
        "    platform: k8s\n"
        "    tags: [widget-images, widget]\n"
    )
    assert entries_declaring_other_tags(tmp_path) == {
        "ansible/inventory/host_vars/planted.yml:widget-images": [
            "widget-images",
            "widget",
        ]
    }


def test_the_entry_census_is_not_empty():
    """Non-vacuity. A containers_list key rename would empty the census and pass the clean test."""
    names = {
        entry["name"]
        for path in containers_list_sources()
        for entry in containers_entries(path)
    }
    assert {"n8n", "sonarr"} <= names, (
        f"the containers_list census no longer reads n8n and sonarr — it found {len(names)} "
        "entries, so the clean test above may be passing over nothing"
    )


# --- fixture-tree planting --------------------------------------------------------------------


def _plant_k8s_role(
    root: Path, name: str, includes: str = "", imports=(), templates=()
):
    """Write a minimal `ansible/roles/k8s/<name>/` under `root` for the red proofs."""
    role = root / "ansible" / "roles" / "k8s" / name
    (role / "tasks").mkdir(parents=True)
    tasks = []
    if includes:
        tasks.append(
            f"- name: Build\n  ansible.builtin.include_role:\n    name: k8s/{includes}\n"
        )
    for target in imports:
        tasks.append(f'- name: Import\n  ansible.builtin.import_tasks: "{target}"\n')
    (role / "tasks" / "main.yml").write_text("---\n" + "".join(tasks or ["[]\n"]))
    if templates:
        (role / "templates").mkdir()
        for template in templates:
            (role / "templates" / template).write_text("{}\n")
