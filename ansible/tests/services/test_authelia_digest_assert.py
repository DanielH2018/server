"""authelia's digest assert refuses a malformed stored digest, and skips only a stubbed run.

"Fail if a stored Authelia digest is malformed" in roles/k8s/authelia/tasks/main.yml requires
each managed user's digest to be the PHC form of the hashing block, and no two of them to be
equal. Authelia reads each digest's parameters back off its own PHC prefix, so a malformed one
deploys green and locks that user out. These tests evaluate the assert's real `vars:` and
`that:` through Ansible's own templar, because a wrong filter chain or a `$` that also matches
before a newline reads as a passing assert.

The guard is held too. A `k8s_dry_run` guard would skip the operator's pre-merge dry run, which
is the only run that proves the pattern accepts the live digests
(docs/target-architecture/render/r3-remove-live-reads.md, Verification).
"""

import base64

import pytest
from ansible.parsing.dataloader import DataLoader
from ansible.template import Templar, trust_as_template
from lib import yaml_fast

from _helpers import ROLES

_TASKS = ROLES / "k8s" / "authelia" / "tasks" / "main.yml"
_NAME = "Fail if a stored Authelia digest is malformed"
_PLACEHOLDER_TASK = (
    "Stand in a placeholder digest for each user a dry run could not hash"
)


def _digest(seed: int, m: int = 65536) -> str:
    """A well-formed argon2id digest: a 16-byte salt and a 32-byte key, unpadded base64."""

    def b64(n: int) -> str:
        return (
            base64.b64encode(bytes((seed + i) % 256 for i in range(n)))
            .decode()
            .rstrip("=")
        )

    return f"$argon2id$v=19$m={m},t=3,p=4${b64(16)}${b64(32)}"


OPERATOR, CLAUDE, AGENT = _digest(1), _digest(2), _digest(3)


def _task() -> dict:
    tasks = yaml_fast.safe_load(_TASKS.read_text())
    found = [t for t in tasks if t.get("name") == _NAME]
    # Non-vacuity: every test below reads this task, so its absence must fail by name.
    assert len(found) == 1, (
        f"expected one task named {_NAME!r} in {_TASKS}, found {len(found)}"
    )
    return found[0]


def _passes(
    operator: str | None = OPERATOR,
    claude: str | None = CLAUDE,
    agent: str | None = AGENT,
    manage_claude: bool = True,
    manage_agent: bool = True,
) -> bool:
    """Whether every `that:` clause of the real task holds on these digests.

    A `None` digest leaves that fact undefined, as a run that read nothing back would.
    """
    task = _task()
    facts = {
        "authelia_password_hash": operator,
        "authelia_claude_password_hash": claude,
        "authelia_agent_password_hash": agent,
    }
    variables: dict[str, object] = {k: v for k, v in facts.items() if v is not None}
    variables |= {
        "authelia_k8s_manage_claude_user": manage_claude,
        "authelia_k8s_manage_agent_user": manage_agent,
    }
    templar = Templar(loader=DataLoader(), variables=variables)
    task_vars = task["vars"]
    variables["authelia_k8s_digest_pattern"] = task_vars["authelia_k8s_digest_pattern"]
    variables["authelia_k8s_managed_digests"] = templar.template(
        trust_as_template(task_vars["authelia_k8s_managed_digests"])
    )
    templar = Templar(loader=DataLoader(), variables=variables)
    return all(
        templar.template(trust_as_template("{{ " + clause + " }}"))
        for clause in task["ansible.builtin.assert"]["that"]
    )


def test_well_formed_distinct_digests_pass():
    assert _passes()


@pytest.mark.parametrize(
    "operator",
    [
        pytest.param("", id="empty"),
        pytest.param(None, id="undefined"),
        pytest.param("Digest: " + OPERATOR, id="digest-prefix"),
        pytest.param(OPERATOR + "\n", id="trailing-newline"),
        pytest.param(OPERATOR + " ", id="trailing-space"),
        pytest.param("correct horse battery staple", id="plaintext"),
        pytest.param(_digest(1, m=1024), id="m-1024"),
        pytest.param(OPERATOR.replace("$argon2id$", "$argon2i$"), id="argon2i"),
    ],
)
def test_a_malformed_operator_digest_fails(operator):
    assert not _passes(operator=operator)


def test_a_malformed_managed_user_digest_fails():
    assert not _passes(agent="Digest: " + AGENT)


def test_two_users_sharing_one_digest_fail():
    assert not _passes(claude=OPERATOR)


def test_a_disabled_users_digest_is_not_checked():
    """The render leaves a disabled user out, so its value, even a stub, must not fail."""
    assert _passes(
        claude="stub-anything", agent=None, manage_claude=False, manage_agent=False
    )


def _dry_run_placeholders() -> dict[str, str]:
    """The digests the no-mutation placeholder task sets when no user's digest was read back."""
    tasks = yaml_fast.safe_load(_TASKS.read_text())
    found = [t for t in tasks if t.get("name") == _PLACEHOLDER_TASK]
    assert len(found) == 1, (
        f"expected one task named {_PLACEHOLDER_TASK!r}, found {len(found)}"
    )
    templar = Templar(loader=DataLoader(), variables={})
    return {
        name: templar.template(trust_as_template(expr))
        for name, expr in found[0]["ansible.builtin.set_fact"].items()
    }


def test_the_dry_run_placeholders_pass():
    """An unstubbed dry run that read no digest back must not stop at this assert (#4569)."""
    placeholders = _dry_run_placeholders()
    assert _passes(
        operator=placeholders["authelia_password_hash"],
        claude=placeholders["authelia_claude_password_hash"],
        agent=placeholders["authelia_agent_password_hash"],
    )


def test_the_assert_is_quiet_and_no_log():
    task = _task()
    assert task.get("no_log") is True
    assert task["ansible.builtin.assert"].get("quiet") is True


_SENTINEL = "not (homelab_secret_stubs | default(false) | bool)"


def _guarded_by_the_sentinel_alone(when: object) -> bool:
    """Whether a task's `when:` is the stub sentinel and nothing else.

    A stubbed run feeds `stub-` strings, so it must skip. Any further condition, such as
    `k8s_dry_run`, would also skip a run that holds the real digests and is meant to check them.
    """
    clauses = when if isinstance(when, list) else [when]
    return clauses == [_SENTINEL]


def test_the_sentinel_guard_is_clean():
    assert _guarded_by_the_sentinel_alone(_task().get("when"))


@pytest.mark.parametrize(
    "when",
    [
        pytest.param([_SENTINEL, "not (k8s_dry_run | bool)"], id="list-with-dry-run"),
        pytest.param(_SENTINEL + " and not (k8s_dry_run | bool)", id="and-dry-run"),
        pytest.param("not (k8s_no_mutate | bool)", id="no-mutate"),
        pytest.param(None, id="unguarded"),
    ],
)
def test_a_widened_or_missing_guard_is_flagged(when):
    assert not _guarded_by_the_sentinel_alone(when)
