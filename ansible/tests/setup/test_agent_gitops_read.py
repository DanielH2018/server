"""The agent user reads the deployer's hold markers and owed ledger through an ACL (#4198).

`claude-agent-setup`, which the claude-code host component ships, grants the ACL on a host that
runs the deployer (`has_gitops`) while `claude_code_agent_gitops_read` is true, and removes it
when the flag is false. The script is rendered from the daniel-server package with host_vars
overrides, because its own host_vars arm neither flag.

Run: uv run pytest ansible/tests/setup/test_agent_gitops_read.py
"""

from _agent_package import agent_setup_script

GRANT = 'setfacl --modify "user:$agent:rX" --modify "default:user:$agent:r" /var/lib/gitops-deploy'
REMOVE = 'setfacl --remove "user:$agent" --remove "default:user:$agent" /var/lib/gitops-deploy'


def test_a_deployer_host_grants_the_agent_read_on_the_deployers_state(tmp_path):
    script = agent_setup_script(tmp_path, has_gitops=True)
    assert GRANT in script
    assert REMOVE not in script


def test_switching_the_read_off_removes_both_entries(tmp_path):
    script = agent_setup_script(
        tmp_path, has_gitops=True, claude_code_agent_gitops_read=False
    )
    assert REMOVE in script
    assert GRANT not in script


def test_a_host_without_the_deployer_touches_no_acl_there(tmp_path):
    script = agent_setup_script(tmp_path, has_gitops=False)
    assert "/var/lib/gitops-deploy" not in script
