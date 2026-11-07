"""The operator runner's process boundary: its argv, its stdin, and what makes it raise."""

import subprocess
from pathlib import Path

import pytest
from lib.operator_kubectl import KubectlFailed, for_mode, runner


class Run:
    def __init__(self, code=0, stdout="out", stderr="why", raises=None):
        self.result = (code, stdout, stderr)
        self.raises = raises
        self.argv: list[str] = []
        self.input = None

    def __call__(self, argv, input=None, **_):
        self.argv, self.input = argv, input
        if self.raises:
            raise self.raises
        code, stdout, stderr = self.result
        return subprocess.CompletedProcess(argv, code, stdout, stderr)


def test_a_call_names_its_kubeconfig_and_passes_stdin():
    run = Run()
    kubectl = runner("/etc/k3s.yaml", "/bin/kubectl", run=run)
    assert kubectl("apply", "-f", "-", stdin="{}") == "out"
    assert run.argv == [
        "/bin/kubectl",
        "--kubeconfig",
        "/etc/k3s.yaml",
        "apply",
        "-f",
        "-",
    ]
    assert run.input == "{}"


@pytest.mark.parametrize(
    "run, message",
    [
        (Run(code=1), "why"),
        (Run(raises=subprocess.TimeoutExpired("kubectl", 60)), "timed out"),
        (Run(raises=OSError("no such file")), "no such file"),
    ],
    ids=["non-zero", "timeout", "unrunnable"],
)
def test_a_failed_call_raises_with_its_cause(run, message):
    with pytest.raises(KubectlFailed, match=message):
        runner("/etc/k3s.yaml", run=run)("get", "pods")


def _for_mode(apply: bool, run: Run, readable: bool = True):
    return for_mode(
        apply,
        "scripts/x.py --apply",
        tool=lambda _: "/bin/kubectl",
        kubeconfig=lambda: Path("/home/u/config"),
        readable=lambda _: readable,
        run=run,
    )


def test_an_apply_uses_the_k3s_kubeconfig_and_a_dry_run_the_readable_one():
    run = Run()
    _for_mode(True, run)("get", "pods")
    assert run.argv[2] == "/etc/rancher/k3s/k3s.yaml"
    _for_mode(False, run)("get", "pods")
    assert run.argv[2] == "/home/u/config"


def test_an_apply_without_the_k3s_kubeconfig_prints_the_sudo_command():
    with pytest.raises(KubectlFailed, match=r"sudo .* -B scripts/x\.py --apply"):
        _for_mode(True, Run(), readable=False)


def test_a_dry_run_does_not_need_the_k3s_kubeconfig():
    assert _for_mode(False, Run(), readable=False)("get", "pods") == "out"
