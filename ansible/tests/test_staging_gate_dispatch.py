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

_REPO = pathlib.Path(__file__).resolve().parents[2]
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
