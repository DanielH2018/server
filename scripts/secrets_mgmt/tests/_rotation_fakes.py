"""Fakes for every `RotationTools` boundary, so the tests around it patch nothing.

Every fake appends `(name, args, kwargs)` to a shared `calls` list, which is how a test proves
"the registry was saved" or "it was not" without reading source.
"""

import datetime as dt
from dataclasses import dataclass, field

import yaml

# Package-qualified, as `_land_fakes.py` reaches `land_lib.tools`: a bare `import
# rotation_tools` binds a SECOND module object in a pytest session, since the module under
# test reaches the same file as `secrets_mgmt.rotation_tools`.
from secrets_mgmt.rotation_tools import RotationTools

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


def build_tools(f: Fakes) -> tuple[RotationTools, list]:
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
        # A named AssertionError carrying the argv, the shape every sibling fake raises
        # (`_findings_fakes.py`, `_land_fakes.py`, `_deploy_fakes.py`). A bare `KeyError`
        # naming the sha alone would make an unscripted VERB — a `git rev-parse` a future
        # caller adds — read as "that sha is not in this history" rather than "this call was
        # never scripted", and the argv would never reach the report.
        if args[0] != "show" or args[1].split(":", 1)[0] not in blobs:
            raise AssertionError(f"unscripted git call: {args}")
        return yaml.safe_dump(blobs[args[1].split(":", 1)[0]])

    def load_registry(*_args, **_kwargs) -> dict:
        calls.append(("load_registry", (), {}))
        return f.registry

    def save_registry(reg, *_args, **_kwargs) -> None:
        calls.append(("save_registry", (reg,), {}))

    def sops_names(*_args, **_kwargs) -> list[str]:
        calls.append(("sops_names", (), {}))
        return list(f.names)

    # `tier_days` is left at its default: every test wants the real table, and the default
    # is a literal in `rotation_tools`.
    return RotationTools(
        git=git,
        today=lambda: f.today,
        load_registry=load_registry,
        save_registry=save_registry,
        sops_names=sops_names,
    ), calls


def named_calls(calls: list[tuple]) -> list[str]:
    """The name of each fake that was called, in order."""
    return [name for name, _args, _kwargs in calls]
