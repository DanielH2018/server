"""The secret-key census follows a secret value past the names a task's body states (#4862).

`test_secret_key_task_census.py` selects a task whose body names a secret key or a fact derived
from one in the same role. A value also travels through a `register:` and through a play-scoped
fact that one role sets and another reads. These check that the census selects both, so an
assert on such a value needs no entry in `STUB_REFUSING` to be held to the sentinel guard.
"""

from pathlib import Path

from _secret_derivation import _word_pattern
from test_secret_key_task_census import census

# No live task reads the register of a secret read, so the register half has fixture members
# only (the `_register_role` cases below). sonarr's two register asserts held a live place until
# sonarr's Flux flip (D5.20) deleted them, and arr-notification's seed report until no role
# included arr-notification any more (#5561).


def test_a_fact_set_in_one_role_and_read_in_another_is_flagged(tmp_path: Path) -> None:
    # Two sibling roles under one roles directory, as the census reads the real tree.
    files = {
        "producer": """
- name: Encode the token
  ansible.builtin.set_fact:
    widget_encoded: "{{ alpha_token | b64encode }}"
""",
        "consumer": """
- name: Post the encoded token
  ansible.builtin.uri:
    url: "https://example.invalid/?t={{ widget_encoded }}"
""",
    }
    for role, body in files.items():
        (tmp_path / role / "tasks").mkdir(parents=True)
        (tmp_path / role / "tasks/main.yml").write_text(body)
    _, offenders = census(
        tmp_path / "consumer", _word_pattern({"alpha_token"}), exempt={}
    )
    assert offenders == ["consumer/main.yml::Post the encoded token"]


def _register_role(tmp_path: Path, read: str) -> Path:
    """A role whose first task registers `widget_read` doing `read`, then asserts on it."""
    role = tmp_path / "widget"
    (role / "tasks").mkdir(parents=True)
    (role / "tasks/main.yml").write_text(
        f"""
- name: Read
{read}
  register: widget_read
- name: Fail if the read is empty
  ansible.builtin.assert:
    that: widget_read.stdout | length > 0
"""
    )
    return role


def test_a_secret_name_in_a_registered_tasks_plain_text_derives_nothing(
    tmp_path: Path,
) -> None:
    # pihole's gravity SQL holds the word `domain`, a secret key's name, outside any Jinja. The
    # census still selects the command itself, but its register carries nothing.
    role = _register_role(
        tmp_path,
        "  ansible.builtin.command:\n    cmd: sqlite3 db 'SELECT alpha_token FROM t'",
    )
    selected, _ = census(role, _word_pattern({"alpha_token"}), exempt={})
    assert selected == ["widget/main.yml::Read"]


def test_a_secret_name_in_a_registered_tasks_until_is_derived(tmp_path: Path) -> None:
    role = _register_role(
        tmp_path,
        "  ansible.builtin.command:\n    cmd: date\n  until: alpha_token in widget_read.stdout",
    )
    _, offenders = census(role, _word_pattern({"alpha_token"}), exempt={})
    assert "widget/main.yml::Fail if the read is empty" in offenders


def test_an_edit_to_the_tree_reaches_the_next_census(tmp_path: Path) -> None:
    # The tree-wide derivation is cached; an edited task file must miss the cache.
    role = _register_role(tmp_path, "  ansible.builtin.command:\n    cmd: date")
    names = _word_pattern({"alpha_token"})
    assert census(role, names, exempt={})[1] == []
    main = role / "tasks/main.yml"
    main.write_text(
        main.read_text().replace("cmd: date", 'cmd: "echo {{ alpha_token }}"')
    )
    assert (
        "widget/main.yml::Fail if the read is empty"
        in census(role, names, exempt={})[1]
    )
