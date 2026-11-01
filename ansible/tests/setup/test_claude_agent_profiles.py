"""`claude_code_agents` builds one checked profile per agent user.

`filter_plugins/claude_agents.py` fills each entry's defaults and refuses a malformed one.
The primary agent's profile comes from the `claude_code_agent_*` scalars, so a host that lists
no further agent builds exactly what it built before.

Run: uv run pytest ansible/tests/setup/test_claude_agent_profiles.py
"""

import pytest
from _setup_render import CLAUDE_CODE, role_context
from ansible.errors import AnsibleFilterError
from claude_agents import PROFILE_FIELDS, claude_agent_profiles

ROLE_DIR = CLAUDE_CODE

PRIMARY = {
    "state": "present",
    "home": "/var/lib/claude",
    "clone_dir": "/var/lib/claude/server",
    "repo": "DanielH2018/server",
    "github_login": "DanielClaudeBot",
    "github_id": 338220904,
    "github_token_var": "claude_code_agent_gh_token",
    "worktree_prefix": "claude",
    "journal_access": True,
    "operator_read": True,
    "operator_config": True,
    "memory_seed": True,
    "dotfiles": True,
}
SECOND = {
    "name": "claude2",
    "github_login": "SecondBot",
    "github_id": 1,
    "github_token_var": "claude2_gh_token",
}


def test_the_default_list_is_the_primary_agent_built_from_the_scalars() -> None:
    context = role_context(ROLE_DIR, {"claude_code_agent_user_enabled": True})
    (profile,) = claude_agent_profiles(
        context["claude_code_agents"],
        context["claude_code_agent_user"],
        context["claude_code_agent_primary_profile"],
    )
    assert profile["name"] == "claude"
    assert profile["primary"] is True
    assert profile["state"] == "present"
    assert profile["home"] == context["claude_code_agent_user_home"]
    assert profile["github_token_var"] == "claude_code_agent_gh_token"
    assert set(profile) == PROFILE_FIELDS | {"primary"}


def test_a_switched_off_primary_is_absent() -> None:
    (profile,) = claude_agent_profiles(
        [{"name": "claude"}], "claude", PRIMARY | {"state": "absent"}
    )
    assert profile["state"] == "absent"


def test_a_further_agent_gets_its_own_paths_and_none_of_the_primarys_grants() -> None:
    _, second = claude_agent_profiles([{"name": "claude"}, SECOND], "claude", PRIMARY)
    assert second["primary"] is False
    assert second["home"] == "/var/lib/claude2"
    assert second["clone_dir"] == "/var/lib/claude2/server"
    assert second["worktree_prefix"] == "claude2"
    assert second["repo"] == PRIMARY["repo"]
    assert second["github_token_var"] == "claude2_gh_token"
    assert second["journal_access"] is False
    assert second["memory_seed"] is False


@pytest.mark.parametrize("field", ["github_login", "github_id", "github_token_var"])
def test_a_further_agent_never_inherits_an_identity(field: str) -> None:
    entry = {k: v for k, v in SECOND.items() if k != field}
    with pytest.raises(
        AnsibleFilterError, match=f"claude2 is present but sets no {field}"
    ):
        claude_agent_profiles([{"name": "claude"}, entry], "claude", PRIMARY)


def test_an_absent_further_agent_needs_no_identity() -> None:
    (profile,) = claude_agent_profiles(
        [{"name": "claude2", "state": "absent"}], "claude", PRIMARY
    )
    assert profile["state"] == "absent"


def test_the_homelab_ui_browser_is_the_primarys_alone() -> None:
    """The host package installs it from the claude_code_agent_* scalars, for the primary alone."""
    with pytest.raises(AnsibleFilterError, match="unknown fields \\['homelab_ui'\\]"):
        claude_agent_profiles([SECOND | {"homelab_ui": True}], "claude", PRIMARY)


def test_the_primary_entry_carries_only_its_name() -> None:
    with pytest.raises(AnsibleFilterError, match="set \\['home'\\] there"):
        claude_agent_profiles([{"name": "claude", "home": "/x"}], "claude", PRIMARY)


@pytest.mark.parametrize(
    ("agents", "message"),
    [
        ([SECOND | {"journal": True}], "unknown fields \\['journal'\\]"),
        ([SECOND | {"state": "gone"}], "state 'gone'"),
        ([SECOND | {"journal_access": "false"}], "journal_access to 'false'"),
        ([{"name": "Bad Name"}], "not a valid user name"),
        ([{"home": "/x"}], "needs a `name`"),
        ([SECOND, SECOND], "share the name"),
        (
            [SECOND, SECOND | {"name": "c3", "home": "/var/lib/claude2"}],
            "share the home",
        ),
        (
            [{"name": "claude"}, SECOND | {"worktree_prefix": "claude"}],
            "share the worktree_prefix",
        ),
    ],
)
def test_a_malformed_list_is_refused(agents: list, message: str) -> None:
    with pytest.raises(AnsibleFilterError, match=message):
        claude_agent_profiles(agents, "claude", PRIMARY)


def test_a_well_formed_pair_is_accepted() -> None:
    assert (
        len(claude_agent_profiles([{"name": "claude"}, SECOND], "claude", PRIMARY)) == 2
    )
