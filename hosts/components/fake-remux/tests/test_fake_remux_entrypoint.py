"""fake-remux runs behind one `fake_remux.py {scan,replace}` entrypoint (#4353).

The two entrypoints `fake_remux_scan.py` and `fake_remux_replace.py` fold into one
`files/fake_remux.py`, and their two libraries move into a PEP 420 namespace package
`files/fake_remux_lib/`, the way `deploy_tools/land_lib/` holds `land.py`'s modules. The move
changes no behaviour, so these tests check where the code is, who still reaches for the old
location, what the package installs, and that the timers still run it.

The tests find each library by the function it defines rather than by its file name, so the
move may keep or shorten the basenames.

The "after the install" tests copy the component's Python entries from `component.yml` into a
temporary install directory, and run the units' commands from there. daniel-box holds the live
fake-remux config, so every subprocess here reads a temporary config and writes temporary state
files.

Run: uv run pytest hosts/components/fake-remux/tests/test_fake_remux_entrypoint.py
"""

import ast
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from functools import cache
from pathlib import Path

import pytest
import yaml

from lib.repo_paths import REPO
from renderer.renderer_lib import host as host_mode

COMPONENT = REPO / "hosts" / "components" / "fake-remux"
FILES = COMPONENT / "files"
LIB = FILES / "fake_remux_lib"
HOST_LIB = REPO / "hosts" / "lib"
ENTRYPOINT = "fake_remux.py"
OPT = "/usr/lib/homelab/fake-remux"
OLD_ENTRY_MODULES = {"fake_remux_scan", "fake_remux_replace"}
OLD_LIB_MODULES = {"fake_remux_logic", "fake_remux_replace_logic"}
OLD_FILES_PATH = re.compile(
    r"files/(?:fake_remux_scan|fake_remux_replace|fake_remux_logic|fake_remux_replace_logic)\.py"
)
# service -> the subcommand its ExecStart must pass to the entrypoint
SERVICE_SUBCOMMANDS = {
    "homelab-fake-remux-scan": "scan",
    "homelab-fake-remux-reconcile": "replace",
}


def _tracked(*pathspec: str) -> list[str]:
    out = subprocess.run(
        ["git", "ls-files", "-z", "--", *pathspec],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    ).stdout
    return [p for p in out.split("\0") if p]


@cache
def _package() -> host_mode.Package:
    return host_mode.render_host("daniel-box")


def _exec_start(service: str) -> list[str]:
    """The service's ExecStart as argv, with systemd's backslash continuations folded in."""
    unit = _package().files[f"/usr/lib/systemd/system/{service}.service"][0].decode()
    folded = re.sub(r"\\\n\s*", " ", unit)
    [line] = [x for x in folded.splitlines() if x.startswith("ExecStart=")]
    return shlex.split(line.removeprefix("ExecStart="))


def _script_and_args(argv: list[str]) -> tuple[str, list[str]]:
    """The `.py` the unit runs and the arguments it passes it."""
    idx = next((i for i, a in enumerate(argv) if a.endswith(".py")), None)
    assert idx is not None, f"no .py script in ExecStart {argv}"
    return argv[idx], argv[idx + 1 :]


def _install(root: Path) -> Path:
    """Copy the component's Python entries under `root`, and return the install directory."""
    spec = yaml.safe_load((COMPONENT / "component.yml").read_text())
    for entry in spec["files"]:
        if not entry["dst"].endswith(".py"):
            continue
        source = HOST_LIB / entry["lib"] if "lib" in entry else COMPONENT / entry["src"]
        target = root / entry["dst"].lstrip("/")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    return root / OPT.lstrip("/")


def _isolated_env(tmp_path: Path) -> dict:
    """An env whose config and state paths all live under tmp_path, never /etc or /var/lib."""
    state = {
        "SONARR_API_KEY": "",
        "FAKE_REMUX_REPLACE_MODE": "off",
        "ARR_DISCORD_WEBHOOK_URL": "",
        "STATE_FILE": str(tmp_path / "state.json"),
        "REPLACE_STATE_FILE": str(tmp_path / "replace_state.json"),
        "LEDGER_FILE": str(tmp_path / "replacements.json"),
    }
    cfg = tmp_path / "config.env"
    cfg.write_text("".join(f"{k}={v}\n" for k, v in state.items()))
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    env.update(state, FAKE_REMUX_CONFIG=str(cfg), FAKE_REMUX_REPLACE_CONFIG=str(cfg))
    return env


def _run(argv, cwd, env):
    return subprocess.run(
        [sys.executable, *argv],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


@pytest.fixture
def installed(tmp_path):
    return _install(tmp_path / "host")


# --- layout ---------------------------------------------------------------------------------


def _lib_file_defining(func: str):
    """The module file under fake_remux_lib/ that defines `func` at top level, or None."""
    if not LIB.is_dir():
        return None
    for path in sorted(LIB.glob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        if any(isinstance(n, ast.FunctionDef) and n.name == func for n in tree.body):
            return path
    return None


def test_libraries_are_tracked_in_fake_remux_lib_without_an_init_file():
    lib = set(_tracked(str(LIB.relative_to(REPO))))
    for func in ("encoder_is_reencoder", "adopt_in_flight"):
        path = _lib_file_defining(func)
        assert path is not None, f"no fake_remux_lib module defines {func}()"
        assert str(path.relative_to(REPO)) in lib
    assert f"{LIB.relative_to(REPO)}/__init__.py" not in lib


def test_only_the_one_entrypoint_keeps_the_fake_remux_prefix_in_files():
    files = str(FILES.relative_to(REPO))
    top = {
        p.rsplit("/", 1)[1]
        for p in _tracked(f"{files}/fake_remux*")
        if p.rsplit("/", 1)[0] == files
    }
    assert top == {ENTRYPOINT}


def _old_location_imports(source: str, inside_lib: bool) -> list[str]:
    # Inside the lib a module may keep its basename and import a sibling by bare name.
    old = set() if inside_lib else OLD_ENTRY_MODULES | OLD_LIB_MODULES
    hits = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom) and node.module and node.module in old:
            hits.append(node.module)
        elif isinstance(node, ast.Import):
            hits += [a.name for a in node.names if a.name in old]
    return hits


def test_no_tracked_module_imports_a_fake_remux_module_from_its_old_location():
    # The mkv repair imports the scan's config loader; the role's own tests import the shell.
    scanned, offenders = set(), {}
    for rel in _tracked("*.py"):
        path = REPO / rel
        if not path.is_file():
            continue
        scanned.add(rel)
        inside_lib = path.is_relative_to(LIB)
        hits = _old_location_imports(path.read_text(), inside_lib)
        if hits:
            offenders[rel] = hits
    assert "hosts/components/fake-remux/files/mkv_attachment_repair.py" in scanned
    assert offenders == {}


def test_no_hand_written_file_names_an_old_files_path():
    me = str(Path(__file__).resolve().relative_to(REPO))
    generated = re.compile(r"<!-- generated_from:.*?<!-- /generated_from -->", re.S)
    scanned, offenders = set(), []
    for rel in _tracked():
        if rel == me or rel.startswith(("docs/reference/", "docs/archive/")):
            continue
        if rel == "scripts/dev/pytest_shard_weights.json":
            continue
        path = REPO / rel
        if not path.is_file():
            continue
        try:
            text = path.read_text()
        except UnicodeDecodeError:
            continue
        if "generated_from:" in text:
            if "<!-- /generated_from -->" not in text:
                continue  # a wholly generated page: its generator owns it
            text = generated.sub("", text)
        scanned.add(rel)
        offenders += [f"{rel}: {m}" for m in sorted(set(OLD_FILES_PATH.findall(text)))]
    assert {
        "hosts/components/fake-remux/CLAUDE.md",
        "docs/autofix-bridge-actuators.md",
    } <= scanned
    assert offenders == []


def test_replacement_library_imports_when_run_directly_from_another_directory(tmp_path):
    # It imports the detection library, so one level deeper it needs its own bootstrap.
    path = _lib_file_defining("adopt_in_flight")
    assert path is not None, "no fake_remux_lib module defines adopt_in_flight()"
    proc = _run([str(path)], tmp_path, _isolated_env(tmp_path))
    assert proc.returncode == 0, proc.stderr[-2000:]


# --- the component and its timers -----------------------------------------------------------


@pytest.mark.parametrize("service", sorted(SERVICE_SUBCOMMANDS))
def test_each_service_runs_the_one_entrypoint_with_its_subcommand(service):
    script, args = _script_and_args(_exec_start(service))
    assert script == f"{OPT}/{ENTRYPOINT}"
    assert args == [SERVICE_SUBCOMMANDS[service]]


def test_the_install_holds_the_entrypoint_and_its_library(installed):
    assert (installed / ENTRYPOINT).is_file(), f"the install has no {ENTRYPOINT}"
    for func in ("encoder_is_reencoder", "adopt_in_flight"):
        path = _lib_file_defining(func)
        assert path is not None, f"no fake_remux_lib module defines {func}()"
        assert (installed / "fake_remux_lib" / path.name).is_file()


@pytest.mark.parametrize("service", sorted(SERVICE_SUBCOMMANDS))
def test_each_service_command_runs_from_the_install_dir(service, installed, tmp_path):
    script, args = _script_and_args(_exec_start(service))
    local = installed / os.path.relpath(script, OPT)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    proc = _run([str(local), *args, "--help"], elsewhere, _isolated_env(tmp_path))
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert not (tmp_path / "state.json").exists(), "--help ran a scan"
    assert not (tmp_path / "replace_state.json").exists(), "--help ran a reconcile"


@pytest.mark.parametrize(
    ("subcommand", "state_file", "msg"),
    [
        ("scan", "state.json", "disabled (no Sonarr API key)"),
        ("replace", "replace_state.json", "replacer off"),
    ],
    ids=["scan", "replace"],
)
def test_subcommand_runs_its_own_half(subcommand, state_file, msg, installed, tmp_path):
    # The scan reports itself disabled on an empty API key; the reconciler reports itself off.
    # Each message comes only from its own half, so it shows which one the subcommand reached.
    entry = installed / ENTRYPOINT
    proc = _run([str(entry), subcommand], tmp_path, _isolated_env(tmp_path))
    assert proc.returncode == 0, proc.stderr[-2000:]
    state = json.loads((tmp_path / state_file).read_text())
    assert (state["ok"], state["msg"]) == (True, msg)


@pytest.mark.parametrize("where", ["repo_root", "elsewhere"])
@pytest.mark.parametrize(
    "argv", [[], ["scan"], ["replace"]], ids=["top", "scan", "replace"]
)
def test_entrypoint_help_runs_from_the_repo_checkout(where, argv, tmp_path):
    # On the host host_lib.py sits beside the script; from the checkout it comes from hosts/lib/.
    entry = FILES / ENTRYPOINT
    assert entry.is_file(), f"{entry.relative_to(REPO)} does not exist"
    env = _isolated_env(tmp_path)
    env["PYTHONPATH"] = str(HOST_LIB)
    cwd = REPO if where == "repo_root" else tmp_path
    proc = _run([str(entry), *argv, "--help"], cwd, env)
    assert proc.returncode == 0, proc.stderr[-2000:]
    if not argv:
        assert "scan" in proc.stdout and "replace" in proc.stdout, proc.stdout
    assert not (tmp_path / "state.json").exists(), "--help ran a scan"
    assert not (tmp_path / "replace_state.json").exists(), "--help ran a reconcile"
