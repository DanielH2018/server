#!/usr/bin/env python3
"""The watchdog and upsmon templates have one source, and daniel-box's render does not move (H4.3b3).

Until H7.8 moves daniel-box, the `nut_host` role owns `host-upsmon.conf.j2` and
`ups-secondary-health.sh.j2`, and daniel-server's host package renders those same two files
(`docs/target-architecture/host/h4-secrets-unit.md`, *NUT on daniel-server*). Two properties
keep that safe:

ONE SOURCE. A copy of either template under `hosts/` forks the shutdown chain's config. The
copy would pass every test that reads the role's file while the package shipped the copy.

THE ANSIBLE-ERA BRANCH IS UNTOUCHED. The packaged branch lives in `{% if %}` blocks inside the
shared template. A block that adds a blank line or an indent changes the file the tick writes
on daniel-box, which is the host that carries the real upsmon. The spec compares that render
with its render at the merge base. A permanent test cannot do that, because a later PR may
change the template on purpose. It compares the render with the render of the same source with
every packaged arm deleted, which differs only by what the blocks themselves add.

Run: uv run pytest ansible/tests/setup/test_nut_host_one_template_source.py
"""

import re
from pathlib import Path

from _helpers import ANSIBLE
from _setup_render import role_context
from lib.ansible_jinja_env import template_env
from lib.render_guard import render_or_error
from lib.repo_paths import REPO

ROLE = ANSIBLE / "roles" / "setup" / "nut_host"
TEMPLATES = ROLE / "templates"
TASKS = (ROLE / "tasks" / "main.yml").read_text()
# The two templates daniel-server's package renders from the role's directory.
SHARED = ("host-upsmon.conf.j2", "ups-secondary-health.sh.j2")
# `{% if nut_host_watchdog_packaged %}` ... `{% else %}` ... `{% endif %}`, each tag alone on
# its line. A tag with other text beside it is not a packaged arm this check can delete.
_ARM = re.compile(
    r"^[ \t]*\{%-? *if +nut_host_watchdog_packaged[^%]*%\}[ \t]*\n"
    r"(?P<packaged>.*?)"
    r"^[ \t]*\{%-? *else *-?%\}[ \t]*\n"
    r"(?P<ansible>.*?)"
    r"^[ \t]*\{%-? *endif *-?%\}[ \t]*\n",
    re.DOTALL | re.MULTILINE,
)
WATCHDOG_ARMS = 3


def role_template_names(tasks_text: str) -> set[str]:
    """Every `.j2` file the role's tasks name as a `src:`."""
    return set(re.findall(r"^\s*src:\s*(\S+\.j2)\s*$", tasks_text, re.MULTILINE))


def second_sources(hosts_root: Path) -> list[Path]:
    """Files under `hosts_root` that share a name with one of the role's shared templates."""
    return sorted(
        path for name in SHARED for path in hosts_root.rglob(name) if path.is_file()
    )


def ansible_arm_only(source: str) -> str:
    """`source` with every packaged arm and its three tags deleted, as if the branch never existed."""
    return _ARM.sub(r"\g<ansible>", source)


def render_text(source: str, name: str, ctx: dict, directory: Path) -> str:
    """`source` rendered as template `name` beside the role's other templates."""
    directory.mkdir(parents=True, exist_ok=True)
    (directory / name).write_text(source)
    text, err = render_or_error(template_env(directory), name, ctx)
    assert text is not None, f"{name}: {err}"
    return text


def branch_whitespace_problems(
    source: str, name: str, ctx: dict, scratch: Path
) -> list[str]:
    """What the packaged blocks add to the Ansible-era render, [] when they add nothing."""
    with_blocks = render_text(source, name, ctx, scratch / "with")
    without = render_text(ansible_arm_only(source), name, ctx, scratch / "without")
    if with_blocks == without:
        return []
    return [
        f"{name}: the Ansible-era render differs once the packaged arms are deleted"
    ]


def test_the_role_renders_both_shared_templates():
    assert set(SHARED) <= role_template_names(TASKS)
    for name in SHARED:
        assert (TEMPLATES / name).is_file()


def test_no_host_package_file_forks_a_shared_template():
    assert second_sources(REPO / "hosts") == []


def test_a_copy_of_a_shared_template_under_hosts_is_flagged(tmp_path):
    copy = tmp_path / "components" / "nut-secondary" / "templates" / SHARED[1]
    copy.parent.mkdir(parents=True)
    copy.write_text("#!/usr/bin/env bash\n")
    assert second_sources(tmp_path) == [copy]


def test_a_template_the_role_stopped_rendering_is_flagged():
    without_watchdog = TASKS.replace("src: ups-secondary-health.sh.j2", "src: other.j2")
    assert not set(SHARED) <= role_template_names(without_watchdog)


def test_the_watchdog_carries_its_packaged_arms():
    """The census must find its subject: three arms, so a renamed flag cannot empty it."""
    source = (TEMPLATES / SHARED[1]).read_text()
    assert len(_ARM.findall(source)) == WATCHDOG_ARMS
    assert ansible_arm_only(source) != source


def test_the_packaged_blocks_add_nothing_to_daniel_boxs_render(tmp_path):
    ctx = role_context(ROLE, {"inventory_hostname": "daniel-box"})
    for name in SHARED:
        source = (TEMPLATES / name).read_text()
        assert branch_whitespace_problems(source, name, ctx, tmp_path / name) == []


def test_a_block_that_indents_its_endif_is_flagged(tmp_path):
    ctx = role_context(ROLE, {"inventory_hostname": "daniel-box"})
    source = (
        (TEMPLATES / SHARED[1]).read_text().replace("{% endif %}", "  {% endif %}", 1)
    )
    assert branch_whitespace_problems(source, SHARED[1], ctx, tmp_path) == [
        f"{SHARED[1]}: the Ansible-era render differs once the packaged arms are deleted"
    ]


def test_the_default_renders_the_ansible_era_branch_on_both_hosts():
    for host in ("daniel-box", "daniel-server"):
        ctx = role_context(ROLE, {"inventory_hostname": host})
        assert ctx["nut_host_watchdog_packaged"] is False
