"""fake-remux runs behind one `fake_remux.py {scan,replace}` entrypoint (#4353).

The two entrypoints `fake_remux_scan.py` and `fake_remux_replace.py` fold into one
`files/fake_remux.py`, and their two libraries move into a PEP 420 namespace package
`files/fake_remux_lib/`, the way `deploy_tools/land_lib/` holds `land.py`'s modules. The move
changes no behaviour, so these tests check where the code is, who still reaches for the old
location, what the role's apply leaves in the install directory, and that the crons still run.

The tests find each library by the function it defines rather than by its file name, so the
move may keep or shorten the basenames.

The "after the apply" tests replay the role's `copy` and `file: state=absent` tasks into a
temporary install directory, with Ansible's src/dest directory semantics, and run the cron
commands from there. This host is daniel-box, which holds the live fake-remux config, so every
subprocess here reads a temporary config and writes temporary state files.

Run: uv run pytest ansible/roles/setup/fake_remux/tests/test_fake_remux_entrypoint.py
"""

import ast
import glob
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import jinja2
import pytest
import yaml
from _helpers import jinja_env

from lib.repo_paths import ALL_VARS, REPO

ROLE = REPO / "ansible" / "roles" / "setup" / "fake_remux"
FILES = ROLE / "files"
LIB = FILES / "fake_remux_lib"
COMMON = REPO / "ansible" / "roles" / "setup" / "common"
ENTRYPOINT = "fake_remux.py"
OLD_ENTRY_MODULES = {"fake_remux_scan", "fake_remux_replace"}
OLD_LIB_MODULES = {"fake_remux_logic", "fake_remux_replace_logic"}
OLD_BASENAMES = sorted(f"{m}.py" for m in OLD_ENTRY_MODULES | OLD_LIB_MODULES)
OLD_FILES_PATH = re.compile(
    r"files/(?:fake_remux_scan|fake_remux_replace|fake_remux_logic|fake_remux_replace_logic)\.py"
)
# cron_file -> the subcommand its job must pass to the entrypoint
CRON_SUBCOMMANDS = {"fake-remux-scan": "scan", "fake-remux-reconcile": "replace"}

COPY = {"ansible.builtin.copy", "copy"}
FILE = {"ansible.builtin.file", "file"}
CRON = {"ansible.builtin.cron", "cron"}
IMPORTS = {
    "ansible.builtin.import_tasks",
    "import_tasks",
    "ansible.builtin.include_tasks",
    "include_tasks",
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


# --- rendering the role's tasks ------------------------------------------------------------


def _fileglob(*patterns, wantlist=False):
    hits = []
    for pat in patterns:
        base = pat if os.path.isabs(pat) else str(FILES / pat)
        hits += sorted(p for p in glob.glob(base) if os.path.isfile(p))
    return hits if wantlist else ",".join(hits)


def _env():
    env = jinja_env()
    env.undefined = jinja2.StrictUndefined
    env.globals["lookup"] = lambda name, *t, **kw: _fileglob(*t, **kw)
    env.globals["query"] = env.globals["q"] = lambda name, *t, **kw: _fileglob(
        *t, wantlist=True
    )
    return env


def _role_vars() -> dict:
    v = yaml.safe_load((ROLE / "defaults" / "main.yml").read_text())
    group_vars = ALL_VARS.read_text()
    m = re.search(r'^host_python_version:\s*"?([^"\s]+)"?', group_vars, re.M)
    assert m, "host_python_version not found in group_vars/all.yml"
    v.update(
        host_python_version=m.group(1),
        role_path=str(ROLE),
        playbook_dir=str(REPO / "ansible"),
        sys_user="ubuntu",
    )
    return v


def _render(value, variables):
    if isinstance(value, str):
        return _env().from_string(value).render(**variables) if "{{" in value else value
    if isinstance(value, list):
        return [_render(v, variables) for v in value]
    if isinstance(value, dict):
        return {k: _render(v, variables) for k, v in value.items()}
    return value


def _tasks(path: Path, variables: dict) -> list[tuple[dict, dict]]:
    """(task, vars) for every task in `path`, following role-local and host_lib imports."""
    out = []
    for task in yaml.safe_load(path.read_text()) or []:
        if "block" in task:
            for sub in task["block"]:
                out.append((sub, variables))
            continue
        imp = next((task[k] for k in IMPORTS if k in task), None)
        if imp is not None:
            target = Path(os.path.normpath(_render(imp, variables)))
            if not target.is_absolute():
                target = path.parent / target
            target = Path(os.path.normpath(target))
            if target.is_relative_to(ROLE) or target.name == "install_host_lib.yml":
                inner = {**variables, **_render(task.get("vars", {}), variables)}
                out += _tasks(target, inner)
            else:
                out.append((task, variables))
            continue
        out.append((task, variables))
    return out


def _module(task: dict, names: set):
    return next((task[k] for k in names if k in task), None)


def _items(task: dict, variables: dict) -> list:
    if "with_fileglob" in task:
        pats = task["with_fileglob"]
        return _fileglob(*([pats] if isinstance(pats, str) else pats), wantlist=True)
    loop = task.get("loop", task.get("with_items"))
    if loop is None:
        return [None]
    rendered = _render(loop, variables)
    if isinstance(rendered, str):
        rendered = [p for p in rendered.split(",") if p]
    return list(rendered)


def _cron_jobs() -> dict[str, list[str]]:
    variables = _role_vars()
    jobs = {}
    for task, tvars in _tasks(ROLE / "tasks" / "main.yml", variables):
        cron = _module(task, CRON)
        if cron and cron.get("cron_file"):
            jobs[cron["cron_file"]] = shlex.split(str(_render(cron["job"], tvars)))
    return jobs


def _script_and_args(argv: list[str]) -> tuple[str, list[str]]:
    """The `.py` the cron runs and the arguments it passes it, up to the redirect."""
    idx = next((i for i, a in enumerate(argv) if a.endswith(".py")), None)
    assert idx is not None, f"no .py script in cron job {argv}"
    args = []
    for a in argv[idx + 1 :]:
        if a.startswith((">", "2>", "|")) or a in {"||", "&&", ";"}:
            break
        args.append(a)
    return argv[idx], args


# --- replaying the apply --------------------------------------------------------------------


def _ansible_copy(src: str, dest: Path) -> None:
    """Ansible's copy semantics for a local src: a dir without a trailing slash lands inside
    dest, a dir with one has its contents merged into dest, and a file lands at dest (or
    inside it when dest is a directory or ends in a slash)."""
    src_path = Path(src) if os.path.isabs(src) else FILES / src
    if src_path.is_dir():
        target = dest if src.endswith("/") else dest / src_path.name
        shutil.copytree(
            src_path,
            target,
            dirs_exist_ok=True,
            ignore=shutil.ignore_patterns("__pycache__"),
        )
        return
    if str(dest).endswith("/") or dest.is_dir():
        dest = dest / src_path.name
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src_path, dest)


def _apply(root: Path) -> Path:
    """Replay the role's copies and removals over an install dir seeded with the old files.

    Returns the install dir (fake_remux_opt_dir under `root`)."""
    variables = _role_vars()
    opt = variables["fake_remux_opt_dir"]
    install = root / opt.lstrip("/")
    install.mkdir(parents=True)
    for name in OLD_BASENAMES:
        (install / name).write_text("# installed by the previous release\n")

    def under_opt(path: str):
        path = os.path.normpath(path)
        if path == opt or path.startswith(opt + "/"):
            return install / os.path.relpath(path, opt)
        return None

    for task, tvars in _tasks(ROLE / "tasks" / "main.yml", variables):
        copy, file_ = _module(task, COPY), _module(task, FILE)
        for item in _items(task, tvars) if (copy or file_) else []:
            ivars = {**tvars, "item": item}
            if copy and "src" in copy:
                dest_raw = str(_render(copy["dest"], ivars))
                dest = under_opt(dest_raw)
                if dest is not None:
                    if dest_raw.endswith("/"):
                        dest = Path(str(dest) + "/")
                    _ansible_copy(str(_render(copy["src"], ivars)), dest)
            elif file_ and file_.get("state") == "absent":
                raw = file_.get("path", file_.get("dest", file_.get("name")))
                target = under_opt(str(_render(raw, ivars)))
                if target is not None and target.is_dir():
                    shutil.rmtree(target)
                elif target is not None and target.exists():
                    target.unlink()
    return install


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
def applied(tmp_path):
    return _apply(tmp_path / "host")


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
    assert "ansible/roles/setup/fake_remux/files/mkv_attachment_repair.py" in scanned
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
        "ansible/roles/setup/fake_remux/CLAUDE.md",
        "docs/autofix-bridge-actuators.md",
    } <= scanned
    assert offenders == []


def test_replacement_library_imports_when_run_directly_from_another_directory(tmp_path):
    # It imports the detection library, so one level deeper it needs its own bootstrap.
    path = _lib_file_defining("adopt_in_flight")
    assert path is not None, "no fake_remux_lib module defines adopt_in_flight()"
    proc = _run([str(path)], tmp_path, _isolated_env(tmp_path))
    assert proc.returncode == 0, proc.stderr[-2000:]


# --- the role and its crons -----------------------------------------------------------------


@pytest.mark.parametrize("cron_file", sorted(CRON_SUBCOMMANDS))
def test_cron_runs_the_one_entrypoint_with_its_subcommand(cron_file):
    jobs = _cron_jobs()
    assert cron_file in jobs, f"no cron task writes {cron_file}; found {sorted(jobs)}"
    script, args = _script_and_args(jobs[cron_file])
    opt = _role_vars()["fake_remux_opt_dir"]
    assert script == f"{opt}/{ENTRYPOINT}"
    assert args[:1] == [CRON_SUBCOMMANDS[cron_file]]


def test_apply_removes_the_old_scripts_from_the_install_dir(applied):
    left = sorted(n for n in OLD_BASENAMES if (applied / n).exists())
    assert left == []


def test_apply_installs_the_entrypoint_and_its_library(applied):
    assert (applied / ENTRYPOINT).is_file(), f"the apply installed no {ENTRYPOINT}"
    for func in ("encoder_is_reencoder", "adopt_in_flight"):
        path = _lib_file_defining(func)
        assert path is not None, f"no fake_remux_lib module defines {func}()"
        assert (applied / "fake_remux_lib" / path.name).is_file()


@pytest.mark.parametrize("cron_file", sorted(CRON_SUBCOMMANDS))
def test_cron_command_runs_from_the_applied_install_dir(cron_file, applied, tmp_path):
    jobs = _cron_jobs()
    assert cron_file in jobs, f"no cron task writes {cron_file}; found {sorted(jobs)}"
    script, args = _script_and_args(jobs[cron_file])
    assert os.path.basename(script) == ENTRYPOINT, script
    opt = _role_vars()["fake_remux_opt_dir"]
    local = applied / os.path.relpath(script, opt)
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
def test_subcommand_runs_its_own_half(subcommand, state_file, msg, applied, tmp_path):
    # The scan reports itself disabled on an empty API key; the reconciler reports itself off.
    # Each message comes only from its own half, so it shows which one the subcommand reached.
    entry = applied / ENTRYPOINT
    assert entry.is_file(), f"the apply installed no {ENTRYPOINT}"
    proc = _run([str(entry), subcommand], tmp_path, _isolated_env(tmp_path))
    assert proc.returncode == 0, proc.stderr[-2000:]
    state = json.loads((tmp_path / state_file).read_text())
    assert (state["ok"], state["msg"]) == (True, msg)


def test_drift_check_records_the_entrypoint_and_each_library_module():
    variables = _role_vars()
    opt = variables["fake_remux_opt_dir"]
    pairs = []
    for task, _ in _tasks(ROLE / "tasks" / "main.yml", variables):
        imp = next((task[k] for k in IMPORTS if k in task), "")
        if str(imp).endswith("stamp_deployed.yml"):
            pairs += _render(task["vars"]["stamp_deployed_pairs"], variables)
    assert pairs, "found no stamp_deployed_pairs in the role's tasks"
    recorded = {p["live"]: p["src"] for p in pairs}
    files = str(FILES.relative_to(REPO))
    expected = {f"{opt}/{ENTRYPOINT}": f"{files}/{ENTRYPOINT}"}
    for func in ("encoder_is_reencoder", "adopt_in_flight"):
        path = _lib_file_defining(func)
        assert path is not None, f"no fake_remux_lib module defines {func}()"
        expected[f"{opt}/fake_remux_lib/{path.name}"] = str(path.relative_to(REPO))
    assert {k: recorded.get(k) for k in expected} == expected
    tracked = set(_tracked(files))
    assert (
        sorted(s for s in recorded.values() if s.startswith(files) and s not in tracked)
        == []
    )


@pytest.mark.parametrize("where", ["repo_root", "elsewhere"])
@pytest.mark.parametrize(
    "argv", [[], ["scan"], ["replace"]], ids=["top", "scan", "replace"]
)
def test_entrypoint_help_runs_from_the_repo_checkout(where, argv, tmp_path):
    # On the host host_lib.py sits beside the script; from the checkout it comes from common/.
    entry = FILES / ENTRYPOINT
    assert entry.is_file(), f"{entry.relative_to(REPO)} does not exist"
    env = _isolated_env(tmp_path)
    env["PYTHONPATH"] = str(COMMON / "files")
    cwd = REPO if where == "repo_root" else tmp_path
    proc = _run([str(entry), *argv, "--help"], cwd, env)
    assert proc.returncode == 0, proc.stderr[-2000:]
    if not argv:
        assert "scan" in proc.stdout and "replace" in proc.stdout, proc.stdout
    assert not (tmp_path / "state.json").exists(), "--help ran a scan"
    assert not (tmp_path / "replace_state.json").exists(), "--help ran a reconcile"
