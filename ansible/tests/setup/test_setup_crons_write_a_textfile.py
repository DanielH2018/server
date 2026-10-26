"""Every setup-role cron and timer writes a homelab_check textfile, or is exempt with a reason.

A cron or timer that stops running pages nothing on its own. node_exporter's systemd collector
is off, so no `node_systemd_*` series exists to alert on, and cron has no unit state at all.
The `homelab_check_*` textfile of `homelab-check-lib.sh:write_check_textfile` is what a
`<Check>NotReporting` rule reads to tell a schedule that stopped from one that runs clean
(docs/target-architecture/monitoring/m3-textfile-timer.md). Until #6165 nothing required one,
and the textfile tests were fixed per-batch lists.

The subject is DERIVED from `tasks/` the way `test_setup_cron_roles_have_a_contract.py` derives
it: every `ansible.builtin.cron` task not `state: absent`, every `kuma_check_timer.yml` import
not literally absent, and every `templates/*.timer.j2`. A member writes a textfile when its
command, or the file a task installs at a path the command names, carries the producer
contract. `EXEMPT` names each member that writes none, with the reason. A reason starting
`GAP` is a schedule that changes state with no deadman at all, filed for later.

Run: uv run pytest ansible/tests/setup/test_setup_crons_write_a_textfile.py
"""

import re
from pathlib import Path

from _helpers import REPO, SETUP_ROLES, load_tasks, walk_tasks
from _role_census import role_dirs, role_task_files

# Either the shared helper, or a producer that writes the contract's series itself.
MARKERS = ("write_check_textfile", "homelab_check_last_run_timestamp_seconds")
KUMA_TIMER = "kuma_check_timer.yml"
_INSTALL_MODULES = ("ansible.builtin.copy", "ansible.builtin.template")

_KUMA = "pushes a Kuma heartbeat that pages a missed run; M6 moves it to a textfile"
_HC = "pings a Healthchecks.io check that pages a missed run"
_GAP = "GAP (#6179): changes the host with no deadman; a run that stops is silent"

EXEMPT: dict[str, str] = {
    "claude_code: timer `claude-cgroup-metrics`": (
        "samples cgroup counters into its own textfile every 30s for the dashboards; it "
        "changes no state, and a stopped sampler shows as a flat line, not a missed verdict"
    ),
    "claude_code: timer `claude-rc-restart`": (
        "a weekly try-restart whose miss only delays adopting an auto-update; a textfile rule "
        "would fire from the deploy until the first weekly run, up to a week of false pages"
    ),
    "fake_remux: cron `fake-remux scan`": (
        "fake-remux-health.sh reads this cron's state file and writes its verdict as the "
        "fake-remux-scan textfile"
    ),
    "fake_remux: cron `fake-remux reconcile`": (
        "fake-remux-health.sh reads this cron's state file and writes its verdict as the "
        "fake-remux-replace textfile"
    ),
    "fake_remux: cron `mkv attachment repair`": (
        "fake-remux-health.sh reads this cron's state file and writes its verdict as the "
        "mkv-attachment-repair textfile"
    ),
    "gitops_deploy: timer `gitops-deploy`": (
        "writes /var/lib/gitops-deploy/last_run every tick, which monitor-bridge's GitOps-Alive "
        "check reads, and gitops_tick_metrics.py counts each tick by outcome"
    ),
    "initial_setup: cron `Weekly apt autoremove`": _GAP,
    "initial_setup: cron `Weekly dpkg purge orphaned configs`": _GAP,
    "initial_setup: cron `Weekly firmware update`": _GAP,
    "initial_setup: cron `Weekly system restart`": _HC,
    "initial_setup: cron `Clean unused Docker images`": _HC,
    "k3s: cron `Longhorn filesystem trim`": (
        "touches a cron-fired stamp that longhorn-backup-health.sh reads into its own "
        "textfile verdict"
    ),
    "k3s: cron `B2 deletion accounting`": (
        "touches a cron-fired stamp that longhorn-backup-health.sh reads into its own "
        "textfile verdict"
    ),
    "k3s: cron `B2 backup budget listing`": (
        "a read-only listing of the bucket for the journal; it changes no state"
    ),
    "k3s: cron `etcd restore drill`": (
        "runs the drill with --list-only, a read-only listing of the snapshots; the full "
        "drill is the hypervisor role's cron"
    ),
    "k8s_host_files: cron `Registry garbage collection`": _KUMA,
    "render_records: timer `kuma-check-render-records`": (
        "render_records_state is pinned absent until R4 deletes the role, so the timer is "
        "never installed"
    ),
}


def _command_members(role: Path) -> dict[str, str]:
    """Member key -> the command text that runs it, for one role's crons and timers."""
    members: dict[str, str] = {}
    for tasks_file in role_task_files(role):
        for task in walk_tasks(load_tasks(tasks_file)):
            cron = task.get("ansible.builtin.cron")
            if isinstance(cron, dict) and cron.get("state") != "absent":
                members[f"{role.name}: cron `{cron.get('name')}`"] = str(
                    cron.get("job")
                )
            imported = task.get("ansible.builtin.import_tasks") or task.get(
                "ansible.builtin.include_tasks"
            )
            kuma = task.get("vars") or {}
            if KUMA_TIMER in str(imported) and kuma.get("kuma_check_state") != "absent":
                key = f"{role.name}: timer `kuma-check-{kuma.get('kuma_check_name')}`"
                members[key] = str(kuma.get("kuma_check_exec"))
    if role.name == "common":
        # common's kuma-check pair is the template every import above instantiates.
        return members
    for timer in sorted((role / "templates").glob("*.timer.j2")):
        stem = timer.name.removesuffix(".timer.j2")
        unit = re.search(r"^Unit=(\S+)\.service$", timer.read_text(), re.M)
        service = role / "templates" / f"{unit[1] if unit else stem}.service.j2"
        members[f"{role.name}: timer `{stem}`"] = (
            service.read_text() if service.is_file() else ""
        )
    return members


def _installed_sources(role: Path) -> dict[str, Path]:
    """Install `dest` -> the role file it is rendered or copied from."""
    sources = {}
    for tasks_file in role_task_files(role):
        for task in walk_tasks(load_tasks(tasks_file)):
            for module in _INSTALL_MODULES:
                args = task.get(module)
                if (
                    not isinstance(args, dict)
                    or "src" not in args
                    or "dest" not in args
                ):
                    continue
                src = str(args["src"]).replace("{{ role_path }}", str(role))
                for candidate in (
                    role / "templates" / src,
                    role / "files" / src,
                    REPO / src,
                    Path(src),
                ):
                    if candidate.is_file():
                        sources[str(args["dest"])] = candidate
                        break
    return sources


def _normalized(name: str) -> str:
    return name.removesuffix(".j2").replace("_", "-")


def _role_files(role: Path) -> list[Path]:
    return [
        p for d in ("templates", "files") for p in (role / d).glob("*") if p.is_file()
    ]


def _with_imports(text: str, role: Path) -> str:
    """The text plus every role template it imports a macro from or includes."""
    names = re.findall(r"{%-?\s*(?:from|import|include)\s+'([^']+)'", text)
    extra = [
        (role / "templates" / n).read_text()
        for n in names
        if (role / "templates" / n).is_file()
    ]
    return "\n".join([text, *extra])


def writes_a_textfile(command: str, role: Path, sources: dict[str, Path]) -> bool:
    """True when the command, or the role file it runs, writes a homelab_check file.

    The file it runs is the one a task installs at a path the command names, or, for an
    install a loop or a shared include writes, the role file whose name matches the basename
    of a path in the command (`live_drift_check.py` for `/usr/local/bin/live-drift-check.py`).
    """
    texts = [_with_imports(command, role)]
    texts += [src.read_text() for dest, src in sources.items() if dest in command]
    basenames = {
        _normalized(p.rsplit("/", 1)[1]) for p in re.findall(r"/[\w.@-]+", command)
    }
    texts += [
        p.read_text() for p in _role_files(role) if _normalized(p.name) in basenames
    ]
    return any(marker in text for text in texts for marker in MARKERS)


def census(roles_dir: Path = SETUP_ROLES) -> dict[str, bool]:
    """Member key -> whether it writes a textfile, over every setup role."""
    found = {}
    for role in role_dirs(roles_dir, skip_dotted=True):
        sources = _installed_sources(role)
        for key, command in _command_members(role).items():
            found[key] = writes_a_textfile(command, role, sources)
    return found


def problems(found: dict[str, bool], exempt: dict[str, str] = EXEMPT) -> list[str]:
    return [
        f"{key}: writes no homelab_check textfile, so a run that stops is silent. Call "
        f"write_check_textfile from homelab-check-lib.sh with a deadline derived from the "
        f"schedule, or add the member to EXEMPT with the reason it needs none"
        for key, writes in sorted(found.items())
        if not writes and key not in exempt
    ]


def test_every_setup_cron_and_timer_writes_a_textfile_or_is_exempt():
    assert not (found := problems(census())), "\n".join(found)


def test_every_exemption_names_a_member_that_still_writes_none():
    found = census()
    stale = [key for key in EXEMPT if found.get(key, True)]
    assert not stale, f"EXEMPT names members that are gone or write a textfile: {stale}"


def test_census_resolves_each_route_a_writer_takes():
    """Non-vacuity: a script cron, a renamed install, a Python kuma timer and a unit line."""
    found = census()
    writers = {
        "k3s: cron `Longhorn backup health`",
        "claude_code: cron `Claude Code telemetry health`",
        "k3s: timer `kuma-check-live-drift`",
        "claude_code: timer `claude-memory-sync`",
        "renovate_agent: timer `renovate-agent`",
    }
    assert {key for key in writers if found.get(key)} == writers
    assert found.get("initial_setup: cron `Weekly apt autoremove`") is False


# ── Fixture pairs ─────────────────────────────────────────────────────────────────────


def _role(tmp_path: Path, script: str) -> Path:
    role = tmp_path / "widget"
    (role / "tasks").mkdir(parents=True)
    (role / "templates").mkdir()
    (role / "templates" / "widget-check.sh.j2").write_text(script)
    (role / "tasks" / "main.yml").write_text(
        "- name: Install the script\n  ansible.builtin.template:\n"
        "    src: widget-check.sh.j2\n    dest: /usr/local/bin/widget-check.sh\n"
        "- name: Schedule it\n  ansible.builtin.cron:\n    name: widget check\n"
        "    job: /usr/local/bin/widget-check.sh 2>&1 | logger -t widget\n"
    )
    return role


def test_a_cron_whose_script_writes_no_textfile_is_flagged(tmp_path):
    _role(tmp_path, "#!/bin/sh\nlogger -t widget ran\n")
    [problem] = problems(census(tmp_path), exempt={})
    assert problem.startswith("widget: cron `widget check`: writes no homelab_check")


def test_a_cron_whose_installed_script_writes_a_textfile_passes(tmp_path):
    _role(tmp_path, '#!/bin/sh\nwrite_check_textfile widget 1 "$(date +%s)" 900\n')
    assert census(tmp_path) == {"widget: cron `widget check`": True}


def test_a_timer_whose_service_writes_no_textfile_is_flagged(tmp_path):
    role = _role(tmp_path, "#!/bin/sh\nwrite_check_textfile widget 1 0 900\n")
    (role / "templates" / "widget-sync.timer.j2").write_text(
        "[Timer]\nOnCalendar=daily\n"
    )
    (role / "templates" / "widget-sync.service.j2").write_text(
        "[Service]\nExecStart=/usr/bin/true\n"
    )
    assert problems(census(tmp_path), exempt={}) == [
        "widget: timer `widget-sync`: writes no homelab_check textfile, so a run that stops "
        "is silent. Call write_check_textfile from homelab-check-lib.sh with a deadline "
        "derived from the schedule, or add the member to EXEMPT with the reason it needs none"
    ]
