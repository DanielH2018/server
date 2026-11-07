"""secret_rotation.py's failure lines share argparse's `<prog>: error:` prefix (#4447).

One grep for `secret_rotation.py: error:` must find every refusal and failure it prints, so the
test drives each stderr path and checks every line it wrote.
"""

import datetime as dt
from types import SimpleNamespace


from secrets_mgmt import secret_rotation as sr
from _rotation_fakes import TODAY, Fakes, build_tools, process_calls


def _reg(name: str, tier: str = "auto", **extra) -> dict:
    return {"entries": {name: {"tier": tier, "last_rotated": "2026-01-01", **extra}}}


def _rotate(name: str) -> SimpleNamespace:
    return SimpleNamespace(name=name, all=False, commit=True, no_derive=True)


def _record(key: str, last_rotated: str) -> SimpleNamespace:
    return SimpleNamespace(key=key, last_rotated=dt.date.fromisoformat(last_rotated))


def test_every_stderr_line_carries_the_scripts_error_prefix(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["/repo/scripts/secrets_mgmt/secret_rotation.py"])
    token = "monitor_bridge_test_token"
    record_key, _ = build_tools(Fakes(registry=_reg("tok", source="record")))
    pinned, _ = build_tools(Fakes(registry=_reg("tok", tier="pinned")))
    write_fails, _ = build_tools(Fakes(registry=_reg(token), run_rc=1))
    future = (TODAY + dt.timedelta(days=1)).isoformat()

    codes = [
        sr.cmd_rotate(_rotate("tok"), record_key),
        sr.cmd_rotate(_rotate("tok"), pinned),
        sr.cmd_rotate(_rotate(token), write_fails),
        sr.cmd_record(_record("absent", "2026-01-01"), pinned),
        sr.cmd_record(_record("tok", future), pinned),
    ]

    assert all(code != 0 for code in codes), codes
    lines = capsys.readouterr().err.splitlines()
    # One line per failing call: a path that printed nothing would pass the prefix check.
    assert len(lines) == len(codes), lines
    for line in lines:
        assert line.startswith("secret_rotation.py: error: "), line


# `monitor-bridge` is the test token's one consumer.
_TOKEN = "monitor_bridge_test_token"


def test_rotate_names_each_consumer_and_runs_no_playbook(capsys):
    tools, calls = build_tools(Fakes(registry=_reg(_TOKEN)))
    assert sr.cmd_rotate(_rotate(_TOKEN), tools) == 0
    assert not [argv for argv, _ in process_calls(calls) if "ansible-playbook" in argv]
    out = capsys.readouterr().out
    assert "not redeployed: monitor-bridge carries owner: flux" in out, out
    # The Flux path, not the refusal: `write` already ran in process (D3.4).
    assert "Flux applies it once the commit carrying the cluster files lands" in out
    assert "Next: commit secrets.yml and secret_rotation.yml" in out, out


def test_rotate_parses_the_flags_secret_rotate_sh_passes():
    """The script ships in a host package, so it can lag the checkout it runs this from."""
    args = sr.build_parser().parse_args(["rotate", "--commit", "--deploy"])

    assert args.func is sr.cmd_rotate and args.commit
