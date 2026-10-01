#!/usr/bin/env python3
"""Which k8s roles act ONLY through the bytes their render digest covers.

THE QUESTION. The GitOps deployer's `k8s_unapplied` discharge accepts a matching render digest
as proof that a role's change is live (`deploy_release.render_proof`). That proof covers
`manifests_digest` and `secret_digest` and nothing else, so it is sound only for a role whose
whole effect is the manifests `k8s/manifests` renders. A role that also writes a host file,
stages a ConfigMap with `kubectl create --from-file`, calls an API or includes another shared
role (`volume-claim`, `image-builder`) acts outside the digest, and a digest match would drop
its line with that half never applied.

THE RULE, FAIL-CLOSED. A role is digest-provable when every task its `tasks/main.yml` reaches is
one of:

  * an `include_role`/`import_role` of `k8s/manifests`;
  * a module that only computes or checks (`PURE_MODULES`);
  * an `include_tasks`/`import_tasks` of a literal file in its own `tasks/`, held to the same
    rule, or a `block` whose every branch is.

and it has no handlers and no `meta/main.yml` dependencies. Anything else, an unreadable file or
a templated include path, reads as NOT provable, which keeps the line for a deploy to clear.
`lookup('file'|'template')` inside a template is fine: its content is in the rendered manifest.

WHO CALLS IT. `deploy_narrow.digest_provable`, as a subprocess for the reason
`shared_role_callers.py` is one: this parses YAML and the deployer runs under `uv run
--no-project`. It prints one JSON object, role to bool.

Usage: digest_provable.py [--repo PATH] ROLE [ROLE ...]
"""

import argparse
import json
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lib import yaml_fast

# Modules whose effect is a fact, a check or a message on the controller. Nothing here writes
# to a host, the cluster or an API.
PURE_MODULES = frozenset({"set_fact", "assert", "debug", "fail", "meta"})

# Task keywords, which sit beside the one module key a task carries.
_KEYWORDS = frozenset(
    {
        "name", "when", "tags", "vars", "loop", "loop_control", "register", "no_log",
        "changed_when", "failed_when", "become", "become_user", "delegate_to", "run_once",
        "ignore_errors", "until", "retries", "delay", "environment", "check_mode", "diff",
        "args", "with_items", "with_dict", "listen", "notify", "any_errors_fatal",
        "throttle", "module_defaults", "timeout",
    }
)  # fmt: skip

_INCLUDE_ROLE = {"include_role", "import_role"}
_INCLUDE_TASKS = {"include_tasks", "import_tasks"}


def _tasks_provable(tasks, tasks_dir: Path, seen: set[Path]) -> bool:
    """Whether every task in `tasks` acts only through the manifests render."""
    if tasks is None:
        return True
    if not isinstance(tasks, list):
        return False
    for task in tasks:
        if not isinstance(task, dict):
            return False
        if "block" in task:
            if not all(
                _tasks_provable(task.get(k), tasks_dir, seen)
                for k in ("block", "rescue", "always")
            ):
                return False
            continue
        modules = [k for k in task if k not in _KEYWORDS]
        if len(modules) != 1:
            return False
        key = modules[0]
        module, arg = key.rsplit(".", 1)[-1], task[key]
        if module in PURE_MODULES:
            continue
        if module in _INCLUDE_ROLE:
            if not (isinstance(arg, dict) and arg.get("name") == "k8s/manifests"):
                return False
            continue
        if module in _INCLUDE_TASKS:
            name = arg.get("file") if isinstance(arg, dict) else arg
            if not isinstance(name, str) or "{{" in name:
                return False
            path = tasks_dir / name
            if path in seen:
                continue
            seen.add(path)
            if not path.is_file() or not _tasks_provable(
                yaml_fast.safe_load(path.read_text()), tasks_dir, seen
            ):
                return False
            continue
        return False
    return True


def digest_provable(role: Path) -> bool:
    """Whether the role at `role` acts only through the bytes its render digest covers."""
    try:
        if any((role / "handlers").glob("*.y*ml")):
            return False
        meta = role / "meta" / "main.yml"
        if meta.is_file() and (yaml_fast.safe_load(meta.read_text()) or {}).get(
            "dependencies"
        ):
            return False
        main = role / "tasks" / "main.yml"
        if not main.is_file():
            return False
        return _tasks_provable(
            yaml_fast.safe_load(main.read_text()), role / "tasks", {main}
        )
    except OSError, yaml.YAMLError, AttributeError:
        return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("roles", nargs="+", help="role directories under roles/k8s/")
    parser.add_argument(
        "--repo", default=".", help="the checkout to read (default: the cwd)"
    )
    args = parser.parse_args(argv)
    k8s = Path(args.repo) / "ansible" / "roles" / "k8s"
    print(json.dumps({r: digest_provable(k8s / r) for r in args.roles}, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
