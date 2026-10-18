"""The flux-ownership census row of target slice H2.1: no flux-owned role keeps a host install.

Flux applies a flux-owned role's manifests and runs none of its tasks. A role that installed a
cron, a script or a host-lib copy and is then flipped leaves that install running with no
writer. The plan is `docs/target-architecture/host/h2-shrink-host-plane.md`, *The flux-ownership
census (the D5 coordination guard)*. The row sits in its own module because
`test_census_rows_roles.py`, which imports it into `ROWS`, is at the cap for a test module.

One subject per k8s `containers_list` entry: the entry's `owner` and the texts of its role's task
files. Only an `owner: flux` subject can offend, so every unflipped role passes and the row needs
no allowlist. H2's family PR for each of crowdsec, configarr, janitorr, qbittorrent, artifacts
and observability turns its installs into absent arms before D5 flips the role.

D5.1c's `HOST_WRITES` row (`_host_write_rows.py`) covers the same flips more strictly: it counts
every host write, absent arms included. This row counts only what a host keeps running.
"""

from _helpers import walk_tasks
from _host_write_rows import _destination
from _row_table import Subject, Census, tracked
from lib import yaml_fast
from lib.k8s_roles import k8s_entries

_K8S = "ansible/roles/k8s/"

# Where a copy or template installs something a host runs or schedules. `/etc/rancher/k3s/` is
# absent on purpose: it is kubectl staging for the Ansible apply path, which stops at the flip,
# and D5's task classes own those files.
_KEPT_PREFIXES = (
    "/usr/local/bin/",
    "/usr/local/lib/",
    "/opt/",
    "/etc/cron.d/",
    "/etc/systemd/",
)
# The shared imports that put a host-lib copy or a setup-deployed fragment on the host.
_KEPT_IMPORTS = ("install_host_lib.yml", "stamp_deployed.yml")
_IMPORTS = frozenset({"import_tasks", "include_tasks"})

# The six roles H2 moves before D5 flips them. Each must be read, unflipped, until its flip.
H2_ROLES = (
    "crowdsec",
    "configarr",
    "janitorr",
    "qbittorrent",
    "artifacts",
    "observability",
)


class RoleSubject(Subject):
    """One k8s entry's role: its `owner` and `{task file: text}`, read on first use."""

    def __init__(
        self, role: str, owner: str | None, tasks: dict[str, str] | None = None
    ):
        super().__init__(_K8S + role, text="")
        self.owner = owner
        self._tasks = tasks

    @property
    def tasks(self) -> dict[str, str]:
        if self._tasks is None:
            self._tasks = {
                rel: Subject(rel).text for rel in tracked(f"{self.rel}/tasks/*.yml")
            }
        return self._tasks


def _role_subjects() -> list[RoleSubject]:
    return [
        RoleSubject(entry.get("role") or name, entry.get("owner"))
        for name, entry in sorted(k8s_entries().items())
    ]


def _kept_installs(text: str) -> list[str]:
    """`<task name>: <what it installs>` for every task in `text` that a host keeps running."""
    found = []
    for task in walk_tasks(yaml_fast.safe_load(text) or []):
        name = task.get("name", "<unnamed>")
        for key, args in task.items():
            module = key.rsplit(".", 1)[-1]
            if module == "cron" and isinstance(args, dict):
                if args.get("state", "present") != "absent":
                    found.append(f"{name}: a present cron")
            elif module in ("copy", "template"):
                dest = _destination(args)
                if isinstance(dest, str) and dest.startswith(_KEPT_PREFIXES):
                    found.append(f"{name}: {module} to {dest}")
            elif module in _IMPORTS and str(args).endswith(_KEPT_IMPORTS):
                found.append(f"{name}: imports {str(args).rsplit('/', 1)[-1]}")
    return found


def kept_installs(subject: Subject) -> list[str]:
    """Every host install a flux-owned role keeps, as `<task file>: <task>: <what>`."""
    if not isinstance(subject, RoleSubject) or subject.owner != "flux":
        return []
    return [
        f"{rel.rsplit('/', 1)[-1]}: {hit}"
        for rel, text in sorted(subject.tasks.items())
        for hit in _kept_installs(text)
    ]


def _role(role: str, body: str, owner: str | None = "flux") -> RoleSubject:
    return RoleSubject(role, owner, {f"{_K8S}{role}/tasks/main.yml": body})


_HOME_ALLOWLIST_CRON = (
    "- name: Schedule the home allowlist refresh\n  ansible.builtin.cron:\n"
    '    name: "CrowdSec home allowlist"\n    user: root\n'
    "    cron_file: crowdsec-home-allowlist\n"
    '    job: "/usr/local/bin/crowdsec-update-home-allowlist.sh"\n'
)

FLUX_OWNERSHIP = Census(
    name="flux-owned-roles-keep-no-host-install",
    reason=(
        "Flux runs no task of a flux-owned role, so a cron, a script under /usr/local, /opt, "
        "/etc/cron.d or /etc/systemd, or a host-lib or setup-deployed import left in one keeps "
        "running on the host with nothing to update or remove it. Before the flip, turn the "
        "install into an absent arm (H2's family PR for the role), then let D5's conversion PR "
        "move the arm to `roles/setup/k8s_host_files/`. Registry's GC cron is the install outside "
        "H2 that would fail once flipped; pi-peer-backup's Pi wrapper moved at its flip."
    ),
    files=_role_subjects,
    offence=kept_installs,
    # A role with no tracked task file was never read, so it is not a member.
    count=lambda s: 1 if isinstance(s, RoleSubject) and s.tasks else 0,
    red=(
        # The named fixture: crowdsec flipped while its home allowlist is still a host cron.
        _role("crowdsec", _HOME_ALLOWLIST_CRON),
        _role(
            "pi-peer-backup",
            "- name: Install the forced-command wrapper on daniel-pi\n"
            "  ansible.builtin.copy:\n    src: pi-peer-backup-shell.sh\n"
            "    dest: /usr/local/bin/pi-peer-backup-shell\n  delegate_to: daniel-pi\n",
        ),
        _role(
            "janitorr",
            "- name: Record the deployed janitorr health reader code\n"
            '  ansible.builtin.import_tasks: "{{ role_path }}/../../setup/common/tasks/'
            'stamp_deployed.yml"\n  vars:\n    stamp_deployed_name: janitorr\n',
        ),
    ),
    green=(
        _role("crowdsec", _HOME_ALLOWLIST_CRON + "    state: absent\n"),
        # Unflipped: the same present cron is the role's own business until the flip.
        _role("crowdsec", _HOME_ALLOWLIST_CRON, owner=None),
        _role(
            "game-stats",
            "- name: Stage the stats script\n  ansible.builtin.copy:\n    src: stats.py\n"
            "    dest: /etc/rancher/k3s/manifests/game-stats/stats.py\n"
            "  delegate_to: daniel-box\n",
        ),
    ),
    # 58 k8s entries when the row landed (2026-10-16): the row reads every one of them.
    min_matches=len(k8s_entries()),
    must_find=frozenset(_K8S + role for role in H2_ROLES),
)
