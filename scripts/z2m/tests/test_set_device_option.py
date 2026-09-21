#!/usr/bin/env python3
"""`set_device_option.sh` against stubbed sops and mosquitto clients.

The script is shell, so the suite can only see it by running it. `sops`, `mosquitto_pub`
and `mosquitto_sub` are stubs on PATH: the pub stub records the topic and payload it was
handed, the sub stub answers with whatever state a test puts in `Z2M_STUB_STATE`. Nothing
reaches a broker or decrypts a secret.

Run: uv run pytest scripts/z2m/tests/test_set_device_option.py
"""

import json
import os
import subprocess
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[3]
_SCRIPT = _REPO / "scripts" / "z2m" / "set_device_option.sh"

_SOPS = """#!/bin/bash
case "$*" in
  *mqtt_username*) echo z2m-user ;;
  *mqtt_password*) echo z2m-pass ;;
  *) exit 1 ;;
esac
"""

# Records every argument on one line so a test can assert the topic and payload exactly.
_MOSQUITTO_PUB = """#!/bin/bash
printf '%s\\n' "$@" >"$Z2M_STUB_PUB_ARGS"
"""

# Answers the canned state after a short delay -- the republish arriving after the publish.
# An empty Z2M_STUB_STATE is a device that never answers.
_MOSQUITTO_SUB = """#!/bin/bash
sleep 1
[[ -n "$Z2M_STUB_STATE" ]] && printf '%s\\n' "$Z2M_STUB_STATE"
exit 0
"""


def _run(
    tmp_path: Path, args: list[str], state: str
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    for name, body in {
        "sops": _SOPS,
        "mosquitto_pub": _MOSQUITTO_PUB,
        "mosquitto_sub": _MOSQUITTO_SUB,
    }.items():
        (bin_dir / name).write_text(body)
        (bin_dir / name).chmod(0o755)
    pub_args = tmp_path / "pub-args"
    env = dict(
        os.environ,
        PATH=f"{bin_dir}:{os.environ['PATH']}",
        Z2M_MQTT_HOST="broker.test",
        Z2M_SECRETS_FILE=str(tmp_path / "unused.yml"),
        Z2M_READBACK_TIMEOUT="3",
        Z2M_STUB_STATE=state,
        Z2M_STUB_PUB_ARGS=str(pub_args),
    )
    result = subprocess.run(
        [str(_SCRIPT), *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    recorded = pub_args.read_text().splitlines() if pub_args.exists() else []
    return result, recorded


def _payload(recorded: list[str]) -> dict:
    return json.loads(recorded[recorded.index("-m") + 1])


def test_a_matching_readback_is_confirmed_and_a_number_is_sent_unquoted(tmp_path):
    result, recorded = _run(
        tmp_path,
        ["Aqara FP300", "absence_delay_timer", "60"],
        state=json.dumps({"absence_delay_timer": 60, "presence": True}),
    )
    assert result.returncode == 0, result.stderr
    assert recorded[recorded.index("-t") + 1] == "zigbee2mqtt/Aqara FP300/set"
    assert _payload(recorded) == {"absence_delay_timer": 60}


def test_a_bare_word_is_sent_as_a_json_string(tmp_path):
    result, recorded = _run(
        tmp_path,
        ["Aqara FP300", "motion_sensitivity", "high"],
        state=json.dumps({"motion_sensitivity": "high"}),
    )
    assert result.returncode == 0, result.stderr
    assert _payload(recorded) == {"motion_sensitivity": "high"}


def test_a_different_readback_is_flagged(tmp_path):
    result, _recorded = _run(
        tmp_path,
        ["Aqara FP300", "motion_sensitivity", "high"],
        state=json.dumps({"motion_sensitivity": "medium"}),
    )
    assert result.returncode == 1, result.stderr
    assert 'republished motion_sensitivity = "medium"' in result.stderr


def test_no_readback_is_unconfirmed_not_rejected(tmp_path):
    result, recorded = _run(
        tmp_path, ["Aqara FP300", "motion_sensitivity", "high"], state=""
    )
    assert result.returncode == 2, result.stderr
    assert "unconfirmed" in result.stderr
    # The publish still happened: a silent device is not a reason to skip the set.
    assert _payload(recorded) == {"motion_sensitivity": "high"}


@pytest.mark.parametrize("args", [[], ["Aqara FP300"], ["Aqara FP300", "k"]])
def test_fewer_than_three_arguments_is_a_usage_error(tmp_path, args):
    result, recorded = _run(tmp_path, args, state="")
    assert result.returncode == 64
    assert recorded == []
