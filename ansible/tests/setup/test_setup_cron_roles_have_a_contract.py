"""Every ansible/roles/setup/ role whose cron or timer changes state carries an
`## Autonomous-role contract` section in its CLAUDE.md.

Root CLAUDE.md's "Where to Look" table routes "adding / changing a cron that changes state"
to "that role's CLAUDE.md *Autonomous-role contract*", so every role whose cron changes
state needs that heading. That every setup role HAS a CLAUDE.md is the setup row of
`ansible/tests/repo/test_role_claude_md.py`.

The subject is DERIVED from `tasks/` (an `ansible.builtin.cron` task not `state: absent`, or a
`templates/*.timer.j2` unit) rather than listed, so a new cron-installing role joins the check
the day it lands. `EXEMPT` names the roles whose crons change no state, each with the reason —
a heartbeat that only pushes Kuma is not an autonomous actor, and demanding a contract of it
would teach readers the heading means nothing.

Red-proof pairs use fixture roles under `tmp_path`; non-vacuity pins named members the live
census must contain, so a renamed `tasks/` layout fails loudly rather than checking nothing.
"""

from pathlib import Path

from _helpers import SETUP_ROLES, load_tasks, walk_tasks
from _role_census import role_dirs

CONTRACT_HEADING = "## Autonomous-role contract"

# Roles whose cron/timer changes no state: it reads, then pushes a heartbeat or a notification.
# Each reason is the thing to re-check before keeping the role here.
EXEMPT: dict[str, str] = {
    "renovate_notify": (
        "reads the open Renovate PRs from GitHub and posts to Discord; it merges, closes and "
        "edits nothing — `renovate_agent` is the role that acts, and it carries the contract"
    ),
    "nut_host": (
        "`ups-secondary-health.sh` reads upsmon.conf, `systemctl is-active nut-monitor` and "
        "`upsc ups.status`, then pushes a Kuma heartbeat; the actor that powers a host off is "
        "the `nut-monitor` systemd service, not this cron"
    ),
    "common": (
        "`templates/kuma-check.timer.j2` is the shared unit pair `tasks/kuma_check_timer.yml` "
        "renders for an importing role; it has no schedule or check of its own, and the role "
        "that imports it owns the check the timer reruns and carries the contract for it"
    ),
}


def _role_dirs(roles_dir: Path = SETUP_ROLES) -> list[Path]:
    """Every role under `roles_dir`, a dotted directory and a retired role's shell skipped.

    A shell is a directory holding only a gitignored `__pycache__/` after the deployer's
    fast-forward removed the role's tracked files; reading it as a role would raise
    `FileNotFoundError` on its missing `CLAUDE.md`.
    """
    return [d for d in role_dirs(roles_dir) if not d.name.startswith(".")]


def _installs_a_cron_or_timer(role_dir: Path) -> bool:
    """True if any task file schedules a cron (not `state: absent`) or the role ships a timer.

    A cron task whose `state` is a Jinja expression (`present if armed else absent`) counts:
    the role CAN install it, and the contract governs what it does when armed.
    """
    for tasks_file in sorted((role_dir / "tasks").glob("*.yml")):
        for task in walk_tasks(load_tasks(tasks_file)):
            cron = task.get("ansible.builtin.cron")
            if isinstance(cron, dict) and cron.get("state") != "absent":
                return True
    return any((role_dir / "templates").glob("*.timer.j2"))


def _has_contract_heading(role_dir: Path) -> bool:
    doc = role_dir / "CLAUDE.md"
    if not doc.is_file():
        return False
    return any(
        line.startswith(CONTRACT_HEADING) for line in doc.read_text().splitlines()
    )


def _contract_problems(role_dir: Path, exempt: dict[str, str] = EXEMPT) -> list[str]:
    """Problems with the contract requirement for one role ([] when it is met or not owed)."""
    if not _installs_a_cron_or_timer(role_dir):
        return []
    if role_dir.name in exempt:
        return []
    if _has_contract_heading(role_dir):
        return []
    return [
        f"{role_dir.name}: installs a cron or timer but its CLAUDE.md has no "
        f"`{CONTRACT_HEADING}` section (root CLAUDE.md routes state-changing crons there; "
        f"add the section, or add the role to EXEMPT with the reason its cron changes no state)"
    ]


def test_every_setup_role_with_a_state_changing_cron_has_the_contract():
    problems = []
    for role_dir in _role_dirs():
        problems.extend(_contract_problems(role_dir))
    assert not problems, "\n".join(problems)


def test_exempt_roles_still_install_a_cron_or_timer():
    """An exemption for a role that no longer schedules anything is dead text — and a role
    that gained a state-changing cron under a stale exemption would be silently waved through.
    """
    stale = [
        name for name in EXEMPT if not _installs_a_cron_or_timer(SETUP_ROLES / name)
    ]
    assert not stale, f"EXEMPT names roles with no cron or timer: {stale}"


def test_census_finds_the_roles_known_to_install_crons():
    """Non-vacuity, per CLAUDE.md's "a check that finds its own subject by pattern" rule."""
    found = {d.name for d in _role_dirs() if _installs_a_cron_or_timer(d)}
    expected = {"gitops_deploy", "renovate_agent", "k3s", "fake_remux", "initial_setup"}
    assert expected <= found, f"missing from the cron/timer census: {expected - found}"


def test_optimize_pi_is_not_exempt():
    """Its container-recovery cron runs `docker start` on any watched container it finds down,
    so it is an autonomous actor and owes the contract. An exemption claiming only that the crons
    delete nothing and change no service configuration is narrower than "changes no state" — an
    argument this guard exists to refuse.
    """
    assert "optimize_pi" not in EXEMPT
    assert _contract_problems(SETUP_ROLES / "optimize_pi") == []


def test_docker_install_is_out_of_the_subject_not_exempt():
    """Its only cron task is the teardown's `state: absent` removal arm; a role that installs
    nothing owes no contract and must not need an EXEMPT entry to pass.
    """
    assert not _installs_a_cron_or_timer(SETUP_ROLES / "docker_install")
    assert "docker_install" not in EXEMPT


# ── Fixture pairs ─────────────────────────────────────────────────────────────────────


def _fixture_role(tmp_path, name: str, *, cron: bool, doc: str | None) -> Path:
    role = tmp_path / name
    (role / "tasks").mkdir(parents=True)
    body = (
        "- name: Schedule it\n  ansible.builtin.cron:\n    name: it\n    job: /bin/true\n"
        if cron
        else "- name: Copy a file\n  ansible.builtin.copy:\n    src: a\n    dest: b\n"
    )
    (role / "tasks" / "main.yml").write_text(body)
    if doc is not None:
        (role / "CLAUDE.md").write_text(doc)
    return role


_LONG_ENOUGH = "# widget\n\n" + "\n".join(f"- line {i}" for i in range(8)) + "\n"


def test_fixture_cron_role_without_the_heading_is_flagged(tmp_path):
    role = _fixture_role(tmp_path, "widget", cron=True, doc=_LONG_ENOUGH)
    problems = _contract_problems(role, exempt={})
    assert len(problems) == 1
    assert problems[0].startswith("widget: installs a cron or timer")


def test_fixture_cron_role_with_the_heading_passes(tmp_path):
    role = _fixture_role(
        tmp_path,
        "widget",
        cron=True,
        doc=_LONG_ENOUGH + f"\n{CONTRACT_HEADING} (what it may do)\n- Scope: x\n",
    )
    assert _contract_problems(role, exempt={}) == []


def test_fixture_role_without_a_cron_owes_no_contract(tmp_path):
    role = _fixture_role(tmp_path, "widget", cron=False, doc=_LONG_ENOUGH)
    assert _contract_problems(role, exempt={}) == []


def test_fixture_exempt_cron_role_passes_without_the_heading(tmp_path):
    role = _fixture_role(tmp_path, "widget", cron=True, doc=_LONG_ENOUGH)
    assert _contract_problems(role, exempt={"widget": "pushes a heartbeat only"}) == []


def test_fixture_absent_cron_does_not_count_as_installing(tmp_path):
    role = tmp_path / "widget"
    (role / "tasks").mkdir(parents=True)
    (role / "tasks" / "main.yml").write_text(
        "- name: Remove it\n  ansible.builtin.cron:\n    name: it\n    state: absent\n"
    )
    assert not _installs_a_cron_or_timer(role)


def test_fixture_timer_template_counts_as_installing(tmp_path):
    role = tmp_path / "widget"
    (role / "tasks").mkdir(parents=True)
    (role / "templates").mkdir()
    (role / "tasks" / "main.yml").write_text(
        "- name: Nothing\n  ansible.builtin.debug:\n"
    )
    (role / "templates" / "widget.timer.j2").write_text("[Timer]\nOnCalendar=daily\n")
    assert _installs_a_cron_or_timer(role)
