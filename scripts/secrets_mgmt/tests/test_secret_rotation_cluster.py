"""`rotate --commit` keeps the cluster files in step before it deploys (slice D3.2b).

Every tree here is built in `tmp_path`, and `write`, `check` and the render are fakes from
`_rotation_fakes.py`: no test runs the real `cluster_vars.py write` or decrypts anything.
`test_cluster_vars_write.py` covers `write` itself against a throwaway age key.

Run: uv run pytest scripts/secrets_mgmt/tests/test_secret_rotation_cluster.py
"""

from pathlib import Path
from types import SimpleNamespace

import pytest

from secrets_mgmt import secret_rotation as sr
from _rotation_fakes import Fakes, build_tools, named_calls
from secrets_mgmt.cluster_vars_check import Expected, ManifestEntry, Tree

# Auto tier with a single-redeploy consumer, so `rotate` both writes it and deploys.
NAME = "monitor_bridge_test_token"


def _args() -> SimpleNamespace:
    return SimpleNamespace(
        name=NAME, all=False, commit=True, deploy=True, no_derive=False
    )


def _cluster_tree(root: Path) -> Path:
    """`root`, holding a cluster file; the hook reads only that it exists."""
    tree = Tree(root)
    tree.cluster_file.parent.mkdir(parents=True)
    tree.cluster_file.write_text("")
    return root


def _rotate(**fakes) -> tuple[int, list]:
    registry = {"entries": {NAME: {"tier": "auto", "last_rotated": "2026-01-01"}}}
    tools, calls = build_tools(Fakes(registry=registry, **fakes))
    return sr.cmd_rotate(_args(), tools), calls


def _deploy_at(calls: list) -> int | None:
    """The index in `calls` of the deploy's `ansible-playbook` run, or None."""
    return next(
        (
            i
            for i, (name, args, _) in enumerate(calls)
            if name == "run" and "ansible-playbook" in args[0]
        ),
        None,
    )


def _deployed(calls: list) -> bool:
    return _deploy_at(calls) is not None


def _cluster_calls(calls: list) -> list[str]:
    return [n for n in named_calls(calls) if n.startswith("cluster_")]


REFERENCED = Expected(frozenset({NAME}), frozenset(), ())
IN_A_MANIFEST = Expected(
    frozenset({"other_key"}),
    frozenset(),
    (ManifestEntry("mosquitto", "passwd.sops.yaml", "", frozenset({NAME})),),
)


@pytest.mark.parametrize(
    "expected",
    [
        pytest.param(REFERENCED, id="cluster-file-key"),
        pytest.param(IN_A_MANIFEST, id="sops-manifest-key"),
    ],
)
def test_a_rotated_cluster_key_reaches_write_then_check_before_the_deploy(
    tmp_path, expected
):
    rc, calls = _rotate(cluster_root=_cluster_tree(tmp_path), expected=expected)
    assert rc == 0
    assert _cluster_calls(calls) == [
        "cluster_expected",
        "cluster_write",
        "cluster_check",
    ]
    write = next(args for name, args, _ in calls if name == "cluster_write")
    assert write == (Tree(tmp_path), expected)
    order = named_calls(calls)
    assert order.index("save_registry") < order.index("cluster_write")
    deploy = _deploy_at(calls)
    assert deploy is not None and order.index("cluster_check") < deploy


def test_a_host_only_key_does_not_reach_write(tmp_path):
    host_only = Expected(frozenset({"some_cluster_key"}), frozenset(), ())
    rc, calls = _rotate(cluster_root=_cluster_tree(tmp_path), expected=host_only)
    assert rc == 0
    assert _cluster_calls(calls) == ["cluster_expected"]
    assert _deployed(calls)


def test_an_absent_cluster_file_makes_the_hook_a_no_op(tmp_path):
    rc, calls = _rotate(cluster_root=tmp_path, expected=REFERENCED)
    assert rc == 0
    assert _cluster_calls(calls) == []
    assert _deployed(calls)


@pytest.mark.parametrize(
    ("failure", "reached"),
    [
        pytest.param({"write_rc": 1}, ["cluster_write"], id="write"),
        pytest.param({"check_rc": 1}, ["cluster_write", "cluster_check"], id="check"),
        pytest.param({"expected_error": RuntimeError("render")}, [], id="render"),
    ],
)
def test_a_failing_cluster_step_returns_non_zero_without_deploying(
    tmp_path, capsys, failure, reached
):
    rc, calls = _rotate(
        cluster_root=_cluster_tree(tmp_path), expected=REFERENCED, **failure
    )
    assert rc == 3
    assert _cluster_calls(calls) == ["cluster_expected", *reached]
    assert not _deployed(calls)
    err = capsys.readouterr().err
    assert "Consumers were NOT deployed" in err, err
    assert "ansible/vars/cluster-manifests" in err, err


@pytest.mark.parametrize(
    ("expected", "names_cluster_files"),
    [
        pytest.param(REFERENCED, True, id="cluster-key"),
        pytest.param(
            Expected(frozenset({"other_key"}), frozenset(), ()), False, id="host-only"
        ),
    ],
)
def test_the_next_line_names_the_cluster_files_only_when_write_rewrote_them(
    tmp_path, capsys, expected, names_cluster_files
):
    """A hand run commits what the Next: line names; CI's `check` fails on a stale `.source`."""
    registry = {"entries": {NAME: {"tier": "auto", "last_rotated": "2026-01-01"}}}
    tools, _ = build_tools(
        Fakes(
            registry=registry, cluster_root=_cluster_tree(tmp_path), expected=expected
        )
    )
    args = _args()
    args.deploy = False
    assert sr.cmd_rotate(args, tools) == 0
    next_line = next(
        line
        for line in capsys.readouterr().out.splitlines()
        if line.startswith("Next:")
    )
    for path in sr.CLUSTER_PATHS:
        assert (path in next_line) is names_cluster_files, next_line
