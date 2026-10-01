"""Guards that UFW's own sysctl file must not undo the role's hardening.

`ansible.posix.sysctl` writes /etc/sysctl.conf and sets the live value. UFW keeps a SECOND
sysctl file, `/etc/ufw/sysctl.conf` (`IPT_SYSCTL` in /etc/default/ufw), and re-applies it on
every `ufw enable`/`reload` and at boot. Ubuntu ships it with
`net/ipv4/conf/{all,default}/log_martians=0` uncommented, so `Enable UFW firewall` — twenty
tasks after the hardening loop, in the SAME file — puts the key back to 0. The role then
reports `changed` for those two keys on every run and never keeps them.

ORDER IS THE GUARD. The rewrite has to run before the hardening loop; after it, the run would
end with UFW's 0 live again. So this file checks position, not just presence.

The census names the tasks it must find, so a rename fails with the member that went missing
rather than passing over an empty list.

Run: uv run pytest ansible/tests/setup/test_ufw_sysctl_keeps_log_martians.py
"""

from _helpers import ANSIBLE
from lib import yaml_fast

NETWORK = ANSIBLE / "roles" / "setup" / "initial_setup" / "tasks" / "network.yml"
UFW_SYSCTL = "/etc/ufw/sysctl.conf"

HARDENING_TASK = "Apply kernel security hardening"
UFW_ENABLE_TASK = "Enable UFW firewall"
FIX_TASK = "Stop UFW's sysctl file from disabling martian logging"


def _tasks() -> list[dict]:
    return [
        t for t in yaml_fast.safe_load(NETWORK.read_text()) or [] if isinstance(t, dict)
    ]


def _rewrites_ufw_log_martians(task: dict) -> bool:
    """True when this task rewrites UFW's log_martians keys to 1."""
    args = task.get("ansible.builtin.lineinfile") or {}
    if str(args.get("path", "")) != UFW_SYSCTL:
        return False
    return "log_martians" in str(args.get("regexp", "")) and str(
        args.get("line", "")
    ).endswith("log_martians=1")


def _index(name: str) -> int:
    names = [str(t.get("name", "")) for t in _tasks()]
    assert name in names, f"census found no task named {name!r} in {NETWORK.name}"
    return names.index(name)


def test_the_role_rewrites_ufws_copy_of_the_log_martians_keys():
    hits = [t for t in _tasks() if _rewrites_ufw_log_martians(t)]
    assert len(hits) == 1, [str(t.get("name")) for t in hits]
    assert str(hits[0].get("name")) == FIX_TASK


def test_a_task_that_leaves_ufws_copy_alone_is_flagged():
    """The rejecting half — the same predicate over the shapes it must NOT accept."""
    assert not _rewrites_ufw_log_martians(
        {
            "ansible.builtin.lineinfile": {
                "path": "/etc/sysctl.conf",
                "regexp": "log_martians",
                "line": "x=1",
            }
        }
    )
    assert not _rewrites_ufw_log_martians(
        {
            "ansible.builtin.lineinfile": {
                "path": UFW_SYSCTL,
                "regexp": "^net/ipv4/ip_forward=",
                "line": "net/ipv4/ip_forward=1",
            }
        }
    )
    assert not _rewrites_ufw_log_martians(
        {
            "ansible.builtin.lineinfile": {
                "path": UFW_SYSCTL,
                "regexp": "log_martians",
                "line": "net/ipv4/conf/all/log_martians=0",
            }
        }
    )


def test_the_rewrite_runs_before_the_hardening_loop_and_before_ufw_reloads():
    fix = _index(FIX_TASK)
    assert fix < _index(HARDENING_TASK), (
        "UFW's file must be fixed before the live value is set"
    )
    assert fix < _index(UFW_ENABLE_TASK), (
        "an enable/reload after the fix re-asserts 1, not 0"
    )
