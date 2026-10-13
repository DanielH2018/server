"""The secret-key census follows a secret value past the names a task's body states (#4862).

`test_secret_key_task_census.py` selects a task whose body names a secret key or a fact derived
from one in the same role. A value also travels through a `register:` and through a play-scoped
fact that one role sets and another reads. These check that the census selects both, so an
assert on such a value needs no entry in `STUB_REFUSING` to be held to the sentinel guard.
"""

from pathlib import Path

from _secret_derivation import _word_pattern
from test_secret_key_task_census import _live_census, census

# The two sonarr asserts STUB_REFUSING names because no derivation followed their register.
SONARR_REGISTER_ASSERTS = (
    "sonarr/verify.yml::Fail if a root folder does not resolve inside the pod",
    "sonarr/verify.yml::Fail if the cluster library is not intact",
)


def test_the_census_selects_sonarrs_register_asserts() -> None:
    selected, _ = _live_census()
    missing = [
        task_id for task_id in SONARR_REGISTER_ASSERTS if task_id not in selected
    ]
    assert missing == [], (
        f"the census does not follow the secret read's register into {missing}"
    )


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
