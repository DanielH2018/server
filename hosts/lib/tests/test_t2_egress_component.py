"""The t2-egress ruleset: it touches only its own table, and it fences exactly the T2 session users.

nft itself checks the syntax on the host, in the loader's ExecStartPre=, because a check here
would need CAP_NET_ADMIN.
"""

import re

from lib import yaml_fast
from lib.repo_paths import REPO

RULESET = REPO / "hosts/components/t2-egress/files/t2-egress.nft"
SESSION_USERS = {
    "renovate-agent": REPO / "hosts/components/renovate-agent/component.yml",
    "claude-triage": REPO / "hosts/daniel-box/package.yml",
}
OWN_TABLE = "inet t2_egress"


def statements(text: str) -> list[str]:
    return [
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def foreign_tables(text: str) -> list[str]:
    """Each statement that names a table other than `inet t2_egress`, or names none."""
    found = []
    for statement in statements(text):
        match = re.match(
            r"(?:add|flush|delete) (?:table|chain|set|rule) (\S+ \S+)", statement
        )
        if not match or match.group(1) != OWN_TABLE:
            found.append(statement)
    return found


def test_the_ruleset_names_only_its_own_table_is_clean():
    """I4: loading it changes no ufw, kube-proxy, flannel or portmap rule."""
    assert statements(RULESET.read_text())
    assert foreign_tables(RULESET.read_text()) == []


def test_a_statement_on_another_table_is_flagged():
    planted = RULESET.read_text() + "\nadd rule ip filter OUTPUT counter\n"
    assert foreign_tables(planted) == ["add rule ip filter OUTPUT counter"]


def test_the_rules_cover_exactly_the_session_users_the_users_arm_declares():
    declared = {
        user["uid"]
        for name, spec in SESSION_USERS.items()
        for user in yaml_fast.safe_load(spec.read_text())["users"]
        if user["name"] == name
    }
    assert len(declared) == len(SESSION_USERS), "a session user is no longer declared"
    rule_uids = {
        frozenset(int(uid) for uid in group.split(","))
        for group in re.findall(r"meta skuid \{([^}]*)\}", RULESET.read_text())
    }
    assert rule_uids == {frozenset(declared)}
