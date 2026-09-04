"""Fakes for every `RotationTools` boundary, so `test_secret_rotation.py` patches nothing.

Every fake appends `(name, args, kwargs)` to a shared `calls` list, which is how a test proves
"the registry was saved after the value was written" without reading source.

THE SUBPROCESS BOUNDARIES KEEP THEIR REAL IMPLEMENTATIONS. `sops_set`, `sops_decrypt` and
`deploy` are `rotation_tools`' own functions with a recording `run` injected, not lambdas.
Replacing them wholesale would retire the argv assertions that are the point of two of them —
the token must reach the process on stdin, never in argv. `process_calls` returns exactly
what those three handed the runner.
"""

from __future__ import annotations

import datetime as dt
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass, field
from functools import partial

import yaml

# Reach the modules under test: pytest prepends only this file's OWN directory, and
# pyproject's `pythonpath` is a pytest setting the bootstrap guard does not accept.
import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))  # scripts/secrets_mgmt

import rotation_tools
import secret_rotation as sr

TODAY = dt.date(2026, 9, 1)


@dataclass
class Fakes:
    """What each fake answers; every field is a per-test override."""

    registry: dict = field(default_factory=lambda: {"entries": {}})
    names: list[str] = field(default_factory=list)
    today: dt.date = TODAY
    # Newest-first (sha, "YYYY-MM-DD", {name: ciphertext}) — one entry per commit that
    # touched the encrypted store.
    history: list[tuple[str, str, dict[str, str]]] = field(default_factory=list)
    git_error: BaseException | None = None
    run_error: BaseException | None = None
    run_rc: int = 0
    run_stdout: str = ""
    tier_days: Mapping[str, int | None] = field(default_factory=lambda: sr.TIER_DAYS)


def build_tools(f: Fakes) -> tuple[rotation_tools.RotationTools, list]:
    """A `RotationTools` answering from `f`, and the call record every fake appends to."""
    calls: list[tuple] = []
    log = "\n".join("%s %s" % (sha, day) for sha, day, _ in f.history)
    blobs = {sha: values for sha, _, values in f.history}

    def git(*args: str) -> str:
        calls.append(("git", args, {}))
        if f.git_error is not None:
            raise f.git_error
        if args[0] == "log":
            return log + "\n"
        return yaml.safe_dump(blobs[args[1].split(":", 1)[0]])

    def run(cmd, **kwargs):
        calls.append(("run", (cmd,), kwargs))
        if f.run_error is not None:
            raise f.run_error
        return subprocess.CompletedProcess(
            cmd, f.run_rc, stdout=f.run_stdout, stderr=""
        )

    def load_registry(*_args, **_kwargs) -> dict:
        calls.append(("load_registry", (), {}))
        return f.registry

    def save_registry(reg, *_args, **_kwargs) -> None:
        calls.append(("save_registry", (reg,), {}))

    def sops_names(*_args, **_kwargs) -> list[str]:
        calls.append(("sops_names", (), {}))
        return list(f.names)

    def kuma_push(url: str, ok: bool, msg: str) -> None:
        calls.append(("kuma_push", (url, ok, msg), {}))

    return rotation_tools.RotationTools(
        git=git,
        today=lambda: f.today,
        load_registry=load_registry,
        save_registry=save_registry,
        sops_names=sops_names,
        sops_decrypt=partial(rotation_tools.decrypted_values, run=run),
        sops_set=partial(rotation_tools.sops_set, run=run),
        kuma_push=kuma_push,
        deploy=partial(rotation_tools.run_deploy, run=run),
        tier_days=f.tier_days,
    ), calls


def process_calls(calls: list[tuple]) -> list[tuple[list[str], dict]]:
    """Only the subprocess calls, as (argv, kwargs), in the order they were made."""
    return [(args[0], kwargs) for name, args, kwargs in calls if name == "run"]


def named_calls(calls: list[tuple]) -> list[str]:
    """The name of each fake that was called, in order."""
    return [name for name, _args, _kwargs in calls]
