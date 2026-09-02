"""The staging gate's forced command is a security boundary, so it is tested like one.

`$SSH_ORIGINAL_COMMAND` is caller-controlled. The restricted key's authorized_keys entry pins
the dispatcher as the only thing that key can run, and the dispatcher is what stops that from
meaning "a shell with extra steps".

THE FINDING THIS EXISTS FOR. A `command=` forced command does NOT stop ssh forwarding stdin.
The gate's original design piped its remote script to `bash -s`, so a forced command that still
read stdin would execute whatever the caller sent and the restriction would be decorative. Two
properties therefore have to hold, and both are driven here: the dispatcher never reads stdin,
and it takes an OPERATION NAME plus validated arguments rather than a script body.

Every rejecting case drives `validate_request` — the same function the live path calls — by
sourcing the rendered script. Sourcing is safe because the file guards its `main` on
`BASH_SOURCE`, which is why that guard is there.
"""

from __future__ import annotations

import pathlib
import subprocess

import yaml
from jinja2 import Environment
from _helpers import REPO

_REPO = REPO
_ROLE = _REPO / "ansible" / "roles" / "setup" / "hypervisor"
_TEMPLATE = _ROLE / "templates" / "staging-gate-dispatch.sh.j2"
_DEFAULTS = _ROLE / "defaults" / "main.yml"

# A syntactically valid object name. Not any real commit -- validation is what is under test,
# and the dispatcher never gets as far as looking it up in these cases.
_SHA = "a" * 40
_REFUSED = 71
_PREP_FAILED = 70


def _render(**overrides) -> str:
    defaults = yaml.safe_load(_DEFAULTS.read_text())
    context = {
        "hypervisor_staging_gate_repo": defaults["hypervisor_staging_gate_repo"],
        "hypervisor_staging_gate_lock": defaults["hypervisor_staging_gate_lock"],
    }
    context.update(overrides)
    return (
        Environment(keep_trailing_newline=True)
        .from_string(_TEMPLATE.read_text())
        .render(**context)
    )


def _script(tmp_path, **overrides) -> pathlib.Path:
    path = tmp_path / "staging-gate-dispatch.sh"
    path.write_text(_render(**overrides))
    path.chmod(0o755)
    return path


def validate(tmp_path, request: str) -> subprocess.CompletedProcess:
    """Drive the dispatcher's own validator with `request` and report what it decided.

    Sources the rendered script rather than reimplementing the rules, so a rule that silently
    stopped matching fails here.
    """
    script = _script(tmp_path)
    return subprocess.run(
        [
            "bash",
            "-c",
            'source "$1"; validate_request "$2"; echo "OK $SHA $TAGS"',
            "_",
            str(script),
            request,
        ],
        capture_output=True,
        text=True,
    )


# ── the accepting half ──────────────────────────────────────────────────────────────────────


def test_a_well_formed_gate_request_is_accepted(tmp_path):
    result = validate(tmp_path, f"gate {_SHA} traefik,authelia,freshrss")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == f"OK {_SHA} traefik,authelia,freshrss"


def test_a_single_tag_is_accepted(tmp_path):
    result = validate(tmp_path, f"gate {_SHA} traefik")
    assert result.returncode == 0, result.stderr


def test_tags_may_carry_dashes_and_underscores(tmp_path):
    """`node-exporter` and `ical-proxy` are real members of the staging subset."""
    result = validate(tmp_path, f"gate {_SHA} node-exporter,ical-proxy")
    assert result.returncode == 0, result.stderr


# ── the rejecting half ──────────────────────────────────────────────────────────────────────
# Each of these must be refused by the SAME function the accepting cases drive, and refused
# with 71 rather than a bare 1 -- a dispatcher refusal means "staging could not be asked", and
# collapsing it into a rejection would fail a merge for a transport reason.


def test_an_empty_command_is_refused(tmp_path):
    """What an interactive `ssh daniel-server` with no command sends."""
    assert validate(tmp_path, "").returncode == _REFUSED


def test_a_bare_shell_is_refused(tmp_path):
    """The original design's command. This is the attack the dispatcher exists to stop."""
    assert validate(tmp_path, "bash -s").returncode == _REFUSED


def test_an_unknown_operation_is_refused(tmp_path):
    assert validate(tmp_path, f"deploy {_SHA} traefik").returncode == _REFUSED


def test_a_short_sha_is_refused(tmp_path):
    assert validate(tmp_path, "gate deadbeef traefik").returncode == _REFUSED


def test_an_uppercase_sha_is_refused(tmp_path):
    assert validate(tmp_path, f"gate {'A' * 40} traefik").returncode == _REFUSED


def test_a_ref_name_in_place_of_a_sha_is_refused(tmp_path):
    """A ref resolves to whatever it points at later, which is not the commit under review."""
    assert validate(tmp_path, "gate master traefik").returncode == _REFUSED


def test_a_semicolon_in_the_tags_is_refused(tmp_path):
    assert validate(tmp_path, f"gate {_SHA} traefik;id").returncode == _REFUSED


def test_command_substitution_in_the_tags_is_refused(tmp_path):
    assert validate(tmp_path, f"gate {_SHA} traefik$(id)").returncode == _REFUSED
    assert validate(tmp_path, f"gate {_SHA} traefik`id`").returncode == _REFUSED


def test_a_trailing_extra_field_is_refused(tmp_path):
    """Field count is checked, so an appended argument cannot ride along with a valid pair."""
    assert validate(tmp_path, f"gate {_SHA} traefik --extra").returncode == _REFUSED


def test_a_path_traversal_in_the_tags_is_refused(tmp_path):
    assert validate(tmp_path, f"gate {_SHA} ../../etc/passwd").returncode == _REFUSED


# ── stdin, which is the whole point ─────────────────────────────────────────────────────────


def test_the_dispatcher_does_not_execute_anything_on_stdin(tmp_path):
    """A forced command does NOT stop ssh forwarding stdin -- so this must be proven, not assumed.

    Runs the dispatcher for real (not sourced) with a valid request and a script body on stdin.
    The checkout it points at does not exist here, so it must stop at prep with 70; what must
    NOT happen is the stdin body running.
    """
    script = _script(tmp_path, hypervisor_staging_gate_repo=str(tmp_path / "absent"))
    result = subprocess.run(
        ["bash", str(script)],
        input="echo PWNED\n",
        capture_output=True,
        text=True,
        env={"SSH_ORIGINAL_COMMAND": f"gate {_SHA} traefik", "PATH": "/usr/bin:/bin"},
    )
    assert "PWNED" not in result.stdout, (
        "the dispatcher executed a script body sent on stdin"
    )
    assert "PWNED" not in result.stderr
    assert result.returncode == _PREP_FAILED, result.stderr


def test_a_refused_request_does_not_execute_stdin_either(tmp_path):
    script = _script(tmp_path, hypervisor_staging_gate_repo=str(tmp_path / "absent"))
    result = subprocess.run(
        ["bash", str(script)],
        input="echo PWNED\n",
        capture_output=True,
        text=True,
        env={"SSH_ORIGINAL_COMMAND": "bash -s", "PATH": "/usr/bin:/bin"},
    )
    assert "PWNED" not in result.stdout + result.stderr
    assert result.returncode == _REFUSED


def test_an_absent_ssh_original_command_is_refused(tmp_path):
    """`set -u` must not turn a missing variable into an unhandled error."""
    script = _script(tmp_path, hypervisor_staging_gate_repo=str(tmp_path / "absent"))
    result = subprocess.run(
        ["bash", str(script)],
        input="",
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin"},
    )
    assert result.returncode == _REFUSED, result.stderr


# ── the refusal must not read as a rejection ────────────────────────────────────────────────


def test_the_refusal_codes_map_to_no_verdict_in_the_caller():
    """Pins the dispatcher's exit codes against staging_gate.py's own classifier.

    A refusal that classified as REJECTED would fail a merge because someone sent a malformed
    request, which is the opposite of what the three-outcome vocabulary is for.
    """
    import sys

    sys.path.insert(0, str(_REPO / "scripts" / "deploy_tools"))
    import staging_gate

    for code in (_PREP_FAILED, _REFUSED):
        assert staging_gate.classify(code) == staging_gate.NO_VERDICT, (
            f"exit {code} must classify as NO_VERDICT -- a dispatcher refusal means staging "
            f"could not be asked, never that it rejected the change"
        )


# ── the authorization, which is a separate property from the script ─────────────────────────
# Everything above proves the dispatcher is safe to run. None of it proves the key is actually
# CONFINED to it: drop `key_options` from the install task and every test above still passes
# while the key becomes an ordinary unrestricted login. That is the present-but-inert shape this
# repo keeps paying for, so the authorization gets its own guard.


def _install_tasks() -> list[dict]:
    return yaml.safe_load((_ROLE / "tasks" / "install.yml").read_text())


def authorization_problems(tasks: list[dict]) -> list[str]:
    """Every reason the staging-gate key would not be confined to the dispatcher."""
    problems = []
    entries = [
        task["ansible.posix.authorized_key"]
        for task in tasks or []
        if isinstance(task, dict)
        and isinstance(task.get("ansible.posix.authorized_key"), dict)
        and task["ansible.posix.authorized_key"].get("state") == "present"
    ]
    if not entries:
        return ["install.yml authorizes no ssh key at all"]

    for entry in entries:
        options = str(entry.get("key_options") or "")
        if "restrict" not in options:
            problems.append(
                "the authorized_key entry does not carry `restrict`, so the key keeps port, "
                "agent and X11 forwarding even if it cannot open a shell"
            )
        if "command=" not in options:
            problems.append(
                "the authorized_key entry has no forced `command=`, so the key is an ordinary "
                "unrestricted login and the dispatcher is bypassed entirely"
            )
        elif "hypervisor_staging_gate_dispatch_path" not in options:
            problems.append(
                "the forced command is not the templated dispatch path, so it can drift from "
                "the file the role actually installs"
            )
    return problems


def test_the_staging_gate_key_is_confined_to_the_dispatcher():
    assert authorization_problems(_install_tasks()) == []


def test_an_unrestricted_authorization_is_flagged():
    bare = [
        {
            "ansible.posix.authorized_key": {
                "user": "ubuntu",
                "key": "x",
                "state": "present",
            }
        }
    ]
    problems = authorization_problems(bare)
    assert any("no forced `command=`" in p for p in problems), problems
    assert any("does not carry `restrict`" in p for p in problems), problems


def test_a_forced_command_without_restrict_is_flagged():
    forced = [
        {
            "ansible.posix.authorized_key": {
                "user": "ubuntu",
                "key": "x",
                "state": "present",
                "key_options": 'command="{{ hypervisor_staging_gate_dispatch_path }}"',
            }
        }
    ]
    problems = authorization_problems(forced)
    assert any("does not carry `restrict`" in p for p in problems), problems


def test_a_forced_command_pointing_somewhere_else_is_flagged():
    drifted = [
        {
            "ansible.posix.authorized_key": {
                "user": "ubuntu",
                "key": "x",
                "state": "present",
                "key_options": 'restrict,command="/usr/local/bin/something-else"',
            }
        }
    ]
    problems = authorization_problems(drifted)
    assert any("not the templated dispatch path" in p for p in problems), problems


def _validate_in(cwd, tmp_path, request: str) -> subprocess.CompletedProcess:
    """Drive the validator with the process's working directory set to `cwd`.

    The shared `validate` helper runs wherever pytest does. Pathname expansion is the one rule
    whose outcome depends on what is on disk next to the process, so this test needs to choose
    that directory rather than inherit it.
    """
    script = _script(tmp_path)
    return subprocess.run(
        [
            "bash",
            "-c",
            'source "$1"; validate_request "$2"; echo "OK $SHA $TAGS"',
            "_",
            str(script),
            request,
        ],
        capture_output=True,
        text=True,
        cwd=str(cwd),
    )


def test_a_glob_cannot_smuggle_a_filename_in_as_the_sha(tmp_path):
    """The field split is unquoted on purpose, and an unquoted expansion also GLOBS.

    Without `set -f` this is not a sharp edge, it is an accepted request: a caller sends
    `gate * traefik`, the `*` expands against the login directory, and if any file there is
    named like a 40-hex object the split yields exactly three valid-looking fields. The
    dispatcher would then hand a filename to the gate as the SHA under test.

    Measured 2026-08-29 against the unhardened version: `gate * traefik` split into 267 fields
    in a real directory. This test builds the narrower case that actually gets through.
    """
    login_dir = tmp_path / "login"
    login_dir.mkdir()
    (login_dir / ("b" * 40)).write_text("a decoy named like an object id")

    result = _validate_in(login_dir, tmp_path, "gate * traefik")
    assert result.returncode == _REFUSED, (
        f"`gate * traefik` must be refused; got rc={result.returncode} "
        f"stdout={result.stdout.strip()!r}. If this accepted, the split glob-expanded and a "
        f"filename reached the gate as the SHA."
    )


def test_the_glob_guard_does_not_break_a_normal_request(tmp_path):
    """The accepting half of the pair:

    disabling pathname expansion must not disturb the ordinary path, and the field split must still
    happen.
    """
    login_dir = tmp_path / "login"
    login_dir.mkdir()
    (login_dir / ("b" * 40)).write_text("the same decoy")

    result = _validate_in(login_dir, tmp_path, f"gate {_SHA} traefik,freshrss")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == f"OK {_SHA} traefik,freshrss"
