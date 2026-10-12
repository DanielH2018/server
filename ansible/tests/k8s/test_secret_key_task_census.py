"""Every k8s task that sends a secret value is skipped under a stubbed dry run.

A shadow render (target slice R1) runs `deploy.sh --dry-run` with a stub file, so every
secret key holds a placeholder. A task that hands a secret to a live system would send that
placeholder: sonarr's API reads drew a 401 that stopped the play, and authelia's hashing pods
would hash a stub. This census reads every task in `ansible/roles/k8s/*/tasks/*.yml` whose
body names a secret key, or a variable derived from one, and requires one of four things of it:

- a `k8s_dry_run`, `k8s_no_mutate` or `homelab_secret_stubs` guard on its `when:`, on an
  enclosing block's, or at the include that pulled its file in;
- a side-effect-free module: `set_fact`, `assert`, `debug` or an include;
- an entry in `EXEMPT`, keyed by task, with a reason.

A guard counts only negated (`not k8s_no_mutate`), because `when: k8s_dry_run | bool` runs the
task under a dry run alone. It is read from the parsed `when:`, so a guard word in a command
or a comment is not credited.

The exemptions are per task, which is why this is its own module: a `Census` row's `allow` map
is keyed by file and would exempt all of authelia's `tasks/main.yml`.
"""

import json
import re
from collections.abc import Iterator, Mapping
from pathlib import Path

from lib import yaml_fast
from lib.repo_paths import ALL_VARS, HOST_VARS
from secret_stubs import homelab_stub_keys

from _helpers import ANSIBLE
from _k8s_guards import K8S_ROLES, _guard_covered_files
from _role_census import role_dirs, role_task_files

SECRETS = ANSIBLE / "vars" / "secrets.yml"

_STUB_GUARD = re.compile(
    r"\bnot\s*\(?\s*(?:k8s_no_mutate|k8s_dry_run|homelab_secret_stubs)\b"
)

_SIDE_EFFECT_FREE = {
    module
    for name in (
        "set_fact",
        "assert",
        "debug",
        "include_tasks",
        "import_tasks",
        "include_role",
        "import_role",
    )
    for module in (name, f"ansible.builtin.{name}")
}

# "<role>/<task file>::<task name>" -> why the task may act under a stub file.
# Empty: authelia's three hashing tasks, the plan's only exemptions, gained a
# `not (k8s_no_mutate | bool)` guard in 5883ef610 and pass on that.
EXEMPT: dict[str, str] = {}

# Must be selected, because it names `sonarr_api_key`, and must pass on its guard.
MUST_FIND_GUARDED = "sonarr/verify.yml::Read the cluster copy's root folders"
# The one derived variable the plan's baseline measured.
MUST_DERIVE = "headlamp_k8s_oidc_issuer_url"


def _var_files() -> list[Path]:
    return [
        *sorted(K8S_ROLES.glob("*/defaults/main.yml")),
        *sorted(ALL_VARS.parent.glob("*.yml")),
        *sorted(HOST_VARS.glob("*.yml")),
    ]


def _word_pattern(names: set[str]) -> re.Pattern[str]:
    return re.compile(r"\b(?:" + "|".join(sorted(map(re.escape, names))) + r")\b")


# A value references a variable only inside a Jinja expression. A bare word does not count:
# headlamp's scopes string "profile,email,groups" holds the secret key name `email`.
_JINJA = re.compile(r"\{\{.*?\}\}|\{%.*?%\}", re.DOTALL)


def derived_variables(secret_keys: set[str], var_files: list[Path]) -> set[str]:
    """The variables in `var_files` whose value references a secret key, transitively."""
    values: dict[str, str] = {}
    for path in var_files:
        for name, value in (yaml_fast.safe_load(path.read_text()) or {}).items():
            expressions = " ".join(_JINJA.findall(json.dumps(value)))
            values[str(name)] = values.get(str(name), "") + " " + expressions
    derived: set[str] = set()
    while True:
        pattern = _word_pattern(secret_keys | derived)
        found = {
            name
            for name, text in values.items()
            if name not in secret_keys and name not in derived and pattern.search(text)
        }
        if not found:
            return derived
        derived |= found


def _when_text(task: dict) -> str:
    when = task.get("when", "")
    return " and ".join(map(str, when)) if isinstance(when, list) else str(when)


def _tasks(tasks: object, guarded: bool = False) -> Iterator[tuple[dict, bool]]:
    """Each task with whether it or an enclosing block carries a stub guard."""
    if not isinstance(tasks, list):
        return
    for task in tasks:
        if not isinstance(task, dict):
            continue
        here = guarded or bool(_STUB_GUARD.search(_when_text(task)))
        yield task, here
        for key in ("block", "rescue", "always"):
            yield from _tasks(task.get(key), here)


def _body(task: dict) -> str:
    """The task's text minus its name and its nested tasks, which are checked on their own."""
    rest = {
        k: v for k, v in task.items() if k not in ("name", "block", "rescue", "always")
    }
    return json.dumps(rest, default=str)


def census(
    role: Path, names: re.Pattern[str], exempt: Mapping[str, str] = EXEMPT
) -> tuple[list[str], list[str]]:
    """Every task in `role` whose body matches `names`, and those of them that fail all four tests.

    Returns:
        The selected task ids, then the offending ones, each `<role>/<file>::<task name>`.
    """
    covered = _guard_covered_files(role, guard=_STUB_GUARD)
    selected, offenders = [], []
    for task_file in role_task_files(role):
        for task, guarded in _tasks(yaml_fast.safe_load(task_file.read_text())):
            if not names.search(_body(task)):
                continue
            key = f"{role.name}/{task_file.name}::{task.get('name')}"
            selected.append(key)
            if guarded or task_file.name in covered or key in exempt:
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


def test_the_derivation_finds_the_measured_derived_variable() -> None:
    keys = set(homelab_stub_keys(yaml_fast.safe_load(SECRETS.read_text())))
    assert MUST_DERIVE in derived_variables(keys, _var_files())


def stale_exemptions(exempt: Mapping[str, str], offenders: list[str]) -> list[str]:
    """The exempted task ids that the census, run with no exemptions, does not flag."""
    return sorted(set(exempt) - set(offenders))


def test_every_exemption_names_a_task_the_census_would_flag() -> None:
    """A stale exemption would hide the removal of a guard the task later gains."""
    _, offenders = _live_census(exempt={})
    assert stale_exemptions(EXEMPT, offenders) == []


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
