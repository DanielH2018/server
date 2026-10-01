"""Guards for the pi-peer-backup key's sshd forced command.

A key authorized on daniel-pi with no options gives anyone who can read the
`pi-peer-backup-ssh` Secret a shell as a NOPASSWD-sudo user there. The key is pinned
to `files/pi-peer-backup-shell.sh`, which re-derives one `sudo rsync --server --sender` of the
wg-easy directory from SSH_ORIGINAL_COMMAND and refuses everything else. The wrapper is run
for real here with `sudo` shadowed by a stub on PATH that prints its argv, so the assertions
are on what the Pi would execute, not on the wrapper's text.
"""

import os
import re

import pytest
from lib import yaml_fast
from lib.k8s_context import resolve_vars, role_defaults
from lib.proc_testing import fake_bin, path_with, run
from _helpers import ROLES, stub_logger_on_path
from _k8s_render import host_context, rendered_docs

ROLE = ROLES / "k8s/pi-peer-backup"
WRAPPER = ROLE / "files/pi-peer-backup-shell.sh"
SRC = "/home/ubuntu/server/containers/wg-easy/config/"

# The request rsync 3.2.7 sends for the CronJob's exact client invocation
# (`rsync -a --chmod=D700 --timeout=120 --rsync-path='sudo rsync'`), captured by
# pointing `-e` at a script that printed its argv. `--chmod` is client-side and never reaches
# the server; `--timeout` does, and the short-option blob after `e` is version-negotiated.
CAPTURED = f"sudo rsync --server --sender -logDtpre.iLsfxCIvu --timeout=120 . {SRC}"


@pytest.fixture
def logger_calls(tmp_path_factory, monkeypatch):
    """The wrapper logs every rejection with `logger`; keep those off the host's syslog."""
    return stub_logger_on_path(tmp_path_factory, monkeypatch)


@pytest.fixture
def fake_sudo(tmp_path_factory, logger_calls):
    return fake_bin(
        tmp_path_factory.mktemp("bin"), sudo='#!/bin/sh\nprintf "%s\\n" "$@"\n'
    )


def _run(fake_sudo, original_command: str | None, src: str = SRC):
    env: dict[str, str] = dict(os.environ)
    env["PATH"] = path_with(fake_sudo, env=env)
    env.pop("SSH_ORIGINAL_COMMAND", None)
    if original_command is not None:
        env["SSH_ORIGINAL_COMMAND"] = original_command
    return run(["bash", str(WRAPPER), src], env=env)


def test_the_captured_nightly_request_is_executed_as_sudo_rsync(fake_sudo):
    # ACCEPT: the real request execs `sudo rsync --server --sender <opts> . <dir>` verbatim.
    result = _run(fake_sudo, CAPTURED)
    assert result.returncode == 0, result.stderr
    assert result.stdout.split("\n")[:-1] == CAPTURED.split()[1:]


@pytest.mark.parametrize(
    "request_",
    [
        None,  # an interactive login: sshd sets no SSH_ORIGINAL_COMMAND
        "bash -i",
        f"sudo rsync --server -logDtpre.iLsfxCIvu . {SRC}",  # a receiver would WRITE to the Pi
        "sudo rsync --server --sender -logDtpre.iLsfxCIvu . /etc/wireguard/",
        f"sudo rsync --server --sender -logDtpre.iLsfxCIvu --remove-source-files . {SRC}",
        f"sudo rsync --server --sender -logDtpre.iLsfxCIvu --timeout=120; id . {SRC}",
        # `-s` (--secluded-args) makes rsync read the path off the protocol stream instead
        # of argv, so the pinned SRC would be bypassed. Both spellings: its own word,
        # and folded into the blob BEFORE the `e` that starts the capability list.
        f"sudo rsync --server --sender -s -logDtpre.iLsfxCIvu . {SRC}",
        f"sudo rsync --server --sender -slogDtpre.iLsfxCIvu . {SRC}",
        f"sudo rsync --server --sender - . {SRC}",
    ],
    ids=[
        "login",
        "shell",
        "receiver",
        "other-path",
        "long-option",
        "metachar",
        "secluded-args",
        "secluded-args-in-blob",
        "bare-dash",
    ],
)
def test_anything_but_a_sender_of_the_pinned_directory_is_refused(fake_sudo, request_):
    # REJECT: nothing is exec'd (the stub prints nothing) and the exit is non-zero.
    result = _run(fake_sudo, request_)
    assert result.returncode != 0
    assert result.stdout == ""
    assert "rejected" in result.stderr


def test_a_rejection_is_logged_through_the_stub(fake_sudo, logger_calls):
    # Non-vacuity for the logger stub: a wrapper that called `logger` by absolute path would
    # escape it and write fixture rejections to the Pi's syslog.
    _run(fake_sudo, "bash -i")
    assert (
        "pi-peer-backup-shell rejected: not an rsync request"
        in logger_calls.read_text()
    )


def test_the_key_is_pinned_to_the_wrapper_installed_one_task_earlier():
    tasks = yaml_fast.safe_load((ROLE / "tasks/main.yml").read_text())
    names = [t.get("name") for t in tasks]
    install = tasks[names.index("Install the forced-command wrapper on daniel-pi")]
    authorize = tasks[names.index("Authorize the pull key on daniel-pi")]
    assert names.index(install["name"]) < names.index(authorize["name"])
    dest = install["ansible.builtin.copy"]["dest"]
    base = host_context()
    options = resolve_vars(
        {"key_options": authorize["ansible.posix.authorized_key"]["key_options"]},
        {**base, **role_defaults("pi-peer-backup", base)},
    )["key_options"]
    assert options.startswith("restrict,")
    pinned = re.fullmatch(rf'restrict,command="{re.escape(dest)} (\S+)"', options)
    assert pinned, options
    # The CronJob pulls the same directory the key is pinned to, and the wrapper tests above
    # exercise it: all three agree at the value a deploy renders, not at a variable name.
    env = next(
        container["env"]
        for role, _tpl, doc in rendered_docs()
        if role == "pi-peer-backup" and doc.get("kind") == "CronJob"
        for container in doc["spec"]["jobTemplate"]["spec"]["template"]["spec"][
            "containers"
        ]
    )
    pi_src = next(e["value"] for e in env if e["name"] == "PI_SRC")
    assert pi_src.rpartition(":")[2] == pinned.group(1) == SRC, (pi_src, options)
