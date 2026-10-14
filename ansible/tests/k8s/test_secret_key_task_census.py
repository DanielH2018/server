"""Every k8s task that sends a secret value is skipped under a stubbed dry run.

A shadow render (target slice R1) runs `deploy.sh --dry-run` with a stub file, so every
secret key holds a placeholder. A task that hands a secret to a live system would send that
placeholder: sonarr's API reads drew a 401 that stopped the play, and authelia's hashing pods
would hash a stub. This census reads every task in `ansible/roles/k8s/*/tasks/*.yml` whose
body names a secret key, or a variable derived from one, and requires one of four things of it:

- a `k8s_dry_run`, `k8s_no_mutate` or `homelab_secret_stubs` guard on its `when:`, on an
  enclosing block's, or at the include that pulled its file in;
- a side-effect-free module: `set_fact`, `debug` or an include;
- an entry in `EXEMPT`, keyed by task, with a reason.

An `assert` or a `fail` is held to a stricter guard. It sends nothing, but it can refuse a stub
value, so only the `homelab_secret_stubs` sentinel credits it: an ordinary dry run holds the real
values and is meant to check them. An exemption for one says why it passes on any stub value.

A derived variable comes from two places. `derived_variables` reads role defaults and inventory
vars. `role_derived_facts` reads the `set_fact` and `register:` tasks of every role beside this
one, since both are play-scoped: a name whose task names a secret-bearing name, or looks up a role
template whose Jinja names one, is derived too. That is how uptime-kuma's `kuma_declarations`, a
parse of a template embedding every push token, reaches the push-token assert (#4850), and how
sonarr's asserts on its API reads' registers are selected (#4862).

A guard counts only negated (`not k8s_no_mutate`), because `when: k8s_dry_run | bool` runs the
task under a dry run alone. It is read from the parsed `when:`, so a guard word in a command
or a comment is not credited.

The exemptions are per task, which is why this is its own module: a `Census` row's `allow` map
is keyed by file and would exempt all of authelia's `tasks/main.yml`.

A second check covers the tasks a stub makes fail rather than act. `STUB_REFUSING` names each,
and each must carry the `homelab_secret_stubs` guard itself. It predates the assert rule above,
which selects every task it names, so it backs up a derivation that regresses.
"""

import json
import re
from collections.abc import Iterable, Mapping
from pathlib import Path

from lib import yaml_fast
from lib.repo_paths import ALL_VARS, HOST_VARS
from secret_stubs import homelab_stub_keys

from _helpers import ANSIBLE
from _k8s_guards import K8S_ROLES, _guard_covered_files
from _role_census import role_dirs, role_task_files
from _secret_derivation import (
    _STUB_GUARD,
    _body,
    _tasks,
    _word_pattern,
    derived_variables,
    role_derived_facts,
)

SECRETS = ANSIBLE / "vars" / "secrets.yml"

# The sentinel alone. A `not k8s_dry_run` guard would also skip a stub-refusing task on an
# ordinary dry run, which holds the real values and is meant to check them.
_SENTINEL_GUARD = re.compile(r"\bnot\s*\(?\s*homelab_secret_stubs\b")

# Send nothing, but fail on a value that does not check out: a stub is such a value.
_STUB_REFUSING_MODULES = {
    module
    for name in ("assert", "fail")
    for module in (name, f"ansible.builtin.{name}")
}

_SIDE_EFFECT_FREE = {
    module
    for name in (
        "set_fact",
        "debug",
        "include_tasks",
        "import_tasks",
        "include_role",
        "import_role",
    )
    for module in (name, f"ansible.builtin.{name}")
}

# "<role>/<task file>::<task name>" -> why the task may act under a stub file. For an `assert` or
# `fail`, why it passes on any stub value. A stub is `homelab_secret_stub`'s label, never empty.
# authelia's three hashing tasks, the plan's only exemptions, gained a
# `not (k8s_no_mutate | bool)` guard in 5883ef610 and pass on that.
EXEMPT: dict[str, str] = {
    "authelia/main.yml::Fail if the claude-ui credentials are missing": (
        "checks only that each value is non-empty, and a stub is never empty"
    ),
    "authelia/main.yml::Fail if the claude-agent credential is missing": (
        "checks only that the value is non-empty, and a stub is never empty"
    ),
    "uptime-kuma/main.yml::Check the declaration index is populated": (
        "counts the parsed declarations, and a stub token changes no count"
    ),
}

# Must be selected, because it names `sonarr_api_key`, and must pass on its guard.
MUST_FIND_GUARDED = "sonarr/verify.yml::Read the cluster copy's root folders"
# No role default or inventory variable derives from a secret key since `domain` was published,
# so `derived_variables` has no real-tree member. Its fixture pair is
# `test_a_task_naming_a_derived_variable_is_flagged`.


def _var_files() -> list[Path]:
    return [
        *sorted(K8S_ROLES.glob("*/defaults/main.yml")),
        *sorted(ALL_VARS.parent.glob("*.yml")),
        *sorted(HOST_VARS.glob("*.yml")),
    ]


def census(
    role: Path, names: re.Pattern[str], exempt: Mapping[str, str] = EXEMPT
) -> tuple[list[str], list[str]]:
    """Every task in `role` whose body names a secret value, and those not covered.

    `names` is widened by `role_derived_facts`. An `assert` or `fail` is covered only by the
    sentinel guard or an exemption; any other task by a stub guard, a side-effect-free module or
    an exemption.

    Returns:
        The selected task ids, then the offending ones, each `<role>/<file>::<task name>`.
    """
    facts = role_derived_facts(role, names)
    if facts:
        names = re.compile(f"{names.pattern}|{_word_pattern(facts).pattern}")
    covered = _guard_covered_files(role, guard=_STUB_GUARD)
    sentinel_covered = _guard_covered_files(role, guard=_SENTINEL_GUARD)
    selected, offenders = [], []
    for task_file in role_task_files(role):
        tasks = yaml_fast.safe_load(task_file.read_text())
        for (task, guarded), (_, sentinel) in zip(
            _tasks(tasks), _tasks(tasks, guard=_SENTINEL_GUARD), strict=True
        ):
            if not names.search(_body(task)):
                continue
            key = f"{role.name}/{task_file.name}::{task.get('name')}"
            selected.append(key)
            if key in exempt:
                continue
            if _STUB_REFUSING_MODULES & task.keys():
                if not (sentinel or task_file.name in sentinel_covered):
                    offenders.append(key)
                continue
            if guarded or task_file.name in covered:
                continue
            if _SIDE_EFFECT_FREE & task.keys():
                continue
            offenders.append(key)
    return selected, offenders


def _live_names() -> re.Pattern[str]:
    keys = set(homelab_stub_keys(yaml_fast.safe_load(SECRETS.read_text())))
    return _word_pattern(keys | derived_variables(keys, _var_files()))


def _live_census(exempt: Mapping[str, str] = EXEMPT) -> tuple[list[str], list[str]]:
    names = _live_names()
    selected, offenders = [], []
    for role in role_dirs():
        s, o = census(role, names, exempt)
        selected += s
        offenders += o
    return selected, offenders


def test_every_task_that_names_a_secret_key_is_skipped_under_stubs() -> None:
    selected, offenders = _live_census()
    assert MUST_FIND_GUARDED in selected, (
        f"the census no longer selects {MUST_FIND_GUARDED!r}, so it may be selecting nothing"
    )
    assert offenders == [], (
        "these tasks name a secret key and would act on a stub under a shadow dry run. Guard "
        "each on `not (homelab_secret_stubs | default(false) | bool)` (or `not k8s_no_mutate` "
        f"if no dry run may run it), or exempt it in EXEMPT with a reason: {offenders}"
    )


def stale_exemptions(exempt: Mapping[str, str], offenders: list[str]) -> list[str]:
    """The exempted task ids that the census, run with no exemptions, does not flag."""
    return sorted(set(exempt) - set(offenders))


def test_every_exemption_names_a_task_the_census_would_flag() -> None:
    """A stale exemption would hide the removal of a guard the task later gains."""
    _, offenders = _live_census(exempt={})
    assert stale_exemptions(EXEMPT, offenders) == []


# "<role>/<task file>::<task name>" of each task that FAILS on a stub value. sonarr's two asserts
# on its API reads' registers left this list once the census followed a register (#4862).
STUB_REFUSING = (
    "uptime-kuma/main.yml::Check every push token is in the format AutoKuma accepts",
)


def stub_refusers_without_the_guard(roles: Path, task_ids: Iterable[str]) -> list[str]:
    """The ids in `task_ids` that a stubbed run would still evaluate.

    An id is `<role>/<task file>::<task name>`, resolved under `roles`. The sentinel guard
    counts on the task, on an enclosing block, or at the include that pulled its file in. An id
    that names no task is returned too, so a rename cannot empty the check.
    """
    flagged = []
    for task_id in task_ids:
        location, name = task_id.split("::", 1)
        role_name, file_name = location.split("/", 1)
        role = roles / role_name
        task_file = role / "tasks" / file_name
        tasks = (
            yaml_fast.safe_load(task_file.read_text()) if task_file.is_file() else []
        )
        guarded = [
            here
            for task, here in _tasks(tasks, guard=_SENTINEL_GUARD)
            if task.get("name") == name
        ]
        covered = file_name in _guard_covered_files(role, guard=_SENTINEL_GUARD)
        if not guarded:
            flagged.append(f"{task_id} (no such task)")
        elif not (covered or all(guarded)):
            flagged.append(task_id)
    return flagged


def test_every_stub_refusing_task_is_skipped_under_stubs() -> None:
    # fact: ansible/roles/k8s/uptime-kuma/CLAUDE.md#Traps
    assert stub_refusers_without_the_guard(K8S_ROLES, STUB_REFUSING) == [], (
        "these tasks fail on a stub value, so a shadow dry run stops at them. Give each "
        "`when: not (homelab_secret_stubs | default(false) | bool)`, never a k8s_dry_run guard"
    )


_PUSH_TOKEN_CHECK = "Check every push token is in the format AutoKuma accepts"


def _push_token_role(tmp_path: Path, when: str | None) -> Path:
    """A role holding uptime-kuma's real push-token assert, with `when` replacing its guard."""
    tasks = yaml_fast.safe_load((K8S_ROLES / "uptime-kuma/tasks/main.yml").read_text())
    task = {
        k: v
        for k, v in next(t for t in tasks if t.get("name") == _PUSH_TOKEN_CHECK).items()
        if k != "when"
    }
    if when is not None:
        task["when"] = when
    return _role(tmp_path, **{"tasks/main.yml": json.dumps([task])})


def test_the_guarded_push_token_check_is_clean(tmp_path: Path) -> None:
    role = _push_token_role(
        tmp_path, "not (homelab_secret_stubs | default(false) | bool)"
    )
    assert (
        stub_refusers_without_the_guard(
            role.parent, [f"widget/main.yml::{_PUSH_TOKEN_CHECK}"]
        )
        == []
    )


def test_the_unguarded_push_token_check_is_flagged(tmp_path: Path) -> None:
    role = _push_token_role(tmp_path, None)
    task_id = f"widget/main.yml::{_PUSH_TOKEN_CHECK}"
    assert stub_refusers_without_the_guard(role.parent, [task_id]) == [task_id]


def test_a_dry_run_guard_on_a_stub_refusing_task_is_flagged(tmp_path: Path) -> None:
    role = _push_token_role(tmp_path, "not k8s_dry_run | bool")
    task_id = f"widget/main.yml::{_PUSH_TOKEN_CHECK}"
    assert stub_refusers_without_the_guard(role.parent, [task_id]) == [task_id]


def test_a_stub_refusing_id_that_names_no_task_is_flagged(tmp_path: Path) -> None:
    role = _push_token_role(
        tmp_path, "not (homelab_secret_stubs | default(false) | bool)"
    )
    assert stub_refusers_without_the_guard(role.parent, ["widget/main.yml::Gone"]) == [
        "widget/main.yml::Gone (no such task)"
    ]


def _role(tmp_path: Path, **files: str) -> Path:
    role = tmp_path / "widget"
    for name, body in files.items():
        path = role / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
    return role


def _sonarr_root_folder_read(drop_when: bool) -> list[dict]:
    tasks = yaml_fast.safe_load((K8S_ROLES / "sonarr/tasks/verify.yml").read_text())
    task = next(
        t for t in tasks if t.get("name") == "Read the cluster copy's root folders"
    )
    if drop_when:
        task = {k: v for k, v in task.items() if k != "when"}
    return [task]


def test_the_guarded_sonarr_read_is_clean(tmp_path: Path) -> None:
    role = _role(
        tmp_path, **{"tasks/main.yml": json.dumps(_sonarr_root_folder_read(False))}
    )
    assert census(role, _word_pattern({"sonarr_api_key"})) == (
        ["widget/main.yml::Read the cluster copy's root folders"],
        [],
    )


def test_the_unguarded_sonarr_read_is_flagged(tmp_path: Path) -> None:
    role = _role(
        tmp_path, **{"tasks/main.yml": json.dumps(_sonarr_root_folder_read(True))}
    )
    _, offenders = census(role, _word_pattern({"sonarr_api_key"}))
    assert offenders == ["widget/main.yml::Read the cluster copy's root folders"]


def test_a_positive_dry_run_condition_is_flagged(tmp_path: Path) -> None:
    role = _role(
        tmp_path,
        **{
            "tasks/main.yml": """
- name: Post the token
  ansible.builtin.uri:
    url: "https://example.invalid/?t={{ alpha_token }}"
  when: k8s_dry_run | bool
"""
        },
    )
    _, offenders = census(role, _word_pattern({"alpha_token"}))
    assert offenders == ["widget/main.yml::Post the token"]


def test_a_task_naming_a_derived_variable_is_flagged(tmp_path: Path) -> None:
    role = _role(
        tmp_path,
        **{
            "defaults/main.yml": (
                'widget_url: "https://{{ widget_host }}"\n'
                'widget_host: "x.{{ domain }}"\n'
                'widget_scopes: "profile,domain"\n'
            ),
            "tasks/main.yml": """
- name: Call the derived URL
  ansible.builtin.uri:
    url: "{{ widget_url }}"
""",
        },
    )
    derived = derived_variables({"domain"}, [role / "defaults/main.yml"])
    assert derived == {"widget_url", "widget_host"}
    _, offenders = census(role, _word_pattern({"domain"} | derived))
    assert offenders == ["widget/main.yml::Call the derived URL"]


def test_a_stub_guard_at_the_include_site_covers_the_file(tmp_path: Path) -> None:
    acting = """
- name: Post the token
  ansible.builtin.uri:
    url: "https://example.invalid/?t={{ alpha_token }}"
"""
    guarded = _role(
        tmp_path / "guarded",
        **{
            "tasks/main.yml": """
- name: Verify
  ansible.builtin.include_tasks: verify.yml
  when: not (homelab_secret_stubs | default(false) | bool)
""",
            "tasks/verify.yml": acting,
        },
    )
    bare = _role(
        tmp_path / "bare",
        **{
            "tasks/main.yml": "- name: Verify\n  ansible.builtin.include_tasks: verify.yml\n",
            "tasks/verify.yml": acting,
        },
    )
    names = _word_pattern({"alpha_token"})
    assert census(guarded, names)[1] == []
    assert census(bare, names)[1] == ["widget/verify.yml::Post the token"]


def test_an_exemption_for_a_guarded_or_missing_task_is_stale(tmp_path: Path) -> None:
    role = _role(
        tmp_path,
        **{
            "tasks/main.yml": """
- name: Post the token
  ansible.builtin.uri:
    url: "https://example.invalid/?t={{ alpha_token }}"
- name: Post the token when not stubbed
  ansible.builtin.uri:
    url: "https://example.invalid/?t={{ alpha_token }}"
  when: not (homelab_secret_stubs | default(false) | bool)
"""
        },
    )
    _, offenders = census(role, _word_pattern({"alpha_token"}), exempt={})
    exempt = {
        "widget/main.yml::Post the token": "acts",
        "widget/main.yml::Post the token when not stubbed": "guarded",
        "widget/main.yml::Gone": "absent",
    }
    assert stale_exemptions(exempt, offenders) == [
        "widget/main.yml::Gone",
        "widget/main.yml::Post the token when not stubbed",
    ]


def test_a_double_quoted_template_lookup_derives_its_fact(tmp_path: Path) -> None:
    role = _role(
        tmp_path,
        **{
            "templates/t.j2": 'token: "{{ alpha_token }}"\n',
            "tasks/main.yml": """
- name: Read the template
  ansible.builtin.set_fact:
    decl: '{{ lookup("template", "t.j2") | from_yaml }}'
- name: Check the token
  ansible.builtin.assert:
    that: decl.token | length == 32
""",
        },
    )
    _, offenders = census(role, _word_pattern({"alpha_token"}))
    assert offenders == ["widget/main.yml::Check the token"]


def test_a_template_naming_a_derived_fact_is_secret_bearing(tmp_path: Path) -> None:
    role = _role(
        tmp_path,
        **{
            "templates/t.j2": 'token: "{{ tok }}"\n',
            "tasks/main.yml": """
- name: Encode the token
  ansible.builtin.set_fact:
    tok: "{{ alpha_token | b64encode }}"
- name: Read the template
  ansible.builtin.set_fact:
    decl: "{{ lookup('template', 't.j2') | from_yaml }}"
- name: Check the token
  ansible.builtin.assert:
    that: decl.token | length == 32
""",
        },
    )
    _, offenders = census(role, _word_pattern({"alpha_token"}))
    assert offenders == ["widget/main.yml::Check the token"]


def test_an_assert_on_a_register_a_secret_read_fills_is_flagged(tmp_path: Path) -> None:
    # The assert names no secret key itself; only the register carries the value (#4862).
    role = _role(
        tmp_path,
        **{
            "tasks/main.yml": """
- name: Read with the token
  ansible.builtin.uri:
    url: "https://example.invalid/?t={{ alpha_token }}"
  register: widget_read
  when: not (homelab_secret_stubs | default(false) | bool)
- name: Fail if the read is empty
  ansible.builtin.assert:
    that: widget_read.json | length > 0
"""
        },
    )
    _, offenders = census(role, _word_pattern({"alpha_token"}), exempt={})
    assert offenders == ["widget/main.yml::Fail if the read is empty"]
