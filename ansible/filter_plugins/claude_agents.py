"""Ansible filter plugin that turns `claude_code_agents` into one checked profile per agent.

The `claude_code` role builds each agent user from a profile. The primary agent, the one the
`claude_code_agent_*` scalars describe, is listed by name only and takes every value from those
scalars, because the lander, the peer ssh login, the homelab-ui browser, the artifacts mount
and claude-rc.service read the scalars too. Every further agent carries its own profile. The fields that give an agent an
identity (its GitHub account and the SOPS variable holding its token) have no default, so a
second agent can never inherit the primary's account by omission.

`roles/setup/claude_code/tasks/agent.yml` maps each profile field back onto the scalar the task
files read, so those files serve every agent unchanged.
"""

import re

from ansible.errors import AnsibleFilterError

# A field every agent's profile carries, and the default a further agent gets for it. None
# means the profile must set it. `home`, `clone_dir` and `worktree_prefix` derive from the name.
FURTHER_AGENT_DEFAULTS = {
    "state": "present",
    "github_login": None,
    "github_id": None,
    "github_token_var": None,
    "journal_access": False,
    "operator_read": True,
    "operator_config": True,
    "memory_seed": False,
    "dotfiles": False,
}
PROFILE_FIELDS = frozenset(FURTHER_AGENT_DEFAULTS) | {
    "name",
    "home",
    "clone_dir",
    "repo",
    "worktree_prefix",
}
BOOL_FIELDS = (
    "journal_access",
    "operator_read",
    "operator_config",
    "memory_seed",
    "dotfiles",
)
STATES = ("present", "absent")
# useradd's own rule for a portable name, without the trailing `$` it allows for machine accounts.
NAME = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")


def _fail(message):
    raise AnsibleFilterError(f"claude_code_agents: {message}")


def _further(entry, primary):
    unknown = sorted(set(entry) - PROFILE_FIELDS)
    if unknown:
        _fail(f"{entry['name']} sets unknown fields {unknown}")
    profile = dict(FURTHER_AGENT_DEFAULTS)
    profile["repo"] = primary["repo"]
    profile.update(entry)
    profile.setdefault("home", f"/var/lib/{entry['name']}")
    profile.setdefault("clone_dir", f"{profile['home']}/server")
    profile.setdefault("worktree_prefix", entry["name"])
    if profile["state"] == "present":
        required = ["github_login", "github_id", "github_token_var"]
        missing = [field for field in required if not profile[field]]
        if missing:
            _fail(f"{entry['name']} is present but sets no {', '.join(missing)}")
    profile["primary"] = False
    return profile


def claude_agent_profiles(agents, primary_name, primary):
    """Return one profile per entry of `agents`, in order, with every field filled.

    `primary` holds the primary agent's fields, rendered from the claude_code_agent_* scalars;
    its entry in `agents` may carry nothing but `name`. Raises AnsibleFilterError on an entry
    that is malformed, or on two agents that would share a name, home or worktree prefix.
    """
    if not isinstance(agents, list):
        _fail(f"must be a list, not {type(agents).__name__}")
    profiles = []
    for entry in agents:
        if not isinstance(entry, dict) or not entry.get("name"):
            _fail(f"every entry needs a `name`, and {entry!r} has none")
        if not NAME.match(str(entry["name"])):
            _fail(f"{entry['name']!r} is not a valid user name")
        if entry["name"] == primary_name:
            extra = sorted(set(entry) - {"name"})
            if extra:
                _fail(
                    f"{primary_name} is the primary agent, whose fields come from the "
                    f"claude_code_agent_* scalars; set {extra} there, not in its entry"
                )
            profile = dict(primary, name=primary_name, primary=True)
        else:
            profile = _further(entry, primary)
        if profile["state"] not in STATES:
            _fail(
                f"{profile['name']} has state {profile['state']!r}, not one of {STATES}"
            )
        # A `when:` on ansible-core 2.19+ refuses a string, so a quoted "false" must fail here,
        # where the message can name the agent and the field, not deep inside agent.yml.
        for field in BOOL_FIELDS:
            if not isinstance(profile[field], bool):
                _fail(
                    f"{profile['name']} sets {field} to {profile[field]!r}, not true or false"
                )
        profiles.append(profile)
    for field in ("name", "home", "worktree_prefix"):
        seen = [p[field] for p in profiles]
        dupes = sorted({value for value in seen if seen.count(value) > 1})
        if dupes:
            _fail(f"two agents share the {field} {dupes}")
    return profiles


class FilterModule:
    def filters(self):
        return {"claude_agent_profiles": claude_agent_profiles}
