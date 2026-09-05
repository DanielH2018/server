"""The pod's `/app` — flat ConfigMap keys mounted back at nested paths — must import.

monitor-bridge's modules are packages under files/ (`bridge/`, `checks/`, `verdicts/`), but a
ConfigMap key cannot contain `/`, so each ships under a flat key (`checks_b2.py`) and the
Deployment's `items:` puts it back at its path (`checks/b2.py`). pytest sees none of that: it
imports from files/ on disk, where the packages already exist, so a wrong `items:` path or a
key that never made it into `monitor_bridge_modules` is green here and `ModuleNotFoundError`
in the pod, on the one workload that cannot page about its own failure.

WHAT THIS PINS, exactly: the RENDERED `items:` list against `monitor_bridge_modules`, and that
the result imports. The key half of the mapping is RE-DERIVED here (`m.replace("/", "_")`)
rather than read back from the `--from-file` line in tasks/main.yml, so a `--from-file` line
that stopped agreeing with that rule would not fail this test. The staging copy task is
likewise unread. `ansible/tests/services/test_monitor_bridge_modules.py` is what keeps the
ship list itself honest against the tree.

This lays out a directory exactly as the RENDERED Deployment's `items:` says the kubelet will,
from the same source files the copy task stages, then imports the entrypoint from it in a
fresh interpreter whose only sys.path entry is that directory. That is the pod's import graph,
proven before the deploy. Both bridges are covered: autofix-bridge mounts the shared
`bridge/common.py` the same way.

Run: uv run pytest ansible/tests/services/test_monitor_bridge_mount_layout.py
"""

import os
import subprocess
import sys
from pathlib import Path

from _helpers import ANSIBLE, K8S_ROLES, load_yaml
from _k8s_render import rendered_docs

MONITOR_BRIDGE = K8S_ROLES / "monitor-bridge"
AUTOFIX_BRIDGE = K8S_ROLES / "autofix-bridge"


def _script_items(role: str, volume: str) -> list[tuple[str, str]]:
    """(key, path) pairs the rendered Deployment mounts for `volume`."""
    for r, _, doc in rendered_docs():
        if r != role or doc.get("kind") != "Deployment":
            continue
        for vol in doc["spec"]["template"]["spec"]["volumes"]:
            if vol["name"] == volume:
                return [(i["key"], i["path"]) for i in vol["configMap"]["items"]]
    raise AssertionError(f"{role}: no Deployment volume named {volume}")


def _monitor_bridge_sources() -> dict[str, Path]:
    """Key -> source file, derived the way the copy task and the --from-file line derive it."""
    modules = load_yaml(MONITOR_BRIDGE / "defaults/main.yml")["monitor_bridge_modules"]
    return {m.replace("/", "_"): MONITOR_BRIDGE / "files" / m for m in modules}


def _autofix_bridge_sources() -> dict[str, Path]:
    modules = load_yaml(AUTOFIX_BRIDGE / "defaults/main.yml")["autofix_bridge_modules"]
    return {
        m["name"]: Path(m["src"].replace("{{ playbook_dir }}", str(ANSIBLE)))
        for m in modules
    }


def _lay_out(app: Path, items, sources) -> None:
    for key, path in items:
        target = app / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(sources[key].read_bytes())


def _import_from(app: Path, module: str) -> subprocess.CompletedProcess:
    """`import <module>` with `app` as the ONLY sys.path entry, like `python /app/<entry>.py`.

    PYTHONPATH is dropped so pyproject's `pythonpath` cannot leak files/ in and resolve what
    the mount would not.
    """
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    return subprocess.run(
        [
            sys.executable,
            "-c",
            # `-c` puts '' at sys.path[0]; `python /app/check.py` puts /app there. Same shape,
            # stdlib intact behind it.
            f"import sys; sys.path[0] = sys.argv[1]; import {module}",
            str(app),
        ],
        capture_output=True,
        text=True,
        env=env,
        cwd=app.parent,
    )


def test_monitor_bridge_items_agree_with_the_ship_list():
    """The three derivations read one list; this pins that they still say the same thing."""
    items = _script_items("monitor-bridge", "monitor-bridge-script")
    sources = _monitor_bridge_sources()
    assert dict(items) == {
        k: p.relative_to(MONITOR_BRIDGE / "files").as_posix()
        for k, p in sources.items()
    }
    assert all("/" not in key for key, _ in items), (
        "a ConfigMap key cannot contain a slash"
    )
    assert ("checks_b2.py", "checks/b2.py") in items, items


def test_monitor_bridge_mount_layout_imports(tmp_path):
    app = tmp_path / "app"
    _lay_out(
        app,
        _script_items("monitor-bridge", "monitor-bridge-script"),
        _monitor_bridge_sources(),
    )
    proc = _import_from(app, "check")
    assert proc.returncode == 0, proc.stderr


def test_autofix_bridge_mount_layout_imports(tmp_path):
    app = tmp_path / "app"
    _lay_out(
        app,
        _script_items("autofix-bridge", "autofix-bridge-script"),
        _autofix_bridge_sources(),
    )
    proc = _import_from(app, "autofix")
    assert proc.returncode == 0, proc.stderr


def test_a_module_missing_from_the_mount_is_caught(tmp_path):
    """Red-proof: the layout check fails when one shipped module never reaches /app."""
    app = tmp_path / "app"
    items = [
        (k, p)
        for k, p in _script_items("monitor-bridge", "monitor-bridge-script")
        if k != "bridge_config.py"
    ]
    _lay_out(app, items, _monitor_bridge_sources())
    proc = _import_from(app, "check")
    assert proc.returncode != 0
    assert "No module named 'bridge.config'" in proc.stderr, proc.stderr
