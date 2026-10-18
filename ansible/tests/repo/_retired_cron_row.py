"""The retired-cron census row of target slice H2.1: no H2 host cron or fragment loses its writer.

Only an absent arm removes a host cron or a setup-deployed fragment, and it runs from the role
that installed the thing. A PR that deletes the install task outright leaves the cron running
with no writer, and one that drops a `stamp_deployed.yml` import with no arm for its fragment
leaves `/var/lib/homelab/setup-deployed-manifest.d/<name>` behind, which turns setup drift DOWN.
The plan is `docs/target-architecture/host/h2-shrink-host-plane.md`, *Retirement by absent arm,
not a list*. The row sits in its own module because `test_census_rows_roles.py`, which imports
it into `ROWS`, is at the cap for a test module.

`H2_CRONS` and `H2_FRAGMENTS` hold every cron and fragment H2 retires or re-homes, from H2.1 on,
so a family PR that deletes a task without its arm fails here whether or not it edits this
file. Each name must keep a task in one of its roles: the live install, or the absent arm.
`RETIRED` holds the names already retired, for which only the absent arm passes. Each family PR
moves its names into `RETIRED` in the same commit as their arms, and H7, which deletes the
arms, deletes the entries. An arm may sit in the role's own task files or, once D5's conversion
PR has moved it, in `roles/setup/k8s_host_files/tasks/<role>.yml`.
"""

from _helpers import walk_tasks
from _row_table import Census, Subject, tracked
from lib import yaml_fast

FRAGMENT_DIR = "/var/lib/homelab/setup-deployed-manifest.d/"
_HOST_FILES = "ansible/roles/setup/k8s_host_files/tasks/"

# Cron task name -> the roles that may hold its task, as `<plane>/<role>`. H2.7 re-homes the
# two Claude crons into `setup/claude_code` under the same names, rather than retiring them.
# Registry's GC cron is D5's to retire, so it is not here.
H2_CRONS = {
    "Cloudflare IP drift": ("k8s/traefik",),
    "CrowdSec home allowlist": ("k8s/crowdsec",),
    "CrowdSec remote allowlist": ("k8s/crowdsec",),
    "CrowdSec bouncer prune": ("k8s/crowdsec",),
    "CrowdSec AppSec verify": ("k8s/crowdsec",),
    "configarr sync health": ("k8s/configarr",),
    "janitorr error health": ("k8s/janitorr",),
    "qbittorrent prefs drift check": ("k8s/qbittorrent",),
    "Sync peer Claude artifacts": ("k8s/artifacts", "setup/claude_code"),
    "Claude Code telemetry health": ("k8s/observability", "setup/claude_code"),
    "Longhorn filesystem trim": ("setup/k3s",),
    "Longhorn restore drill": ("setup/k3s",),
    "Off-box etcd snapshot": ("setup/k3s",),
}
# `stamp_deployed_name` -> the roles that may hold its import or its absent arm.
H2_FRAGMENTS = {
    "configarr": ("k8s/configarr",),
    "janitorr": ("k8s/janitorr",),
    "k3s-longhorn-reap": ("setup/k3s",),
}
# The names already retired: only a `state: absent` task passes for these.
RETIRED = frozenset(
    {"Cloudflare IP drift", "configarr sync health", "configarr", "k3s-longhorn-reap"}
)


class RetiredName(Subject):
    """One cron or fragment name, with `{task file: text}` of every file that may hold its task."""

    def __init__(
        self,
        kind: str,
        name: str,
        roles: tuple[str, ...],
        tasks: dict[str, str] | None = None,
    ):
        super().__init__(f"ansible/roles/{roles[0]}#{kind}:{name}", text="")
        self.kind, self.name, self.roles = kind, name, roles
        self._tasks = tasks

    @property
    def tasks(self) -> dict[str, str]:
        if self._tasks is None:
            specs = [f"ansible/roles/{role}/tasks/*.yml" for role in self.roles]
            specs += [f"{_HOST_FILES}{role.split('/')[1]}.yml" for role in self.roles]
            self._tasks = {rel: Subject(rel).text for rel in tracked(*specs)}
        return self._tasks


def _names() -> list[RetiredName]:
    return [RetiredName("cron", name, roles) for name, roles in H2_CRONS.items()] + [
        RetiredName("fragment", name, roles) for name, roles in H2_FRAGMENTS.items()
    ]


def _states(subject: RetiredName) -> set[str]:
    """`present` and/or `absent`: what the tasks naming `subject` do with it."""
    found = set()
    for text in subject.tasks.values():
        for task in walk_tasks(yaml_fast.safe_load(text) or []):
            for key, args in task.items():
                module = key.rsplit(".", 1)[-1]
                if subject.kind == "cron" and module == "cron":
                    if isinstance(args, dict) and args.get("name") == subject.name:
                        found.add(args.get("state", "present"))
                elif subject.kind == "fragment" and module == "file":
                    loop = task.get("loop") or task.get("with_items") or []
                    path = (args or {}).get("path") if isinstance(args, dict) else None
                    paths = loop if path == "{{ item }}" else [path]
                    if (
                        FRAGMENT_DIR + subject.name in paths
                        and args.get("state") == "absent"
                    ):
                        found.add("absent")
                elif subject.kind == "fragment" and module == "import_tasks":
                    stamp = (task.get("vars") or {}).get("stamp_deployed_name")
                    if (
                        str(args).endswith("stamp_deployed.yml")
                        and stamp == subject.name
                    ):
                        found.add("present")
    return found


def missing_arms(subject: Subject) -> list[str]:
    """Why `subject`'s cron or fragment has lost its writer, or [] when it has one."""
    if not isinstance(subject, RetiredName):
        return []
    states = _states(subject)
    if subject.name in RETIRED and "absent" not in states:
        return [f"retired {subject.kind} {subject.name!r} has no `state: absent` task"]
    if not states:
        install = "cron task" if subject.kind == "cron" else "stamp_deployed.yml import"
        return [
            f"{subject.kind} {subject.name!r} has neither its {install} nor an absent arm "
            f"in {', '.join(subject.roles)}"
        ]
    return []


def _subject(kind: str, name: str, role: str, body: str) -> RetiredName:
    return RetiredName(
        kind, name, (role,), {f"ansible/roles/{role}/tasks/main.yml": body}
    )


def _cron(name: str, state: str = "") -> str:
    return (
        f"- name: Schedule it\n  ansible.builtin.cron:\n    name: {name!r}\n"
        f"    cron_file: x\n{state}"
    )


_JANITORR_IMPORT = (
    "- name: Record the deployed janitorr health reader code\n"
    '  ansible.builtin.import_tasks: "{{ role_path }}/../../setup/common/tasks/'
    'stamp_deployed.yml"\n  vars:\n    stamp_deployed_name: janitorr\n'
)
_REAP_ARM = (
    "- name: Retire the host-installed Longhorn reapers\n  ansible.builtin.file:\n"
    '    path: "{{ item }}"\n    state: absent\n  loop:\n'
    f"    - /opt/longhorn-reap\n    - {FRAGMENT_DIR}k3s-longhorn-reap\n"
)
_UNRELATED = "- name: Unrelated\n  ansible.builtin.debug:\n    msg: x\n"
_HOME = "CrowdSec home allowlist"

RETIRED_CRON_ARMS = Census(
    name="h2-host-crons-keep-a-writer",
    reason=(
        "A host cron or setup-deployed fragment is removed only by a `state: absent` task in "
        "the role that installed it, which runs in that role's own deploy or hand apply. "
        "Delete the install task without an arm and the cron keeps running with no writer; "
        "drop a `stamp_deployed.yml` import without one and the stale fragment turns setup "
        "drift DOWN. Turn the task into an absent arm, move the name into RETIRED, and keep "
        "the arm until H7 deletes it with the entry."
    ),
    files=_names,
    offence=missing_arms,
    # A name whose roles hold no tracked task file was never read, so it is not a member.
    count=lambda s: 1 if isinstance(s, RetiredName) and s.tasks else 0,
    red=(
        # A family PR that deletes crowdsec's home allowlist cron rather than flipping it.
        _subject("cron", _HOME, "k8s/crowdsec", _UNRELATED),
        # A retired cron whose arm went back to installing it.
        _subject(
            "cron",
            "Cloudflare IP drift",
            "k8s/traefik",
            _cron("Cloudflare IP drift", "    state: present\n"),
        ),
        # janitorr's import dropped with no arm for its fragment.
        _subject("fragment", "janitorr", "k8s/janitorr", _UNRELATED),
        # The reaper retirement's arm with the fragment's path gone from its loop.
        _subject(
            "fragment",
            "k3s-longhorn-reap",
            "setup/k3s",
            _REAP_ARM.replace(f"    - {FRAGMENT_DIR}k3s-longhorn-reap\n", ""),
        ),
    ),
    green=(
        _subject("cron", _HOME, "k8s/crowdsec", _cron(_HOME)),
        _subject("cron", _HOME, "k8s/crowdsec", _cron(_HOME, "    state: absent\n")),
        _subject("fragment", "janitorr", "k8s/janitorr", _JANITORR_IMPORT),
        _subject(
            "fragment",
            "janitorr",
            "k8s/janitorr",
            "- name: Remove the fragment\n  ansible.builtin.file:\n"
            f"    path: {FRAGMENT_DIR}janitorr\n    state: absent\n",
        ),
        _subject("fragment", "k3s-longhorn-reap", "setup/k3s", _REAP_ARM),
    ),
    min_matches=len(H2_CRONS) + len(H2_FRAGMENTS),
    must_find=frozenset(
        {
            "ansible/roles/k8s/traefik#cron:Cloudflare IP drift",
            "ansible/roles/setup/k3s#fragment:k3s-longhorn-reap",
        }
    ),
)
