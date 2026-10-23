"""The secret-key task census sees a task that fails on a stub value (#4850).

Two gaps let uptime-kuma's push-token assert stop the first shadow-render dry run behind a green
census. The derivation read only role defaults and inventory vars, so a `set_fact` over
`lookup('template', ...)` of a template that names a secret key derived nothing. And an `assert`
was credited as side-effect free, which answers whether a task sends a secret but not whether it
passes on a stub. These tests build refusing tasks nobody named, which the census must select.

Each synthetic role goes through `census(role, names)` with the secret key alone in `names`, so
the role's own template-derived names have to be found where the role directory is in hand.
These tests live beside the census rather than in it so its functions stay the fix's to change.
"""

import json
from pathlib import Path

import test_secret_key_task_census as census_mod

_SECRET_KEY = "alpha_token"
_TEMPLATE = "declarations.yaml.j2"

_READ_DECLARATIONS = {
    "name": "Read the declarations back as data",
    "no_log": True,
    "ansible.builtin.set_fact": {
        "widget_declarations": f"{{{{ lookup('template', '{_TEMPLATE}') | from_yaml }}}}"
    },
}


def _role(roles: Path, name: str, template: str, tasks: list[dict]) -> Path:
    role = roles / name
    (role / "templates").mkdir(parents=True)
    (role / "templates" / _TEMPLATE).write_text(template)
    (role / "tasks").mkdir()
    (role / "tasks" / "main.yml").write_text(json.dumps(tasks))
    return role


def _secret_role(roles: Path, *tasks: dict, name: str = "widget") -> Path:
    """A role whose template embeds the secret key and whose set_fact reads it back."""
    template = f'token: "{{{{ {_SECRET_KEY} }}}}"\n'
    return _role(roles, name, template, [_READ_DECLARATIONS, *tasks])


def _token_assert(**extra: object) -> dict:
    return {
        "name": "Check the token is well formed",
        "ansible.builtin.assert": {
            "that": "widget_declarations.token | length == 32",
            "fail_msg": "the token is not 32 characters",
        },
        **extra,
    }


def _run(
    role: Path, exempt: dict[str, str] | None = None
) -> tuple[list[str], list[str]]:
    """The census of `role`, with only the secret key named."""
    names = census_mod._word_pattern({_SECRET_KEY})
    return census_mod.census(role, names, exempt or {})


_ASSERT_ID = "widget/main.yml::Check the token is well formed"


def test_an_unguarded_assert_on_a_template_derived_fact_is_flagged(
    tmp_path: Path,
) -> None:
    role = _secret_role(tmp_path, _token_assert())
    _, offenders = _run(role)
    assert _ASSERT_ID in offenders


def test_a_template_naming_no_secret_key_derives_nothing(tmp_path: Path) -> None:
    # Separate roles directories: siblings share facts, and both roles set widget_declarations.
    secret = _secret_role(tmp_path / "a", _token_assert())
    plain = _role(
        tmp_path / "b",
        "gadget",
        'token: "{{ gadget_label }}"\n',
        [_READ_DECLARATIONS, _token_assert()],
    )
    assert _ASSERT_ID in _run(secret)[1]
    assert _run(plain)[1] == []


def test_an_unguarded_fail_on_a_template_derived_fact_is_flagged(
    tmp_path: Path,
) -> None:
    fail = {
        "name": "Refuse a malformed token",
        "ansible.builtin.fail": {"msg": "the token is not 32 characters"},
        "when": "widget_declarations.token | length != 32",
    }
    role = _secret_role(tmp_path, fail)
    _, offenders = _run(role)
    assert "widget/main.yml::Refuse a malformed token" in offenders


def test_an_unguarded_assert_naming_a_secret_key_directly_is_flagged(
    tmp_path: Path,
) -> None:
    """The assert credit alone, with no template derivation involved."""
    check = {
        "name": "Check the key is well formed",
        "ansible.builtin.assert": {"that": f"{_SECRET_KEY} | length == 32"},
    }
    role = _role(tmp_path, "widget", "plain: true\n", [check])
    _, offenders = _run(role)
    assert offenders == ["widget/main.yml::Check the key is well formed"]


def test_a_task_that_acts_on_a_template_derived_fact_is_flagged(tmp_path: Path) -> None:
    """The derivation alone: a uri task was never credited, but it named no secret key."""
    post = {
        "name": "Post the token",
        "ansible.builtin.uri": {
            "url": "https://example.invalid/?t={{ widget_declarations.token }}"
        },
    }
    role = _secret_role(tmp_path, post)
    _, offenders = _run(role)
    assert "widget/main.yml::Post the token" in offenders


def test_a_dry_run_guard_does_not_cover_a_stub_refusing_assert(tmp_path: Path) -> None:
    """An ordinary dry run holds the real values, so only the sentinel guard may skip it."""
    role = _secret_role(tmp_path, _token_assert(when="not (k8s_dry_run | bool)"))
    _, offenders = _run(role)
    assert _ASSERT_ID in offenders


def test_the_sentinel_guarded_assert_is_selected_and_clean(tmp_path: Path) -> None:
    role = _secret_role(
        tmp_path,
        _token_assert(when="not (homelab_secret_stubs | default(false) | bool)"),
    )
    selected, offenders = _run(role)
    assert _ASSERT_ID in selected
    assert _ASSERT_ID not in offenders


def test_an_exempted_assert_is_selected_and_not_flagged(tmp_path: Path) -> None:
    role = _secret_role(tmp_path, _token_assert())
    selected, offenders = _run(role, exempt={_ASSERT_ID: "passes on any stub value"})
    assert _ASSERT_ID in selected
    assert _ASSERT_ID not in offenders
