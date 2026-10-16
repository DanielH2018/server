"""The retired-cron census row of target slice H2.1: every retired host cron keeps its absent arm.

Only an absent arm removes a host cron or a setup-deployed fragment, and it runs from the role
that installed the thing. A PR that deletes the install task outright leaves the cron running
with no writer, and one that drops a `stamp_deployed.yml` import with no arm for its fragment
leaves `/var/lib/homelab/setup-deployed-manifest.d/<name>` behind, which turns setup drift DOWN.
The plan is `docs/target-architecture/host/h2-shrink-host-plane.md`, *Retirement by absent arm,
not a list*. The row sits in its own module because `test_census_rows_roles.py`, which imports
it into `ROWS`, is at the cap for a test module.

`RETIRED_CRONS` and `RETIRED_FRAGMENTS` hold what has been retired so far. Each H2 family PR
adds the crons and fragments it retires in the same commit as their absent arms, and H7, which
deletes the arms, deletes the entries with them. An arm may sit in the role's own task files or,
once D5's conversion PR has moved it, in `roles/setup/k8s_host_files/tasks/<role>.yml`.
"""

from _helpers import walk_tasks
from _row_table import Census, Subject, tracked
from lib import yaml_fast

FRAGMENT_DIR = "/var/lib/homelab/setup-deployed-manifest.d/"
_HOST_FILES = "ansible/roles/setup/k8s_host_files/tasks/"

# `<plane>/<role>` -> the cron task names the role retired.
RETIRED_CRONS = {
    "k8s/traefik": ("Cloudflare IP drift",),
}
# `<plane>/<role>` -> the `stamp_deployed_name` of each fragment the role retired.
RETIRED_FRAGMENTS = {
    "setup/k3s": ("k3s-longhorn-reap",),
}


class RetiringRole(Subject):
    """One role that retired a cron or a fragment: `{task file: text}` of every file that may hold its arms."""

    def __init__(self, role: str, tasks: dict[str, str] | None = None):
        super().__init__(f"ansible/roles/{role}", text="")
        self.role = role
        self._tasks = tasks

    @property
    def tasks(self) -> dict[str, str]:
        if self._tasks is None:
            name = self.role.split("/", 1)[1]
            rels = tracked(f"{self.rel}/tasks/*.yml", f"{_HOST_FILES}{name}.yml")
            self._tasks = {rel: Subject(rel).text for rel in rels}
        return self._tasks


def _retiring_roles() -> list[RetiringRole]:
    return [RetiringRole(role) for role in sorted({*RETIRED_CRONS, *RETIRED_FRAGMENTS})]


def _absent_arms(subject: RetiringRole) -> tuple[set[str], set[str]]:
    """(cron names, fragment paths) that the role's tasks remove with `state: absent`."""
    crons, paths = set(), set()
    for text in subject.tasks.values():
        for task in walk_tasks(yaml_fast.safe_load(text) or []):
            for key, args in task.items():
                if not isinstance(args, dict) or args.get("state") != "absent":
                    continue
                module = key.rsplit(".", 1)[-1]
                if module == "cron":
                    crons.add(args.get("name"))
                elif module == "file":
                    loop = task.get("loop") or task.get("with_items") or []
                    targets = (
                        loop if args.get("path") == "{{ item }}" else [args.get("path")]
                    )
                    paths.update(t for t in targets if isinstance(t, str))
    return crons, paths


def missing_arms(subject: Subject) -> list[str]:
    """Every retired cron or fragment of `subject`'s role that no absent arm removes."""
    if not isinstance(subject, RetiringRole):
        return []
    crons, paths = _absent_arms(subject)
    return [
        f"cron {name!r} has no `state: absent` task"
        for name in RETIRED_CRONS.get(subject.role, ())
        if name not in crons
    ] + [
        f"fragment {name!r} has no absent arm for {FRAGMENT_DIR}{name}"
        for name in RETIRED_FRAGMENTS.get(subject.role, ())
        if FRAGMENT_DIR + name not in paths
    ]


def _role(role: str, body: str) -> RetiringRole:
    return RetiringRole(role, {f"ansible/roles/{role}/tasks/main.yml": body})


_DRIFT_ARM = (
    "- name: Remove the Cloudflare IP drift cron\n  ansible.builtin.cron:\n"
    '    name: "Cloudflare IP drift"\n    cron_file: cloudflare-ip-drift\n'
)
_REAP_ARM = (
    "- name: Retire the host-installed Longhorn reapers\n  ansible.builtin.file:\n"
    '    path: "{{ item }}"\n    state: absent\n  loop:\n'
    f"    - /opt/longhorn-reap\n    - {FRAGMENT_DIR}k3s-longhorn-reap\n"
)

RETIRED_CRON_ARMS = Census(
    name="retired-host-crons-keep-their-absent-arm",
    reason=(
        "A retired host cron or setup-deployed fragment is removed only by a `state: absent` "
        "task in the role that installed it, which runs in that role's own deploy or hand "
        "apply. Delete the arm and the cron keeps running with no writer; drop a "
        "`stamp_deployed.yml` import without an arm and the stale fragment turns setup drift "
        "DOWN. Keep the arm until H7 deletes it with this row's entry."
    ),
    files=_retiring_roles,
    offence=missing_arms,
    red=(
        # The install task deleted outright rather than flipped to absent.
        _role(
            "k8s/traefik", "- name: Unrelated\n  ansible.builtin.debug:\n    msg: x\n"
        ),
        # The arm is there, but it still installs the cron.
        _role("k8s/traefik", _DRIFT_ARM + "    state: present\n"),
        # The `stamp_deployed.yml` import dropped with no arm for its fragment.
        _role(
            "setup/k3s",
            _REAP_ARM.replace(f"    - {FRAGMENT_DIR}k3s-longhorn-reap\n", ""),
        ),
    ),
    green=(
        _role("k8s/traefik", _DRIFT_ARM + "    state: absent\n"),
        _role("setup/k3s", _REAP_ARM),
    ),
    must_find=frozenset({"ansible/roles/k8s/traefik", "ansible/roles/setup/k3s"}),
    min_matches=2,
)
