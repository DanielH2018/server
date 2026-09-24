"""Every ansible/roles/setup/ role has a CLAUDE.md, and every one whose cron or timer changes
state carries an `## Autonomous-role contract` section.

Written for issue #1854. Root CLAUDE.md's "Where to Look" table routes "adding / changing a
cron that changes state" to "that role's CLAUDE.md *Autonomous-role contract*", and on
2026-09-17 only `initial_setup` (and `k8s/autofix-bridge`) had the heading — `gitops_deploy`
and `renovate_agent`, the two most state-changing crons in the tree, did not, and `k3s`
(seven `files/*.py`, a dozen crons) had no CLAUDE.md at all. `test_k8s_roles_have_claude_md.py`
covers `roles/k8s/` only.

Two guards, one subject:

1. Existence, for every setup role. The k8s test's `_check_role_doc` is NOT reused: its
   deploy-tag arm assumes the `containers_list` convention where the tag is the role's
   directory name, and setup roles are applied through `initial_setup.yml` with tags that do
   not follow it (`common` is include-only and has no tag at all). Existence plus a minimum
   length is what transfers.

2. The contract heading, for the roles that install a cron or timer. The subject is DERIVED
   from `tasks/` (an `ansible.builtin.cron` task not `state: absent`, or a `templates/*.timer.j2`
   unit) rather than listed, so a new cron-installing role joins the check the day it lands.
   `EXEMPT` names the roles whose crons change no state, each with the reason — a heartbeat
   that only pushes Kuma is not an autonomous actor, and demanding a contract of it would
   teach readers the heading means nothing.

Red-proof pairs use fixture roles under `tmp_path`; non-vacuity pins named members the live
census must contain, so a renamed `tasks/` layout fails loudly rather than checking nothing.

3. A size ceiling (issue #2126). `gitops_deploy/CLAUDE.md` had grown to 1315 lines (~27k
   tokens) of postmortem before its record moved to `docs/gitops-pipeline.md`, and a role doc is
   loaded whole on every touch of the role. A doc over `MAX_LINES` (`wc -l` lines, the issue's
   verify-by) fails unless `OVER_CEILING` names the role with the reason, and an entry there for
   a doc that has since shrunk fails too. Same shape as the k8s test's ceiling.
"""

from pathlib import Path

from _helpers import SETUP_ROLES, load_tasks, walk_tasks

CONTRACT_HEADING = "## Autonomous-role contract"
MIN_NON_BLANK_LINES = 8
MAX_LINES = 400

# Roles whose CLAUDE.md is allowed over MAX_LINES, each with the reason. An entry is a
# justification, not a waiver: name what in the doc is an operating rule that cannot move to a
# docs/ page, or split the file instead.
OVER_CEILING: dict[str, str] = {
    "gitops_deploy": (
        "404 lines on 2026-09-24. It sat at exactly 400 and #2348 added a fourth broad-plane "
        "rule — that a broad range deploys the promoted image bumps riding on it — which is "
        "an operating rule, not history: its record is already in docs/gitops-pipeline.md. "
        "Trim two lines of an existing rule before adding to it."
    ),
    "hypervisor": (
        "411 lines on 2026-09-21, predating the ceiling; the staging-guest lifecycle it "
        "documents has no docs/ page of its own yet. Trim or split it before adding to it."
    ),
}

# Roles whose cron/timer changes no state: it reads, then pushes a heartbeat or a notification.
# Each reason is the thing to re-check before keeping the role here.
EXEMPT: dict[str, str] = {
    "optimize_pi": (
        "both crons (`Pi SD-card health heartbeat`, `Pi container-recovery heartbeat`) read the "
        "Pi and push Kuma; neither restarts, deletes or writes anything on the host"
    ),
    "claude_code": (
        "`claude-cgroup-metrics.timer` reads cgroup counters; `claude-rc-restart.timer` is a "
        "weekly `systemctl try-restart` to adopt an auto-update, which starts nothing that was "
        "down and creates nothing"
    ),
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
    return sorted(
        d for d in roles_dir.iterdir() if d.is_dir() and not d.name.startswith(".")
    )


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


def _non_blank_lines(text: str) -> list[str]:
    return [line for line in text.splitlines() if line.strip()]


def _line_count(text: str) -> int:
    """What `wc -l` reports: newline characters, so a trailing newline is not a line."""
    return text.count("\n")


def _doc_problems(
    role_dir: Path, over_ceiling: dict[str, str] = OVER_CEILING
) -> list[str]:
    doc = role_dir / "CLAUDE.md"
    if not doc.is_file():
        return [f"{role_dir.name}: no CLAUDE.md"]
    text = doc.read_text()
    if len(_non_blank_lines(text)) < MIN_NON_BLANK_LINES:
        return [
            f"{role_dir.name}: CLAUDE.md has fewer than {MIN_NON_BLANK_LINES} non-blank lines"
        ]
    lines = _line_count(text)
    if lines > MAX_LINES and role_dir.name not in over_ceiling:
        return [
            f"{role_dir.name}: CLAUDE.md is {lines} lines (wc -l), over the {MAX_LINES}-line "
            f"ceiling — move history and measurements to a docs/ page, or add the role to "
            f"OVER_CEILING with the reason (issue #2126)"
        ]
    return []


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


def test_every_setup_role_has_an_adequate_claude_md():
    problems = []
    for role_dir in _role_dirs():
        problems.extend(_doc_problems(role_dir))
    assert not problems, "\n".join(problems)


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


def test_census_sees_at_least_twelve_setup_roles():
    n = len(_role_dirs())
    assert n >= 12, f"only found {n} setup role directories under {SETUP_ROLES}"


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


def test_fixture_role_with_no_doc_is_flagged(tmp_path):
    role = _fixture_role(tmp_path, "widget", cron=False, doc=None)
    assert _doc_problems(role) == ["widget: no CLAUDE.md"]


def test_fixture_role_with_a_short_doc_is_flagged(tmp_path):
    role = _fixture_role(tmp_path, "widget", cron=False, doc="# widget\n")
    assert _doc_problems(role) == ["widget: CLAUDE.md has fewer than 8 non-blank lines"]


def test_fixture_role_with_an_adequate_doc_passes(tmp_path):
    role = _fixture_role(tmp_path, "widget", cron=False, doc=_LONG_ENOUGH)
    assert _doc_problems(role) == []


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


def _sized_doc(lines: int) -> str:
    """A widget doc padded to exactly `lines` lines (wc -l)."""
    return "# widget\n\n" + "".join(f"- line {i}\n" for i in range(lines - 2))


def test_fixture_role_over_the_ceiling_is_flagged(tmp_path):
    role = _fixture_role(tmp_path, "widget", cron=False, doc=_sized_doc(MAX_LINES + 1))
    problems = _doc_problems(role, over_ceiling={})
    assert len(problems) == 1 and problems[0].startswith(
        f"widget: CLAUDE.md is {MAX_LINES + 1} lines (wc -l), over the {MAX_LINES}-line ceiling"
    ), problems


def test_fixture_role_at_the_ceiling_passes(tmp_path):
    role = _fixture_role(tmp_path, "widget", cron=False, doc=_sized_doc(MAX_LINES))
    assert _doc_problems(role, over_ceiling={}) == []


def test_fixture_role_over_the_ceiling_passes_when_justified(tmp_path):
    role = _fixture_role(tmp_path, "widget", cron=False, doc=_sized_doc(MAX_LINES + 1))
    assert _doc_problems(role, over_ceiling={"widget": "a reason"}) == []


def test_over_ceiling_entries_are_still_over_the_ceiling():
    """A justification for a doc that has since shrunk is dead text, and would wave a regrowth
    through; an entry for a role that no longer exists is the same rot.
    """
    stale = [
        name
        for name in OVER_CEILING
        if not (SETUP_ROLES / name / "CLAUDE.md").is_file()
        or _line_count((SETUP_ROLES / name / "CLAUDE.md").read_text()) <= MAX_LINES
    ]
    assert not stale, (
        f"OVER_CEILING names docs no longer over {MAX_LINES} lines: {stale}"
    )


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
