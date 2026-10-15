"""secret_rotation.py's failure lines share argparse's `<prog>: error:` prefix (#4447).

One grep for `secret_rotation.py: error:` must find every refusal and failure it prints, so the
test drives each stderr path and checks every line it wrote.
"""

import datetime as dt
from dataclasses import replace
from types import SimpleNamespace


from secrets_mgmt import secret_rotation as sr
from _rotation_fakes import TODAY, Fakes, build_tools, process_calls


def _reg(name: str, tier: str = "auto", **extra) -> dict:
    return {"entries": {name: {"tier": tier, "last_rotated": "2026-01-01", **extra}}}


def _rotate(name: str) -> SimpleNamespace:
    return SimpleNamespace(
        name=name, all=False, commit=True, deploy=True, no_derive=True
    )


def _record(key: str, last_rotated: str) -> SimpleNamespace:
    return SimpleNamespace(key=key, last_rotated=dt.date.fromisoformat(last_rotated))


def _push() -> SimpleNamespace:
    return SimpleNamespace(
        push=True, check=False, no_derive=True, extra_down=None, all=False
    )


def test_every_stderr_line_carries_the_scripts_error_prefix(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["/repo/scripts/secrets_mgmt/secret_rotation.py"])
    monkeypatch.delenv("SECRET_ROTATION_KUMA", raising=False)
    token = "monitor_bridge_test_token"
    record_key, _ = build_tools(Fakes(registry=_reg("tok", source="record")))
    pinned, _ = build_tools(Fakes(registry=_reg("tok", tier="pinned")))
    write_fails, _ = build_tools(Fakes(registry=_reg(token), run_rc=1))
    deploy_fails = replace(write_fails, sops_set=lambda _name, _value: None)
    future = (TODAY + dt.timedelta(days=1)).isoformat()

    codes = [
        sr.cmd_rotate(_rotate("tok"), record_key),
        sr.cmd_rotate(_rotate("tok"), pinned),
        sr.cmd_rotate(_rotate(token), write_fails),
        sr.cmd_rotate(_rotate(token), deploy_fails),
        sr.cmd_record(_record("absent", "2026-01-01"), pinned),
        sr.cmd_record(_record("tok", future), pinned),
        sr.cmd_audit(_push(), pinned),
    ]

    assert all(code != 0 for code in codes), codes
    lines = capsys.readouterr().err.splitlines()
    # One line per failing call: a path that printed nothing would pass the prefix check.
    assert len(lines) == len(codes), lines
    for line in lines:
        assert line.startswith("secret_rotation.py: error: "), line


# A token whose consumer Flux applies (D2.4b): the redeploy leaves that tag out, and runs no
# deploy at all when no consumer is left. `monitor-bridge` is the test token's one consumer.
_TOKEN = "monitor_bridge_test_token"


def _deploys(calls) -> list[list[str]]:
    return [argv for argv, _ in process_calls(calls) if "ansible-playbook" in argv]


def test_rotate_deploy_of_an_ansible_owned_consumer_is_clean(capsys):
    tools, calls = build_tools(Fakes(registry=_reg(_TOKEN)))
    assert sr.cmd_rotate(_rotate(_TOKEN), tools) == 0
    assert [argv[-1] for argv in _deploys(calls)] == ["monitor-bridge"]
    assert "not redeployed" not in capsys.readouterr().out


def test_rotate_deploy_of_a_flux_owned_consumer_is_flagged(capsys):
    tools, calls = build_tools(
        Fakes(registry=_reg(_TOKEN), owners={"monitor-bridge": "flux"})
    )
    assert sr.cmd_rotate(_rotate(_TOKEN), tools) == 0
    assert _deploys(calls) == []
    out = capsys.readouterr().out
    assert "not redeployed: monitor-bridge carries owner: flux" in out, out
    # The Flux path, not the refusal: `write` already ran in process (D3.4).
    assert "Flux applies it once the commit carrying the cluster files lands" in out
    assert "deployed nothing" in out, out


def test_rotate_next_line_leaves_out_a_flux_owned_consumer(capsys):
    """Without `--deploy` the hint must not name a tag `deploy.sh` refuses with exit 5, and
    must still say to commit: Flux reaches the rotated value only through a committed tree."""
    tools, _ = build_tools(
        Fakes(registry=_reg(_TOKEN), owners={"monitor-bridge": "flux"})
    )
    args = _rotate(_TOKEN)
    args.deploy = False
    assert sr.cmd_rotate(args, tools) == 0
    out = capsys.readouterr().out
    assert "deploy.sh --tags" not in out, out
    assert "Next: commit secrets.yml and secret_rotation.yml" in out, out
