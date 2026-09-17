#!/usr/bin/env python3
"""`deploy_detach_notify.py` names a no-host run as deploy.sh's own exit, not ansible's.

deploy.sh exit 78 means the playbook reached PLAY RECAP naming no host; ansible itself exited
0 for that, and the wrapper read the recap (issue #1814). The Discord post for a --detach run
used to say "ansible-playbook exited non-zero -- see the log" for every non-zero status, which
for 78 sends the reader to a log whose ansible run looks clean.

`--no-post` keeps the test off the webhook, and a non-zero status returns before any probe
runs, so nothing here needs patching.

Run: uv run pytest scripts/deploy_tools/tests/test_deploy_detach_notify_no_hosts.py
"""

import deploy_detach_notify as notify_mod


def test_main_names_a_no_host_run_as_the_wrappers_own_exit(capsys):
    code = notify_mod.main(
        ["--status", "78", "--log", "/tmp/x", "--tags", "n8n", "--no-post"]
    )
    out = capsys.readouterr().out
    assert code == 1
    assert "FAILED (deploy.sh exit 78)" in out
    assert "matched NO host" in out
    assert "exited non-zero" not in out


def test_main_still_blames_ansible_for_its_own_non_zero(capsys):
    """CLEAN half: any other non-zero status keeps the ansible wording."""
    code = notify_mod.main(
        ["--status", "2", "--log", "/tmp/x", "--tags", "n8n", "--no-post"]
    )
    out = capsys.readouterr().out
    assert code == 1
    assert "FAILED (ansible exit 2)" in out
    assert "exited non-zero" in out
